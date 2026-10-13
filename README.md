# 🚗 ADAS KPI Processing Pipelines

End-to-end tooling for extracting and visualizing ADAS KPIs from MF4 logs. Supports longitudinal (AEB, FCW, LSAEB) and lateral (LKA) features, event-level KPIs, cycle/availability KPIs, and interactive Plotly visualizations.

---

## 📦 Install
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```
PySide6 is included in `requirements.txt` for the GUI; on some systems you may need Qt platform plugins (X11/Wayland on Linux, or ensure Xcode Command Line Tools on macOS).

---

## ▶️ Run
- GUI (PySide6): `python3 -m src.gui.app`  
  Select MF4 folder → pick features → hit RUN; logs stream into the window as each feature runs sequentially.
- Compatibility entrypoint: `python3 -m src.run_kpi_tool` (launches the same GUI).
- **KPI Studio** (configurable KPI extractor): `python3 -m src.kpi_studio.gui.app`, or the CLI `python3 -m src.kpi_studio.cli run src/kpi_studio/profiles/aeb_basic.kpi.yaml rec1.mf4 rec2.mf4`. See [docs/kpi_studio_usage.md](docs/kpi_studio_usage.md).
Configs: `src/config/config_as_long.json` and `src/config/config_as_lat.json`.

Outputs (per run) under `rawdata/analysis_results/`:
- `*_kpi_results.xlsx` with event sheets (`aeb`, `fcw`, `lsaeb`, `lka`) plus shared `cycleKPI`.
- HTML plots per feature in subfolders (e.g., `analysis_results/aeb/`).
- KPI catalog: `docs/kpi_catalog.md`.

---

## 🧭 Workflow
1) **Config load** (`src/config/config.py`)  
   Reads JSON + KPI Excel (vbRcSignals, graphSpec, lineColors, markerShapes, KPI schemas, calibratables).
2) **Signal extraction** (`src/pipeline/input_handler.py`)  
   Parses MF4, maps signals, writes extracted MF4 chunks and `.mat` files.
3) **Event detection** (`src/pipeline/base/base_event_segmenter.py` subclasses)  
   Splits logs into per-event chunks (AEB/FCW/LSAEB/LKA).
4) **KPI extraction**  
   - Event KPIs via `BaseEventKpiExtractor` subclasses.  
   - Cycle/availability KPIs via `BaseCycleKpiExtractor` subclasses. Shared distance-weighted features now use `src/pipeline/base/rule_based_cycle_kpi_extractor.py`.  
   Exported into `*_kpi_results.xlsx`.
5) **Visualization**  
   `src/viz` renders Matplotlib → Plotly HTML (calibrations, averages, legend ordering).

---

## 📂 Key Modules
- `src/config/` — configs, enum definitions, KPI schemas.
- `src/pipeline/` — feature registry, declarative pipelines, event/cycle extractors, visualizers.
- `src/viz/` — plotter registry, exporters, figure/style/filter managers.
- `src/gui/` — PySide6 desktop UI (folder selection, feature toggles, runner).
- `src/utils/` — IO helpers, KPI table builders, path utilities.

---

## 🧱 Extension Model
- Add new features in `src/pipeline/registry.py`; the GUI runner and domain entrypoints read from that registry instead of hard-coding feature lists.
- Reuse `src/pipeline/base/base_pipeline.py` by declaring component classes on the pipeline class rather than re-implementing `_detect_events`, `_extract_kpis`, and `_visualize_results`.
- Reuse `src/pipeline/base/rule_based_cycle_kpi_extractor.py` for distance-based cycle KPIs driven by signal masks and calibratable thresholds.
- Keep exported KPI names documented in `docs/kpi_catalog.md`.

---

## 🗂️ Outputs & Sheets
- Event KPIs: per-feature sheets (`aeb`, `fcw`, `lsaeb`, `lka`).
- Cycle KPIs: combined `cycleKPI` sheet keyed by `label` + `feature` so multiple features coexist.
- Plots: HTML in `analysis_results/<feature>/Fig_## - <title>.html`.

---

## 🧪 Quick Checks
```bash
python3 -m src.run_kpi_tool
# verify analysis_results contains updated Excel and HTML outputs
```

---

## 📦 Build Executable (PyInstaller)
Ensure dependencies are installed (`pip install -r requirements.txt`), then build from repo root:
```bash
pyinstaller run_kpi_tool.spec
```
Artifacts:
- `dist/ADAS_KPI_Tool/` directory bundle (contains CLI + GUI).
- On macOS, a `.app` bundle is also produced.
If the GUI fails to start due to Qt plugins, verify `PySide6` is installed and rerun the build; the spec already collects PySide6, pyecharts, and Qt data.

---

## 🛠️ Notes
- Requirements: see `requirements.txt` (asammdf, numpy/pandas/scipy, matplotlib/plotly/pyecharts, openpyxl, PyYAML, pyinstaller, etc.).
- Keep the KPI Excel files in `src/config/` aligned with vbRcSignals, graph specs, and KPI definitions.
- Migration: `signal_map.xlsx` is deprecated; move vbRcSignals into `kpi_as_long.xlsx` and `kpi_as_lat.xlsx`.
