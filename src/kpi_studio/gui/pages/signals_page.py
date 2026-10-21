"""Signals side panel: pick signals from the channels found in the files, and define derived signals."""

from __future__ import annotations

import re
from typing import Optional

from PySide6.QtCore import QEvent, QPointF, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap, QPolygonF
from PySide6.QtWidgets import (
    QStyle,
    QAbstractItemView,
    QApplication,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QSplitter,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTableWidget,
    QTableWidgetItem,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from src.kpi_studio.core.model import derived_to_yaml, signal_units
from src.kpi_studio.core.rename import rename_signal_references
from src.kpi_studio.gui.state import AppState
from src.kpi_studio.gui.widgets import context_menu
from src.kpi_studio.gui.widgets.common import ExprEdit, HighlightDelegate, matches
from src.kpi_studio.gui.widgets.derived_builder import DerivedBuilder
from src.kpi_studio.gui.widgets.signal_viewer import KIND_TIPS, KINDS, SignalViewer
from src.kpi_studio.io.mf4 import ChannelInfo, match_channel
from src.kpi_studio.toolbox.units import COMMON_UNITS
from src.kpi_studio.toolbox.units import plain as plain_unit
from src.kpi_studio.toolbox.units import pretty as pretty_unit

LIB_COLOR = QColor("#6b6b66")


_DOTS: dict[bool, QIcon] = {}


def _dot_icon(on: bool) -> QIcon:
    """A small blue dot (or the same-sized blank, so the names stay lined up)."""
    if on not in _DOTS:
        pm = QPixmap(10, 10)
        pm.fill(Qt.transparent)
        if on:
            p = QPainter(pm)
            p.setRenderHint(QPainter.Antialiasing)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor("#007aff"))
            p.drawEllipse(2, 2, 6, 6)
            p.end()
        _DOTS[on] = QIcon(pm)
    return _DOTS[on]




class KindDelegate(QStyledItemDelegate):
    """The Kind column: a drop-down list, opened with one click."""

    ARROW = 18  # room kept right of the text for the arrow
    kinds = KINDS

    def sizeHint(self, option, index):
        hint = super().sizeHint(option, index)
        width = max(option.fontMetrics.horizontalAdvance(k) for k in self.kinds) + 12 + self.ARROW
        return QSize(max(hint.width(), width), hint.height())

    def paint(self, painter, option, index):
        r = option.rect
        text_opt = QStyleOptionViewItem(option)
        text_opt.rect = r.adjusted(0, 0, -self.ARROW, 0)
        super().paint(painter, text_opt, index)
        if option.state & QStyle.State_Selected:  # the arrow's strip keeps the selection colour
            painter.fillRect(r.adjusted(r.width() - self.ARROW, 0, 0, 0), option.palette.highlight())
        cx, cy = r.right() - 9, r.center().y() + 1
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(Qt.NoPen)
        painter.setBrush(option.palette.highlightedText() if option.state & QStyle.State_Selected
                         else QColor("#8e8e93"))
        painter.drawPolygon(QPolygonF([QPointF(cx - 4, cy - 2), QPointF(cx + 4, cy - 2), QPointF(cx, cy + 3)]))
        painter.restore()

    def createEditor(self, parent, option, index):
        box = QComboBox(parent)
        for i, k in enumerate(self.kinds):
            box.addItem(k)
            box.setItemData(i, KIND_TIPS[k], Qt.ToolTipRole)
        box.activated.connect(lambda _i, b=box: (self.commitData.emit(b), self.closeEditor.emit(b)))
        QTimer.singleShot(0, box.showPopup)
        return box

    def setEditorData(self, editor, index):
        text = str(index.data() or "continuous")
        editor.setCurrentIndex(self.kinds.index(text) if text in self.kinds else 0)

    def setModelData(self, editor, model, index):
        model.setData(index, editor.currentText())


class DerivedKindDelegate(KindDelegate):
    """Kind of a derived signal (an enum needs a value table, which derived signals don't have)."""

    kinds = ["continuous", "discrete", "boolean"]


UNIT_CHOICES = COMMON_UNITS
NO_UNIT = "(no unit)"


class UnitDelegate(KindDelegate):
    """The Unit column: a drop-down list of common units, opened with one click; another unit can be typed."""

    def sizeHint(self, option, index):
        hint = QStyledItemDelegate.sizeHint(self, option, index)
        width = max(option.fontMetrics.horizontalAdvance(u) for u in UNIT_CHOICES) + 12 + self.ARROW
        return QSize(max(hint.width(), width), hint.height())

    def createEditor(self, parent, option, index):
        box = QComboBox(parent)
        box.setEditable(True)  # a unit not in the list (e.g. %, bar) can still be typed
        box.setInsertPolicy(QComboBox.NoInsert)
        box.addItems([NO_UNIT] + [pretty_unit(u) for u in UNIT_CHOICES])
        box.setMaxVisibleItems(len(UNIT_CHOICES) + 2)
        box.activated.connect(lambda _i, b=box: (self.commitData.emit(b), self.closeEditor.emit(b)))
        QTimer.singleShot(0, box.showPopup)
        return box

    def setEditorData(self, editor, index):
        text = pretty_unit(str(index.data() or ""))
        if text and editor.findText(text) < 0:
            editor.insertItem(1, text)  # its own unit stays one click away
        editor.setCurrentText(text or NO_UNIT)

    def setModelData(self, editor, model, index):
        text = editor.currentText().strip()
        model.setData(index, "" if text == NO_UNIT else plain_unit(text))  # stored as m/s^2

    def displayText(self, value, locale):
        return pretty_unit(str(value or ""))


def _num(v) -> str:
    v = float(v)
    return str(int(v)) if v.is_integer() else repr(v)


def format_enum(enum) -> str:
    """'0 = Off, 3 = Served' for a value table; a table name as it is."""
    if isinstance(enum, dict):
        return ", ".join(f"{_num(v)} = {k}" for k, v in sorted(enum.items(), key=lambda kv: kv[1]))
    return enum or ""


def enum_summary(enum) -> str:
    """Short cell text: '8 values: Longitudinal Off, …' for a value table; a table name as it is."""
    if isinstance(enum, dict):
        first = min(enum.items(), key=lambda kv: kv[1])[0] if enum else ""
        return f"{len(enum)} values: {first}, …" if len(enum) > 1 else f"1 value: {first}"
    return enum or ""


def parse_enum(text: str):
    """Inverse of format_enum: a value table when the text has 'value = text' pairs, else a table name."""
    text = text.strip()
    if "=" not in text:
        return text or None
    out = {}
    for part in text.split(","):
        if "=" not in part:
            continue
        v, label = (x.strip() for x in part.split("=", 1))
        try:
            out[label] = int(float(v)) if float(v).is_integer() else float(v)
        except ValueError:
            continue
    return out or None


def _identifier(name: str) -> str:
    base = name.replace("\\", "/").split("/")[-1]
    ident = re.sub(r"[^A-Za-z0-9_]", "_", base).strip("_") or "signal"
    if ident[0].isdigit():
        ident = "s_" + ident
    return ident


class SignalsPage(QWidget):
    DER_COLS = ["Name", "Expression", "Unit", "Kind", "Description"]
    SIG_COLS = ["Name", "Channel candidates (first match wins)", "Unit", "Kind", "Enum", "Required", "Found in"]

    def __init__(self, state: AppState, parent=None, viewer: SignalViewer | None = None):
        super().__init__(parent)
        self.state = state
        self.viewer = viewer or SignalViewer(state)  # shared panel below the tabs (main window)
        self._loading = False

        split = QSplitter(Qt.Horizontal)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)  # shown in the "Signals" side panel, which has its own margins
        outer.setSpacing(6)
        outer.addWidget(split, 1)

        def title(text, tip=""):
            lab = QLabel(text)
            lab.setObjectName("sectionTitle")
            lab.setToolTip(tip)
            return lab

        def compact_table(table, min_rows):
            table.verticalHeader().setDefaultSectionSize(22)
            table.horizontalHeader().setFixedHeight(26)
            table.setMinimumHeight(26 + 22 * min_rows + 4)

        # ---- left: channels in the files
        left = QWidget()
        left.setToolTip("Signals in the loaded files. Pick the ones you need and give each a short name. When "
                        "files name the same signal differently, list all channel names; the first one found "
                        "in a file is used.")
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.setSpacing(6)
        head = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Filter… (wildcards: * ?  e.g. lmc*it)")
        head.addWidget(self.search, 1)
        for b in context_menu.plus_minus(add=self.add_selected_channels,
                                         add_tip="Add the selected channels as signals"):
            head.addWidget(b)
        ll.addLayout(head)
        self.tree = QTreeWidget()
        self.tree.setItemDelegateForColumn(0, HighlightDelegate(lambda: self.search.text(), self.tree))  # matches
        self.tree.setIconSize(QSize(10, 10))  # the dot in front of channels already added as signals
        self.tree.setHeaderLabels(["Signal", "Unit", "Files"])
        self.tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.tree.header().setStretchLastSection(False)
        self.tree.header().setSectionResizeMode(0, QHeaderView.Stretch)
        self.tree.header().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.tree.header().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        ll.addWidget(self.tree, 1)
        left.setMinimumWidth(220)
        split.addWidget(left)

        # ---- right: profile signals, derived signals
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(6)

        vsplit = QSplitter(Qt.Vertical)
        rl.addWidget(vsplit, 1)

        sig_box = QWidget()
        sl = QVBoxLayout(sig_box)
        sl.setContentsMargins(0, 0, 0, 0)
        sl.setSpacing(4)
        row = QHBoxLayout()
        row.addWidget(title("Selected signals", "Signals this profile reads from the files"))
        row.addStretch(1)
        for b in context_menu.plus_minus(remove=self.delete_signal, remove_tip="Delete the selected signals"):
            row.addWidget(b)
        sl.addLayout(row)
        self.sig_table = QTableWidget(0, len(self.SIG_COLS))
        self.sig_table.setHorizontalHeaderLabels(self.SIG_COLS)
        self.sig_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        compact_table(self.sig_table, 2)
        self.sig_table.setItemDelegateForColumn(2, UnitDelegate(self.sig_table))
        self.sig_table.setItemDelegateForColumn(3, KindDelegate(self.sig_table))
        self.sig_table.clicked.connect(lambda ix: ix.column() in (2, 3) and ix.flags() & Qt.ItemIsEditable
                                       and self.sig_table.edit(ix))
        sl.addWidget(self.sig_table, 1)
        vsplit.addWidget(sig_box)

        der_box = QWidget()
        dl = QVBoxLayout(der_box)
        dl.setContentsMargins(0, 0, 0, 0)
        dl.setSpacing(4)
        drow = QHBoxLayout()
        drow.addWidget(title("Derived signals", "Right-click to add or edit one: pick a signal and a processing "
                                                "method, or type an expression, e.g. lowpass(x, 10)"))
        dhint = QLabel("a signal + a method, or an expression")
        dhint.setObjectName("hint")
        drow.addWidget(dhint)
        drow.addStretch(1)
        for b in context_menu.plus_minus(add=self.add_derived, remove=self.delete_derived,
                                         add_tip="Add a derived signal: a signal + a method",
                                         remove_tip="Delete the selected derived signals"):
            drow.addWidget(b)
        dl.addLayout(drow)
        self.der_table = QTableWidget(0, len(self.DER_COLS))
        self.der_table.setHorizontalHeaderLabels(self.DER_COLS)
        self.der_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.der_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.Stretch)
        self.der_table.setColumnWidth(2, 80)
        compact_table(self.der_table, 1)
        self.der_table.setItemDelegateForColumn(2, UnitDelegate(self.der_table))
        self.der_table.setItemDelegateForColumn(3, DerivedKindDelegate(self.der_table))
        self.der_table.clicked.connect(lambda ix: ix.column() in (2, 3) and self.der_table.edit(ix))
        dl.addWidget(self.der_table, 1)
        vsplit.addWidget(der_box)

        vsplit.setSizes([300, 160])
        split.addWidget(right)
        split.setSizes([360, 740])

        self.search.textChanged.connect(self.refresh_channels)
        self.tree.itemDoubleClicked.connect(lambda *_: self.add_selected_channels())
        # right-click menus: every add / delete is in one
        context_menu.install(self.tree, item=lambda rows: [("Add as signal", self.add_selected_channels)])
        self.tree.setToolTip("Drag channels onto Selected signals, double-click one or right-click it to add "
                             "it as a signal")
        # drag channels from the tree and drop them on the Signals table
        self.tree.setDragEnabled(True)
        self.tree.setDragDropMode(QAbstractItemView.DragOnly)
        self.sig_table.setAcceptDrops(True)
        self.sig_table.viewport().setAcceptDrops(True)
        self.sig_table.viewport().installEventFilter(self)
        # and drag the table's own rows up or down to reorder the signals
        self.sig_table.setDragEnabled(True)
        self.sig_table.setDragDropMode(QAbstractItemView.DragDrop)
        self.sig_table.setDefaultDropAction(Qt.MoveAction)
        self.sig_table.setDropIndicatorShown(False)
        self._drop_line = QFrame(self.sig_table.viewport())
        self._drop_line.setStyleSheet("background: #007aff;")
        self._drop_line.hide()
        context_menu.install(self.sig_table,
                             item=lambda rows: [("Delete" if len(rows) == 1 else f"Delete {len(rows)} rows",
                                                 self.delete_signal)])
        context_menu.install(self.der_table, add=[("Add…", self.add_derived)], item=self._derived_actions)
        self.sig_table.itemChanged.connect(self.signal_cell_changed)
        self.sig_table.itemDoubleClicked.connect(self._enum_double_clicked)
        self.der_table.itemChanged.connect(self.derived_cell_changed)

        state.files_changed.connect(self.refresh_channels)
        state.files_changed.connect(self.refresh_signals)
        state.profile_changed.connect(self.refresh_all)
        # units / descriptions come from the files without asking: fill them whenever files or signals change
        state.files_changed.connect(self._schedule_fill)
        state.profile_changed.connect(self._schedule_fill)
        state.validated.connect(self.refresh_signals_if_imports)
        self._shown_resolved_sig = None
        self.last_rename = None
        self.refresh_all()

    # ------------------------------------------------------------------ channels
    def _channel_union(self) -> dict[str, dict]:
        union: dict[str, dict] = {}
        for f in self.state.checked_files:
            idx = self.state.indexes.get(f)
            if idx is None or not idx.ok:
                continue
            for c in idx.channels:
                key = f"{c.group_name}::{c.name}"
                e = union.setdefault(key, {"info": c, "files": set()})
                e["files"].add(f)
        return union

    def refresh_channels(self):
        needle = self.search.text().strip()
        union = self._channel_union()
        n_files = sum(1 for f in self.state.checked_files if self.state.indexes.get(f) and self.state.indexes[f].ok)
        self.tree.clear()
        groups: dict[str, QTreeWidgetItem] = {}
        shown = 0
        for key in sorted(union):
            e = union[key]
            c: ChannelInfo = e["info"]
            if needle and not (matches(needle, key) or matches(needle, c.name)):
                continue
            g = groups.get(c.group_name)
            if g is None:
                g = groups[c.group_name] = QTreeWidgetItem([c.group_name, "", ""])
                self.tree.addTopLevelItem(g)
            item = QTreeWidgetItem([c.name, pretty_unit(c.unit), f"{len(e['files'])}/{n_files}"])
            item.setData(0, Qt.UserRole, key)
            if len(e["files"]) < n_files:
                item.setForeground(2, QColor("#c27a00"))
            g.addChild(item)
            shown += 1
            if shown > 5000:
                break
        if needle or len(groups) < 8:
            self.tree.expandAll()
        self.mark_used_channels()

    def used_channel_keys(self) -> set[str]:
        """The channels the Signals table already reads (each signal's first matching channel)."""
        infos = [e["info"] for e in self._channel_union().values()]
        # the same tiers as match_channel, as lookups (files can hold many thousands of channels)
        tiers = (lambda c: c.name, lambda c: c.name.lower(), lambda c: c.clean.lower(), lambda c: c.base.lower())
        lookups = []
        for tier in tiers:
            first: dict[str, list] = {}
            for c in infos:
                first.setdefault(tier(c), []).append(c)
            lookups.append(first)
        used = set()
        for _name, d, _lib in self._rows():
            for cand in d.get("channels", []) or []:
                group, name = (s.strip() for s in cand.split("::", 1)) if "::" in cand else (None, cand.strip())
                hit = None
                for i, first in enumerate(lookups):
                    hits = [c for c in first.get(name if i == 0 else name.lower(), [])
                            if group is None or c.group_name.lower() == group.lower()]
                    if hits:
                        hit = hits[0]
                        break
                if hit is not None:
                    used.add(f"{hit.group_name}::{hit.name}")
                    break
        return used

    def mark_used_channels(self):
        """A small dot in front of each channel already added as a signal."""
        used = self.used_channel_keys()
        for g in range(self.tree.topLevelItemCount()):
            group = self.tree.topLevelItem(g)
            for i in range(group.childCount()):
                item = group.child(i)
                on = item.data(0, Qt.UserRole) in used
                item.setIcon(0, _dot_icon(on))
                item.setData(0, Qt.UserRole + 1, on)

    def eventFilter(self, obj, e):
        """Channels dragged from the tree are added as signals when dropped on the Signals table; the
        table's own rows dragged within it reorder the signals."""
        if obj is self.sig_table.viewport() and e.type() == QEvent.DragLeave:
            self._drop_line.hide()
        if obj is self.sig_table.viewport() and e.type() in (QEvent.DragEnter, QEvent.DragMove, QEvent.Drop):
            if e.source() is self.sig_table:
                e.setDropAction(Qt.MoveAction)
                e.accept()
                to = self.drop_row(int(e.position().y()))
                if e.type() == QEvent.Drop:
                    self._drop_line.hide()
                    self.move_signals(sorted({i.row() for i in self.sig_table.selectedIndexes()}), to)
                else:
                    self._show_drop_line(to)
                return True
            if e.source() is not self.tree:
                return False
            e.setDropAction(Qt.CopyAction)
            e.accept()
            if e.type() == QEvent.Drop:
                self.add_selected_channels()  # the dragged channels are the tree's selected ones
            return True
        return super().eventFilter(obj, e)

    def own_count(self) -> int:
        """The table lists the profile's own signals first, then those from libraries (not movable)."""
        return len(self.state.data.get("signals", {}) or {})

    def drop_row(self, y: int) -> int:
        """Where rows dropped at viewport height `y` go: before the row under it (after it in its lower
        half), never among the library signals."""
        t = self.sig_table
        r = t.rowAt(y)
        if r < 0:
            r = t.rowCount()
        elif y > t.rowViewportPosition(r) + t.rowHeight(r) // 2:
            r += 1
        return min(r, self.own_count())

    def _show_drop_line(self, row: int):
        t = self.sig_table
        y = t.rowViewportPosition(row) if row < t.rowCount() else (
            t.rowViewportPosition(row - 1) + t.rowHeight(row - 1) if row else 0)
        self._drop_line.setGeometry(0, max(0, y - 1), t.viewport().width(), 2)
        self._drop_line.show()
        self._drop_line.raise_()

    def move_signals(self, rows: list[int], to: int):
        """Move the signals in table `rows` so they sit before row `to`, keeping their order."""
        sigs = self.state.data.get("signals", {}) or {}
        names = list(sigs)
        moving = [names[r] for r in rows if r < len(names)]
        if not moving:
            return
        before = [n for n in names[:to] if n not in moving]
        after = [n for n in names[to:] if n not in moving]
        order = before + moving + after
        if order == names:
            return
        reordered = {n: sigs[n] for n in order}
        sigs.clear()
        sigs.update(reordered)
        self.state.edited(structure=True)
        sel = self.sig_table.selectionModel()
        sel.clearSelection()
        for r in range(len(before), len(before) + len(moving)):  # the moved rows stay selected
            sel.select(self.sig_table.model().index(r, 0), sel.SelectionFlag.Select | sel.SelectionFlag.Rows)

    def add_selected_channels(self):
        sigs = self.state.data.setdefault("signals", {})
        union = self._channel_union()
        added = 0
        for item in self.tree.selectedItems():
            key = item.data(0, Qt.UserRole)
            if not key:
                continue
            c = union[key]["info"]
            name = _identifier(c.name)
            base = name
            i = 2
            while name in sigs or name in self.state.data.get("derived", {}):
                name = f"{base}_{i}"
                i += 1
            sigs[name] = self._from_channel({"channels": [key]}, c)
            added += 1
        if added:
            self.state.edited(structure=True)

    @staticmethod
    def _from_channel(d: dict, c: ChannelInfo, *, only_empty: bool = False) -> dict:
        """Take unit, kind and description from the file's channel information."""
        if c.unit and not (only_empty and d.get("unit")):
            d["unit"] = c.unit
        if c.enum and not d.get("enum"):
            d["enum"] = {text: value for text, value in c.enum}  # the file's value table
            d["kind"] = "enum"
        if c.comment and not (only_empty and d.get("description")):
            d["description"] = c.comment
        return d

    def _schedule_fill(self):
        QTimer.singleShot(0, self.fill_from_files)

    def fill_from_files(self) -> list[str]:
        """Fill empty units and descriptions (and mark text-valued channels discrete) from the loaded files.

        Runs by itself whenever files are indexed or the signals change; never overwrites what the user set."""
        sigs = self.state.data.get("signals", {}) or {}
        channels = [c for f in self.state.checked_files if (idx := self.state.indexes.get(f)) is not None and idx.ok
                    for c in idx.channels]
        if not channels or not sigs:
            return []
        changed = []
        for name, d in sigs.items():
            before = dict(d)
            for cand in d.get("channels", []):
                c, _ = match_channel(cand, channels)
                if c is not None:
                    self._from_channel(d, c, only_empty=True)
                    break
            if d != before:
                changed.append(name)
        if changed:
            self.state.edited(structure=True)
            win = self.window()
            if hasattr(win, "statusBar"):
                win.statusBar().showMessage(f"Units / descriptions taken from the files for: {', '.join(changed)}",
                                            8000)
        return changed

    # ------------------------------------------------------------------ signals table
    def refresh_all(self):
        self._loading = True
        self._loading = False
        self.refresh_signals()
        self.refresh_derived()

    def refresh_signals_if_imports(self):
        # library signals appear only after the profile resolves; refresh when that set changes
        names = tuple(self.state.resolved.signals) if self.state.resolved else ()
        if names != self._shown_resolved_sig:
            self.refresh_signals()

    def _rows(self):
        own = self.state.data.get("signals", {}) or {}
        rows = [(n, d, False) for n, d in own.items()]
        lib_signals, _ = self.state.library_definitions()  # from the files: never stale after a rename
        for n, sdef in lib_signals.items():
            if n not in own:
                rows.append((n, sdef.model_dump(exclude_none=True), True))
        return rows

    def _found(self, sdict: dict) -> str:
        """In how many recordings (a combined recording counts once) any channel candidate exists."""
        idx = self.state.indexes
        recs = [[f for f in r.files if idx.get(f) and idx[f].ok] for r in self.state.recordings()]
        recs = [r for r in recs if r]
        if not recs:
            return ""
        hits = sum(1 for files in recs
                   if any(match_channel(c, idx[f].channels)[0] is not None
                          for c in sdict.get("channels", []) for f in files))
        return f"{hits}/{len(recs)}"

    def refresh_signals(self):
        self._loading = True
        rows = self._rows()
        self._shown_resolved_sig = tuple(self.state.resolved.signals) if self.state.resolved else ()
        self.sig_table.setRowCount(len(rows))
        for r, (name, d, from_lib) in enumerate(rows):
            vals = [name, ", ".join(d.get("channels", [])), d.get("unit") or "", d.get("kind", "continuous"),
                    enum_summary(d.get("enum")), "yes" if d.get("required", True) else "no", self._found(d)]
            for c, v in enumerate(vals):
                item = QTableWidgetItem(v)
                item.setData(Qt.UserRole, (name, from_lib))
                if c == 0 and d.get("description"):
                    item.setToolTip(d["description"])
                if c == 3:
                    item.setToolTip(KIND_TIPS.get(v, "") + "\n\nClick to change")
                if c == 4:
                    if isinstance(d.get("enum"), dict):
                        item.setFlags(item.flags() & ~Qt.ItemIsEditable)  # edited in the table dialog
                        item.setToolTip("\n".join(f"{_num(v)} = {k}" for k, v in
                                                  sorted(d["enum"].items(), key=lambda kv: kv[1]))
                                        + "\n\nDouble-click to see the values")
                    else:
                        item.setToolTip("Name of an enum table, or double-click with a value table to edit it")
                if from_lib:
                    item.setForeground(LIB_COLOR)
                if c in (0, 6) and (from_lib or c == 6):
                    item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                if c == 6 and v and v.split("/")[0] != v.split("/")[1]:
                    item.setForeground(QColor("#c27a00") if not d.get("required", True) else QColor("#e34948"))
                self.sig_table.setItem(r, c, item)
        self._loading = False
        self.mark_used_channels()

    def signal_cell_changed(self, item: QTableWidgetItem):
        if self._loading:
            return
        name, from_lib = item.data(Qt.UserRole)
        sigs = self.state.data.setdefault("signals", {})
        if from_lib and name not in sigs:
            sigs[name] = self.state.resolved.signals[name].model_dump(exclude_none=True)
        d = sigs[name]
        col, text = item.column(), item.text().strip()
        if col == 0:
            if text != name:
                if self._rename_ok(text):
                    self.state.data["signals"] = {(text if k == name else k): v for k, v in sigs.items()}
                    self._renamed(name, text)
            self.state.edited(structure=True)
            return
        if col == 1:
            d["channels"] = [c.strip() for c in text.split(",") if c.strip()]
        elif col == 2:
            d["unit"] = text or None
        elif col == 3:
            d["kind"] = text.lower() if text.lower() in KINDS else "continuous"
            self.viewer.draw()  # stepped lines and boolean strips follow at once
        elif col == 4:
            d["enum"] = parse_enum(text)
        elif col == 5:
            d["required"] = text.lower() not in ("no", "false", "0", "n")
        self.state.edited(structure=from_lib)

    def _enum_double_clicked(self, item):
        if item.column() != 4:
            return
        name, from_lib = item.data(Qt.UserRole)
        sigs = self.state.data.setdefault("signals", {})
        current = (sigs.get(name) or {}).get("enum") if not from_lib or name in sigs else None
        if from_lib and name not in sigs:
            current = self.state.resolved.signals[name].enum if self.state.resolved is not None else None
        if not isinstance(current, dict):
            return  # a table name: edited in the cell
        EnumDialog(name, current, self).exec()

    def _derived_actions(self, rows):
        actions = []
        if len(rows) == 1:
            name = self.der_table.item(next(iter(rows)), 0).data(Qt.UserRole)
            actions.append(("Edit…", lambda: self.edit_derived(name)))
        actions.append(("Delete" if len(rows) == 1 else f"Delete {len(rows)} rows", self.delete_derived))
        return actions

    def delete_signal(self):
        rows = sorted({i.row() for i in self.sig_table.selectedIndexes()}, reverse=True)
        sigs = self.state.data.get("signals", {})
        changed = False
        for r in rows:
            name, from_lib = self.sig_table.item(r, 0).data(Qt.UserRole)
            if name in sigs:
                del sigs[name]
                changed = True
            elif from_lib:
                QMessageBox.information(self, "Library signal",
                                        f"'{name}' comes from an imported library; remove the library to drop it.")
        if changed:
            self.state.edited(structure=True)

    def shutdown(self):
        self.viewer.shutdown()

    # ------------------------------------------------------------------ derived
    @staticmethod
    def _der_fields(value) -> dict:
        """{expr, unit, description, kind} from either YAML form (plain expression or mapping)."""
        if isinstance(value, dict):
            return {"expr": value.get("expr", "") or "", "unit": value.get("unit") or "",
                    "description": value.get("description", "") or "", "kind": value.get("kind") or ""}
        return {"expr": value or "", "unit": "", "description": "", "kind": ""}

    def _set_der_field(self, name, key, text):
        derived = self.state.data.setdefault("derived", {})
        if name not in derived:
            return False
        f = self._der_fields(derived[name])
        if f[key] == text:
            return False
        f[key] = text
        derived[name] = derived_to_yaml(f)
        return True

    def refresh_derived(self):
        self._loading = True
        derived = self.state.data.get("derived", {}) or {}
        self.der_table.setRowCount(len(derived))
        for r, (name, value) in enumerate(derived.items()):
            f = self._der_fields(value)
            item = QTableWidgetItem(name)
            item.setData(Qt.UserRole, name)
            item.setToolTip(f["description"])
            self.der_table.setItem(r, 0, item)
            edit = ExprEdit(self._names_except(name), enums_fn=self.state.enum_map, units_fn=self._units,
                            build_fn=lambda _e, n=name: self.edit_derived(n),
                            build_label="Build with a method… (signal, method, arguments)")
            edit.setText(f["expr"])
            edit.editingFinished.connect(lambda n=name, e=edit: self._derived_expr(n, e.text()))
            self.der_table.setCellWidget(r, 1, edit)
            unit = QTableWidgetItem(f["unit"])
            unit.setToolTip("Unit shown in the signal viewer and plots, e.g. km/h")
            self.der_table.setItem(r, 2, unit)
            kind = QTableWidgetItem(self.state.derived_kind(name))
            kind.setToolTip(KIND_TIPS.get(kind.text(), "") + ("" if f["kind"] else "\n(from the expression)")
                            + "\n\nClick to change")
            if not f["kind"]:
                kind.setForeground(QColor("#8e8e93"))  # not set: what the expression gives
            self.der_table.setItem(r, 3, kind)
            desc = QTableWidgetItem(f["description"])
            desc.setToolTip(f["description"] or "What this signal means (free text)")
            self.der_table.setItem(r, 4, desc)
        self._loading = False

    def _names_except(self, name):
        return lambda: [n for n in self.state.signal_names() if n != name]

    def _derived_expr(self, name, text):
        if self._set_der_field(name, "expr", text.strip()):
            self.state.edited()

    def derived_cell_changed(self, item):
        if self._loading:
            return
        name = self.der_table.item(item.row(), 0).data(Qt.UserRole)
        if item.column() == 0:
            new = item.text().strip()
            derived = self.state.data.setdefault("derived", {})
            if new != name and self._rename_ok(new):
                self.state.data["derived"] = {(new if k == name else k): v for k, v in derived.items()}
                self._renamed(name, new)
                self.state.edited(structure=True)
            elif new != name:
                QTimer.singleShot(0, self.refresh_derived)  # put the old name back
        elif item.column() == 3:
            kind = item.text().strip()
            if kind == self.state.derived_kind(name, inferred=True):
                kind = ""  # the same as the expression gives: keep following it
            if self._set_der_field(name, "kind", kind):
                self.state.edited()
            QTimer.singleShot(0, self.refresh_derived)
        elif item.column() in (2, 4):
            key = "unit" if item.column() == 2 else "description"
            if self._set_der_field(name, key, item.text().strip()):
                self.state.edited()

    def _units(self) -> dict:
        units = {n: (d.get("unit") or "") for n, d in (self.state.data.get("signals", {}) or {}).items()}
        if self.state.resolved is not None:
            units = {**signal_units(self.state.resolved), **units}
        for n, v in (self.state.data.get("derived", {}) or {}).items():
            units[n] = self._der_fields(v)["unit"]
        return units

    def open_builder(self, name: str = "") -> Optional[dict]:
        """Run the derived-signal dialog; returns its values, or None when cancelled."""
        derived = self.state.data.get("derived", {}) or {}
        f = self._der_fields(derived.get(name)) if name else {"expr": "", "unit": "", "description": "", "kind": ""}
        dlg = DerivedBuilder(self.state.signal_names(), self._units(), self, name=name, expr=f["expr"],
                             unit=f["unit"], description=f["description"],
                             taken=set(self.state.signal_names()) | set(derived), enums_fn=self.state.enum_map)
        return {**dlg.values(), "kind": f["kind"]} if dlg.exec() == DerivedBuilder.Accepted else None

    def add_derived(self):
        v = self.open_builder()
        if v is None:
            return
        self.state.data.setdefault("derived", {})[v["name"]] = derived_to_yaml(v)
        self.state.edited(structure=True)

    def edit_derived(self, name: Optional[str] = None):
        if name is None:
            row = self.der_table.currentRow()
            if row < 0:
                return
            name = self.der_table.item(row, 0).data(Qt.UserRole)
        v = self.open_builder(name)
        if v is None:
            return
        derived = self.state.data.setdefault("derived", {})
        # keep the table order when renaming
        self.state.data["derived"] = {(v["name"] if k == name else k): (derived_to_yaml(v) if k == name else d)
                                      for k, d in derived.items()}
        if v["name"] != name:
            self._renamed(name, v["name"])
        self.state.edited(structure=True)

    def _rename_ok(self, new: str) -> bool:
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", new):
            QMessageBox.warning(self, "Rename", f"'{new}' is not a valid name: use letters, digits and _, "
                                               "not starting with a digit.")
            return False
        if new in self.state.taken_names():
            QMessageBox.warning(self, "Rename", f"'{new}' is already a signal or derived signal.")
            return False
        return True

    def _renamed(self, old: str, new: str):
        """Point every expression, moment, KPI and plot that used `old` at `new`."""
        n = rename_signal_references(self.state.data, old, new)
        self.last_rename = (old, new, n)
        win = self.window()
        if n and hasattr(win, "statusBar"):
            win.statusBar().showMessage(f"Renamed {old} → {new}: updated {n} place{'s' if n != 1 else ''} "
                                        f"in derived signals, events, KPIs and plots", 10000)

    def delete_derived(self):
        rows = sorted({i.row() for i in self.der_table.selectedIndexes()}, reverse=True)
        derived = self.state.data.get("derived", {})
        for r in rows:
            derived.pop(self.der_table.item(r, 0).data(Qt.UserRole), None)
        if rows:
            self.state.edited(structure=True)


class EnumDialog(QDialog):
    """The value table of an enum signal, one value per row (raw value | text). View only: the values
    come from the file, so there is nothing to add, delete or edit."""

    def __init__(self, signal: str, enum: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Enum values of {signal}")
        lay = QVBoxLayout(self)
        info = QLabel("Use the text in conditions, e.g. " + signal + ' == "' + (next(iter(enum), "") or "TEXT") + '"')
        info.setWordWrap(True)
        info.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(info)
        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["Value", "Text"])
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(24)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setContextMenuPolicy(Qt.NoContextMenu)
        for text, value in sorted(enum.items(), key=lambda kv: kv[1]):
            self._add(_num(value), text)
        lay.addWidget(self.table, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)
        self._info = info
        self._fitted = False

    def showEvent(self, e):
        if not self._fitted:  # once styled (fonts and paddings are final then)
            self._fitted = True
            self._fit()
        super().showEvent(e)

    def _fit(self):
        """As large as the values and their full texts need (within the screen)."""
        t = self.table
        t.ensurePolished()
        h_head = t.horizontalHeader()
        widths = [max(t.sizeHintForColumn(c), h_head.sectionSizeHint(c)) + 8 for c in range(2)]
        t.setColumnWidth(0, widths[0])
        frame = 2 * t.frameWidth()
        rows_h = sum(t.rowHeight(r) for r in range(t.rowCount())) or t.verticalHeader().defaultSectionSize()
        table_w = sum(widths) + frame
        table_h = h_head.sizeHint().height() + rows_h + frame + 2
        lay = self.layout()
        m = lay.contentsMargins()
        screen = (self.screen() or QApplication.primaryScreen()).availableGeometry()
        w = min(max(table_w + m.left() + m.right(), 320), int(screen.width() * 0.9))
        info_h = self._info.heightForWidth(w - m.left() - m.right())
        buttons = self.findChild(QDialogButtonBox).sizeHint().height()
        h = m.top() + info_h + lay.spacing() + table_h + lay.spacing() + buttons + m.bottom()
        t.setMinimumSize(table_w if w >= table_w + m.left() + m.right() else 0, 0)
        self.resize(w, min(h, int(screen.height() * 0.85)))

    def _add(self, value: str, text: str):
        r = self.table.rowCount()
        self.table.insertRow(r)
        v = QTableWidgetItem(value)
        self.table.setItem(r, 0, v)
        self.table.setItem(r, 1, QTableWidgetItem(text))

    def values(self) -> dict:
        out = {}
        for r in range(self.table.rowCount()):
            v, t = self.table.item(r, 0), self.table.item(r, 1)
            text = t.text().strip() if t else ""
            try:
                num = float(v.text()) if v else None
            except ValueError:
                num = None
            if text and num is not None:
                out[text] = int(num) if num.is_integer() else num
        return out
