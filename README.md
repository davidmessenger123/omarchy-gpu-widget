# davidjm.gpu

Live view of the graphics card for the [Omarchy](https://omarchy.org/) shell
bar: utilization, clocks, temperatures, power draw, fan speed and VRAM usage —
all read-only and unprivileged. A monitor glyph shows utilization + core
temperature and turns red at/above your chosen "hot" threshold; left-click
opens a panel with the full picture.

**AMD** cards are read straight from the `amdgpu` sysfs tree. If no AMD card
is present, the monitor falls back to any **NVIDIA** GPU and reads it via
`nvidia-smi`. One card is followed (the first AMD card exposing
`gpu_busy_percent`, else the first NVIDIA GPU) — no cluster orchestration.

It is built as a **service** plus a thin **bar-widget**: one long-lived
`gpumon.py` process runs per shell (not per monitor), so a multi-monitor setup
never spins up one helper per screen. Every bar surface — one per monitor —
renders the same shared state.

## What you get

| Piece | File | Kind |
|---|---|---|
| Monitor engine (owns the `gpumon.py` process + state file) | `Service.qml` | `service` |
| Bar pill + detail panel | `BarWidget.qml` | `bar-widget` |
| AMD sysfs / NVIDIA `nvidia-smi` reader | `gpumon.py` | — |

The widget is **always visible** even when no supported GPU is detected — it
shows "GPU —" instead of a readout, so you can tell at a glance that the data
source isn't being found.

## Requirements

- A supported GPU:
  - **AMD** on the `amdgpu` driver with `gpu_busy_percent` exposed
    (RDNA-era cards and later), read from sysfs.
  - **NVIDIA** with the proprietary `nvidia` driver and `nvidia-smi` on
    `PATH` (reads fine as a normal user).
- `python3` (Arch base already includes it).
- Omarchy 4.x with the stock shell bar.

## Install

```bash
omarchy plugin add https://github.com/davidmessenger123/omarchy-gpu-widget --enable --yes
```

That clones the repo into `~/.config/omarchy/plugins/davidjm.gpu`, registers
the **service**, and places the **widget** in the bar (right section by
default). Both are needed: the service is what runs `gpumon.py` — the layout
entry is what puts the pill on the bar (the widget finds the service via
`bar.shell.serviceFor("davidjm.gpu")`).

Or install by hand:

```bash
mkdir -p ~/.config/omarchy/plugins/davidjm.gpu
cp manifest.json BarWidget.qml Service.qml gpumon.py ~/.config/omarchy/plugins/davidjm.gpu/
omarchy-shell shell rescanPlugins
omarchy plugin enable davidjm.gpu
```

To move the pill: `omarchy bar move davidjm.gpu --section left` (or `center`/`right`).

## Usage

- **Left click** — toggle the detail panel.
- **Middle click** — re-read the GPU now.
- The readout turns red when the junction temperature reaches your `hotTemp`
  threshold.

### The panel

- Header (card, e.g. `card1`), utilization + memory busy readout.
- SCLK / MCLK current clocks (clock and memory clock, in `GHz`/`MHz`).
- VRAM usage with a usage bar (turns urgent above 85%).
- Power draw and fan speed.
- Edge / junction / memory temperature chips.
- **Hot temperature** chips (`85`–`105 °C`) — click one to set when the pill
  turns red; the choice persists to that layout entry in `shell.json`.

## Configure

The shell hot-reloads `shell.json` on save. Optional setting on the layout
entry:

```jsonc
{
  "bar": {
    "layout": {
      "left": [
        { "id": "davidjm.gpu", "hotTemp": "90" }
      ]
    }
  }
}
```

| Key | Type | Default | Description |
| --- | --- | --- | --- |
| `hotTemp` | string | `"95"` | Junction temperature (°C) at/above which the pill reads red. One of `85`, `90`, `95`, `100`, `105`. |

## How it works

`Service.qml` (a single instance, one per shell — never one per monitor) runs
one long-lived `/usr/bin/python3 gpumon.py --state-file /tmp/davidjm-gpu-state.json`
that re-reads live GPU counters every second and overwrites a small JSON
state file in place (never tmp+rename — the watcher binds to the file's
inode). A `FileView` watches the file and republishes the parsed object as
`service.state`; every bar widget binds to it. `gpumon.py` needs no elevation
and is pure Python stdlib.

AMD is detected as the first card exposing `gpu_busy_percent` (with an
`amdgpu` hwmon). Active clocks come from the `pp_dpm_sclk` / `pp_dpm_mclk`
tables, taking the `*`-marked state — including the `S:` (stable) state
modern cards label their lowest power level with.

When no AMD card is found, `gpumon.py` falls back to NVIDIA: it shells out to
`nvidia-smi --query-gpu=...` (name, utilization, VRAM, temperature, power,
`clocks.sm` / `clocks.mem`, fan) once per tick and maps the first GPU onto the
same state keys — utilization + core temperature on the pill, clocks and VRAM
in the panel. Fan speed shows as "—" because `nvidia-smi` only reports a
percentage, not RPM.

If the helper ever dies the service restarts it after a short debounce; the
first write lands at startup so the readout is never blank at login.

## Remove

```bash
omarchy plugin remove davidjm.gpu
```

The plugin keeps no state of its own: everything it reads comes from sysfs on
each tick, and its settings (if any) live only in the entry you put in
`~/.config/omarchy/shell.json`, which `omarchy plugin remove` leaves untouched
so a reinstall keeps your configuration.

## License

MIT — see [`LICENSE`](LICENSE).