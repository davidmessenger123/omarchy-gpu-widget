#!/usr/bin/python3
"""gpumon — publish live GPU metrics for the GPU Monitor bar widget.

A long-lived monitor (one process, not one per tick): every `interval`
seconds it reads the amdgpu sysfs hierarchy — utilization, current DPM
clocks, temperatures, power draw, fan speed and VRAM usage — and writes a
small JSON state file that the plugin's service watches.

  gpumon.py [--state-file PATH] [--interval SECONDS]

The state file is overwritten in place (never tmp+rename): the bar's
QFileSystemWatcher binds to the file's inode, and a rename would orphan it.
Read-only and unprivileged; needs no elevation. The DRM card is detected
automatically (first card exposing gpu_busy_percent with an amdgpu hwmon).
"""

import argparse
import glob
import json
import os
import re
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


def get_state():
    card, dev = find_card()
    empty = {
        "present": False,
        "card": None,
        "util": None, "memUtil": None,
        "sclkMhz": None, "mclkMhz": None,
        "tempEdge": None, "tempJunction": None, "tempMem": None,
        "powerW": None, "fanRpm": None,
        "vramUsedG": None, "vramTotalG": None,
    }
    if dev is None:
        return empty

    state = dict(empty)
    state["present"] = True
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