# KPI Studio — quick start

KPI Studio is a profile-driven KPI extractor for MF4 recordings. A profile
(`*.kpi.yaml`) names the signals, the events that open a window, the moments
inside that window, and the KPIs read at those moments. The design is in
[kpi_studio_architecture.md](kpi_studio_architecture.md).

## GUI

```
python run_kpi_studio.py        # or: python -m src.kpi_studio.gui.app
```

1. **Files** – add two or more MF4 files (or a folder, or drag and drop).
   When files are not time-synced, double-click a file's *Offset [s]* cell and
   enter a shift (positive moves it later). The offset is used by the
   viewer, the event preview and the KPI run; it is not saved with the profile.
   On the command line: `--offset FILE=SECONDS` (repeatable).
2. **Signals** – import libraries (`vehicle`, `aeb_core`, …) or pick channels
   from the tree. A signal may list several channel names; the first one found
   in a file is used. *Add derived…* builds a derived signal step by step:
   pick the signal, pick the method (scale, offset, add/subtract a signal,
   filters, derivative, unit conversion, …) and fill in the arguments it asks
   for, plus an optional unit and description. The result is an expression
   such as `lowpass(longActAccel, 10)`, which can also be typed directly.
3. **Events & KPIs** – define an event (a start/end condition or a preset such
   as `target_decel_window`), its moments (e.g. `decel_onset`, `argmin`,
   `crossing`), then KPIs (value at a moment, duration, a reducer over a range (e.g. `distance`: speed
   integrated between two moments, in m, converted from the speed signal's own unit),
   a formula, or *which conditions are met*: a text result such as `PB + FB` naming the
   conditions met between two moments, e.g. which braking functions were active in the window; each condition is a signal, operator and
   value, with an enum signal's values to pick, or *expression* to type a longer one). Every window in a file is found, each one a separate occurrence. The rule
   before *Start when* / *End when* picks which time the condition is met: *first* (the default),
   or *last* (start at the last time it becomes true before the end; end at the last time it is met
   before the next start). *Edit condition* writes conditions for you, including value changes:
   *changes to 3* → `rising(sig == 3)`, *changes from 3* → `falling(sig == 3)`,
   *changes from → to 0, 3* → `(falling(sig == 0) and rising(sig == 3))`. Use them as
   an event's *Start when* or, with *Edit condition* under Moments, as a `first` moment.
   The ▾ list of every condition field (start, end, moments) starts with the same
   *Edit condition*, so one method serves all three. *Run preview* evaluates the profile on one file and draws the
   window and moments. *Save KPI to library* shares a KPI between profiles.
4. **Plots & Run** – choose the plots, *Check files*, then *RUN*. A scatter plot's *Colour / group by*
   can also be a KPI: a numeric KPI colours the points on a light-to-dark scale, a text KPI (e.g.
   `PB + FB`) gives one colour per text. In *Merge with row*, pick another scatter plot's row
   number to draw both in one chart (same X; Y values of different units get a row each; titled by
   the first title set). By default the
   result goes to `kpi_results/` next to the input MF4 files and replaces the previous
   result there (one result per data folder); *Browse* picks another folder (each run then gets its own timestamped folder in it), *Default* goes back.
   The output folder holds `kpi_results.xlsx`, `plots/`, `dashboard.html` and the resolved
   profile used for the run.

## Command line

```
python -m src.kpi_studio.cli validate  PROFILE
python -m src.kpi_studio.cli preflight PROFILE FILE...
python -m src.kpi_studio.cli run       PROFILE FILE... [-o OUT] [--workers N] [--force]
python -m src.kpi_studio.cli channels  FILE
python -m src.kpi_studio.cli tools     [--kind moment]
```

Example: `python -m src.kpi_studio.cli run src/kpi_studio/profiles/aeb_basic.kpi.yaml rec1.mf4 rec2.mf4`
writes `kpi_results/` next to the MF4 files (replaced each run); `-o OUT` writes a new
timestamped folder under OUT instead.

## Toolbox

`cli tools` lists every tool with its signature. Your own tools go in a folder
of `.py` files using the `@tool` decorator from
`src.kpi_studio.toolbox.registry`; load them with `--user-tools FOLDER` on the
command line, or the **User tools…** button in the GUI (remembered for next time).

## Tests

```
QT_QPA_PLATFORM=offscreen python -m pytest tests/kpi_studio
```

## Build the app

```
pyinstaller kpi_studio.spec
```

Creates `dist/KPI_Studio/` (and `KPI_Studio.app` on macOS).
