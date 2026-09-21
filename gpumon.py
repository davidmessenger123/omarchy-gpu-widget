#!/usr/bin/python3
"""gpumon — publish live GPU metrics for the GPU Monitor bar widget.

A long-lived monitor (one process, not one per tick): every `interval`
seconds it reads live GPU counters and writes a small JSON state file that
the plugin's service watches.

  gpumon.py [--state-file PATH] [--interval SECONDS]

Read-only and unprivileged; needs no elevation. The GPU is detected
automatically: an AMD card exposing gpu_busy_percent is read straight from
the amdgpu sysfs tree; if none is found, any NVIDIA GPU present is read via
`nvidia-smi`. The state file is overwritten in place (never tmp+rename):
the bar's QFileSystemWatcher binds to the file's inode, and a rename would
orphan it.
"""

import argparse
import glob
import json
import os
import re
import subprocess
import sys
import time

DEFAULT_STATE = "/tmp/davidjm-gpu-state.json"
DEFAULT_INTERVAL = 1.0


def _read(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return None


def _read_int(path):
    try:
        return int(_read(path).split()[0])
    except (ValueError, IndexError, AttributeError):
        return None


def _read_float(path):
    try:
        return float(_read(path).split()[0])
    except (ValueError, IndexError, AttributeError):
        return None


def find_card():
    """(card_dir, device_dir) for the first AMD card with gpu_busy_percent."""
    for card in sorted(glob.glob("/sys/class/drm/card[0-9]*")):
        dev = os.path.join(card, "device")
        if os.path.exists(os.path.join(dev, "gpu_busy_percent")):
            return card, dev
    return None, None


def find_hwmon(device_dir):
    """hwmon dir (name == amdgpu) for a card's device dir, or None."""
    hwmon_root = os.path.join(device_dir, "hwmon")
    try:
        entries = sorted(os.listdir(hwmon_root))
    except OSError:
        return None
    for entry in entries:
        base = os.path.join(hwmon_root, entry)
        if _read(os.path.join(base, "name")) == "amdgpu":
            return base
    return None


def dpm_current(path):
    """Current DPM clock in MHz from a pp_dpm_* table ('*' line), or None.

    Most states are numbered ('0: 500Mhz '), but modern AMD cards also label
    their lowest power state 'S' (the stable/static clock, e.g. 'S: 0Mhz *')
    and mark whichever state is active with a trailing '*'. Accept both
    prefixes so an idle sclk still reads back instead of coming out as None.
    """
    text = _read(path)
    if text is None:
        return None
    for line in text.splitlines():
        m = re.match(r"\s*[0-9S]+:\s*([0-9.]+)Mhz\s*\*", line)
        if m:
            return float(m.group(1))
    return None


def temp_input(hwmon, index):
    """hwmon tempN_input in °C (millidegrees), or None."""
    val = _read_int(os.path.join(hwmon, "temp%d_input" % index))
    return round(val / 1000.0, 1) if val is not None else None


def empty_state():
    """All-None board state used when no supported GPU is detected."""
    return {
        "present": False,
        "vendor": None,
        "card": None,
        "util": None, "memUtil": None,
        "sclkMhz": None, "mclkMhz": None,
        "tempEdge": None, "tempJunction": None, "tempMem": None,
        "powerW": None, "fanRpm": None,
        "vramUsedG": None, "vramTotalG": None,
    }


def _amd_state(card, dev):
    """State for an AMD card read from its amdgpu sysfs device dir."""
    state = empty_state()
    state["present"] = True
    state["vendor"] = "AMD"
    state["card"] = os.path.basename(card)
    state["util"] = _read_int(os.path.join(dev, "gpu_busy_percent"))
    state["memUtil"] = _read_int(os.path.join(dev, "mem_busy_percent"))
    state["sclkMhz"] = dpm_current(os.path.join(dev, "pp_dpm_sclk"))
    state["mclkMhz"] = dpm_current(os.path.join(dev, "pp_dpm_mclk"))

    total = _read_int(os.path.join(dev, "mem_info_vram_total"))
    used = _read_int(os.path.join(dev, "mem_info_vram_used"))
    if total and used is not None:
        state["vramTotalG"] = round(total / (1024 ** 3), 1)
        state["vramUsedG"] = round(used / (1024 ** 3), 1)

    hwmon = find_hwmon(dev)
    if hwmon:
        state["tempEdge"] = temp_input(hwmon, 1)
        state["tempJunction"] = temp_input(hwmon, 2)
        state["tempMem"] = temp_input(hwmon, 3)
        power = _read_int(os.path.join(hwmon, "power1_average"))
        state["powerW"] = round(power / 1e6, 1) if power is not None else None
        state["fanRpm"] = _read_int(os.path.join(hwmon, "fan1_input"))

    return state


NV_QUERY = ("name,utilization.gpu,memory.used,memory.total,temperature.gpu,"
            "power.draw,clocks.sm,clocks.mem,fan.speed")


def _nv_num(raw):
    """Float from an nvidia-smi raw token ('[N/A]'/'[Not Supported]' -> None)."""
    try:
        return float(raw.split()[0])
    except (ValueError, IndexError, AttributeError):
        return None


def _nvidia_rows():
    """Parsed nvidia-smi rows (one per GPU), or [] when nothing is there."""
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=" + NV_QUERY,
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=3, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    rows = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        rows.append([p.strip() for p in line.split(",")])
    return rows


def _nvidia_state():
    """State for the first NVIDIA GPU via nvidia-smi, or None if absent."""
    rows = _nvidia_rows()
    if not rows:
        return None
    r = rows[0]

    state = empty_state()
    state["present"] = True
    state["vendor"] = "NVIDIA"
    state["card"] = r[0]
    state["util"] = _nv_num(r[1])
    state["tempJunction"] = _nv_num(r[4])
    state["powerW"] = _nv_num(r[5])
    state["sclkMhz"] = _nv_num(r[6])
    state["mclkMhz"] = _nv_num(r[7])
    # nvidia-smi only reports fan speed as a percentage, never an RPM.
    state["fanRpm"] = None

    used, total = _nv_num(r[2]), _nv_num(r[3])
    if used is not None and total:
        state["vramUsedG"] = round(used / 1024, 1)
        state["vramTotalG"] = round(total / 1024, 1)
        state["memUtil"] = round(min(100.0, used / total * 100))

    return state


def get_state():
    """Live state for the detected GPU: AMD sysfs first, NVIDIA fallback."""
    card, dev = find_card()
    if dev is not None:
        return _amd_state(card, dev)
    nv = _nvidia_state()
    if nv is not None:
        return nv
    return empty_state()


def write_state(state_file, state):
    with open(state_file, "w", encoding="utf-8") as fh:
        json.dump(state, fh)
        fh.flush()
        os.fsync(fh.fileno())


def monitor(state_file, interval):
    while True:
        try:
            write_state(state_file, get_state())
        except Exception:
            pass
        time.sleep(interval)


def main(argv=None):
    args = argv if argv is not None else sys.argv[1:]
    ap = argparse.ArgumentParser(prog="gpumon.py", description=__doc__)
    ap.add_argument("--state-file", default=DEFAULT_STATE)
    ap.add_argument("--interval", type=float, default=DEFAULT_INTERVAL)
    parsed = ap.parse_args(args)
    if parsed.interval <= 0:
        parsed.interval = DEFAULT_INTERVAL
    monitor(parsed.state_file, parsed.interval)


if __name__ == "__main__":
    main()