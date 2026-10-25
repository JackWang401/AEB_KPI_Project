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
   In the signal viewer, select 2 or more signals, right-click and choose *Common axis*: they are
   drawn in one subplot on one value axis (*axis 1*, *axis 2*, …). Once an axis exists, *Common axis*
   offers *Join axis N* or *New common axis*. *Range of common axis…* fixes its minimum and maximum
   (or back to automatic), *Rename common axis…* names it, *Remove from common axis* undoes it.
   The list shows each common axis as a name row with its signals indented under it; right-click the
   name row for *Range…*, *Rename…*, *Select its signals* and *Remove common axis* (double-click renames).
   On macOS, Ctrl+click (as well as ⌘+click) adds a signal to the selection.
   Common axes are saved in the profile (`common_axes:` with each axis' `name`, `signals` and optional
   `range: [min, max]`), so they come back when the profile is opened again. So are the ticked
   signals, measured and derived (`shown_signals:`): opening the profile ticks and plots them again.
   Each event keeps its own ticked signals (`shown_signals:` under the event): picking another event in
   the viewer ticks its set; an event without one starts with none ticked.
2. **Signals** – import libraries (`vehicle`, `aeb_core`, …) or pick channels
   from the tree. A signal may list several channel names; the first one found
   in a file is used. *Add derived…* builds a derived signal step by step:
   pick the signal, pick the method (scale, offset, add/subtract a signal,
   filters, derivative, unit conversion, …) and fill in the arguments it asks
   for, plus an optional unit and description. The result is an expression
   such as `lowpass(longActAccel, 10)`, which can also be typed directly.
   The *Units* methods (`to_mps`, `to_kph`, `to_mph`, `to_deg_s`, `to_deg`, `to_rad`, `to_rad_s`,
   `to_mps2`, `to_mps3`, `to_m`, `to_cm`, `to_km`, `to_s`, `to_ms`, `to_g`) convert from the unit in
   the signal's Unit column; when that is empty the builder asks for it and writes it into the call,
   e.g. `to_kph(raw, "mph")`. `convert(x, "bar", "kpa")` covers any other pair.
3. **Events & KPIs** – define an event (a start/end condition; an older profile's
   event preset, and its domain-specific tools such as `vehicle_stop` or `ttc`, still run), its moments (e.g. `first`, `argmin`,
   `crossing`), its *Ranges* (named spans from one moment to another, e.g. `braking`: `aeb_braking` to
   `veh_stopped`; `event` = start to end and `window` = window_start to window_end are built in), then
   KPIs (the KPI list shows the selected event's KPIs; a new KPI belongs to that event; an event no KPI or plot
   uses may stay unfinished: its problems show as warnings and it is left out of the run). A KPI is one *Expression*, signal first: `<signal>.<reducer>(<range>)` or `<signal> @ <moment>`,
   e.g. `gap_dist.change(braking).abs()`, `change(gap_dist, braking).abs()` (the same),
   `gap_dist @ veh_stopped` (its value at that moment; `(gap_dist @ veh_stopped).to_cm()`), `ego_speed_kph.distance(braking)` (in m, converted
   from the speed's own unit), `(fb_state == 1).any(event)`, `duration(braking)`. A reducer takes the signal
   and one range (options as keywords); `change` = the value at the range's end minus the value at its
   start. `.abs()`, `.sign()`, `.sqrt()` and `.to_<unit>()` after it act on the result, while
   `abs(x).change(braking)` reduces abs(x). `which(braking, PB=pb_state == 1, FB=fb_state == 1)` gives a
   text result naming the conditions met, such as `PB + FB`; an expression of KPIs listed earlier for
   the event (`gap_too_small or speed_at_release > 0`) is a formula of them. Dragging `abs` or `to_kph`
   from the toolbox onto the field appends it; dragging a reducer swaps the reducer. The yaml keeps the
   parts: `reduce: change, of: gap_dist, over: braking, then: [abs]`, `value: egoSpeed, at: veh_stopped`,
   `which: {PB: ...}, over: braking` or `formula: ...`. In the yaml a range KPI reads `over: braking` and the event lists
   `ranges: {braking: [aeb_braking, veh_stopped]}`; an older profile's `between: [a, b]` and
   `duration: [a, b]` KPIs are moved onto named ranges when it is opened (save to keep it). Every window in a file is found, each one a separate occurrence. The rule
   before *Start when* / *End when* picks which time the condition is met: *first* (the default),
   or *last* (start at the last time it becomes true before the end; end at the last time it is met
   before the next start). *Edit condition* writes conditions for you, including value changes:
   *changes to 3* → `rising(sig == 3)`, *changes from 3* → `falling(sig == 3)`,
   *changes from → to 0, 3* → `(falling(sig == 0) and rising(sig == 3))`. Use them as
   an event's *Start when* or, with *Edit condition* under Moments, as a `first` moment.
   The ▾ list of every condition field (start, end, moments) starts with the same
   *Edit condition*, so one method serves all three. *Run preview* evaluates the profile on every checked file and draws the
   window and moments; *occurrence* (n / total) steps through the event's occurrences in all files. *Save KPI to library* shares a KPI between profiles.
4. **Plots & Run** – choose the plots, *Check files*, then *RUN*. A scatter plot's *Colour by 1*
   can also be a KPI: a numeric KPI colours the points on a light-to-dark scale, a text KPI (e.g.
   `PB + FB`) gives one colour per text. *Colour-by columns* (1 to 5, default 1) adds more
   colour columns: the points then get one colour per combination, e.g. `FB · aeb_fail: true`
   (saved as `also_color_by`). In *Merge with row*, pick another scatter plot's row
   number to draw both in one chart (same X and Y axes, told apart by marker shape; titled by
   the first title set). By default the
   result goes to `kpi_results/` next to the input MF4 files and replaces the previous
   result there (one result per data folder); *Browse* picks another folder (results then go to its `kpi_results/`, replaced by each run, so
   it is always the same Excel file), *Default* goes back.
   The output folder holds `kpi_results.xlsx`, `plots/`, `dashboard.html` and the resolved
   profile used for the run.
   `kpi_results.xlsx` collects every run into that folder: rows of files not in this run are kept,
   rows of a file with exactly the same path are replaced. Type a file's software version in the
   *SW version* column of the Files panel (double-click); it is written next to its results and kept
   for later runs.

Speed: each file's header (channel list, time range) is read once and kept in
`~/.kpi_studio/index_cache/` (set `KPI_STUDIO_CACHE_DIR` to move it), so adding a file again is instant;
a changed file is read again. A run opens only the channel groups that hold mapped signals and reads
each group once. *Parallel files* on the Run page processes that many recordings at the same time.

## Command line

```
python -m src.kpi_studio.cli validate  PROFILE
python -m src.kpi_studio.cli preflight PROFILE FILE...
python -m src.kpi_studio.cli run       PROFILE FILE... [-o OUT] [--workers N] [--force]
python -m src.kpi_studio.cli channels  FILE
python -m src.kpi_studio.cli tools     [--kind moment]
```

Example: `python -m src.kpi_studio.cli run src/kpi_studio/profiles/aeb_basic.kpi.yaml rec1.mf4 rec2.mf4`
writes `kpi_results/` next to the MF4 files (replaced each run); `-o OUT` writes
`OUT/kpi_results/` instead (also replaced each run).

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
