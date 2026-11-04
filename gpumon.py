#!/usr/bin/python3
"""Publish live GPU metrics for the GPU Monitor bar widget."""

import argparse
import csv
import fcntl
import glob
import io
import json
import math
import os
import re
import secrets
import select
import shutil
import stat
import subprocess
import sys
import time

DEFAULT_STATE = ""
DEFAULT_INTERVAL = 1.0
MAX_INTERVAL = 3600.0
NVIDIA_CACHE_SECONDS = 3.0
STATE_DIRNAME = "omarchy-gpu-widget"
STATE_FILENAME = "davidjm-gpu-state.json"
STATE_SCHEMA = 1
MAX_STATE_BYTES = 64 * 1024
MAX_READ_BYTES = 16 * 1024
MAX_NVIDIA_OUTPUT = 64 * 1024
MAX_GPU_ROWS = 16
MAX_SENSORS = 32
SYSTEM_BINDIRS = ["/usr/local/bin", "/usr/bin", "/bin"]
SYSTEM_PATH = ":".join(SYSTEM_BINDIRS)
SENSOR_KINDS = {"edge", "junction", "memory", "core", "gpu", "unknown"}
NUMERIC_FIELDS = {
    "util": (0, 100),
    "memUtil": (0, 100),
    "sclkMhz": (0, 1000000),
    "mclkMhz": (0, 1000000),
    "tempEdge": (-100, 200),
    "tempJunction": (-100, 200),
    "tempMem": (-100, 200),
    "tempGpu": (-100, 200),
    "powerW": (0, 1000000),
    "fanRpm": (0, 1000000),
    "vramUsedG": (0, 1000000),
    "vramTotalG": (0, 1000000),
}
DPM_RE = re.compile(r"\s*[0-9S]+:\s*([0-9.]+)Mhz\s*\*")


class StateSecurityError(RuntimeError):
    pass


class StateValidationError(ValueError):
    pass


def _reject_constant(token):
    raise ValueError(token)


def _uid():
    return os.geteuid()


def _private_directory(path, create=False):
    if (not isinstance(path, str) or not os.path.isabs(path)
            or any(ord(char) < 32 or ord(char) == 127 for char in path)):
        raise StateSecurityError("state directory must be absolute")
    if os.path.realpath(path) != os.path.normpath(path):
        raise StateSecurityError("state directory must not contain symlinks")
    if create:
        try:
            os.mkdir(path, 0o700)
        except FileExistsError:
            pass
        except OSError as error:
            raise StateSecurityError("cannot create state directory") from error
    try:
        info = os.lstat(path)
    except OSError as error:
        raise StateSecurityError("state directory is unavailable") from error
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != _uid():
        raise StateSecurityError("state directory has unsafe type or owner")
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise StateSecurityError("state directory is accessible by another user")
    return path


def runtime_directory(value=None):
    path = os.environ.get("XDG_RUNTIME_DIR", "") if value is None else value
    if not isinstance(path, str) or not path or not os.path.isabs(path):
        raise StateSecurityError("XDG_RUNTIME_DIR is unavailable")
    path = os.path.normpath(path)
    if path == os.path.sep:
        raise StateSecurityError("XDG_RUNTIME_DIR is unavailable")
    return _private_directory(path)


def runtime_state_path(value=None):
    base = runtime_directory(value)
    directory = os.path.join(base, STATE_DIRNAME)
    _private_directory(directory, create=True)
    return os.path.join(directory, STATE_FILENAME)


def default_state_path():
    return runtime_state_path()


def state_file_path():
    return runtime_state_path()


def validate_state_path(path, create_parent=False):
    if isinstance(path, str):
        path = os.path.normpath(path)
    if (not isinstance(path, str) or not os.path.isabs(path)
            or any(ord(char) < 32 or ord(char) == 127 for char in path)):
        raise StateSecurityError("state path must be absolute")
    if os.path.basename(path) in ("", ".", ".."):
        raise StateSecurityError("state filename is invalid")
    _private_directory(os.path.dirname(path), create=create_parent)
    return path


def _read_text(path, max_bytes=MAX_READ_BYTES):
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if info.st_size > max_bytes:
            return None
        chunks = []
        size = 0
        while size < max_bytes:
            chunk = os.read(fd, min(4096, max_bytes - size))
            if not chunk:
                break
            size += len(chunk)
            chunks.append(chunk)
        return b"".join(chunks).decode("utf-8", "strict").strip()
    except (OSError, UnicodeError):
        return None
    finally:
        os.close(fd)


def _read(path):
    return _read_text(path)


def _read_int(path):
    text = _read(path)
    if text is None:
        return None
    pieces = text.split()
    if not pieces:
        return None
    try:
        return int(pieces[0])
    except (ValueError, OverflowError):
        return None


def _read_float(path):
    text = _read(path)
    if text is None:
        return None
    pieces = text.split()
    if not pieces:
        return None
    try:
        value = float(pieces[0])
    except (ValueError, OverflowError):
        return None
    return value if math.isfinite(value) else None


_IGPU_CACHE = {"card": "", "time": 0.0, "rc6": None}


def _bounded_int(path, upper):
    value = _read_int(path)
    return value if value is not None and 0 <= value <= upper else None


def _rc6_utilization(previous_ms, current_ms, elapsed_ms):
    if previous_ms is None or current_ms is None or elapsed_ms <= 0:
        return None
    delta = current_ms - previous_ms
    if delta < 0 or delta > elapsed_ms * 1.5:
        return None
    return round(max(0.0, min(100.0, (1.0 - delta / elapsed_ms) * 100.0)), 1)


def intel_igpu_state():
    """Best-effort Intel iGPU frequency and RC6-based utilization."""
    global _IGPU_CACHE
    for card in sorted(glob.glob("/sys/class/drm/card[0-9]*")):
        if _read(os.path.join(card, "device", "vendor")) != "0x8086":
            continue
        freq = _bounded_int(os.path.join(card, "gt_cur_freq_mhz"), 100000)
        maximum = _bounded_int(os.path.join(card, "gt_max_freq_mhz"), 100000)
        if freq is None:
            freq = _bounded_int(os.path.join(card, "gt", "gt0", "rps_cur_freq_mhz"), 100000)
        if maximum is None:
            maximum = _bounded_int(os.path.join(card, "gt", "gt0", "rps_max_freq_mhz"), 100000)
        rc6 = _bounded_int(os.path.join(card, "gt", "gt0", "rc6_residency_ms"), 1 << 62)
        now = time.monotonic()
        util = None
        if _IGPU_CACHE["card"] == card and _IGPU_CACHE["rc6"] is not None and rc6 is not None:
            util = _rc6_utilization(_IGPU_CACHE["rc6"], rc6, (now - _IGPU_CACHE["time"]) * 1000.0)
        _IGPU_CACHE = {"card": card, "time": now, "rc6": rc6}
        return {
            "vendor": "Intel",
            "card": os.path.basename(card)[:128],
            "freqMhz": freq,
            "maxMhz": maximum,
            "util": util,
        }
    return None


def find_card():
    for card in sorted(glob.glob("/sys/class/drm/card[0-9]*")):
        dev = os.path.join(card, "device")
        try:
            info = os.lstat(os.path.join(dev, "gpu_busy_percent"))
        except OSError:
            continue
        if stat.S_ISREG(info.st_mode):
            return card, dev
    return None, None


def find_hwmon(device_dir):
    hwmon_root = os.path.join(device_dir, "hwmon")
    try:
        entries = sorted(os.listdir(hwmon_root))
    except OSError:
        return None
    for entry in entries:
        base = os.path.join(hwmon_root, entry)
        try:
            info = os.lstat(base)
        except OSError:
            continue
        if stat.S_ISDIR(info.st_mode) and _read(os.path.join(base, "name")) == "amdgpu":
            return base
    return None


def dpm_current(path):
    text = _read(path)
    if text is None:
        return None
    for line in text.splitlines():
        match = DPM_RE.match(line)
        if match:
            try:
                value = float(match.group(1))
            except ValueError:
                return None
            return value if math.isfinite(value) and value >= 0 else None
    return None


def temp_input(hwmon, index):
    value = _read_int(os.path.join(hwmon, "temp{0}_input".format(index)))
    return round(value / 1000.0, 1) if value is not None else None


def sensor_kind(label, vendor):
    text = str(label or "").strip().lower().replace("_", " ")
    text = re.sub(r"[-]+", " ", text)
    if vendor == "AMD":
        if "junction" in text or "hotspot" in text or "hot spot" in text:
            return "junction"
        if "edge" in text:
            return "edge"
        if "memory" in text or re.search(r"\bmem\b", text):
            return "memory"
        if "gpu" in text or "die" in text:
            return "gpu"
        return "unknown"
    if "junction" in text or "hotspot" in text:
        return "junction"
    if "memory" in text or re.search(r"\bmem\b", text):
        return "memory"
    if "core" in text or "gpu" in text:
        return "core"
    return "unknown"


def read_temp_sensors(hwmon, vendor):
    sensors = []
    try:
        entries = sorted(os.listdir(hwmon))
    except OSError:
        return sensors
    for entry in entries:
        match = re.fullmatch(r"temp([0-9]+)_input", entry)
        if not match:
            continue
        index = match.group(1)
        value = _read_int(os.path.join(hwmon, entry))
        if value is None or value < -100000 or value > 200000:
            continue
        label = _read(os.path.join(hwmon, "temp{0}_label".format(index)))
        if not label:
            label = "GPU {0}".format(index)
        kind = sensor_kind(label, vendor)
        sensors.append({
            "label": str(label)[:64],
            "value": round(value / 1000.0, 1),
            "kind": kind,
        })
        if len(sensors) >= MAX_SENSORS:
            break
    return sensors


def empty_state():
    return {
        "schema": STATE_SCHEMA,
        "present": False,
        "vendor": None,
        "card": None,
        "util": None,
        "memUtil": None,
        "sclkMhz": None,
        "mclkMhz": None,
        "tempEdge": None,
        "tempJunction": None,
        "tempMem": None,
        "tempGpu": None,
        "powerW": None,
        "fanRpm": None,
        "vramUsedG": None,
        "vramTotalG": None,
        "tempSensors": [],
        "alertTemp": None,
        "alertLabel": "",
    }


def _amd_state(card, dev):
    state = empty_state()
    state["present"] = True
    state["vendor"] = "AMD"
    state["card"] = os.path.basename(card)[:128]
    state["util"] = _read_int(os.path.join(dev, "gpu_busy_percent"))
    state["memUtil"] = _read_int(os.path.join(dev, "mem_busy_percent"))
    state["sclkMhz"] = dpm_current(os.path.join(dev, "pp_dpm_sclk"))
    state["mclkMhz"] = dpm_current(os.path.join(dev, "pp_dpm_mclk"))
    total = _read_int(os.path.join(dev, "mem_info_vram_total"))
    used = _read_int(os.path.join(dev, "mem_info_vram_used"))
    if total is not None and used is not None and total > 0 and used >= 0:
        state["vramTotalG"] = round(total / (1024 ** 3), 1)
        state["vramUsedG"] = round(used / (1024 ** 3), 1)
    hwmon = find_hwmon(dev)
    if hwmon:
        state["tempSensors"] = read_temp_sensors(hwmon, "AMD")
        for sensor in state["tempSensors"]:
            if sensor["kind"] == "edge" and state["tempEdge"] is None:
                state["tempEdge"] = sensor["value"]
            elif sensor["kind"] == "junction" and state["tempJunction"] is None:
                state["tempJunction"] = sensor["value"]
            elif sensor["kind"] == "memory" and state["tempMem"] is None:
                state["tempMem"] = sensor["value"]
            elif sensor["kind"] == "gpu" and state["tempGpu"] is None:
                state["tempGpu"] = sensor["value"]
        power = _read_int(os.path.join(hwmon, "power1_average"))
        state["powerW"] = round(power / 1e6, 1) if power is not None and power >= 0 else None
        state["fanRpm"] = _read_int(os.path.join(hwmon, "fan1_input"))
    try:
        return validate_state(state)
    except StateValidationError:
        return empty_state()


NV_QUERY = "name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw,clocks.sm,clocks.mem,fan.speed"


def _trusted_nvidia_smi():
    resolved = shutil.which("nvidia-smi", path=SYSTEM_PATH)
    if not resolved:
        return None
    try:
        real = os.path.realpath(resolved)
        info = os.stat(real)
    except OSError:
        return None
    if not (real.startswith("/usr/") or real.startswith("/opt/")):
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or stat.S_IMODE(info.st_mode) & 0o022:
        return None
    return real


def _run_nvidia(command):
    process = None
    chunks = []
    size = 0
    end = time.monotonic() + 3
    try:
        process = subprocess.Popen(
            [command, "--query-gpu=" + NV_QUERY, "--format=csv,noheader,nounits"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            bufsize=0,
        )
        while True:
            remaining = end - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(command, 3)
            ready, _, _ = select.select([process.stdout], [], [], remaining)
            if not ready:
                raise subprocess.TimeoutExpired(command, 3)
            chunk = process.stdout.read(min(4096, MAX_NVIDIA_OUTPUT - size + 1))
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_NVIDIA_OUTPUT:
                raise subprocess.SubprocessError("nvidia-smi output exceeded limit")
            chunks.append(chunk)
        return process.wait(timeout=max(0, end - time.monotonic())), b"".join(chunks)
    except (OSError, subprocess.SubprocessError):
        return 1, b""
    finally:
        if process is not None:
            if process.poll() is None:
                try:
                    process.kill()
                except OSError:
                    pass
            try:
                process.wait(timeout=1)
            except (OSError, subprocess.SubprocessError):
                pass
            try:
                process.stdout.close()
            except (OSError, ValueError):
                pass


def _nvidia_rows(binary=None):
    command = binary or _trusted_nvidia_smi()
    if not command:
        return []
    return_code, raw = _run_nvidia(command)
    if return_code != 0 or len(raw) > MAX_NVIDIA_OUTPUT:
        return []
    try:
        text = raw.decode("utf-8", "strict")
    except UnicodeError:
        return []
    try:
        parsed = list(csv.reader(io.StringIO(text)))
    except csv.Error:
        return []
    rows = []
    for fields in parsed:
        if len(fields) != 9:
            return []
        fields = [field.strip() for field in fields]
        if not fields[0] or len(fields[0]) > 128 or any(ord(char) < 32 for char in fields[0]):
            return []
        rows.append(fields)
        if len(rows) > MAX_GPU_ROWS:
            return []
    return rows


def _nv_num(raw):
    value = str(raw or "").strip()
    if not value or value.startswith("["):
        return None
    token = value.split()[0]
    try:
        number = float(token)
    except (ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


_NVIDIA_CACHE_TIME = 0.0
_NVIDIA_CACHE_VALUE = None
_NVIDIA_CACHE_SET = False


def _nvidia_state(binary=None):
    global _NVIDIA_CACHE_TIME, _NVIDIA_CACHE_VALUE, _NVIDIA_CACHE_SET
    if binary is not None:
        return _query_nvidia_state(binary)
    now = time.monotonic()
    if _NVIDIA_CACHE_SET and now - _NVIDIA_CACHE_TIME < NVIDIA_CACHE_SECONDS:
        return dict(_NVIDIA_CACHE_VALUE) if _NVIDIA_CACHE_VALUE is not None else None
    value = _query_nvidia_state()
    _NVIDIA_CACHE_TIME = now
    _NVIDIA_CACHE_VALUE = value
    _NVIDIA_CACHE_SET = True
    return value


def _query_nvidia_state(binary=None):
    rows = _nvidia_rows(binary)
    if not rows:
        return None
    row = rows[0]
    state = empty_state()
    state["present"] = True
    state["vendor"] = "NVIDIA"
    state["card"] = row[0]
    state["util"] = _nv_num(row[1])
    state["tempGpu"] = _nv_num(row[4])
    state["powerW"] = _nv_num(row[5])
    state["sclkMhz"] = _nv_num(row[6])
    state["mclkMhz"] = _nv_num(row[7])
    state["tempSensors"] = []
    if state["tempGpu"] is not None:
        state["tempSensors"] = [{
            "label": "GPU core",
            "value": state["tempGpu"],
            "kind": "core",
        }]
    used = _nv_num(row[2])
    total = _nv_num(row[3])
    if used is not None and total is not None and total > 0 and used >= 0:
        state["vramUsedG"] = round(used / 1024, 1)
        state["vramTotalG"] = round(total / 1024, 1)
        state["memUtil"] = round(min(100.0, used / total * 100))
    try:
        return validate_state(state)
    except StateValidationError:
        return None


def get_state():
    card, dev = find_card()
    if dev is not None:
        state = _amd_state(card, dev)
    else:
        nvidia = _nvidia_state()
        state = nvidia if nvidia is not None else empty_state()
    igpu = intel_igpu_state()
    if igpu is not None:
        state["igpu"] = igpu
    return state


def _number(value, field):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise StateValidationError("invalid numeric state field")
    try:
        finite = math.isfinite(float(value))
    except (OverflowError, ValueError) as error:
        raise StateValidationError("non-finite state field") from error
    if not finite:
        raise StateValidationError("non-finite state field")
    low, high = NUMERIC_FIELDS[field]
    if value < low or value > high:
        raise StateValidationError("state field out of range")
    return value


def validate_state(value):
    if not isinstance(value, dict):
        raise StateValidationError("state is not an object")
    required = set(empty_state())
    missing = required.difference(value)
    if missing:
        raise StateValidationError("state schema is incomplete")
    if type(value["schema"]) is not int or value["schema"] != STATE_SCHEMA:
        raise StateValidationError("state schema version is invalid")
    present = value["present"]
    if not isinstance(present, bool):
        raise StateValidationError("state presence is invalid")
    result = empty_state()
    result["present"] = present
    if "igpu" in value and value["igpu"] is not None:
        igpu = value["igpu"]
        if not isinstance(igpu, dict) or igpu.get("vendor") != "Intel":
            raise StateValidationError("integrated GPU metadata is invalid")
        if not isinstance(igpu.get("card"), str) or not igpu["card"] or len(igpu["card"]) > 128:
            raise StateValidationError("integrated GPU card is invalid")
        for field in ("freqMhz", "maxMhz", "util"):
            item = igpu.get(field)
            if item is not None:
                _number(item, "sclkMhz" if field != "util" else "util")
    if not present:
        if value["vendor"] is not None or value["card"] is not None:
            raise StateValidationError("absent state has device metadata")
        if any(value[field] is not None for field in NUMERIC_FIELDS):
            raise StateValidationError("absent state has telemetry")
        if value["tempSensors"] != [] or value["alertTemp"] is not None or value["alertLabel"] != "":
            raise StateValidationError("absent state has sensor data")
        return result
    vendor = value["vendor"]
    if vendor not in ("AMD", "NVIDIA"):
        raise StateValidationError("state vendor is invalid")
    card = value["card"]
    if not isinstance(card, str) or not card or len(card) > 128 or any(ord(char) < 32 for char in card):
        raise StateValidationError("state card is invalid")
    result["vendor"] = vendor
    result["card"] = card
    for field in NUMERIC_FIELDS:
        if field == "tempJunction" and vendor == "NVIDIA" and value[field] is not None:
            raise StateValidationError("NVIDIA state cannot contain a junction value")
        result[field] = _number(value[field], field)
    alert_temp = value["alertTemp"]
    if alert_temp is not None:
        _number(alert_temp, "tempGpu")
    alert_label = value["alertLabel"]
    if not isinstance(alert_label, str) or len(alert_label) > 32 or any(ord(char) < 32 for char in alert_label):
        raise StateValidationError("state alert label is invalid")
    sensors = value["tempSensors"]
    if not isinstance(sensors, list) or len(sensors) > MAX_SENSORS:
        raise StateValidationError("state sensors are invalid")
    clean_sensors = []
    seen = set()
    for sensor in sensors:
        if not isinstance(sensor, dict):
            raise StateValidationError("state sensor is invalid")
        label = sensor.get("label")
        kind = sensor.get("kind")
        if (not isinstance(label, str) or not label or len(label) > 64
                or any(ord(char) < 32 for char in label) or kind not in SENSOR_KINDS
                or (vendor == "NVIDIA" and kind == "junction")):
            raise StateValidationError("state sensor label is invalid")
        if sensor.get("value") is None:
            raise StateValidationError("state sensor value is invalid")
        sensor_value = _number(sensor.get("value"), "tempGpu")
        identity = (label, kind)
        if identity in seen:
            raise StateValidationError("state sensor is duplicated")
        seen.add(identity)
        clean_sensors.append({"label": label, "value": sensor_value, "kind": kind})
    result["tempSensors"] = clean_sensors
    if result["vramUsedG"] is not None and result["vramTotalG"] is not None:
        if result["vramUsedG"] > result["vramTotalG"]:
            raise StateValidationError("VRAM usage exceeds total")
    if result["tempJunction"] is not None:
        result["alertTemp"] = result["tempJunction"]
        result["alertLabel"] = "Junction"
    elif vendor == "NVIDIA" and result["tempGpu"] is not None:
        result["alertTemp"] = result["tempGpu"]
        result["alertLabel"] = "GPU core"
    return result


def _read_state_file(path):
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
    except OSError as error:
        raise StateSecurityError("cannot open state file") from error
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != _uid()
                or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) & 0o077
                or info.st_size > MAX_STATE_BYTES):
            raise StateSecurityError("state file has unsafe metadata")
        chunks = []
        size = 0
        while size < info.st_size:
            chunk = os.read(fd, min(4096, info.st_size - size))
            if not chunk:
                break
            size += len(chunk)
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def _open_state_lock(state_file, exclusive, create=False):
    lock_path = state_file + ".lock"
    flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    if create:
        flags |= os.O_CREAT
    try:
        fd = os.open(lock_path, flags, 0o600)
    except FileNotFoundError:
        if not create:
            return None
        raise StateSecurityError("cannot create state lock")
    except OSError as error:
        raise StateSecurityError("cannot open state lock") from error
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != _uid()
                or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) & 0o077
                or info.st_size > 4096):
            raise StateSecurityError("state lock has unsafe metadata")
        if create:
            os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        return fd
    except Exception:
        os.close(fd)
        raise


def _close_state_lock(fd):
    if fd is None:
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def validate_published_state(value):
    result = validate_state(value)
    if value["alertTemp"] != result["alertTemp"] or value["alertLabel"] != result["alertLabel"]:
        raise StateValidationError("state alert fields are inconsistent")
    return result


def decode_state(raw):
    if not isinstance(raw, (bytes, bytearray)) or len(raw) > MAX_STATE_BYTES:
        raise StateValidationError("state payload is too large")
    try:
        value = json.loads(bytes(raw).decode("utf-8", "strict"), parse_constant=_reject_constant)
    except (UnicodeError, ValueError) as error:
        raise StateValidationError("state JSON is malformed") from error
    return validate_published_state(value)


def read_state(path):
    path = validate_state_path(path)
    lock_fd = _open_state_lock(path, exclusive=False)
    try:
        return decode_state(_read_state_file(path))
    finally:
        _close_state_lock(lock_fd)


def parse_state(raw):
    return decode_state(raw)


def _write_all(fd, data):
    view = memoryview(data)
    written = 0
    while written < len(view):
        amount = os.write(fd, view[written:])
        if amount <= 0:
            raise OSError("short state write")
        written += amount


def _validate_state_target(path):
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return
    except OSError as error:
        raise StateSecurityError("state file cannot be opened safely") from error
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != _uid()
                or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) & 0o077
                or info.st_size > MAX_STATE_BYTES):
            raise StateSecurityError("state file has unsafe metadata")
    finally:
        os.close(fd)


def write_state(state_file, state):
    clean = validate_state(state)
    try:
        raw = json.dumps(clean, ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise StateValidationError("state cannot be encoded") from error
    if len(raw) > MAX_STATE_BYTES:
        raise StateValidationError("state payload is too large")
    state_file = validate_state_path(state_file, create_parent=True)
    lock_fd = _open_state_lock(state_file, exclusive=True, create=True)
    temp_path = os.path.join(
        os.path.dirname(state_file),
        "." + os.path.basename(state_file) + "." + secrets.token_hex(12) + ".tmp",
    )
    try:
        _validate_state_target(state_file)
        temp_fd = os.open(
            temp_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
            0o600,
        )
        try:
            _write_all(temp_fd, raw)
            os.fchmod(temp_fd, 0o600)
            os.fsync(temp_fd)
        except Exception:
            try:
                os.unlink(temp_path)
            except FileNotFoundError:
                pass
            raise
        finally:
            os.close(temp_fd)
        try:
            os.replace(temp_path, state_file)
            directory_fd = os.open(
                os.path.dirname(state_file),
                os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
            )
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except Exception:
            try:
                os.unlink(temp_path)
            except FileNotFoundError:
                pass
            raise
    finally:
        _close_state_lock(lock_fd)


def monitor(state_file, interval):
    last_error = None
    last_payload = None
    while True:
        try:
            state = get_state()
            payload = json.dumps(state, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)
            if payload != last_payload:
                write_state(state_file, state)
                last_payload = payload
            last_error = None
        except (StateSecurityError, StateValidationError, OSError, ValueError) as error:
            message = str(error)
            if message != last_error:
                print("gpumon: {0}".format(message), file=sys.stderr, flush=True)
                last_error = message
        time.sleep(interval)


def _positive_interval(value):
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise argparse.ArgumentTypeError("must be a number") from error
    if not math.isfinite(number) or number <= 0 or number > MAX_INTERVAL:
        raise argparse.ArgumentTypeError("must be finite, positive, and at most 3600 seconds")
    return number


def main(argv=None):
    values = argv if argv is not None else sys.argv[1:]
    parser = argparse.ArgumentParser(prog="gpumon.py", description=__doc__)
    parser.add_argument("--state-file", default=None)
    parser.add_argument("--interval", type=_positive_interval, default=DEFAULT_INTERVAL)
    parsed = parser.parse_args(values)
    interval = parsed.interval
    try:
        state_file = runtime_state_path() if not parsed.state_file else validate_state_path(parsed.state_file, True)
    except StateSecurityError as error:
        print("gpumon: {0}".format(error), file=sys.stderr, flush=True)
        return 2
    monitor(state_file, interval)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
