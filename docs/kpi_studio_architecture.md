# KPI Studio — architecture for a configurable KPI extractor

Status: design agreed (decisions in section 12), no implementation yet.
Branch: `claude/project-thread-ki5k8a` (forked from `codex-reconstruct-kpi-pipeline`).

## 1. What we are building

The current tool hard-codes one pipeline per feature (AEB, FCW, LSAEB, LKA): each has its own
segmenter, KPI extractor and visualizer class, and its KPI definitions live in Python plus
`kpi_as_long.xlsx`. Adding a KPI means writing code.

KPI Studio turns that around. The user describes KPIs in a **profile**, a human-readable YAML file,
using a GUI, and a generic engine executes it:

1. **Load** two or more MF4 files.
2. **Pick signals** from the channels found in those files and give them stable names.
3. **Define events, windows and moments**: when something happens, which time window to look at,
   and the exact moment inside it where each KPI is read. Each KPI gets a name; the whole set is
   saved as a profile file.
4. **Run**: extract every KPI for every event occurrence in every file, write an Excel workbook and
   interactive HTML plots.

Non-goals for the first version: replacing the existing feature pipelines (they keep working
side by side), cycle/availability KPIs over whole drives (can be added later as a `reduce` over the
full recording), editing MF4 files.

## 2. Core concepts

| Concept | Meaning | Example |
|---|---|---|
| **Profile** | One YAML file holding everything below. Has a name and a schema version. | `aeb_ncap.kpi.yaml` |
| **Signal** | A stable name mapped to one or more candidate MF4 channel names, with unit and kind. | `egoSpeed` ← `VehSpd`, `ESP_v_Signal` |
| **Derived signal** | A signal computed from others with an expression. | `egoSpeedKph = egoSpeed * 3.6` |
| **Event** | A condition whose rising edge starts an occurrence; optional end condition. | `aebRequest in (1, 2)` |
| **Window** | Time range analysed around each occurrence. | 6 s before start, 3 s after end |
| **Moment** | A named instant inside the window, picked by a rule. | first time `longActAccelFlt < -2` after start |
| **KPI** | A named value read at a moment, between two moments, or reduced over a range. | speed at trigger, brake latency, peak decel |
| **Plot** | A chart of KPI results or of signals around occurrences. | scatter of stop distance vs. trigger speed |

The key idea for "pick the exact moment" is that **moments are first-class and named**. KPIs refer
to moments instead of re-implementing search logic, so one moment (e.g. `brake_onset`) can feed
several KPIs and is drawn as a marker in the verification plots.

## 3. Profile file format

YAML was chosen over JSON/Excel because it allows comments, diffs cleanly in git, and stays readable
when hand-edited. Every profile is validated against a strict schema (unknown keys are errors, so a
typo like `widnow:` is caught immediately instead of silently ignored).

```yaml
schema_version: 1
name: AEB basic
description: Trigger speed, latency and stopping KPIs for AEB activations

imports:                        # shared KPI library files (section 3.4)
  - library/aeb_core.kpi-lib.yaml

timebase:
  rate_hz: 100                  # every file is resampled onto this grid

signals:
  egoSpeed:
    channels: [VehSpd, ESP_v_Signal, egoSpeed]   # first channel found in a file wins
    unit: m/s
    kind: continuous            # continuous -> linear interpolation
  aebRequest:
    channels: [AEB_Req, aebRequest]
    kind: discrete              # discrete -> zero-order hold, never interpolated
    enum: AutoEmergencyBrakingRequestType        # optional, from enum_definitions.yaml
  longActAccel:
    channels: [LongAccel, longActAccel]
    unit: m/s^2
  objDistance:
    channels: [ObjDistX, objDistance]
    unit: m
    required: false             # KPIs using it become "missing_signal" instead of failing the file

derived:
  egoSpeedKph: egoSpeed * 3.6
  longActAccelFlt: lowpass(longActAccel, 10)     # Butterworth, cutoff in Hz
  egoSpeedMph:                                   # long form: unit and description are optional
    expr: egoSpeed * 2.23694
    unit: mph
    description: ego speed in miles per hour

events:
  aeb:
    start: aebRequest in (FULL, PARTIAL)         # enum names or numbers
    end: aebRequest == NONE                      # optional; default = start condition goes false
    min_duration: 0.1                            # s, ignore glitches
    merge_gap: 0.5                               # s, merge occurrences closer than this
    window: { pre: 6.0, post: 3.0 }              # relative to start / end
    moments:
      brake_onset: { first: longActAccelFlt < -2, after: start, within: 1.5 }
      stop:        { first: egoSpeed < 0.1, after: start }
      peak_decel:  { argmin: longActAccelFlt, between: [start, end] }
      ttc_1s:      { crossing: objDistance / max(egoSpeed, 0.1), level: 1.0, direction: falling, before: start }

kpis:
  - name: trigger_speed
    event: aeb
    value: egoSpeedKph
    at: start
    unit: km/h
  - name: brake_latency
    event: aeb
    duration: [start, brake_onset]
    unit: s
  - name: peak_decel
    event: aeb
    value: longActAccelFlt
    at: peak_decel
    unit: m/s^2
  - name: stop_distance
    event: aeb
    value: objDistance
    at: stop
    unit: m
  - name: max_abs_steer_rate
    event: aeb
    reduce: max
    of: abs(derivative(steerWheelAngle))
    between: [start, end]
  - use: aeb_core.impact_rel_speed   # KPI taken from the imported library

plots:
  - type: scatter
    title: Stop distance vs trigger speed
    x: trigger_speed
    y: stop_distance
    color_by: file
    reference_lines: [{ y: 0, label: contact }]
  - type: box
    title: Brake latency per file
    y: brake_latency
    group_by: file
  - type: timeseries                # one figure per occurrence, for verification
    event: aeb
    signals: [egoSpeedKph, longActAccelFlt, aebRequest]
    show_window: true
    show_moments: true
```

### 3.1 Moment rules

All rules search inside the occurrence window unless narrowed.

| Rule | Picks | Options |
|---|---|---|
| `start`, `end`, `window_start`, `window_end` | built-in moments of every occurrence | — |
| `first: <cond>` / `last: <cond>` | first/last sample where the condition is true | `after`, `before`, `within` (s), `hold` (must stay true for N s) |
| `argmax: <expr>` / `argmin: <expr>` | time of the extreme value | `between: [m1, m2]` |
| `crossing: <expr>` | time `<expr>` crosses `level` | `direction: rising/falling/any`, `after`, `before`; time is interpolated between samples |
| `offset: <moment>` | another moment shifted by a constant | `by: -0.5` |

If a rule finds nothing, the moment is `None` and every KPI depending on it gets status
`moment_not_found`. Nothing crashes, and the reason is visible in Excel.

### 3.2 KPI forms

| Form | Keys | Result |
|---|---|---|
| Value at a moment | `value`, `at` | expression evaluated at that instant (interpolated or held by signal kind) |
| Duration | `duration: [m1, m2]` | `t(m2) - t(m1)` in seconds |
| Reduction | `reduce` (`max`, `min`, `mean`, `integral`, `time_true`, `count_rising`), `of`, `between` | one number over a range |

Every KPI result row also stores the moment time it was read at, so a reviewer can jump to it.

### 3.3 Expression language

Conditions and values are short expressions such as `aebRequest in (FULL, PARTIAL) and egoSpeed > 5`.
They are parsed with Python's `ast` module against a whitelist and evaluated vectorised on numpy
arrays; **nothing is ever passed to `eval`**. Allowed:

- signal names, numbers, enum member names of the signal being compared
- `+ - * / ** abs`, comparisons, `and or not`, `in (...)`
- any function registered in the **toolbox** (section 4), e.g. `lowpass(x, 10)`, `derivative(x)`,
  `rising(cond)`, `hold(cond, 0.2)`, `ttc(objDistance, relSpeed)`

Expressions are checked when the profile loads (unknown names, wrong argument counts) and errors
point to the profile key, e.g. `events.aeb.moments.brake_onset: unknown signal 'longAccFlt'`.

### 3.4 Shared KPI library

Signals, derived signals, events, moments and KPIs can live in **library files**
(`*.kpi-lib.yaml`, same syntax as a profile minus `plots` and `timebase`). A profile lists them under
`imports` and refers to entries as `<library>.<name>`, either with `use:` or directly in expressions
and moment references. Rules:

- Each library has its own namespace, so two libraries may both define `trigger_speed`.
- A profile may override an imported signal's `channels` (different logger, same meaning) but not
  silently redefine an imported KPI; a name clash is a load error.
- Imports are resolved at load time into one flat profile. Every run saves that **resolved** profile,
  so results stay reproducible even if a library changes later.
- In the GUI, the KPI editor has "Save to library" and "Add from library" actions.

### 3.5 Profile scope

One profile is applied to every file in a run. Speed-dependent calibration curves are out of
scope for the first version; thresholds are constants in the profile.

## 4. Toolbox

The toolbox is the library of reusable, tested building blocks that profiles call. It is how
today's hand-written logic (filters, onset detection, braking windows, stop distance, TTC) becomes
available to any KPI without new pipeline code.

### 4.1 Tool kinds

| Kind | Input → output | Used in | Examples |
|---|---|---|---|
| **Signal** | arrays → array | `derived`, any expression | `lowpass`, `highpass`, `moving_average`, `derivative`, `jerk`, `integral`, `delay`, `rate_limit`, `to_kph`, `to_deg`, `to_g`, `rel_speed`, `ttc`, `enum_decode`, `fillna_hold` |
| **Condition** | arrays → bool array | event `start`/`end`, `first`/`last` | `rising`, `falling`, `hold(cond, s)`, `debounce(cond, on_s, off_s)`, `between`, `in_band(x, ref, tol)`, `stable(x, tol, s)`, `changed(x)` |
| **Moment** | arrays + search range → time or None | `moments:` | `first`, `last`, `argmax`, `argmin`, `crossing`, `offset`, `decel_onset(jerk, thd)`, `jerk_end(jerk, thd)`, `first_nonzero(x)`, `vehicle_stop(speed, thd, hold)`, `settled(x, tol, s)` |
| **Reducer** | arrays + range → number | `reduce:` | `max`, `min`, `mean`, `time_mean`, `rms`, `peak_to_peak`, `integral`, `distance(speed)`, `time_true`, `count_rising`, `value_at` |
| **Event preset** | arrays → occurrences | `events:` via `preset:` | `aeb_request`, `fcw_request`, `lsaeb_cpm`, `lka_intervention`, `target_decel_window` |

Event presets and domain moments are thin wrappers around existing, already-tested functions, so a
profile can say `start: { preset: aeb_request, signal: aebRequest }` or
`brake_onset: { decel_onset: jerk(longActAccelFlt), threshold: -5, after: start }` instead of
spelling out the logic.

### 4.2 How a tool is defined

```python
# src/kpi_studio/toolbox/signal/filters.py
@tool(kind="signal", category="Filters", unit="same")
def lowpass(x: Signal, cutoff_hz: Param[float, Gt(0)], order: Param[int] = 4) -> Signal:
    """Zero-phase Butterworth low-pass filter."""
    ...
```

- The `@tool` decorator registers the function in one **registry** with its kind, category,
  parameter types and limits, unit behaviour and docstring.
- The expression validator, the moment/reducer schema and the GUI all read the registry. Adding a
  tool therefore automatically makes it valid in profiles, shows it in the GUI's tool palette with
  its documentation, and builds its parameter form; no GUI code is written per tool.
- Tools are pure numpy functions: no file access, no Qt, no globals. They receive the common
  timebase (`dt`, `time`) through a context object when they need it.
- Every tool states how it handles NaN, empty input and arrays shorter than its own needs
  (e.g. a filter shorter than its pad length returns the input with a warning, as
  `accel_filter` does today).

### 4.3 Toolbox robustness

- **Contract tests** run automatically on every registered tool with a standard set of synthetic
  inputs (constant, step, ramp, noise, NaN gaps, length 0/1/2, wrong dtype). A tool cannot be added
  without passing them.
- Each tool also has its own behaviour tests with known answers.
- Tools are versioned with the package; the run's `RunInfo` sheet records the toolbox version.
- **User tools** (optional, off by default): Python files in a user tools folder are registered at
  start-up when enabled in settings. They are listed separately in the GUI and recorded in
  `RunInfo`, and they run through the same contract tests from a "Check tools" button.

### 4.4 Seeded from the current code

| Existing code | Becomes |
|---|---|
| `utils/signal_filters.accel_filter` | `lowpass` |
| `utils/mf4_postprocess` conversions (`egoSpeed*3.6`, `np.degrees`) | `to_kph`, `to_deg` |
| `utils/mf4_extractor.convert_tact_unit` | `to_g` and unit conversion helpers |
| `utils/enum_loader.EnumMapper` | `enum_decode`, enum names in conditions |
| `utils/event_detector/as_long/aeb.detect_aeb_events` | event preset `aeb_request` |
| `utils/event_detector/as_long/fcw.detect_fcw_events`, `lsaeb.detect_lsaeb_events`, `as_lat/lka.detect_lka_events` | event presets `fcw_request`, `lsaeb_cpm`, `lka_intervention` (merge window as parameter) |
| `event_detector/as_long/aeb.find_first_last_indices`, `find_aeb_intv_start` | `first`, `last` with tolerance |
| `event_detector/as_long/aeb.find_aeb_intv_end` | `vehicle_stop` and `first` combined in the AEB library |
| `event_detector/as_long/decel.detect_decel_onset`, `detect_brake_jerk_end` | moments `decel_onset`, `jerk_end` |
| `kpis/as_long/autonomous_braking_window` | event preset `target_decel_window` |
| `kpis/as_long/actual_decel_onset` | moment `decel_onset` applied after a request edge |
| `kpis/as_long/braking_average_accel` | reducer `time_mean` |
| `kpis/as_long/braking_stop_distance` | reducer `distance(speed)` |
| `kpis/as_long/aeb/*` (latency, distance, impact speed, steering, throttle, yaw, lat accel, brake mode), `fcw/*`, `lsaeb/distance` | KPIs in `library/aeb_core`, `library/fcw_core`, `library/lsaeb_core`, built from the tools above; the classes' numeric core is moved into tools where it is generic |

The existing classes take an `extractor` object and read config attributes; the toolbox versions
take arrays and explicit parameters instead, so they can be tested and reused on their own.

## 5. Architecture

```mermaid
flowchart TB
  subgraph GUI["gui (PySide6)"]
    FP[Files page] --> SP[Signals page] --> KP[KPI editor + preview] --> RP[Run page]
  end
  CLI[cli.py] --> RS
  GUI --> RS[app.RunService]
  GUI --> PS
  RS --> PS[io.profile_store]
  RS --> MR[io.mf4_reader]
  RS --> EN[core.engine]
  RS --> RW[report.excel_writer]
  RS --> PL[report.plots]
  EN --> EX[core.expr]
  EN --> MD[core.model]
  EN --> TB[toolbox registry]
  EX --> TB
  GUI --> TB
  PS --> MD
```

Dependency rule: `gui → app → io / report → core → toolbox`. `core` and `toolbox` import only
numpy, scipy, pandas and pydantic,
never Qt, asammdf or openpyxl. That keeps the engine fast to unit-test with synthetic arrays and
lets the same run be driven from the GUI, the CLI or a test.

### 5.1 Module layout

```
src/kpi_studio/
  core/
    model.py          # pydantic schema: Profile, SignalDef, EventDef, MomentRule, KpiDef, PlotDef
    expr.py           # safe parser + vectorised evaluator, name/arity checks
    engine.py         # detect_occurrences(), resolve_moments(), compute_kpis()
    results.py        # Occurrence, KpiResult, Status enum, results -> DataFrame
  toolbox/
    registry.py       # @tool decorator, Param types, lookup by kind/category
    signal/           # filters.py, calculus.py, units.py, kinematics.py (ttc, rel_speed), enums.py
    condition/        # edges.py, timing.py (hold, debounce), bands.py
    moment/           # search.py (first/last/argmax/crossing), braking.py (decel_onset, vehicle_stop)
    reducer/          # stats.py, integrals.py (distance, time_mean)
    event/            # presets.py (aeb_request, fcw_request, lsaeb_cpm, lka_intervention, ...)
  io/
    mf4_index.py      # scan files: channels, units, rasters, duration (no sample data)
    mf4_reader.py     # read only mapped channels, align onto timebase -> SignalFrame
    profile_store.py  # load/save YAML, imports resolution, schema_version migrations, atomic writes
  report/
    excel_writer.py   # Summary / per-event / Details / Files / RunInfo sheets
    plots.py          # Plotly figures from PlotDef + dashboard index
  app/
    run_service.py    # preflight, per-file jobs, cancellation, progress events
    preflight.py      # signal coverage per file, expression errors, warnings
  gui/
    main_window.py    # 4-step wizard shell
    pages/files_page.py, signals_page.py, kpi_editor_page.py, run_page.py
    widgets/condition_builder.py, moment_editor.py, signal_preview.py, tool_palette.py
  cli.py              # python -m src.kpi_studio.cli run profile.yaml a.mf4 b.mf4 -o out/
library/aeb_core.kpi-lib.yaml, fcw_core.kpi-lib.yaml, lsaeb_core.kpi-lib.yaml
profiles/examples/aeb_basic.kpi.yaml
```

### 5.2 Run data flow

```
files ──► mf4_index (headers only) ──► preflight ──► user confirms
                                                    │
for each file (worker process) ─────────────────────┘
   mf4_reader: read mapped channels → align to 100 Hz grid → SignalFrame
   engine: derived signals → occurrences per event → moments → KPI results
   return list[KpiResult] + FileReport           (no intermediate MF4 chunks on disk)
                    │
merge results ──► excel_writer ──► plots ──► dashboard.html
```

Key data types:

- `SignalFrame`: `time: ndarray`, `values: dict[str, ndarray]`, `units`, `kinds`, `source_channel`
  per signal (for traceability).
- `Occurrence`: file, event, index, `t_start`, `t_end`, `t_window_start`, `t_window_end`.
- `KpiResult`: file, event, occurrence, kpi, value, unit, moment time, `status`, message.
  Results are a long, tidy table; Excel's wide view is a pivot of it.

## 6. GUI

A four-step window that mirrors the workflow, with the profile always editable and savable.

1. **Files**: add/remove MF4 files (drag and drop, multi-select). Shows duration, size and a
   load status per file. Headers are indexed in the background so the window never freezes.
2. **Signals**: a searchable channel tree, merged across files, with a coverage column
   ("found in 3/4 files"). Picking a channel creates a signal; when files name the same thing
   differently the user adds alternatives to `channels`. Derived signals are added with an
   expression field that validates as you type. A small plot previews the signal from any file.
3. **KPI editor**: left list of events and KPIs; right side a form:
   - event start/end built with a condition builder (signal, operator, value rows joined by and/or)
     that writes the expression text, with a raw-text toggle for power users;
   - window pre/post spin boxes;
   - moment editor (rule type dropdown + fields from section 3.1);
   - KPI form (name, form, unit).
   A **Preview** pane runs the current profile on one selected file and draws the signals with the
   window shaded and every moment marked, plus the KPI values. This is how the user confirms the
   moment is the exact one they meant before running a batch.
   **Save / Save as** writes the profile YAML; the KPI and profile names are what the user sees.
4. **Run**: output folder, preflight summary (missing signals per file, warnings), Run and Cancel
   buttons, per-file progress, log panel, and buttons to open the Excel and dashboard when done.

A **tool palette** (searchable, grouped by category) sits beside every expression field: picking a
tool inserts it with a parameter form built from the registry and shows its documentation.

The last files, folder and profile are remembered with `QSettings`, as the current GUI does.

## 7. Outputs

`<output>/<profile>_<timestamp>/`

- `kpi_results.xlsx`
  - `Summary`: one row per occurrence (file, event, #, start time, end time) and one column per KPI,
    header includes the unit; failed cells are empty and highlighted.
  - one sheet per event when the profile has several events.
  - `Details`: the long table, including moment times, status and message for every KPI.
  - `Files`: per file status, duration, which channel each signal was read from, missing signals.
  - `RunInfo`: profile name, profile SHA-256, tool version, run time, input file list, full profile
    text.
- `plots/*.html` plus `dashboard.html` linking all of them.
- `profile.kpi.yaml`: a copy of the exact profile used, so any result can be reproduced.

## 8. Robustness

| Risk | How the design handles it |
|---|---|
| Bad or mistyped profile | Strict pydantic schema, unknown keys rejected, expressions validated at load, errors name the exact key. |
| Different channel names across loggers | Ordered `channels` candidates per signal, exact name first then base name (logic reused from `mf4_extractor._match_channel`). |
| Signal missing in some files | Preflight shows it before the run; optional signals degrade to `missing_signal` per KPI; required ones skip only that file. |
| Different rates, duplicate or non-monotonic timestamps | Every file aligned onto one timebase over the overlapping range; duplicates dropped, sorting enforced; no extrapolation; discrete signals held, never interpolated. |
| One file or KPI fails | Isolation at file, occurrence and KPI level. Each failure becomes a status row; the run continues and the `Files` sheet says what happened. |
| Large files / memory | Only mapped channels are read (`MDF.select`); files are processed one per worker process and released; no MF4 chunk files are written. |
| GUI freezing | Indexing, preview and runs happen off the UI thread; progress is reported through typed Qt signals, and Cancel stops between files. |
| Arbitrary code in profiles | Whitelisted AST evaluator, no `eval`/`exec`. |
| Output corrupted or locked | Profile and Excel written to a temp file then renamed; a locked Excel (open in Excel on Windows) produces a clear message and a retry, not a crash. |
| Results that can't be reproduced | Profile copy, hash, tool version and inputs saved with every run. |
| A toolbox function misbehaves on odd input | Contract tests on every tool (NaN, empty, short, constant, step); user tools off by default and checked the same way. |
| A shared library changes under old results | Runs save the resolved profile; library name clashes are load errors. |
| Profile format evolves | `schema_version` plus migration functions in `profile_store`; old profiles keep loading. |
| Silent wrong numbers | Every KPI stores the moment time; verification time-series plots show window and moments; golden tests on known signals. |

Logging uses the standard `logging` module (file + GUI handler) instead of redirecting stdout as the
current runner does.

## 9. What is reused from `codex-reconstruct-kpi-pipeline`

| Existing code | Use in KPI Studio |
|---|---|
| `src/utils/mf4_extractor.py`: channel matching, suffix cleanup, duplicate-timestamp handling, unit maps and `convert_tact_unit` | Moved into `io/mf4_reader.py` / `io/mf4_index.py` behind a smaller API; the signal-database (Excel) path is dropped. |
| `src/utils/signal_mdf.py`: robust time-vector resolution (`_resolve_time`), `safe_load_mdf` | Reused for files with several masters or missing time channels. |
| `src/utils/signal_filters.py`, `enum_loader.py`, `event_detector/*`, `kpis/*` | Moved into the toolbox; see section 4.4 for the one-by-one mapping. |
| `config/enum_definitions.yaml` | Enum names usable in conditions (`aebRequest == FULL`). |
| `src/utils/exporter.py` | Pattern kept, rewritten to write the whole workbook once instead of re-opening it per sheet. |
| `src/viz/visualizers/dashboard_visualizer.py` | Reused as is for `dashboard.html`. |
| `src/viz/plotters/*` and `src/viz/core/*` | Styling and layer ideas (averages, reference lines) carried over; new figures are built directly in Plotly because the current plotters are tied to `graphSpec` sheets and Matplotlib-to-Plotly conversion. |
| `src/gui/main_window.py`, `gui/controllers/pipeline_runner.py` | Stylesheet, `QSettings` persistence and the `QThread` runner pattern; stdout capture replaced by typed progress signals. |
| `src/utils/path_manager.py`, `run_kpi_tool.spec`, `hooks/` | Resource paths and PyInstaller packaging; the spec gains a second entry point. |
| Existing AEB/FCW/LSAEB KPI definitions | Rebuilt as library files on top of the toolbox; today's results are the parity reference. |

Not reused: per-feature pipeline/segmenter/extractor classes, the Excel-based configs
(`kpi_as_long.xlsx`, `graphSpec`), and writing event chunks to disk.

## 10. Testing

- `core` unit tests with synthetic arrays: every moment rule, every KPI form, edge cases (event at
  file start/end, no occurrences, NaNs, missing moment).
- Toolbox contract tests over every registered tool, plus per-tool known-answer tests, including
  parity with the original functions they were moved from.
- Expression tests: valid and invalid syntax, rejected constructs (`__import__`, attribute access).
- Profile round-trip, import resolution and migration tests; example profiles and libraries
  validated in CI.
- End-to-end: small MF4 files generated with asammdf in the test fixtures, run through the CLI,
  Excel compared to golden values.
- Parity test: the AEB example profile against current pipeline results on the same recording.
- GUI smoke tests with `pytest-qt` in offscreen mode (load profile, preview, run).

## 11. Implementation plan

1. **Toolbox foundation**: registry, contract tests, and the seeded tools from section 4.4.
2. **Core and CLI**: schema with imports, expression engine, occurrences/moments/KPIs, MF4 reader,
   Excel writer, CLI. Usable headless with a hand-written profile.
3. **Libraries and parity**: `aeb_core`, `fcw_core`, `lsaeb_core` libraries matched against the
   current tool's results.
4. **Plots and dashboard**.
5. **GUI**: files, signals, KPI editor with tool palette and preview, run page.
6. **Packaging**: PyInstaller entry point.

## 12. Decisions

| Question | Decision (Jack, 2026-10-13) |
|---|---|
| One profile for all files in a run? | Yes. |
| Excel `Summary` with one row per occurrence? | Yes. |
| Speed-dependent calibration curves in v1? | No, constants only. |
| Shareable KPIs between profiles? | Yes, through library files (section 3.4). |
| Rich toolbox of reusable KPI functions | Yes, seeded from the current code (section 4). |
