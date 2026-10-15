"""Signal viewer: plot any of the profile's signals (raw and derived), shared by tabs 2 and 3.

With an event chosen and "Run preview" pressed it also evaluates the whole profile on the first
ticked recording: the plot zooms to the chosen occurrence, shades the event, marks its moments
and lists the KPI values next to the plot.

Works without valid events or KPIs: only the signals and derived signals are compiled.
Stacked subplots share the time axis; the matplotlib toolbar gives zoom and pan.
Click (or drag) in a plot to place cursor A, Shift+click for cursor B; each subplot then
shows the signal values at the cursors and their difference. Right-click clears them.
"""

from __future__ import annotations

import traceback
from pathlib import Path

import numpy as np
from PySide6.QtCore import QEvent, QMimeData, QSize, Qt, QThread, QTimer, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QSizePolicy,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from src.kpi_studio.core.align import build_frame
from src.kpi_studio.core.engine import FileEvaluator
from src.kpi_studio.core.model import signal_units
from src.kpi_studio.core.results import FileOutcome, Status
from src.kpi_studio.gui.widgets.common import make_canvas
from src.kpi_studio.io import profile_store
from src.kpi_studio.io.mf4 import read_signals

NO_EVENT = "(signals only)"
SIGNAL_MIME = "application/x-kpi-studio-signals"
LINESTYLES = ["-", "--", ":", "-."]
MARGIN_PX = dict(left=64, right=250, top=10, bottom=40, gap=12)   # right: the signal / value list


class SignalList(QListWidget):
    """Signal list whose items can be dragged onto a subplot."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setDragEnabled(True)
        self.setDragDropMode(QAbstractItemView.DragOnly)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)

    def mimeTypes(self):
        return [SIGNAL_MIME]

    def mimeData(self, items):
        md = QMimeData()
        md.setData(SIGNAL_MIME, "\n".join(i.text() for i in items).encode())
        return md
COLORS = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"]
MAX_AXES = 8
AXIS_MIN_PX = 55     # each subplot gets at least this height (the panel grows instead of squashing)
PLOTS_MAX_MIN_PX = 190  # ...up to this total, so many subplots still fit on screen


class ElideLabel(QLabel):
    """One-line hint that shortens with "…" when there is no room (full text in the tooltip)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._full = ""
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.setMinimumWidth(40)

    def setText(self, text):
        self._full = text or ""
        self._elide()

    def text(self):
        return self._full

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._elide()

    def _elide(self):
        shown = self.fontMetrics().elidedText(self._full, Qt.ElideRight, max(self.width(), 40))
        super().setText(shown)
        if shown != self._full and not self.toolTip():
            super().setToolTip(self._full)


def _fmt(v, labels=None) -> str:
    if v is None:
        return "–"
    text = (labels or {}).get(float(v))
    return f"{v:.4g} {text}" if text else f"{v:.4g}"


class SignalLoadWorker(QThread):
    """Read every profile signal from one file and compute the derived signals."""

    done = Signal(object)    # dict(file, time, values, units, missing, issues)
    failed = Signal(str)

    def __init__(self, compiled, path: str, index=None, parent=None):
        super().__init__(parent)
        self.cp = compiled
        self.path = path
        self.index = index

    def run(self):
        try:
            defs = self.cp.profile.signals
            raw, issues = read_signals(self.path, defs, self.index)
            frame, align_issues, _ = build_frame(raw, defs, self.cp.profile.timebase.rate_hz,
                                                 profile_store.load_enum_tables())
            outcome = FileOutcome(file=getattr(self.path, "name", self.path))
            ev = FileEvaluator(self.cp, frame, outcome.file)
            ev.compute_derived(outcome)
            values = {k: v for k, v in ev.env.values.items() if k != "time"}
            units = signal_units(self.cp.profile)
            labels = {n: r.labels for n, r in raw.items() if r.labels}
            self.done.emit({"file": outcome.file, "time": ev.time, "values": values, "units": units,
                            "labels": labels, "offsets": dict(getattr(self.path, "offsets", {}) or {}),
                            "missing": dict(ev.env.missing),
                            "issues": [i.message for i in issues + align_issues + outcome.issues
                                       if i.severity != "info"]})
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}")


class SignalViewer(QWidget):
    """Recording picker (several at once) + checkable signal list + stacked time plots.

    With one recording ticked each signal gets its own colour; with several, each recording
    gets a colour and they are overlaid in the same subplot so runs can be compared."""

    def __init__(self, state, parent=None):
        super().__init__(parent)
        self.state = state
        self.data = None            # last loaded recording (kept for callers that show one)
        self.cache: dict = {}       # recording name -> loaded data
        self.queue: list = []
        self.worker = None
        self.spans: list[tuple[float, float]] = []
        self.cursors: list = [None, None]    # cursor A and B times [s]
        self._axes: list = []
        self._series: dict = {}              # axes -> [(label, time, values, enum labels)]
        self._artists: list = []
        self._dragging = None
        self.host: dict[str, str] = {}       # signal -> signal whose subplot it is drawn in (drag and drop)
        self._ax_groups: dict = {}           # axes -> signal names drawn there
        self._enum_axes: set = set()
        self._legend: dict = {}              # axes -> [(label, color, linestyle, unit)] for the side list
        self.preview: dict | None = None      # {"rec": name, "outcome": FileOutcome} from Run preview
        self.preview_worker = None

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        bar = QHBoxLayout()
        self.load_btn = QPushButton("Load signals")
        self.load_btn.setToolTip("Read the ticked signals from the recording")
        bar.addWidget(self.load_btn)
        # which recordings are plotted: the one selected on the Files tab (several can be overlaid
        # through select_recordings); not shown
        self.files = QListWidget(self)
        self.files.setVisible(False)
        self.separate_btn = QPushButton("One signal per subplot")
        self.separate_btn.setToolTip("Undo drag and drop: give every signal its own subplot again")
        self.separate_btn.setVisible(False)
        bar.addWidget(self.separate_btn)
        self.status = ElideLabel()
        self.status.setObjectName("hint")
        pbar = bar  # one row: load + status, event preview, cursor readout
        pbar.addSpacing(12)
        pbar.addWidget(QLabel("Event"))
        self.pv_event = QComboBox()
        self.pv_event.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.pv_event.setMinimumContentsLength(16)
        self.pv_event.setToolTip("Choose an event and press Run preview to see its occurrences, moments and KPIs")
        pbar.addWidget(self.pv_event)
        pbar.addWidget(QLabel("occurrence"))
        self.pv_occ = QSpinBox()
        self.pv_occ.setMinimum(1)
        self.pv_occ.setMinimumWidth(60)
        pbar.addWidget(self.pv_occ)
        self.pv_run = QPushButton("Run preview")
        self.pv_run.setObjectName("primary")
        self.pv_run.setToolTip("Evaluate the profile on the first ticked recording")
        pbar.addWidget(self.pv_run)
        self.pv_status = ElideLabel()  # long texts shorten with … instead of widening the window
        self.pv_status.setObjectName("hint")
        pbar.addWidget(self.pv_status, 2)
        pbar.addWidget(self.status, 1)
        self.cursor_info = QLabel()
        self.cursor_info.setObjectName("hint")
        pbar.addWidget(self.cursor_info)
        lay.addLayout(bar)

        split = QSplitter(Qt.Horizontal)
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        self.list = SignalList()
        self.list.setToolTip("Tick signals to plot them. Drag a signal onto a plot to draw it in that subplot.")
        ll.addWidget(self.list, 1)
        check_row = QHBoxLayout()
        check_all = QPushButton("Check all")
        clear = QPushButton("Uncheck all")
        check_row.addWidget(check_all)
        check_row.addWidget(clear)
        ll.addLayout(check_row)
        split.addWidget(left)

        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        self.fig, self.canvas, Toolbar = make_canvas()
        self.fig.set_layout_engine("none")  # margins are set in pixels (_apply_layout)
        self.toolbar = Toolbar(self.canvas, right)
        self.toolbar.setIconSize(QSize(18, 18))  # compact: leave the height to the plots
        rl.addWidget(self.toolbar)
        rl.addWidget(self.canvas, 1)  # subplots share the panel height (no scrolling)
        self.canvas.setAcceptDrops(True)
        self.canvas.installEventFilter(self)
        split.addWidget(right)
        self.kpi_table = QTableWidget(0, 5)
        self.kpi_table.setHorizontalHeaderLabels(["KPI", "Value", "Unit", "Status", "Message"])
        self.kpi_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.Stretch)
        self.kpi_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.kpi_table.verticalHeader().setDefaultSectionSize(22)
        self.kpi_table.setVisible(False)
        split.addWidget(self.kpi_table)
        split.setStretchFactor(1, 1)
        split.setSizes([230, 800, 360])
        lay.addWidget(split, 1)

        self.load_btn.clicked.connect(self.load)
        self.files.itemChanged.connect(lambda *_: self._files_ticked())
        state.offsets_changed.connect(self._offsets_changed)
        self._reload_timer = QTimer(self)
        self._reload_timer.setSingleShot(True)
        self._reload_timer.setInterval(400)
        self._reload_timer.timeout.connect(self._reload_shifted)
        self.list.itemChanged.connect(lambda *_: self._files_ticked())  # ticking a signal loads and plots it
        check_all.clicked.connect(self.check_all)
        clear.clicked.connect(self.uncheck_all)
        self.canvas.mpl_connect("button_press_event", self._on_press)
        self.canvas.mpl_connect("motion_notify_event", self._on_motion)
        self.canvas.mpl_connect("button_release_event", self._on_release)
        self.canvas.mpl_connect("resize_event", lambda *_: self._apply_layout())
        self.separate_btn.clicked.connect(self.separate_all)
        state.files_changed.connect(self.refresh_files)
        state.validated.connect(self.refresh_names)
        state.validated.connect(self._drop_stale_cache)
        state.validated.connect(self.refresh_events)
        state.profile_changed.connect(self._clear_preview)
        self.pv_run.clicked.connect(self.run_preview)
        self.pv_event.currentTextChanged.connect(lambda *_: self._event_changed())
        self.pv_occ.valueChanged.connect(lambda *_: self.draw())
        self._cache_key = None
        self.refresh_files()
        self.refresh_names()
        self.refresh_events()
        self.draw()

    # ------------------------------------------------------------------ lists
    def refresh_files(self):
        recs = self.state.recordings()
        ticked = set(self.selected_names())
        self.files.blockSignals(True)
        self.files.clear()
        for i, r in enumerate(recs):
            it = QListWidgetItem(self._file_label(r))
            it.setData(Qt.UserRole, r.name)
            it.setToolTip("\n".join(r.files))
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            on = r.name in ticked or (not ticked and i == 0)
            it.setCheckState(Qt.Checked if on else Qt.Unchecked)
            self.files.addItem(it)
        self.files.blockSignals(False)
        self.cache = {k: v for k, v in self.cache.items() if k in {r.name for r in recs}}

    # ------------------------------------------------------------------ time offsets
    def _file_label(self, r) -> str:
        offs = {self.state.offsets.get(f, 0.0) for f in r.files}
        if offs == {0.0}:
            return r.label
        if len(offs) == 1:
            return r.label + f"   ({offs.pop():+.3f} s)"
        return r.label + "   (shifted)"

    def _offsets_changed(self, files):
        """Shifted files are read again (the shift is applied when reading, as in the KPI run)."""
        recs = self.state.recordings()
        hit = {r.name for r in recs if set(r.files) & set(files)}
        for name in hit:
            self.cache.pop(name, None)
        if self.preview and self.preview["rec"] in hit:
            self.preview = None
            self.pv_status.setText("Time offset changed: press Run preview again")
        if hit & set(self.selected_names()):
            self._reload_timer.start()

    def _reload_shifted(self):
        if self.worker is not None:  # still reading: try again shortly
            self._reload_timer.start()
            return
        self.load()

    def show_recording_of(self, path: str):
        """Plot the recording a file belongs to (the row selected on the Files tab)."""
        rec = next((r for r in self.state.recordings() if path in r.files), None)
        if rec is not None and self.selected_names() != [rec.name]:
            self.select_recordings([rec.name])
            self._files_ticked()

    def selected_names(self) -> list[str]:
        return [self.files.item(i).data(Qt.UserRole) for i in range(self.files.count())
                if self.files.item(i).checkState() == Qt.Checked]

    def selected(self) -> list:
        names = set(self.selected_names())
        return [r for r in self.state.recordings() if r.name in names]

    def select_recordings(self, names, only=True):
        self.files.blockSignals(True)
        for i in range(self.files.count()):
            it = self.files.item(i)
            if it.data(Qt.UserRole) in names:
                it.setCheckState(Qt.Checked)
            elif only:
                it.setCheckState(Qt.Unchecked)
        self.files.blockSignals(False)

    def refresh_names(self):
        names = self.state.signal_names()
        existing = [self.list.item(i).text() for i in range(self.list.count())]
        if existing == names:
            self._set_name_tips()
            return
        checked = set(self.checked())
        self.list.blockSignals(True)
        self.list.clear()
        for n in names:
            it = QListWidgetItem(n)
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(Qt.Checked if n in checked else Qt.Unchecked)
            self.list.addItem(it)
        self.list.blockSignals(False)
        self._set_name_tips()

    def _set_name_tips(self):
        """Tooltip per signal: its unit and description."""
        r = self.state.resolved
        defs = {**dict(r.signals), **dict(r.derived)} if r is not None else {}
        for i in range(self.list.count()):
            it = self.list.item(i)
            d = defs.get(it.text())
            tip = ""
            if d is not None:
                tip = "\n".join(x for x in (f"[{d.unit}]" if d.unit else "", d.description) if x)
            it.setToolTip(tip)

    def _drop_stale_cache(self):
        # signal definitions changed (channels, units, derived expressions): re-read next time
        key = repr(self.state.clean_data().get("signals")) + repr(self.state.clean_data().get("derived")) \
            + repr(self.state.data.get("imports")) + repr((self.state.data.get("timebase") or {}))
        if key != self._cache_key:
            self._cache_key = key
            self.cache.clear()

    def checked(self) -> list[str]:
        return [self.list.item(i).text() for i in range(self.list.count())
                if self.list.item(i).checkState() == Qt.Checked]

    def set_checked(self, names, only=False):
        self.list.blockSignals(True)
        for i in range(self.list.count()):
            it = self.list.item(i)
            if it.text() in names:
                it.setCheckState(Qt.Checked)
            elif only:
                it.setCheckState(Qt.Unchecked)
        self.list.blockSignals(False)

    def check_all(self):
        self.set_checked([self.list.item(i).text() for i in range(self.list.count())
                          if not self.list.item(i).isHidden()])
        self._files_ticked()

    def uncheck_all(self):
        self.set_checked([], only=True)
        self.draw()

    def _files_ticked(self):
        if self.state.files and self.checked() and (not self.selected()
                                                   or any(r.name not in self.cache for r in self.selected())):
            self.load()
        else:
            self.draw()

    def current_file(self):
        """First ticked recording (one file, or several files read together)."""
        sel = self.selected()
        return sel[0] if sel else None

    # ------------------------------------------------------------------ load + draw
    def plot_signals(self, names, spans=()):
        """Check `names` (only those) and plot them, loading recordings first if needed."""
        self.set_checked(names, only=True)
        self.spans = list(spans)
        if any(r.name not in self.cache for r in self.selected()) or not self.selected():
            self.load()
        else:
            self.draw()

    def load(self):
        if not self.state.files:
            self.status.setText("Add MF4 files on the Files tab first.")
            return
        if not self.selected() and self.files.count():
            self.select_recordings([self.files.item(0).data(Qt.UserRole)])
        if self.worker is not None:
            return
        cp = self.state.signals_only_compiled()
        if cp is None:
            self.status.setText("The signal definitions have errors; see Profile check below.")
            return
        if not self.checked():
            self.set_checked(list(cp.profile.signals)[:3])
        self._cp = cp
        self.queue = [r for r in self.selected() if r.name not in self.cache]
        if not self.queue:
            self.draw()
            return
        self.load_btn.setEnabled(False)
        self._next()

    def _next(self):
        if not self.queue:
            self.load_btn.setEnabled(True)
            self._show_status()
            self.draw()
            return
        r = self.queue.pop(0)
        self.status.setText(f"Reading {r.label}…")
        self.worker = SignalLoadWorker(self._cp, r, dict(self.state.indexes), self)
        self.worker.done.connect(self._loaded)
        self.worker.failed.connect(lambda m, r=r: self.status.setText(f"Could not read {r.label}: "
                                                                       + m.splitlines()[0]))
        self.worker.finished.connect(self._worker_finished)
        self.worker.start()

    def _worker_finished(self):
        self.worker = None
        self._next()

    def _current_offsets(self, name) -> dict:
        r = next((r for r in self.state.recordings() if r.name == name), None)
        return dict(r.offsets) if r is not None else {}

    def _loaded(self, data):
        if data.get("offsets", {}) != self._current_offsets(data["file"]):
            return  # read before the time offset changed; it is read again
        self.data = data
        self.cache[data["file"]] = data

    def _show_status(self):
        parts, tips = [], []
        for r in self.selected():
            d = self.cache.get(r.name)
            if d is None:
                continue
            t = d["time"]
            span = f"{t[0]:.1f}–{t[-1]:.1f} s" if len(t) else "no samples"
            miss = f", missing {len(d['missing'])}" if d["missing"] else ""
            parts.append(f"{Path(r.name).name}: {span}{miss}")
            tips += [f"{Path(r.name).name}: {k}: {v}" for k, v in d["missing"].items()] + d["issues"]
        self.status.setText("  ·  ".join(parts))
        self.status.setToolTip("\n".join(tips))

    # ------------------------------------------------------------------ drag and drop
    def eventFilter(self, obj, event):
        if obj is self.canvas and event.type() in (QEvent.DragEnter, QEvent.DragMove, QEvent.Drop):
            md = event.mimeData()
            if not md.hasFormat(SIGNAL_MIME):
                return False
            if event.type() == QEvent.Drop:
                names = [n for n in bytes(md.data(SIGNAL_MIME)).decode().split("\n") if n]
                self.drop_signals(names, self.axes_at(event.position()))
            event.acceptProposedAction()
            return True
        return super().eventFilter(obj, event)

    def axes_at(self, pos):
        """The subplot under a widget position (QPointF), or None."""
        ratio = self.canvas.devicePixelRatioF()
        x, y = pos.x() * ratio, (self.canvas.height() - pos.y()) * ratio
        return next((ax for ax in self._axes if ax.bbox.contains(x, y)), None)

    def drop_signals(self, names, ax=None):
        """Draw `names` in the subplot `ax` (its first signal hosts them); outside any subplot: own subplots."""
        group = self._ax_groups.get(ax) or []
        host = group[0] if group else None
        for n in names:
            if host is None or n == host:
                self.host.pop(n, None)
                continue
            for other, h in list(self.host.items()):  # whatever n hosted moves along
                if h == n:
                    self.host[other] = host
            self.host[n] = host
        self.set_checked(names)
        self.draw()

    def separate_all(self):
        self.host.clear()
        self.draw()

    def _groups(self, names: list[str]) -> list[list[str]]:
        """Signals grouped by subplot, in list order; the host of a group comes first."""
        avail = set(names)

        def root(n):
            seen = set()
            while n in self.host and self.host[n] in avail and n not in seen:
                seen.add(n)
                n = self.host[n]
            return n

        groups: dict[str, list[str]] = {}
        for n in names:
            groups.setdefault(root(n), []).append(n)
        return [[r] + [n for n in g if n != r] for r, g in groups.items()]

    def _apply_layout(self):
        """Fixed pixel margins, so labels and moment names never squeeze the subplots."""
        n = len(self._axes)
        if not n:
            return
        w, h = max(self.canvas.width(), 1), max(self.canvas.height(), 1)
        m = dict(MARGIN_PX)
        if any(ax.get_title() for ax in self._axes):
            m["top"] += 16
        if self._enum_axes:
            m["left"] += 70
        axis_h = max((h - m["top"] - m["bottom"] - m["gap"] * (n - 1)) / n, 1)
        left, right = min(m["left"] / w, 0.4), 1 - m["right"] / w
        top, bottom = 1 - m["top"] / h, m["bottom"] / h
        if right - left < 0.1:  # panel too small for the pixel margins (e.g. while it is being resized)
            left, right = 0.1, 0.9
        if top - bottom < 0.1:
            bottom, top = 0.1, 0.9
        self.fig.subplots_adjust(left=left, right=right, top=top, bottom=bottom, hspace=m["gap"] / axis_h)

    def draw(self):
        from matplotlib.ticker import MaxNLocator

        self.fig.clear()
        self._axes, self._series, self._artists, self._ax_groups, self._enum_axes = [], {}, [], {}, set()
        self._legend = {}
        names = self.checked()
        loaded = [(r, self.cache[r.name]) for r in self.selected() if r.name in self.cache]
        self.separate_btn.setVisible(bool(self.host))
        if not loaded:
            self.kpi_table.setVisible(False)
            self._message("Tick signals on the left, then press Load signals")
            return
        available = [n for n in names if any(n in d["values"] for _, d in loaded)]
        groups = self._groups(available)
        shown = groups[:MAX_AXES]
        if not shown:
            self._message("Not available in the ticked recordings: " + ", ".join(names) if names
                          else "Tick one or more signals on the left")
            return
        overlay = len(loaded) > 1
        axes = self.fig.subplots(len(shown), 1, sharex=True, squeeze=False)[:, 0]
        m = MARGIN_PX
        self.canvas.setMinimumHeight(m["top"] + m["bottom"] + 16
                                     + min(len(shown) * (AXIS_MIN_PX + m["gap"]), PLOTS_MAX_MIN_PX))
        for i, (ax, group) in enumerate(zip(axes, shown)):
            multi = len(group) > 1
            for k, n in enumerate(group):
                for j, (r, d) in enumerate(loaded):
                    if n not in d["values"]:
                        continue
                    y = np.asarray(d["values"][n], dtype=float)
                    t = np.asarray(d["time"], dtype=float)
                    c = COLORS[k % len(COLORS)] if multi else COLORS[(j if overlay else i) % len(COLORS)]
                    ls = LINESTYLES[j % len(LINESTYLES)] if (multi and overlay) else "-"
                    ax.plot(t, y, color=c, lw=1.2, ls=ls)
                    label = f"{n} · {Path(r.name).name}" if overlay else n
                    enum = d.get("labels", {}).get(n)
                    self._series.setdefault(ax, []).append((label, t, y, enum))
                    self._legend.setdefault(ax, []).append((label, c, ls, d["units"].get(n, "")))
                    if enum and not multi:
                        self._enum_ticks(ax, enum, y)
            if ax not in self._enum_axes:
                ax.yaxis.set_major_locator(MaxNLocator(nbins=4, min_n_ticks=2))
            for lo, hi in self.spans:
                ax.axvspan(lo, hi, color="#2a78d6", alpha=0.08, lw=0)
            ax.grid(True, color="#e6e5e0", lw=0.6)
            ax.tick_params(labelsize=7.5)
            ax.margins(y=0.08)
            self._ax_groups[ax] = group
        axes[-1].set_xlabel("time [s]", fontsize=8)
        if len(groups) > MAX_AXES:
            axes[0].set_title(f"Showing the first {MAX_AXES} subplots", fontsize=8, color="#6b6b66")
        self._decorate_preview(list(axes), loaded)
        self._axes = list(axes)
        self._apply_layout()
        self._update_cursors()

    def _message(self, text):
        ax = self.fig.add_subplot(111)
        ax.text(0.5, 0.5, text, ha="center", va="center", transform=ax.transAxes, color="#6b6b66", wrap=True)
        ax.set_axis_off()
        self.fig.subplots_adjust(left=0.02, right=0.98, top=0.98, bottom=0.02)
        self.canvas.setMinimumHeight(120)
        self.canvas.draw_idle()

    def _enum_ticks(self, ax, enum: dict, y):
        """Y ticks named by the file's texts, only for the values that occur (long tables stay legible)."""
        present = set(np.unique(y[np.isfinite(y)]).tolist())
        ticks = [k for k in sorted(enum) if k in present] or sorted(enum)
        if len(ticks) > 10:
            return
        ax.set_yticks(ticks, [(enum[k][:20] + "…") if len(enum[k]) > 21 else enum[k] for k in ticks])
        self._enum_axes.add(ax)

    # ------------------------------------------------------------------ event preview
    def refresh_events(self):
        cur = self.pv_event.currentText()
        names = [NO_EVENT] + self.state.event_names()
        if [self.pv_event.itemText(i) for i in range(self.pv_event.count())] == names:
            return
        self.pv_event.blockSignals(True)
        self.pv_event.clear()
        self.pv_event.addItems(names)
        self.pv_event.setCurrentText(cur if cur in names else NO_EVENT)
        self.pv_event.blockSignals(False)

    def current_event(self):
        ev = self.pv_event.currentText()
        return None if ev in ("", NO_EVENT) else ev

    def set_event(self, name):
        """Show `name` (tab 3 calls this when an event or KPI is selected)."""
        self.refresh_events()
        if name and self.pv_event.findText(name) >= 0 and self.pv_event.currentText() != name:
            self.pv_event.setCurrentText(name)

    def _event_changed(self):
        self._update_occ_range()
        self.draw()

    def _clear_preview(self):
        self.preview = None
        self.pv_status.setText("")
        self.draw()

    def _occurrences(self):
        ev = self.current_event()
        if not self.preview or ev is None:
            return []
        return [o for o in self.preview["outcome"].occurrences if o.event == ev]

    def _update_occ_range(self):
        self.pv_occ.blockSignals(True)
        self.pv_occ.setMaximum(max(len(self._occurrences()), 1))
        self.pv_occ.blockSignals(False)

    def default_signals(self, event: str) -> list[str]:
        """Signals the event's trigger and moments use (first four)."""
        cp = self.state.compiled
        if cp is None or event not in cp.events:
            return []
        ev = cp.events[event]
        out: list[str] = []
        for c in [ev.start, *ev.preset_inputs] + [m.expr for m in ev.moments.values()]:
            if c is not None:
                out += [d for d in sorted(c.deps) if d != "time" and d not in out]
        return out[:4]

    def run_preview(self):
        if not self.state.files:
            self.pv_status.setText("Add files on the Files tab first.")
            return
        if self.state.compiled is None:
            self.pv_status.setText("Fix the errors under Profile check first; signals can still be plotted.")
            return
        if self.preview_worker is not None:
            return
        if not self.selected() and self.files.count():
            self.select_recordings([self.files.item(0).data(Qt.UserRole)])
        rec = self.current_file()
        if rec is None:
            return
        from src.kpi_studio.gui.workers import PreviewWorker

        self.pv_status.setText(f"Running the profile on {rec.label}…")
        self.pv_run.setEnabled(False)
        self.preview_worker = PreviewWorker(self.state.compiled, rec, dict(self.state.indexes), self)
        self.preview_worker.done.connect(lambda data, r=rec: self._preview_done(r, data))
        self.preview_worker.failed.connect(lambda m: self.pv_status.setText("Preview failed: " + m.splitlines()[0]))
        self.preview_worker.finished.connect(self._preview_finished)
        self.preview_worker.start()

    def _preview_finished(self):
        self.preview_worker = None
        self.pv_run.setEnabled(True)

    def _preview_done(self, rec, data):
        if dict(rec.offsets) != self._current_offsets(rec.name):
            self.pv_status.setText("Time offset changed while running: press Run preview again")
            return
        out = data["outcome"]
        old = self.cache.get(rec.name, {})
        self.cache[rec.name] = {
            "file": rec.name, "time": data["time"],
            "values": {k: v for k, v in data["values"].items() if k != "time"},
            "units": signal_units(self.state.compiled.profile), "labels": old.get("labels", {}),
            "missing": old.get("missing", {}),
            "issues": [i.message for i in out.issues if i.severity != "info"]}
        self.data = self.cache[rec.name]
        self.preview = {"rec": rec.name, "outcome": out}
        ev = self.current_event()
        if ev is None and out.occurrences:
            self.set_event(out.occurrences[0].event)
            ev = self.current_event()
        if not self.checked() and ev:
            self.set_checked(self.default_signals(ev) or self.state.signal_names()[:3])
        self._update_occ_range()
        n = len(self._occurrences())
        issues = [i.message for i in out.issues if i.severity != "info"]
        self.pv_status.setText(f"{rec.label}: {len(out.occurrences)} occurrence(s)"
                               + (f", {n} of '{ev}'" if ev else "")
                               + (f"  ·  {len(issues)} warning(s)" if issues else ""))
        self.pv_status.setToolTip("\n".join(issues))
        self.draw()

    def _decorate_preview(self, axes, loaded):
        """Zoom to the chosen occurrence, shade it, mark its moments and list its KPIs."""
        self.kpi_table.setRowCount(0)
        ev = self.current_event()
        active = bool(self.preview and ev and any(r.name == self.preview["rec"] for r, _ in loaded))
        self.kpi_table.setVisible(active)
        if not active:
            return
        out = self.preview["outcome"]
        occs = self._occurrences()
        idx = self.pv_occ.value()
        occ = next((o for o in occs if o.index == idx), None)
        if occ is None:
            for ax in axes:
                for o in occs:
                    ax.axvspan(o.t_start, o.t_end, color="#2a78d6", alpha=0.10, lw=0)
            axes[0].set_title(f"Whole file: no occurrence of '{ev}' found" if not occs
                              else f"Whole file: occurrence #{idx} of '{ev}' not found", fontsize=8, color="#6b6b66")
            return
        moments = [m for m in out.moments if m.event == ev and m.occurrence == idx]
        for i, ax in enumerate(axes):
            ax.axvspan(occ.t_start, occ.t_end, color="#2a78d6", alpha=0.10, lw=0)
            for m in moments:
                if m.time is not None:
                    ax.axvline(m.time, color="#5f5e5a", ls=":", lw=1)
                    if i == 0:
                        ax.annotate(f" {m.moment}", (m.time, 0.02), xycoords=("data", "axes fraction"),
                                    fontsize=7, rotation=90, va="bottom", ha="right", color="#5f5e5a",
                                    annotation_clip=False)
        lo, hi = occ.t_window_start, occ.t_window_end
        axes[0].set_xlim(lo, hi)
        for ax in axes:  # fit y to the window, not the whole file
            parts = [y[(t >= lo) & (t <= hi)] for _, t, y, _ in self._series.get(ax, [])]
            vals = np.concatenate(parts) if parts else np.array([])
            vals = vals[np.isfinite(vals)]
            if vals.size:
                y0, y1 = float(vals.min()), float(vals.max())
                pad = 0.08 * (y1 - y0) or max(abs(y0) * 0.1, 0.5)
                ax.set_ylim(y0 - pad, y1 + pad)
        res = [r for r in out.results if r.event == ev and r.occurrence == idx]
        self.kpi_table.setRowCount(len(res))
        for r, k in enumerate(res):
            vals = [k.kpi, "" if k.value is None else f"{k.value:.3f}", k.unit, k.status, k.message]
            for c, v in enumerate(vals):
                item = QTableWidgetItem(v)
                if c == 3 and k.status != Status.OK:
                    item.setForeground(Qt.red)
                self.kpi_table.setItem(r, c, item)

    # ------------------------------------------------------------------ cursors
    def _on_press(self, event):
        if self.toolbar.mode or event.inaxes not in self._axes or event.xdata is None:
            return
        if event.button == 3:
            self.clear_cursors()
            return
        if event.button != 1:
            return
        self._dragging = 1 if event.key == "shift" else 0
        self.set_cursor(self._dragging, event.xdata)

    def _on_motion(self, event):
        if self._dragging is not None and event.xdata is not None and event.inaxes in self._axes:
            self.set_cursor(self._dragging, event.xdata)

    def _on_release(self, _event):
        self._dragging = None

    def set_cursor(self, which: int, t):
        self.cursors[which] = None if t is None else float(t)
        self._update_cursors()

    def clear_cursors(self):
        self.cursors = [None, None]
        self._update_cursors()

    @staticmethod
    def value_at(time, values, t):
        """Value of the sample nearest to `t`, or None outside the recorded time span."""
        if t is None or not len(time) or t < time[0] or t > time[-1]:
            return None
        i = int(np.clip(np.searchsorted(time, t), 1, len(time) - 1))
        if abs(time[i - 1] - t) <= abs(time[i] - t):
            i -= 1
        v = float(values[i])
        return None if np.isnan(v) else v

    def readouts(self) -> dict:
        """Signal name -> lines of text shown in that subplot for the current cursors."""
        out = {}
        a, b = self.cursors
        for ax in self._axes:
            lines = []
            for label, t, y, enum in self._series.get(ax, []):
                va, vb = self.value_at(t, y, a), self.value_at(t, y, b)
                parts = []
                if a is not None:
                    parts.append(f"A {_fmt(va, enum)}")
                if b is not None:
                    parts.append(f"B {_fmt(vb, enum)}")
                if va is not None and vb is not None and not enum:
                    parts.append(f"Δ {_fmt(vb - va)}")
                if parts:
                    prefix = f"{label}: " if len(self._series[ax]) > 1 else ""
                    lines.append(prefix + "  ".join(parts))
            out[ax] = lines
        return out

    def _update_cursors(self):
        for art in self._artists:
            try:
                art.remove()
            except (ValueError, NotImplementedError):
                pass
        self._artists = []
        a, b = self.cursors
        if self._axes:
            for ax, lines in self.readouts().items():
                for t, style in ((a, "-"), (b, "--")):
                    if t is not None:
                        self._artists.append(ax.axvline(t, color="#1f1f1e", lw=0.9, ls=style, zorder=5))
            self._draw_side_lists()
        info = []
        if a is not None:
            info.append(f"A = {a:.3f} s")
        if b is not None:
            info.append(f"B = {b:.3f} s")
        if a is not None and b is not None:
            info.append(f"Δt = {b - a:.3f} s")
        self.cursor_info.setText("   ".join(info) if info else "Click a plot to place a cursor (Shift+click: second)")
        self.canvas.draw_idle()

    def _draw_side_lists(self):
        """MDA-style list right of each subplot: a colour mark, the signal name and its value at the cursors."""
        from matplotlib.transforms import ScaledTranslation, blended_transform_factory

        a, b = self.cursors
        line_h = 11.5 / 72  # inches per text line (8 pt + leading)
        for ax in self._axes:
            base = blended_transform_factory(ax.transAxes, ax.transAxes)
            row = 0
            for (label, t, y, enum), (_, color, ls, unit) in zip(self._series.get(ax, []), self._legend.get(ax, [])):
                def at(dx_in, rows):
                    return base + ScaledTranslation(dx_in, -rows * line_h - 2 / 72, self.fig.dpi_scale_trans)

                self._artists.append(ax.text(1, 1, "━━" if ls == "-" else "╍╍", transform=at(0.06, row),
                                             fontsize=8, va="top", ha="left", color=color, clip_on=False))
                self._artists.append(ax.text(1, 1, label + (f" [{unit}]" if unit else ""), transform=at(0.28, row),
                                             fontsize=8, va="top", ha="left", color="#1f1f1e", clip_on=False,
                                             fontweight="semibold"))
                row += 1
                values = []
                if a is not None:
                    values.append(f"A {_fmt(self.value_at(t, y, a), enum)}")
                if b is not None:
                    vb = self.value_at(t, y, b)
                    values.append(f"B {_fmt(vb, enum)}")
                    va = self.value_at(t, y, a) if a is not None else None
                    if va is not None and vb is not None and not enum:
                        values.append(f"Δ {_fmt(vb - va)}")
                if values:
                    self._artists.append(ax.text(1, 1, "   ".join(values), transform=at(0.28, row), fontsize=8,
                                                 va="top", ha="left", family="monospace", color=color,
                                                 clip_on=False))
                    row += 1
                row += 0.35

    def shutdown(self):
        self.queue = []
        for w in (self.worker, self.preview_worker):
            if w is not None:
                w.wait(5000)
