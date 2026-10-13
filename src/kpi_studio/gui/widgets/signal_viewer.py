"""Signal viewer: plot any of the profile's signals (raw and derived) from one file.

Works without valid events or KPIs: only the signals and derived signals are compiled.
Stacked subplots share the time axis; the matplotlib toolbar gives zoom and pan.
"""

from __future__ import annotations

import traceback
from pathlib import Path

import numpy as np
from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from src.kpi_studio.core.align import build_frame
from src.kpi_studio.core.engine import FileEvaluator
from src.kpi_studio.core.results import FileOutcome
from src.kpi_studio.gui.widgets.common import make_canvas, matches
from src.kpi_studio.io import profile_store
from src.kpi_studio.io.mf4 import read_signals

COLORS = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"]
MAX_AXES = 8


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
            units = {n: (d.unit or "") for n, d in defs.items()}
            self.done.emit({"file": outcome.file, "time": ev.time, "values": values, "units": units,
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

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        bar = QHBoxLayout()
        self.load_btn = QPushButton("Load signals")
        self.load_btn.setToolTip("Read the ticked signals from the ticked recordings")
        bar.addWidget(self.load_btn)
        self.status = QLabel()
        self.status.setObjectName("hint")
        bar.addWidget(self.status, 1)
        lay.addLayout(bar)

        split = QSplitter(Qt.Horizontal)
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.addWidget(QLabel("Recordings (tick one or more)"))
        self.files = QListWidget()
        self.files.setMaximumHeight(90)
        ll.addWidget(self.files)
        ll.addWidget(QLabel("Signals"))
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("Filter signals… (wildcards: * ?)")
        ll.addWidget(self.filter)
        self.list = QListWidget()
        ll.addWidget(self.list, 1)
        clear = QPushButton("Uncheck all")
        ll.addWidget(clear)
        split.addWidget(left)

        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        self.fig, self.canvas, Toolbar = make_canvas()
        self.toolbar = Toolbar(self.canvas, right)
        rl.addWidget(self.toolbar)
        rl.addWidget(self.canvas, 1)
        split.addWidget(right)
        split.setStretchFactor(1, 1)
        split.setSizes([230, 800])
        lay.addWidget(split, 1)

        self.load_btn.clicked.connect(self.load)
        self.files.itemChanged.connect(lambda *_: self._files_ticked())
        self.list.itemChanged.connect(lambda *_: self.draw())
        self.filter.textChanged.connect(self._apply_filter)
        clear.clicked.connect(self.uncheck_all)
        state.files_changed.connect(self.refresh_files)
        state.validated.connect(self.refresh_names)
        state.validated.connect(self._drop_stale_cache)
        self._cache_key = None
        self.refresh_files()
        self.refresh_names()
        self.draw()

    # ------------------------------------------------------------------ lists
    def refresh_files(self):
        recs = self.state.recordings()
        ticked = set(self.selected_names())
        self.files.blockSignals(True)
        self.files.clear()
        for i, r in enumerate(recs):
            it = QListWidgetItem(r.label)
            it.setData(Qt.UserRole, r.name)
            it.setToolTip("\n".join(r.files))
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            on = r.name in ticked or (not ticked and i == 0)
            it.setCheckState(Qt.Checked if on else Qt.Unchecked)
            self.files.addItem(it)
        self.files.blockSignals(False)
        self.cache = {k: v for k, v in self.cache.items() if k in {r.name for r in recs}}

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
        self._apply_filter()

    def _drop_stale_cache(self):
        # signal definitions changed (channels, units, derived expressions): re-read next time
        key = repr(self.state.clean_data().get("signals")) + repr(self.state.clean_data().get("derived")) \
            + repr(self.state.data.get("imports")) + repr((self.state.data.get("timebase") or {}))
        if key != self._cache_key:
            self._cache_key = key
            self.cache.clear()

    def _apply_filter(self):
        needle = self.filter.text().strip()
        for i in range(self.list.count()):
            it = self.list.item(i)
            it.setHidden(not matches(needle, it.text()))

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

    def uncheck_all(self):
        self.set_checked([], only=True)
        self.draw()

    def _files_ticked(self):
        if any(r.name not in self.cache for r in self.selected()) and self.cache:
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

    def _loaded(self, data):
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

    def draw(self):
        self.fig.clear()
        names = self.checked()
        loaded = [(r, self.cache[r.name]) for r in self.selected() if r.name in self.cache]
        if not loaded:
            ax = self.fig.add_subplot(111)
            ax.text(0.5, 0.5, "Tick recordings and signals on the left, then press Load signals", ha="center",
                    va="center", transform=ax.transAxes, color="#6b6b66")
            ax.set_axis_off()
            self.canvas.draw_idle()
            return
        available = [n for n in names if any(n in d["values"] for _, d in loaded)]
        shown = available[:MAX_AXES]
        if not shown:
            ax = self.fig.add_subplot(111)
            msg = "Tick one or more signals on the left"
            if names:
                msg = "Not available in the ticked recordings: " + ", ".join(names)
            ax.text(0.5, 0.5, msg, ha="center", va="center", transform=ax.transAxes, color="#6b6b66", wrap=True)
            ax.set_axis_off()
            self.canvas.draw_idle()
            return
        overlay = len(loaded) > 1
        axes = self.fig.subplots(len(shown), 1, sharex=True, squeeze=False)[:, 0]
        for i, (ax, n) in enumerate(zip(axes, shown)):
            unit = ""
            for j, (r, d) in enumerate(loaded):
                if n not in d["values"]:
                    continue
                y = np.asarray(d["values"][n], dtype=float)
                color = COLORS[(j if overlay else i) % len(COLORS)]
                ax.plot(d["time"], y, color=color, lw=1.2, label=Path(r.name).name if i == 0 else None)
                unit = unit or d["units"].get(n, "")
            ax.text(0.005, 0.97, f"{n} [{unit}]" if unit else n, transform=ax.transAxes, fontsize=8, va="top",
                    ha="left", color="#1f1f1e", bbox=dict(facecolor="white", edgecolor="none", alpha=0.75, pad=1.5))
            for lo, hi in self.spans:
                ax.axvspan(lo, hi, color="#2a78d6", alpha=0.08, lw=0)
            ax.grid(True, color="#e6e5e0", lw=0.6)
            ax.tick_params(labelsize=7)
        if overlay:
            axes[0].legend(fontsize=7, loc="upper right", framealpha=0.85)
        axes[-1].set_xlabel("time [s]", fontsize=8)
        if len(available) > MAX_AXES:
            axes[0].set_title(f"Showing the first {MAX_AXES} ticked signals", fontsize=8, color="#6b6b66")
        self.canvas.draw_idle()

    def shutdown(self):
        self.queue = []
        if self.worker is not None:
            self.worker.wait(5000)
