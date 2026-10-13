# KPI Studio

A configurable KPI extractor for ADAS MF4 recordings. Instead of hard-coding each
feature, you describe **what to measure** in a readable profile file and KPI Studio
does the rest:

1. **Load** two or more MF4 files.
2. **Pick the signals** the KPIs need (one name per signal, with fallback channel names
   for recordings that log it differently).
3. **Define the window and the moments** where KPIs are read, in a GUI, and save them by
   name to a `*.kpi.yaml` profile.
4. **Run**: KPIs go to one Excel workbook, plus interactive Plotly plots and a dashboard.

> This branch (`kpi-studio`) is separate from `codex-reconstruct-kpi-pipeline`, which
> keeps the feature-specific pipeline. KPI Studio reuses a few of its helpers
> (filters and AEB/FCW/LSAEB/LKA detectors under `src/utils/`), so those stay in the tree.

The design is in [docs/kpi_studio_architecture.md](docs/kpi_studio_architecture.md).

---

## Install
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Run
| What | Command |
|---|---|
| GUI | `python3 run_kpi_studio.py` (or `python3 -m src.kpi_studio.gui.app`) |
| Extract KPIs | `python3 -m src.kpi_studio.cli run PROFILE rec1.mf4 rec2.mf4 [-o OUT] [--workers N]` |
| Check a profile | `python3 -m src.kpi_studio.cli validate PROFILE` |
| Check signal coverage | `python3 -m src.kpi_studio.cli preflight PROFILE rec1.mf4 ...` |
| List channels in a file | `python3 -m src.kpi_studio.cli channels rec1.mf4` |
| List toolbox functions | `python3 -m src.kpi_studio.cli tools [--kind moment]` |

Example profile: `src/kpi_studio/profiles/aeb_basic.kpi.yaml`.
Step-by-step guide: [docs/kpi_studio_usage.md](docs/kpi_studio_usage.md).

## Output
Each run writes a new folder `<output>/<profile>_<timestamp>/`:
- `kpi_results.xlsx`: Summary (one row per event occurrence), one sheet per event,
  Details (status of every KPI), Moments, Files, RunInfo.
- `plots/Fig_## - <title>.html` and `dashboard.html`.
- `profile.resolved.kpi.yaml`: the exact profile used, for reproducibility.

A file with a missing signal or a broken recording is reported and skipped; the
other files still produce results.

---

## Concepts
| Term | Meaning |
|---|---|
| **Signal** | A short name mapped to one or more channel candidates; the first found in a file wins. Units are converted. |
| **Derived signal** | An expression over signals, e.g. `lowpass(longActAccel, 10)`. |
| **Event** | The window to analyse: a start/end condition (`aebTargetDecel < -6`) or a preset detector, with pre/post margins. |
| **Moment** | A named instant inside the event, e.g. `decel_onset(longJerk, threshold=-25, after=start)`. |
| **KPI** | A value at a moment, a duration between moments, a reducer over a range (`max`, `mean`, `integral`, …), or a formula over earlier KPIs. |
| **Library** | `*.kpi-lib.yaml` file of signals, events and KPIs shared between profiles (`vehicle`, `aeb_core`, `fcw_core`, `lsaeb_core`). |

## Source layout
```
src/kpi_studio/
  toolbox/   signal, condition, moment, reducer and event tools (@tool registry)
  core/      profile model, expression language, validation, alignment, engine
  io/        profile/library store (YAML), MF4 reader
  report/    Excel writer, Plotly plots and dashboard
  app/       run service (preflight, parallel processing, output folder)
  gui/       PySide6 HMI (Files, Signals, Events & KPIs, Plots & Run)
  library/   built-in KPI libraries
  profiles/  example profiles
  cli.py     command line
src/utils/   reused helpers from the pipeline (filters, event detectors, paths)
src/config/  enum_definitions.yaml (enum text for discrete signals)
```

## Your own tools
Put `.py` files using the `@tool` decorator from `src.kpi_studio.toolbox.registry` in a
folder and load it with `--user-tools FOLDER` (CLI) or the **User tools…** button (GUI). They appear in
the toolbox next to the built-in ones.

## Tests
```bash
QT_QPA_PLATFORM=offscreen python3 -m pytest tests/kpi_studio
```

## Build the app (PyInstaller)
```bash
pyinstaller kpi_studio.spec
```
Produces `dist/KPI_Studio/` (and `KPI_Studio.app` on macOS). Run it with no arguments
for the GUI, or with CLI arguments (`KPI_Studio run PROFILE files...`).
The legacy pipeline's `run_kpi_tool.spec` is left unchanged.
