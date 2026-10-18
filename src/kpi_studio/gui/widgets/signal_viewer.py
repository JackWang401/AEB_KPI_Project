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

import sys
import traceback
from pathlib import Path

import numpy as np
from PySide6.QtCore import QEvent, QMimeData, QPoint, QSize, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QColor, QCursor, QKeySequence, QPainter, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QSizePolicy,
    QSpinBox,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPushButton,
    QScrollArea,
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
MARGIN_PX = dict(left=64, right=250, top=10, bottom=40, gap=0)   # right: the signal / value list


class SignalList(QListWidget):
    """Signal list whose items can be dragged onto a subplot, or up and down the list to reorder the
    subplots (they follow the list's order)."""

    reordered = Signal()
    header_activated = Signal(str)  # double-click on a common axis' name: its name

    def __init__(self, parent=None):
        super().__init__(parent)
        self.ctrl_clicked = False  # the last press was a macOS Ctrl+click (selects, opens no menu)
        self.setDragEnabled(True)
        self.setDragDropMode(QAbstractItemView.DragDrop)
        self.setDropIndicatorShown(True)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)

    def header_at(self, pos) -> str | None:
        """Name of the common axis whose name row is under viewport position `pos`."""
        it = self.itemAt(pos)
        tag = it.data(AXIS_ROLE) if it is not None else None
        if tag and tag.get("first") and pos.y() < self.visualItemRect(it).top() + HEADER_PX:
            return tag["name"]
        return None

    def mousePressEvent(self, e):
        # macOS turns Ctrl+click into a right click; here it adds or removes the row from the
        # selection instead (as Ctrl+click does on Windows), the menu stays on the real right click
        self.ctrl_clicked = (CTRL_CLICK_SELECTS and e.button() == Qt.RightButton
                             and bool(e.modifiers() & Qt.MetaModifier))
        if self.ctrl_clicked:
            it = self.itemAt(e.position().toPoint())
            if it is not None:
                it.setSelected(not it.isSelected())
            e.accept()
            return
        super().mousePressEvent(e)

    def mouseDoubleClickEvent(self, e):
        label = self.header_at(e.position().toPoint())
        if label:
            self.header_activated.emit(label)
            e.accept()
            return
        super().mouseDoubleClickEvent(e)

    def mimeTypes(self):
        return [SIGNAL_MIME]

    def supportedDropActions(self):
        return Qt.CopyAction  # dragging never removes a signal from the list

    def mimeData(self, items):
        md = QMimeData()
        md.setData(SIGNAL_MIME, "\n".join(i.text() for i in items).encode())
        return md

    def dragEnterEvent(self, e):
        if e.source() is self:
            super().dragEnterEvent(e)
            e.setDropAction(Qt.CopyAction)  # (a move would let Qt delete the dragged rows)
            e.accept()
        else:
            e.ignore()

    def dragMoveEvent(self, e):
        if e.source() is self:
            super().dragMoveEvent(e)  # (draws the drop indicator)
            e.setDropAction(Qt.CopyAction)
            e.accept()
        else:
            e.ignore()

    def drop_row(self, y: int) -> int:
        """Where rows dropped at viewport height `y` go: before the row under it, after it in its lower half."""
        idx = self.indexAt(QPoint(5, y))
        if not idx.isValid():
            return self.count()
        rect = self.visualRect(idx)
        return idx.row() + (1 if y > rect.center().y() else 0)

    def dropEvent(self, e):
        if e.source() is not self:
            e.ignore()
            return
        e.setDropAction(Qt.CopyAction)
        e.accept()
        self.move_rows(sorted(self.row(i) for i in self.selectedItems()), self.drop_row(int(e.position().y())))
        self.setState(QAbstractItemView.NoState)
        self.viewport().update()

    def move_rows(self, rows: list[int], to: int):
        """Move `rows` (kept in their order) to sit before row `to`."""
        if not rows:
            return
        self.blockSignals(True)
        items = [self.takeItem(r - k) for k, r in enumerate(rows)]
        to -= sum(1 for r in rows if r < to)
        for k, it in enumerate(items):
            self.insertItem(to + k, it)
        self.clearSelection()
        for it in items:
            it.setSelected(True)
        self.blockSignals(False)
        self.reordered.emit()
COLORS = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"]
KINDS = ["continuous", "discrete", "boolean", "enum"]
KIND_TIPS = {
    "continuous": "Measured values: linearly interpolated between samples, drawn as a plain line",
    "discrete": "Counters, states and other step values: held until the next sample, drawn as a stepped line",
    "boolean": "0/1 flags: held, drawn as a shaded stepped line at the bottom of the plot",
    "enum": "Values with names (a value table): held, drawn as a stepped line",
}
AXIS_ROLE = Qt.UserRole + 7  # list item data: the signal's common axis, see SignalViewer._tag_axes
HEADER_PX = 22   # height of a common axis' name row, drawn above its first signal
INDENT_PX = 20   # signals of a common axis are indented under its name
CTRL_CLICK_SELECTS = sys.platform == "darwin"  # macOS: Ctrl+click adds to the selection (no menu)


def _group_rects(option, tag):
    """(header rect or None, content rect) of a list row on a common axis."""
    r = option.rect
    if tag.get("first"):
        return r.adjusted(0, 0, 0, -(r.height() - HEADER_PX)), r.adjusted(INDENT_PX, HEADER_PX, 0, 0)
    return None, r.adjusted(INDENT_PX, 0, 0, 0)


class AxisTagDelegate(QStyledItemDelegate):
    """Signals on a common axis are listed under the axis' name, indented and joined by a line
    in the axis' colour:   axis 1 / ├ signal1 / └ signal2."""

    def sizeHint(self, option, index):
        size = super().sizeHint(option, index)
        tag = index.data(AXIS_ROLE)
        if tag and tag.get("first"):
            size.setHeight(size.height() + HEADER_PX)
        return size

    def _shifted(self, option, index):
        tag = index.data(AXIS_ROLE)
        if not tag:
            return option, None, None
        opt = QStyleOptionViewItem(option)
        header, opt.rect = _group_rects(option, tag)
        return opt, header, tag

    def editorEvent(self, event, model, option, index):
        opt, _, _ = self._shifted(option, index)  # the check box moved with the row's content
        return super().editorEvent(event, model, opt, index)

    def paint(self, painter, option, index):
        opt, header, tag = self._shifted(option, index)
        super().paint(painter, opt, index)
        if not tag:
            return
        colour = QColor(tag["colour"])
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        if header is not None:
            band = QColor(colour)
            band.setAlpha(28)
            painter.fillRect(header.adjusted(2, 2, -2, -1), band)
            font = painter.font()
            font.setBold(True)
            painter.setFont(font)
            painter.setPen(colour)
            text_rect = header.adjusted(8, 0, -8, 0)
            painter.drawText(text_rect, Qt.AlignVCenter | Qt.AlignLeft, tag["name"])
            if tag.get("range"):
                font.setBold(False)
                painter.setFont(font)
                painter.setPen(QColor("#6b6b66"))
                painter.drawText(text_rect, Qt.AlignVCenter | Qt.AlignRight, tag["range"])
        pen = painter.pen()
        pen.setColor(colour)
        pen.setWidthF(1.5)
        painter.setPen(pen)
        x = option.rect.left() + 10
        mid = opt.rect.center().y()
        top = opt.rect.top() - (2 if header is not None else 0)
        painter.drawLine(x, top, x, mid if tag.get("last") else option.rect.bottom() + 1)
        painter.drawLine(x, mid, x + INDENT_PX - 13, mid)
        painter.restore()


AXIS_MIN_PX = 70     # each subplot gets at least this height: with more, the plots scroll
BOOL_RATIO = 0.4  # height of a boolean subplot, relative to the others


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


def _same_axes(a, b) -> bool:
    """Two `common_axes` lists hold the same axes (the order of axes and signals aside)."""
    def norm(axes):
        out = set()
        for d in axes or []:
            if isinstance(d, dict):
                rng = d.get("range")
                out.add((d.get("name"), frozenset(d.get("signals") or []),
                         tuple(float(x) for x in rng) if isinstance(rng, (list, tuple)) else None))
        return out

    try:
        return norm(a) == norm(b)
    except (TypeError, ValueError):
        return False


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
            # every read signal also held between its samples (no interpolation), for stepped lines
            held_defs = {n: d.model_copy(update={"kind": "discrete"}) for n, d in defs.items()}
            held_frame, _, _ = build_frame(raw, held_defs, self.cp.profile.timebase.rate_hz,
                                           profile_store.load_enum_tables())
            held = dict(held_frame.values) if len(held_frame.time) == len(ev.time) else {}
            units = signal_units(self.cp.profile)
            labels = {n: r.labels for n, r in raw.items() if r.labels}
            self.done.emit({"file": outcome.file, "time": ev.time, "values": values, "held": held, "units": units,
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
        self.side_px = MARGIN_PX["right"]  # width of the signal / value list right of the plots (drag its border)
        self._side_drag = False
        self._zoom = None  # right-drag zoom in progress: (axes, x0, y0, px0, py0, rectangle)
        self.host: dict[str, str] = {}       # signal -> signal whose subplot it is drawn in (drag and drop)
        self.common: dict[str, str] = {}     # signal -> name of the common axis it shares (right-click)
        self.axis_range: dict[str, tuple] = {}  # common axis -> fixed (min, max) of its value axis
        self._loading_axes = False  # taking the axes from the profile: do not write them back yet
        self._pending_shown = None  # ticked signals from an opened profile, until the list holds them
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
        # which recordings are plotted: the one selected in the Files panel (several can be overlaid
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
        self.list.setToolTip("Tick signals to plot them. Drag a signal onto a plot to draw it in that subplot, "
                             "or up and down this list to change the order of the subplots.")
        self.order: list[str] = []  # the list's order once the user dragged it (else alphabetical)
        self.list.reordered.connect(self._reordered)
        self.list.setItemDelegate(AxisTagDelegate(self.list))
        self.step_choice: dict[str, bool] = {}  # right-click choices; otherwise discrete signals are stepped
        self.list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._list_menu)
        self.list.header_activated.connect(self.ask_rename_axis)
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
        self.toolbar = Toolbar(self.canvas, right, coordinates=False)  # no (x, y) readout
        self.canvas.setFocusPolicy(Qt.StrongFocus)
        for key in (QKeySequence.Undo, QKeySequence("Meta+Z")):  # Ctrl+Z / ⌘Z, and Control+Z on a Mac
            QShortcut(key, self.canvas, self.undo_zoom, context=Qt.WidgetShortcut)
        self.toolbar.setIconSize(QSize(18, 18))  # compact: leave the height to the plots
        rl.addWidget(self.toolbar)
        # subplots share the panel height; when there are too many for it, they scroll
        self.plot_scroll = QScrollArea()
        self.plot_scroll.setWidgetResizable(True)
        self.plot_scroll.setFrameShape(QFrame.NoFrame)
        self.plot_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.plot_scroll.setWidget(self.canvas)
        rl.addWidget(self.plot_scroll, 1)
        self.canvas.setAcceptDrops(True)
        self.canvas.installEventFilter(self)
        split.addWidget(right)
        split.setStretchFactor(1, 1)
        split.setSizes([230, 1160])
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
        state.profile_changed.connect(self.load_axes)
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
        """Plot the recording a file belongs to (the row selected in the Files panel)."""
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

    def _reordered(self):
        self.order = [self.list.item(i).text() for i in range(self.list.count())]
        self._tag_axes()  # a common axis' signals stay together under its name
        if self.checked():
            self.draw()

    def refresh_names(self):
        names = self.state.signal_names()
        if self.order:  # the user's order; new signals after it, alphabetically
            names = [n for n in self.order if n in names] + [n for n in names if n not in self.order]
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
        self._tag_axes()
        self._apply_shown()

    def _apply_shown(self):
        """Tick the signals saved in the profile (`shown_signals`) once the list holds them, and plot them."""
        if self._pending_shown is None or not self.list.count():
            return
        names, self._pending_shown = self._pending_shown, None
        self.set_checked(names, only=True)
        if self.checked():
            self._files_ticked()
        else:
            self.draw()

    def _store_shown(self):
        """Keep the ticked signals in the profile, so the same plot comes back when it is opened again."""
        if self._pending_shown is not None or self._loading_axes or not self.list.count():
            return
        shown = self.checked()
        if set(shown) == set(self.state.data.get("shown_signals") or []):
            return
        if shown:
            self.state.data["shown_signals"] = shown
        else:
            self.state.data.pop("shown_signals", None)
        self.state.edited()

    def axis_colour(self, name: str) -> str:
        order = list(dict.fromkeys(self.common.values()))
        return COLORS[order.index(name) % len(COLORS)] if name in order else COLORS[0]

    def _cluster_axes(self):
        """Move the signals of each common axis together, under the first of them in the list."""
        names = [self.list.item(i).text() for i in range(self.list.count())]
        new, done = [], set()
        for n in names:
            axis = self.common.get(n)
            if axis is None:
                new.append(n)
            elif axis not in done:
                done.add(axis)
                new += [m for m in names if self.common.get(m) == axis]
        if new == names:
            return
        self.list.blockSignals(True)
        items = {self.list.item(i).text(): self.list.item(i) for i in range(self.list.count())}
        selected = {it.text() for it in self.list.selectedItems()}
        for _ in range(self.list.count()):
            self.list.takeItem(0)
        for n in new:
            self.list.addItem(items[n])
            items[n].setSelected(n in selected)
        self.list.blockSignals(False)
        self.order = new

    def _tag_axes(self):
        """List the signals of each common axis under its name (see AxisTagDelegate)."""
        self._cluster_axes()
        count = self.list.count()
        for i in range(count):
            it = self.list.item(i)
            axis = self.common.get(it.text())
            if not axis:
                it.setData(AXIS_ROLE, None)
                continue
            prev = self.common.get(self.list.item(i - 1).text()) if i > 0 else None
            nxt = self.common.get(self.list.item(i + 1).text()) if i + 1 < count else None
            rng = self.axis_range.get(axis)
            it.setData(AXIS_ROLE, {"name": axis, "colour": self.axis_colour(axis), "first": prev != axis,
                                   "last": nxt != axis, "range": f"{rng[0]:g} … {rng[1]:g}" if rng else ""})
        self.list.doItemsLayout()
        self.list.viewport().update()
        self._store_axes()

    def axes_data(self) -> list[dict]:
        """The common axes as kept in the profile (`common_axes`), in list order."""
        order = [self.list.item(i).text() for i in range(self.list.count())]
        rank = {n: i for i, n in enumerate(order)}
        out = []
        for a in self.axis_names():
            members = sorted((n for n, x in self.common.items() if x == a), key=lambda n: rank.get(n, len(rank)))
            d = {"name": a, "signals": members}
            if a in self.axis_range:
                d["range"] = list(self.axis_range[a])
            out.append(d)
        return sorted(out, key=lambda d: min(rank.get(n, len(rank)) for n in d["signals"]))

    def _store_axes(self):
        """Keep the common axes in the profile, so they come back when it is opened again."""
        if self._loading_axes:
            return
        data = self.axes_data()
        if _same_axes(data, self.state.data.get("common_axes")):
            return
        if data:
            self.state.data["common_axes"] = data
        else:
            self.state.data.pop("common_axes", None)
        self.state.edited()

    def load_axes(self):
        """Take the common axes and ticked signals from the profile (opened, new, or renamed signals)."""
        shown = [n for n in self.state.data.get("shown_signals") or [] if isinstance(n, str)]
        if set(shown) != set(self.checked()):
            self._pending_shown = shown
            self._apply_shown()
        saved = self.state.data.get("common_axes") or []
        if _same_axes(saved, self.axes_data()):
            return
        for n in set(self.common) | set(self.host):
            self.host.pop(n, None)
        self.common, self.axis_range = {}, {}
        for d in saved:
            if not isinstance(d, dict) or not d.get("name") or d["name"] in self.axis_range or \
                    d["name"] in self.common.values():
                continue
            members = [n for n in d.get("signals") or [] if isinstance(n, str) and n not in self.common]
            for n in members:
                self.common[n] = d["name"]
                if n != members[0]:
                    self.host[n] = members[0]
            rng = d.get("range")
            if members and isinstance(rng, (list, tuple)) and len(rng) == 2:
                try:
                    lo, hi = float(rng[0]), float(rng[1])
                    if lo < hi:
                        self.axis_range[d["name"]] = (lo, hi)
                except (TypeError, ValueError):
                    pass
        self._loading_axes = True
        try:
            self._tag_axes()
        finally:
            self._loading_axes = False
        if self._axes:
            self.draw()

    def ask_axis_name(self, names: list[str]) -> str | None:
        current = next((self.common[n] for n in names if n in self.common), "")
        taken = list(dict.fromkeys(self.common.values()))
        default = current or next(f"axis {k}" for k in range(1, len(taken) + 2) if f"axis {k}" not in taken)
        text, ok = QInputDialog.getText(self, "Common axis", "Name of the common axis (an existing name joins "
                                        "that axis):", text=default)
        return text.strip() if ok and text.strip() else None

    def common_axis(self, names: list[str]):
        label = self.ask_axis_name(names)
        if label:
            self.set_common_axis(names, label)

    def axis_names(self) -> list[str]:
        return list(dict.fromkeys(self.common.values()))

    def new_common_axis(self, names: list[str]) -> str:
        """Put `names` on a new common axis, named axis 1, axis 2, … (rename it from the menu)."""
        taken = self.axis_names()
        label = next(f"axis {k}" for k in range(1, len(taken) + 2) if f"axis {k}" not in taken)
        self.set_common_axis(names, label)
        return label

    def rename_axis(self, old: str, new: str):
        new = new.strip()
        if not new or new == old:
            return
        if new in self.axis_names():  # an existing name: the two axes become one
            self.set_common_axis([n for n, a in self.common.items() if a == old], new)
            self.axis_range.pop(old, None)
            return
        self.common = {n: (new if a == old else a) for n, a in self.common.items()}
        if old in self.axis_range:
            self.axis_range[new] = self.axis_range.pop(old)
        self._tag_axes()
        self.draw()

    def ask_rename_axis(self, label: str):
        text, ok = QInputDialog.getText(self, "Rename common axis", "New name (an existing name merges the "
                                        "two axes):", text=label)
        if ok:
            self.rename_axis(label, text)

    def set_axis_range(self, label: str, rng):
        """Fix the value range of common axis `label` to (min, max); None: fit to the data again."""
        if rng is None:
            self.axis_range.pop(label, None)
        else:
            lo, hi = float(rng[0]), float(rng[1])
            if not lo < hi:
                raise ValueError("the minimum must be below the maximum")
            self.axis_range[label] = (lo, hi)
        self._tag_axes()
        self.draw()

    def _axis_of(self, label: str):
        return next((ax for ax, g in self._ax_groups.items() if any(self.common.get(n) == label for n in g)), None)

    def ask_axis_range(self, label: str):
        """Dialog for the range of common axis `label`: (min, max), None for automatic, or False if cancelled."""
        from PySide6.QtWidgets import QCheckBox, QDialog, QDialogButtonBox, QFormLayout, QLineEdit

        cur = self.axis_range.get(label)
        ax = self._axis_of(label)
        shown = cur or (ax.get_ylim() if ax is not None else (0.0, 1.0))
        dlg = QDialog(self)
        dlg.setWindowTitle(f"Range of {label}")
        form = QFormLayout(dlg)
        auto = QCheckBox("Automatic (fit to the signals)")
        auto.setChecked(cur is None)
        lo, hi = QLineEdit(f"{shown[0]:.6g}"), QLineEdit(f"{shown[1]:.6g}")
        form.addRow(auto)
        form.addRow("Minimum", lo)
        form.addRow("Maximum", hi)
        err = QLabel("")
        err.setStyleSheet("color: #c0392b;")
        form.addRow(err)
        for w in (lo, hi):
            w.setEnabled(cur is not None)
        auto.toggled.connect(lambda on: [w.setEnabled(not on) for w in (lo, hi)])
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        form.addRow(buttons)
        result = []

        def accept():
            if auto.isChecked():
                result.append(None)
                return dlg.accept()
            try:
                a, b = float(lo.text()), float(hi.text())
            except ValueError:
                return err.setText("Type a number in both boxes")
            if not a < b:
                return err.setText("The minimum must be below the maximum")
            result.append((a, b))
            dlg.accept()

        buttons.accepted.connect(accept)
        buttons.rejected.connect(dlg.reject)
        self._range_dialog = dlg  # for tests
        if dlg.exec() != QDialog.Accepted or not result:
            return False
        return result[0]

    def range_axis(self, label: str):
        rng = self.ask_axis_range(label)
        if rng is not False:
            self.set_axis_range(label, rng)

    def set_common_axis(self, names: list[str], label: str):
        """Draw `names` in one subplot named `label`, together with any signals already on that axis."""
        names = [n for n in names if self.common.get(n) != label]  # already on it: nothing to do
        if not names:
            return
        members = [n for n, a in self.common.items() if a == label and n not in names]
        self.uncommon(names, draw=False)
        host = members[0] if members else names[0]
        for n in names:
            self.common[n] = label
            if n == host:
                self.host.pop(n, None)
            else:
                self.host[n] = host
        self.set_checked(names + members)
        self._tag_axes()
        self.draw()

    def uncommon(self, names, draw=True):
        """Take `names` off their common axis: each back to a subplot of its own."""
        for n in names:
            label = self.common.pop(n, None)
            hosted = [o for o, h in self.host.items() if h == n]
            self.host.pop(n, None)
            rest = [o for o in hosted if o not in names]
            for o in hosted:
                self.host.pop(o, None)
            for o in rest[1:]:  # the others stay together under a new host
                self.host[o] = rest[0]
            if label and len([o for o, a in self.common.items() if a == label]) == 1:
                self.common = {o: a for o, a in self.common.items() if a != label}  # one left: no axis
            if label and label not in self.common.values():
                self.axis_range.pop(label, None)
        if draw:
            self._tag_axes()
            self.draw()

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
            if self.common.get(it.text()):
                tip = "\n".join(x for x in (tip, f"Common axis: {self.common[it.text()]}") if x)
            it.setToolTip(tip)

    def _drop_stale_cache(self):
        # signal definitions changed (channels, units, derived expressions): re-read next time
        key = repr(self.state.clean_data().get("signals")) + repr(self.state.clean_data().get("derived")) \
            + repr(self.state.data.get("imports")) + repr((self.state.data.get("timebase") or {}))
        if key != self._cache_key:
            stale = self._cache_key is not None and bool(self.cache)
            self._cache_key = key
            self.cache.clear()
            if stale and self._axes and self.worker is None:
                QTimer.singleShot(0, self, self.load)  # re-read what is plotted, so the plot never goes blank

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
            self.status.setText("Add MF4 files in the Files panel on the left edge first.")
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
        if obj is self.canvas and event.type() == QEvent.Wheel:  # the wheel scrolls the subplots
            QApplication.sendEvent(self.plot_scroll.verticalScrollBar(), event)
            return True
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
        label = self.common.get(host) if host else None
        for n in names:  # a dropped signal joins the target's common axis, or leaves its own
            if label:
                self.common[n] = label
            else:
                self.common.pop(n, None)
        for n in names:
            if host is None or n == host:
                self.host.pop(n, None)
                continue
            for other, h in list(self.host.items()):  # whatever n hosted moves along
                if h == n:
                    self.host[other] = host
            self.host[n] = host
        self.set_checked(names)
        self._tag_axes()
        self.draw()

    def separate_all(self):
        self.host.clear()
        self.common.clear()
        self.axis_range.clear()
        self._tag_axes()
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

        def key(n):
            # a common axis is one subplot whatever the drag-and-drop links say
            axis = self.common.get(n) or self.common.get(root(n))
            return ("axis", axis) if axis else ("host", root(n))

        groups: dict[tuple, list[str]] = {}
        for n in names:
            groups.setdefault(key(n), []).append(n)
        out = []
        for k, g in groups.items():
            r = root(g[0]) if k[0] == "host" else next((n for n in g if self.host.get(n) not in g), g[0])
            out.append([r] + [n for n in g if n != r] if r in g else g)
        return out

    def _apply_layout(self):
        """Fixed pixel margins, so labels and moment names never squeeze the subplots."""
        n = len(self._axes)
        if not n:
            return
        w, h = max(self.canvas.width(), 1), max(self.canvas.height(), 1)
        m = dict(MARGIN_PX, right=self.side_px)
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

        self._store_shown()
        self.fig.clear()
        self.toolbar.update()  # new subplots: forget the zoom history of the old ones
        self._axes, self._series, self._artists, self._ax_groups, self._enum_axes = [], {}, [], {}, set()
        self._bool_axes = set()
        self._legend = {}
        self._names = {}        # axes -> signal name of each series (same order as _series)
        self.kpi_lines = {}     # axes -> {signal: [(text, ok)]}: preview KPIs listed under their signal
        self.kpi_others = []    # preview KPIs whose signals are not plotted
        names = self.checked()
        loaded = [(r, self.cache[r.name]) for r in self.selected() if r.name in self.cache]
        self.separate_btn.setVisible(bool(self.host))
        if not loaded:
            self._message("Tick signals on the left to plot them")
            return
        available = [n for n in names if any(n in d["values"] for _, d in loaded)]
        groups = self._groups(available)
        shown = groups
        if not shown:
            self._message("Not available in the ticked recordings: " + ", ".join(names) if names
                          else "Tick one or more signals on the left")
            return
        # boolean subplots go to the bottom, as low strips with the true spans shaded (as in ETAS MDA)
        flags = [all(self.kind(n) == "boolean" for n in g) for g in shown]
        shown = [g for g, f in zip(shown, flags) if not f] + [g for g, f in zip(shown, flags) if f]
        ratios = sorted((BOOL_RATIO if f else 1.0 for f in flags), reverse=True)
        overlay = len(loaded) > 1
        axes = self.fig.subplots(len(shown), 1, sharex=True, squeeze=False,
                                 gridspec_kw={"height_ratios": ratios})[:, 0]
        m = MARGIN_PX
        self.canvas.setMinimumHeight(int(m["top"] + m["bottom"] + 16
                                         + sum(r * AXIS_MIN_PX + m["gap"] for r in ratios)))
        for i, (ax, group) in enumerate(zip(axes, shown)):
            multi = len(group) > 1
            for k, n in enumerate(group):
                for j, (r, d) in enumerate(loaded):
                    if n not in d["values"]:
                        continue
                    step = self.is_stepped(n)
                    y = np.asarray(d.get("held", {}).get(n, d["values"][n]) if step else d["values"][n], dtype=float)
                    t = np.asarray(d["time"], dtype=float)
                    c = COLORS[k % len(COLORS)] if multi else COLORS[(j if overlay else i) % len(COLORS)]
                    ls = LINESTYLES[j % len(LINESTYLES)] if (multi and overlay) else "-"
                    ax.plot(t, y, color=c, lw=1.2, ls=ls,
                            drawstyle="steps-post" if step else "default")  # (right-click)
                    if self.kind(n) == "boolean":
                        ax.fill_between(t, 0, np.where(np.isfinite(y), y, 0), step="post" if step else None,
                                        color=c, alpha=0.18, lw=0)
                    label = f"{n} · {Path(r.name).name}" if overlay else n
                    enum = d.get("labels", {}).get(n)
                    self._series.setdefault(ax, []).append((label, t, y, enum))
                    self._legend.setdefault(ax, []).append((label, c, ls, d["units"].get(n, "")))
                    self._names.setdefault(ax, []).append(n)
                    if enum and not multi:
                        self._enum_ticks(ax, enum, y)
            if all(self.kind(n) == "boolean" for n in group):
                ax.set_yticks([])  # a flag needs no value axis: the shading shows when it is on
                ax.set_ylim(-0.15, 1.25)
                self._bool_axes.add(ax)
            elif ax not in self._enum_axes:
                ax.yaxis.set_major_locator(MaxNLocator(nbins=4, min_n_ticks=2))
            for lo, hi in self.spans:
                ax.axvspan(lo, hi, color="#2a78d6", alpha=0.08, lw=0)
            ax.grid(True, color="#e6e5e0", lw=0.6)
            ax.tick_params(labelsize=7.5)
            if ax not in self._bool_axes:
                ax.margins(y=0.08)
            self._ax_groups[ax] = group
            label = next((self.common[n] for n in group if n in self.common), None)
            if label in self.axis_range:  # a range chosen for the common axis (right-click)
                ax.set_ylim(*self.axis_range[label])
            if label and multi:  # the common axis' name, top left in its colour
                ax.text(0.005, 0.97, label, transform=ax.transAxes, ha="left", va="top", fontsize=7.5,
                        color=self.axis_colour(label), fontweight="bold",
                        bbox=dict(boxstyle="round,pad=0.2", fc="white", ec=self.axis_colour(label), lw=0.6))
        axes[-1].set_xlabel("time [s]", fontsize=8)
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
            self.pv_status.setText("Add files in the Files panel on the left edge first.")
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
            "held": old.get("held", {}) if len(old.get("time", [])) == len(data["time"]) else {},
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
        self._issues_tip = "\n".join(issues)
        self.pv_status.setToolTip(self._issues_tip)
        self.draw()

    def _decorate_preview(self, axes, loaded):
        """Zoom to the chosen occurrence, shade it, mark its moments and list its KPIs next to the signals
        they read (KPIs of no plotted signal under the first subplot's list)."""
        ev = self.current_event()
        active = bool(self.preview and ev and any(r.name == self.preview["rec"] for r, _ in loaded))
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
            if self._fixed_range(ax):
                continue
            parts = [y[(t >= lo) & (t <= hi)] for _, t, y, _ in self._series.get(ax, [])]
            vals = np.concatenate(parts) if parts else np.array([])
            vals = vals[np.isfinite(vals)]
            if vals.size:
                y0, y1 = float(vals.min()), float(vals.max())
                pad = 0.08 * (y1 - y0) or max(abs(y0) * 0.1, 0.5)
                ax.set_ylim(y0 - pad, y1 + pad)
        res = [r for r in out.results if r.event == ev and r.occurrence == idx]
        self._place_kpis(axes, res)

    def kpi_signals(self, event: str, kpi: str) -> list[str]:
        """The signals a KPI reads (its value or reduced expression), for listing it next to them."""
        compiled = self.state.compiled
        k = next((k for k in (compiled.kpis_for(event) if compiled else []) if k.name == kpi), None)
        if k is None:
            return []
        deps = [*(sorted(k.value.deps) if k.value is not None else []),
                *(sorted(k.of.deps) if k.of is not None else []),
                *(d for _label, c in k.which for d in sorted(c.deps))]
        deps = list(dict.fromkeys(deps))
        return [d for d in deps if d != "time"]

    def _place_kpis(self, axes, results):
        """Each KPI goes under the first plotted signal it reads; the others are listed in the status tip."""
        others = []
        for k in results:
            ok = k.status == Status.OK
            value = "—" if k.value is None else (k.text or f"{k.value:.3f}")
            text = f"{k.kpi} = {value}" + (f" {k.unit}" if k.unit and k.value is not None else "")
            if not ok:
                why = k.message or k.status
                text += f"  ({why[:28] + '…' if len(why) > 29 else why})"
            reads = self.kpi_signals(k.event, k.kpi)
            spot = next(((ax, n) for n in reads for ax in axes if n in self._names.get(ax, [])), None)
            if spot is None:
                others.append(text)
            else:
                self.kpi_lines.setdefault(spot[0], {}).setdefault(spot[1], []).append((text, ok))
        self.kpi_others = others
        base = self.pv_status.text().split("  ·  KPIs")[0]
        if others:
            self.pv_status.setText(f"{base}  ·  KPIs of signals not plotted: {len(others)} (hover)")
            self.pv_status.setToolTip("\n\n".join(filter(None, [
                getattr(self, "_issues_tip", ""),
                "Tick the signal a KPI reads to see its value next to it:\n" + "\n".join(others)])))
        else:
            self.pv_status.setText(base)
            self.pv_status.setToolTip(getattr(self, "_issues_tip", ""))

    # ------------------------------------------------------------------ cursors
    def signal_menu(self, names: list[str], menu: QMenu | None = None) -> QMenu:
        """Right-click menu for `names`: draw them as a stepped or a plain line, and their Kind."""
        menu = menu or QMenu(self)
        menu.setToolTipsVisible(True)
        act = menu.addAction("Stepped line" + (f" ({len(names)} signals)" if len(names) > 1 else ""))
        act.setCheckable(True)
        act.setChecked(bool(names) and all(self.is_stepped(n) for n in names))
        act.setToolTip("Hold each value until the next sample (for states, flags and other discrete signals)")
        act.triggered.connect(lambda on, ns=list(names): self.set_stepped(ns, on))
        menu.addSection("Kind")
        own = [n for n in names if self._kind_editable(n)]
        for k in KINDS:
            act = menu.addAction(k)
            act.setCheckable(True)
            act.setChecked(bool(names) and all(self.kind(n) == k for n in names))
            act.setEnabled(bool(own))
            act.setToolTip(KIND_TIPS[k] if own else "Derived signals have no Kind")
            act.triggered.connect(lambda _on, ns=own, k=k: self.set_kind(ns, k))
        menu.addSeparator()
        self._common_menu(menu, list(names))
        return menu

    def _common_menu(self, menu: QMenu, names: list[str]):
        """Common axis entries: a new axis for the selected signals, or join one already there;
        then the range, name and removal of the axes they are on."""
        axes = self.axis_names()
        if not axes:
            act = menu.addAction("Common axis")
            act.setEnabled(len(names) >= 2)
            act.setToolTip("Draw the selected signals together in one subplot, on one value axis"
                           if len(names) >= 2 else "Select 2 or more signals (Ctrl+click or Shift+click) "
                           "to share an axis")
            act.triggered.connect(lambda _on, ns=names: self.new_common_axis(ns))
        else:
            sub = QMenu("Common axis", menu)  # owned by the menu, so it outlives this function
            menu.addMenu(sub)
            sub.setToolTipsVisible(True)
            for a in axes:
                act = sub.addAction(f"Join {a}")
                act.setCheckable(True)
                act.setChecked(bool(names) and all(self.common.get(n) == a for n in names))
                act.setToolTip(f"Draw the selected signals in the subplot of {a}")
                act.triggered.connect(lambda _on, ns=names, a=a: self.set_common_axis(ns, a))
            sub.addSeparator()
            act = sub.addAction("New common axis")
            act.setToolTip("Put the selected signals on an axis of their own")
            act.triggered.connect(lambda _on, ns=names: self.new_common_axis(ns))
        own = list(dict.fromkeys(self.common[n] for n in names if n in self.common))
        for a in own:
            suffix = f" ({a})" if len(own) > 1 else ""
            act = menu.addAction(f"Range of common axis{suffix}…")
            rng = self.axis_range.get(a)
            act.setToolTip(f"Now {rng[0]:g} to {rng[1]:g}" if rng else "Now fitted to the signals")
            act.triggered.connect(lambda _on, a=a: self.range_axis(a))
            act = menu.addAction(f"Rename common axis{suffix}…")
            act.triggered.connect(lambda _on, a=a: self.ask_rename_axis(a))
        on = [n for n in names if n in self.common]
        if on:
            act = menu.addAction("Remove from common axis")
            act.triggered.connect(lambda _on, ns=on: self.uncommon(ns))

    def axis_menu(self, label: str) -> QMenu:
        """Right-click on a common axis' name in the list: its range, name, and ungrouping."""
        menu = QMenu(self)
        menu.setToolTipsVisible(True)
        members = [n for n, a in self.common.items() if a == label]
        act = menu.addAction("Range…")
        rng = self.axis_range.get(label)
        act.setToolTip(f"Now {rng[0]:g} to {rng[1]:g}" if rng else "Now fitted to the signals")
        act.triggered.connect(lambda _on: self.range_axis(label))
        menu.addAction("Rename…").triggered.connect(lambda _on: self.ask_rename_axis(label))
        menu.addAction("Select its signals").triggered.connect(lambda _on: self._select(members))
        menu.addSeparator()
        act = menu.addAction("Remove common axis")
        act.setToolTip("Each of its signals back to a subplot of its own")
        act.triggered.connect(lambda _on: self.uncommon(members))
        return menu

    def _select(self, names):
        for i in range(self.list.count()):
            it = self.list.item(i)
            it.setSelected(it.text() in names)

    def _fixed_range(self, ax) -> bool:
        return any(self.common.get(n) in self.axis_range for n in self._ax_groups.get(ax, []))

    def _kind_editable(self, name: str) -> bool:
        return name in (self.state.data.get("signals") or {}) or (
            self.state.resolved is not None and name in self.state.resolved.signals)

    def set_kind(self, names, kind: str):
        """Set the Kind of `names` (as in Selected signals); their stepped-line choice follows the Kind again."""
        sigs = self.state.data.setdefault("signals", {})
        for n in names:
            if n not in sigs:  # from an imported library: becomes the profile's own
                sigs[n] = self.state.resolved.signals[n].model_dump(exclude_none=True)
            sigs[n]["kind"] = kind
            self.step_choice.pop(n, None)
        if names:
            self.state.edited(structure=True)
            if self._axes:
                self.draw()  # a boolean moves to the bottom at once

    def kind(self, name: str) -> str:
        """The signal's Kind (Selected signals); derived signals count as continuous."""
        d = (self.state.data.get("signals") or {}).get(name)
        if d is None and self.state.resolved is not None and name in self.state.resolved.signals:
            return self.state.resolved.signals[name].kind
        return (d or {}).get("kind") or "continuous"

    def is_stepped(self, name: str) -> bool:
        """Drawn as a stepped line: the right-click choice, else any Kind but continuous."""
        if name in self.step_choice:
            return self.step_choice[name]
        return self.kind(name) != "continuous"

    def set_stepped(self, names, on: bool):
        for n in names:
            self.step_choice[n] = on
        if self._axes:
            self.draw()

    def _list_menu(self, pos):
        if self.list.ctrl_clicked:  # macOS Ctrl+click: it selected the row, no menu
            return
        label = self.list.header_at(pos)
        if label:
            self.axis_menu(label).exec(self.list.viewport().mapToGlobal(pos))
            return
        it = self.list.itemAt(pos)
        if it is None:
            return
        names = [i.text() for i in self.list.selectedItems()] if it.isSelected() else [it.text()]
        self.signal_menu(names).exec(self.list.viewport().mapToGlobal(pos))

    def subplot_menu(self, ax) -> QMenu:
        """Right-click on a subplot's signal names (right of the plot): one entry per signal."""
        names = list(dict.fromkeys(self._names.get(ax, [])))
        if len(names) == 1:
            return self.signal_menu(names)
        menu = QMenu(self)
        for n in names:
            self.signal_menu([n], menu.addMenu(n))
        return menu

    def _legend_axes(self, event):
        """The subplot whose signal list (right of it) is under a mouse event, or None."""
        return next((ax for ax in self._axes if ax.bbox.y0 <= event.y <= ax.bbox.y1 and event.x > ax.bbox.x1), None)

    def on_side_border(self, event) -> bool:
        """Mouse event over the border between the plots and the value list right of them."""
        if not self._axes or self.toolbar.mode or getattr(event, "x", None) is None:
            return False
        ratio = self.canvas.devicePixelRatioF()
        x1 = self._axes[0].bbox.x1
        top, bottom = self._axes[0].bbox.y1, self._axes[-1].bbox.y0
        return -4 * ratio <= event.x - x1 <= 10 * ratio and bottom <= event.y <= top

    def set_side_width(self, px: float):
        """Width of the value list right of the plots, in pixels (the plots keep at least 200)."""
        self.side_px = int(min(max(px, 120), max(self.canvas.width() - MARGIN_PX["left"] - 200, 120)))
        self._apply_layout()
        self._update_cursors()

    def _on_press(self, event):
        if event.button == 1 and self.on_side_border(event):
            self._side_drag = True
            return
        if event.button == 3 and not self.toolbar.mode and event.inaxes not in self._axes:
            ax = self._legend_axes(event)
            if ax is not None:
                self.subplot_menu(ax).exec(QCursor.pos())
            return
        if self.toolbar.mode or event.inaxes not in self._axes or event.xdata is None:
            return
        if event.button == 3:  # drag: zoom into the box; a plain right-click clears the cursors
            from matplotlib.patches import Rectangle
            ax = event.inaxes
            rect = Rectangle((event.xdata, event.ydata), 0, 0, fill=True, facecolor="#2a78d6", alpha=0.12,
                             edgecolor="#2a78d6", lw=0.8, zorder=6)
            ax.add_patch(rect)
            self._zoom = (ax, event.xdata, event.ydata, event.x, event.y, rect)
            self.canvas.setFocus()
            return
        if event.button != 1:
            return
        self._dragging = 1 if event.key == "shift" else 0
        self.set_cursor(self._dragging, event.xdata)

    def _on_motion(self, event):
        if self._zoom is not None:
            ax, x0, y0, _, _, rect = self._zoom
            x, y = ax.transData.inverted().transform((event.x, event.y))
            rect.set_bounds(min(x0, x), min(y0, y), abs(x - x0), abs(y - y0))
            self.canvas.draw_idle()
            return
        if self._side_drag:
            ratio = self.canvas.devicePixelRatioF()
            self.set_side_width((self.fig.bbox.width - event.x) / ratio)
            return
        if self.on_side_border(event):
            self.canvas.setCursor(Qt.SplitHCursor)
        elif self.canvas.cursor().shape() == Qt.SplitHCursor:
            self.canvas.unsetCursor()
        if self._dragging is not None and event.xdata is not None and event.inaxes in self._axes:
            self.set_cursor(self._dragging, event.xdata)

    def _on_release(self, event):
        self._dragging = None
        self._side_drag = False
        if self._zoom is not None:
            ax, x0, y0, px0, py0, rect = self._zoom
            self._zoom = None
            rect.remove()
            ratio = self.canvas.devicePixelRatioF()
            dx, dy = abs(event.x - px0) / ratio, abs(event.y - py0) / ratio
            if dx < 5 and dy < 5:
                self.clear_cursors()
                return
            x, y = ax.transData.inverted().transform((event.x, event.y))
            self.zoom_to(ax, (x0, x) if dx >= 5 else None, (y0, y) if dy >= 10 else None)

    def zoom_to(self, ax, xs=None, ys=None):
        """Zoom to a time range (every subplot) and/or a value range of `ax`; Ctrl+Z (⌘Z) goes back."""
        if self.toolbar._nav_stack() is None:
            self.toolbar.push_current()  # the full view, to come back to
        if xs is not None:
            ax.set_xlim(min(xs), max(xs))
        if ys is not None:
            ax.set_ylim(min(ys), max(ys))
        self.toolbar.push_current()
        self._update_cursors()

    def undo_zoom(self):
        """Back to the view before the last zoom (Ctrl+Z / ⌘Z on the plot)."""
        if self.toolbar._nav_stack._pos > 0:
            self.toolbar.back()
            self._update_cursors()

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
        self.cursor_info.setText("   ".join(info) if info else "Click a plot to place a cursor (Shift+click: second) · right-drag to zoom, Ctrl+Z to undo")
        self.canvas.draw_idle()

    def _draw_side_lists(self):
        """MDA-style list right of each subplot: a colour mark, the signal name and its value at the cursors."""
        from matplotlib.transforms import ScaledTranslation, blended_transform_factory

        a, b = self.cursors
        line_h = 11.5 / 72  # inches per text line (8 pt + leading)
        for ax in self._axes:
            base = blended_transform_factory(ax.transAxes, ax.transAxes)
            row = 0
            kpis = dict(self.kpi_lines.get(ax, {}))

            room = ax.get_position().height * self.fig.get_figheight() / line_h  # text rows the subplot fits

            def kpi_rows(sig, color):
                nonlocal row
                lines = kpis.pop(sig, [])
                for n, (text, ok) in enumerate(lines):
                    if row + 1 > room and n < len(lines):  # no room left: say how many are not shown
                        self._artists.append(ax.text(1, 1, f"… {len(lines) - n} more KPI(s)", transform=at(0.28, row),
                                                     fontsize=8, va="top", ha="left", color="#6b6b66",
                                                     clip_on=False))
                        row += 1
                        return
                    self._artists.append(ax.text(1, 1, text, transform=at(0.28, row), fontsize=8, va="top",
                                                 ha="left", color="#1f1f1e" if ok else "#d03b3b", clip_on=False))
                    row += 1

            def at(dx_in, rows):
                return base + ScaledTranslation(dx_in, -rows * line_h - 2 / 72, self.fig.dpi_scale_trans)

            names = self._names.get(ax, [])
            for i, ((label, t, y, enum), (_, color, ls, unit)) in enumerate(
                    zip(self._series.get(ax, []), self._legend.get(ax, []))):
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
                if i < len(names):
                    kpi_rows(names[i], color)  # the preview KPIs that read this signal
                row += 0.35

    def shutdown(self):
        self.queue = []
        for w in (self.worker, self.preview_worker):
            if w is not None:
                w.wait(5000)
