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
    QComboBox,
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
from src.kpi_studio.gui.widgets.common import make_canvas
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
    """File picker + checkable signal list + stacked time plots."""

    def __init__(self, state, parent=None):
        super().__init__(parent)
        self.state = state
        self.data = None
        self.worker = None
        self.spans: list[tuple[float, float]] = []

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        bar = QHBoxLayout()
        bar.addWidget(QLabel("File"))
        self.file = QComboBox()
        self.file.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.file.setMinimumContentsLength(24)
        bar.addWidget(self.file, 1)
        self.load_btn = QPushButton("Load signals")
        bar.addWidget(self.load_btn)
        self.status = QLabel()
        self.status.setObjectName("hint")
        bar.addWidget(self.status, 2)
        lay.addLayout(bar)

        split = QSplitter(Qt.Horizontal)
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("Filter signals…")
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
        split.setSizes([200, 800])
        lay.addWidget(split, 1)

        self.load_btn.clicked.connect(self.load)
        self.file.currentIndexChanged.connect(lambda *_: self._file_changed())
        self.list.itemChanged.connect(lambda *_: self.draw())
        self.filter.textChanged.connect(self._apply_filter)
        clear.clicked.connect(self.uncheck_all)
        state.files_changed.connect(self.refresh_files)
        state.validated.connect(self.refresh_names)
        self.refresh_files()
        self.refresh_names()
        self.draw()

    # ------------------------------------------------------------------ lists
    def refresh_files(self):
        cur = self.file.currentText()
        names = [r.label for r in self.state.recordings()]
        self.file.blockSignals(True)
        self.file.clear()
        self.file.addItems(names)
        if cur in names:
            self.file.setCurrentText(cur)
        self.file.blockSignals(False)

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

    def _apply_filter(self):
        needle = self.filter.text().strip().lower()
        for i in range(self.list.count()):
            it = self.list.item(i)
            it.setHidden(bool(needle) and needle not in it.text().lower())

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

    def _file_changed(self):
        cur = self.current_file()
        if self.data is not None and cur is not None and cur.name != self.data["file"]:
            self.load()

    def current_file(self):
        """The selected recording (one file, or several files read together)."""
        recs = self.state.recordings()
        if not recs:
            return None
        return recs[min(max(self.file.currentIndex(), 0), len(recs) - 1)]

    # ------------------------------------------------------------------ load + draw
    def plot_signals(self, names, spans=()):
        """Check `names` (only those) and plot them, loading the file first if needed."""
        self.set_checked(names, only=True)
        self.spans = list(spans)
        cur = self.current_file()
        if self.data is None or cur is None or self.data["file"] != cur.name:
            self.load()
        else:
            self.draw()

    def load(self):
        f = self.current_file()
        if f is None:
            self.status.setText("Add MF4 files on the Files tab first.")
            return
        if self.worker is not None:
            return
        cp = self.state.signals_only_compiled()
        if cp is None:
            self.status.setText("The signal definitions have errors; see Profile check below.")
            return
        if not self.checked():
            self.set_checked(list(cp.profile.signals)[:3])
        self.status.setText(f"Reading {f.label}…")
        self.load_btn.setEnabled(False)
        self.worker = SignalLoadWorker(cp, f, dict(self.state.indexes), self)
        self.worker.done.connect(self._loaded)
        self.worker.failed.connect(lambda m: self.status.setText("Could not read the file: " + m.splitlines()[0]))
        self.worker.finished.connect(self._worker_finished)
        self.worker.start()

    def _worker_finished(self):
        self.worker = None
        self.load_btn.setEnabled(True)

    def _loaded(self, data):
        self.data = data
        t = data["time"]
        span = f"{t[0]:.1f}–{t[-1]:.1f} s" if len(t) else "no samples"
        note = f"{Path(data['file']).name}: {span}"
        if data["missing"]:
            note += f"  ·  not available: {', '.join(sorted(data['missing']))}"
        self.status.setText(note)
        self.status.setToolTip("\n".join(f"{k}: {v}" for k, v in data["missing"].items()) +
                               ("\n\n" + "\n".join(data["issues"]) if data["issues"] else ""))
        self.draw()

    def draw(self):
        self.fig.clear()
        names = self.checked()
        if self.data is None:
            ax = self.fig.add_subplot(111)
            ax.text(0.5, 0.5, "Tick signals on the left, then press Load signals", ha="center", va="center",
                    transform=ax.transAxes, color="#6b6b66")
            ax.set_axis_off()
            self.canvas.draw_idle()
            return
        t, values, units = self.data["time"], self.data["values"], self.data["units"]
        shown = [n for n in names if n in values][:MAX_AXES]
        if not shown:
            ax = self.fig.add_subplot(111)
            msg = "Tick one or more signals on the left"
            if names:
                msg = "Not available in this file: " + ", ".join(names)
            ax.text(0.5, 0.5, msg, ha="center", va="center", transform=ax.transAxes, color="#6b6b66", wrap=True)
            ax.set_axis_off()
            self.canvas.draw_idle()
            return
        axes = self.fig.subplots(len(shown), 1, sharex=True, squeeze=False)[:, 0]
        for i, (ax, n) in enumerate(zip(axes, shown)):
            y = np.asarray(values[n], dtype=float)
            ax.plot(t, y, color=COLORS[i % len(COLORS)], lw=1.3)
            unit = units.get(n, "")
            ax.text(0.005, 0.97, f"{n} [{unit}]" if unit else n, transform=ax.transAxes, fontsize=8, va="top",
                    ha="left", color="#1f1f1e", bbox=dict(facecolor="white", edgecolor="none", alpha=0.75, pad=1.5))
            for lo, hi in self.spans:
                ax.axvspan(lo, hi, color="#2a78d6", alpha=0.08, lw=0)
            ax.grid(True, color="#e6e5e0", lw=0.6)
            ax.tick_params(labelsize=7)
        axes[-1].set_xlabel("time [s]", fontsize=8)
        if len([n for n in names if n in values]) > MAX_AXES:
            axes[0].set_title(f"Showing the first {MAX_AXES} ticked signals", fontsize=8, color="#6b6b66")
        self.canvas.draw_idle()

    def shutdown(self):
        if self.worker is not None:
            self.worker.wait(5000)
