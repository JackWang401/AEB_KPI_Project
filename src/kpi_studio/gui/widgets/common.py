"""Reusable widgets: validated expression field, tool palette, condition builder, plot canvas."""

from __future__ import annotations

import fnmatch
import re
from typing import Callable, Optional

import yaml
from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QFocusEvent, QIcon, QPainter, QPalette, QPixmap, QPolygonF
from PySide6.QtWidgets import (
    QApplication,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QComboBox,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QWidgetAction,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from src.kpi_studio import toolbox
from src.kpi_studio.core.expr import ExprError, compile_expr
from src.kpi_studio.gui.widgets.context_menu import plus_minus

# outer padding of every page (left, top, right, bottom), macOS-style generous margins
PAGE_MARGINS = (20, 16, 20, 16)

def matches(needle: str, text: str) -> bool:
    """Case-insensitive search: plain text matches anywhere; with * or ? it is a wildcard
    pattern, also anywhere in the text ('lmc*it' finds 'LmcAxTrgtLimit')."""
    needle = (needle or "").strip().lower()
    if not needle:
        return True
    text = text.lower()
    if any(c in needle for c in "*?["):
        return fnmatch.fnmatchcase(text, f"*{needle}*")
    return needle in text


def match_spans(needle: str, text: str) -> list[tuple[int, int]]:
    """Where `needle` matches in `text`, as (start, end) pairs, the way `matches` searches: every
    occurrence of plain text; for a wildcard pattern its first (shortest) match."""
    needle = (needle or "").strip()
    if not needle or not text:
        return []
    if any(c in needle for c in "*?["):
        core = needle.strip("*")  # (a leading or trailing * adds nothing to highlight)
        if not core:
            return []
        rx = "".join(".*?" if c == "*" else "." if c == "?" else re.escape(c) for c in core)
        m = re.search(rx, text, re.IGNORECASE)
        return [m.span()] if m and m.end() > m.start() else []
    return [m.span() for m in re.finditer(re.escape(needle), text, re.IGNORECASE)]


class HighlightDelegate(QStyledItemDelegate):
    """Draws an item's text with the parts matching the search (`needle_fn()`) highlighted."""

    COLOR = QColor("#ffe08a")

    def __init__(self, needle_fn: Callable[[], str], parent=None):
        super().__init__(parent)
        self.needle_fn = needle_fn

    def paint(self, painter, option, index):
        spans = match_spans(self.needle_fn(), str(index.data(Qt.DisplayRole) or ""))
        if not spans:
            return super().paint(painter, option, index)
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        text = opt.text
        opt.text = ""
        widget = opt.widget
        style = widget.style() if widget else QApplication.style()
        style.drawControl(QStyle.CE_ItemViewItem, opt, painter, widget)
        rect = style.subElementRect(QStyle.SE_ItemViewItemText, opt, widget).adjusted(3, 0, -3, 0)
        fm = opt.fontMetrics
        shown = fm.elidedText(text, Qt.ElideRight, rect.width())
        cut = len(shown) - 1 if shown != text else len(shown)  # (before the …)
        painter.save()
        painter.setFont(opt.font)
        y = rect.top() + (rect.height() - fm.height()) // 2
        marked = []
        for start, end in spans:
            start, end = min(start, cut), min(end, cut)
            if end <= start:
                continue
            x0 = rect.left() + fm.horizontalAdvance(shown[:start])
            w = fm.horizontalAdvance(shown[start:end])
            painter.setPen(Qt.NoPen)
            painter.setBrush(self.COLOR)
            painter.drawRoundedRect(QRectF(x0, y, w, fm.height()), 2, 2)  # (exact: neighbours stay readable)
            marked.append((x0, start, end))
        selected = bool(opt.state & QStyle.State_Selected)
        painter.setPen(opt.palette.color(QPalette.HighlightedText if selected else QPalette.Text))
        painter.drawText(rect, int(Qt.AlignLeft | Qt.AlignVCenter), shown)
        if selected:  # dark text on the highlight, also in a selected (blue) row
            painter.setPen(QColor("#1d1d1f"))
            for x0, start, end in marked:
                painter.drawText(rect.adjusted(x0 - rect.left(), 0, 0, 0), int(Qt.AlignLeft | Qt.AlignVCenter),
                                 shown[start:end])
        painter.restore()


OK_STYLE = ""
BAD_STYLE = "QLineEdit { border: 1px solid #e34948; background: #fdf1f1; }"


def _arrow_icon() -> QIcon:
    pm = QPixmap(16, 16)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor("#6b6b66"))
    p.drawPolygon(QPolygonF([QPointF(3, 6), QPointF(13, 6), QPointF(8, 11)]))
    p.end()
    return QIcon(pm)


def enum_tokens(members: dict) -> list[str]:
    """An enum's texts as they are written in a condition (quoted unless a plain name), in value order."""
    return [m if m.isidentifier() else f'"{m}"' for m, _ in sorted(members.items(), key=lambda kv: kv[1])]


class NamePicker(QMenu):
    """Popup with a filter box and a list of names; emits the picked name."""

    picked = Signal(str)

    def __init__(self, sections: dict[str, list[str]], parent=None, actions: Optional[dict] = None):
        super().__init__(parent)
        self.actions_map = dict(actions or {})
        box = QWidget()
        lay = QVBoxLayout(box)
        lay.setContentsMargins(6, 6, 6, 6)
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("Filter… (wildcards: * ?)")
        self.list = QListWidget()
        self.list.setMinimumSize(280, 260)
        for label in self.actions_map:
            it = QListWidgetItem("➜ " + label)
            it.setData(Qt.UserRole + 1, label)
            it.setForeground(QColor("#2a78d6"))
            self.list.addItem(it)
        for title, names in sections.items():
            if not names:
                continue
            head = QListWidgetItem(title)
            head.setFlags(Qt.NoItemFlags)
            f = head.font()
            f.setBold(True)
            head.setFont(f)
            self.list.addItem(head)
            for n in names:
                it = QListWidgetItem("   " + n)
                it.setData(Qt.UserRole, n)
                self.list.addItem(it)
        lay.addWidget(self.filter)
        lay.addWidget(self.list)
        act = QWidgetAction(self)
        act.setDefaultWidget(box)
        self.addAction(act)
        self.filter.textChanged.connect(self._apply)
        self.filter.returnPressed.connect(self._pick_first)
        self.list.itemClicked.connect(self._pick)
        self.list.itemActivated.connect(self._pick)
        self.aboutToShow.connect(self.filter.setFocus)

    def names(self) -> list[str]:
        return [self.list.item(i).data(Qt.UserRole) for i in range(self.list.count())
                if self.list.item(i).data(Qt.UserRole) and not self.list.item(i).isHidden()]

    def _apply(self, text):
        for i in range(self.list.count()):
            it = self.list.item(i)
            name = it.data(Qt.UserRole)
            if name:
                it.setHidden(not matches(text, name))

    def _pick_first(self):
        names = self.names()
        if names:
            self.picked.emit(names[0])
            self.close()

    def _pick(self, item):
        action = item.data(Qt.UserRole + 1)
        if action:
            self.close()
            self.actions_map[action]()
            return
        name = item.data(Qt.UserRole)
        if name:
            self.picked.emit(name)
            self.close()


class ExprEdit(QLineEdit):
    """Line edit that validates an expression as you type against the current signal names.

    The ▾ button at its right end opens a searchable list of signals (and any extra names,
    e.g. moments) and inserts the picked one at the cursor."""

    focused = Signal(object)

    def __init__(self, names_fn: Callable[[], list[str]], parent=None, *, allow_empty=True, placeholder="",
                 enums_fn: Optional[Callable[[], dict]] = None,
                 extras_fn: Optional[Callable[[], dict]] = None,
                 build_fn: Optional[Callable[["ExprEdit"], None]] = None,
                 build_label: str = "Build condition… (compare, changes to / from)"):
        super().__init__(parent)
        self.build_fn = build_fn
        self.build_label = build_label
        self.names_fn = names_fn
        self.enums_fn = enums_fn or (lambda: {})
        self.extras_fn = extras_fn or (lambda: {})
        self.allow_empty = allow_empty
        self.error: Optional[str] = None
        self.setPlaceholderText(placeholder)
        self.textChanged.connect(self.check)
        self.pick_action = QAction(_arrow_icon(), "Pick a signal", self)
        self.pick_action.setToolTip("Pick a signal to insert")
        self.pick_action.triggered.connect(self.open_picker)
        self.addAction(self.pick_action, QLineEdit.TrailingPosition)
        self.picker: Optional[NamePicker] = None

    def picker_sections(self) -> dict[str, list[str]]:
        sections = {}
        for name in self.enum_signals_used():  # e.g. `state == ` typed: its values come first
            sections[f"Values of {name}"] = enum_tokens(self.enums_fn()[name])
        sections["Signals"] = list(self.names_fn())
        sections.update(self.extras_fn() or {})
        return sections

    def enum_signals_used(self) -> list[str]:
        """The enum signals written in the text, the one nearest before the cursor first."""
        enums = self.enums_fn() or {}
        if not enums:
            return []
        text, pos = self.text(), self.cursorPosition()
        found = [(m.start(), m.group()) for m in re.finditer(r"[A-Za-z_]\w*", text) if m.group() in enums]
        before = [n for at, n in sorted(found, reverse=True) if at < pos]
        after = [n for at, n in found if at >= pos]
        return list(dict.fromkeys(before + after))

    def open_picker(self):
        actions = {}
        if self.build_fn is not None:
            actions[self.build_label] = lambda: self.build_fn(self)
        self.picker = NamePicker(self.picker_sections(), self, actions=actions)
        self.picker.picked.connect(self.insert_name)
        self.picker.popup(self.mapToGlobal(self.rect().bottomLeft()))

    def insert_name(self, name: str):
        """Insert `name` at the cursor (replacing a selection), with spaces around it as needed."""
        text, pos = self.text(), self.cursorPosition()
        if self.hasSelectedText():
            start = self.selectionStart()
            text = text[:start] + text[start + len(self.selectedText()):]
            pos = start
        before, after = text[:pos], text[pos:]
        lead = " " if before and not before.endswith((" ", "(", ",")) else ""
        trail = " " if after and not after.startswith((" ", ")", ",")) else ""
        self.setText(before + lead + name + trail + after)
        self.setCursorPosition(len(before + lead + name))
        self.setFocus()
        self.editingFinished.emit()

    def focusInEvent(self, event: QFocusEvent):
        super().focusInEvent(event)
        self.focused.emit(self)

    def check(self):
        text = self.text().strip()
        if not text:
            self.error = None if self.allow_empty else "required"
        else:
            try:
                compile_expr(text, names=set(self.names_fn()) | {"time"}, enums=self.enums_fn())
                self.error = None
            except ExprError as exc:
                self.error = str(exc)
        self.setStyleSheet(BAD_STYLE if self.error else OK_STYLE)
        self.setToolTip(self.error or "")
        return self.error is None

    def insert_text(self, snippet: str):
        self.insert(snippet)
        self.setFocus()


def parse_options(text: str) -> dict:
    """'after: start, within: 1.5' -> dict (YAML flow mapping body)."""
    text = (text or "").strip()
    if not text:
        return {}
    data = yaml.safe_load("{" + text + "}")
    if not isinstance(data, dict):
        raise ValueError("options must look like  key: value, key: value")
    return data


def format_options(opts: dict) -> str:
    parts = []
    for k, v in (opts or {}).items():
        if isinstance(v, (list, tuple)):
            v = "[" + ", ".join(str(x) for x in v) + "]"
        parts.append(f"{k}: {v}")
    return ", ".join(parts)


class ToolPalette(QWidget):
    """Searchable toolbox browser. Double-click inserts a call into the last focused ExprEdit."""

    insert_requested = Signal(str)

    def __init__(self, kinds=("signal", "condition", "moment", "reducer", "event"), parent=None,
                 names_fn: Optional[Callable[[], list[str]]] = None):
        super().__init__(parent)
        self.kinds = kinds
        self.names_fn = names_fn
        self._names: list[str] = []
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        title = QLabel("Toolbox")
        title.setObjectName("sectionTitle")
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search signals and tools…")
        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setToolTip("Hover a tool to read what it does; double-click to insert it")
        lay.addWidget(title)
        lay.addWidget(self.search)
        lay.addWidget(self.tree, 1)
        self.search.textChanged.connect(self.populate)
        self.tree.itemDoubleClicked.connect(self._insert)
        self.populate()

    def refresh_names(self):
        """Re-list the profile's signals when they changed (keeps the tree's expansion otherwise)."""
        names = list(self.names_fn()) if self.names_fn else []
        if names != self._names:
            self.populate()

    def populate(self):
        needle = self.search.text().strip().lower()
        self.tree.clear()
        self._names = list(self.names_fn()) if self.names_fn else []
        shown = [n for n in self._names if matches(needle, n)]
        if self.names_fn is not None:
            top = QTreeWidgetItem([f"Signals ({len(self._names)})"])
            top.setToolTip(0, "Double-click a signal to insert its name into the field you are editing")
            self.tree.addTopLevelItem(top)
            for n in shown:
                item = QTreeWidgetItem([n])
                item.setData(0, Qt.UserRole + 1, n)
                item.setToolTip(0, self.doc_html(item))
                top.addChild(item)
            if not self._names:
                top.addChild(QTreeWidgetItem(["(add signals in the Signals panel)"]))
            top.setExpanded(True)
        labels = {"signal": "Signal tools", "condition": "Conditions", "moment": "Moments (rules)",
                  "reducer": "Reducers", "event": "Event presets"}
        for kind in self.kinds:
            specs = [s for s in toolbox.tools(kind) if matches(needle, s.name)
                     or (not any(c in needle for c in "*?[") and (needle in s.doc.lower() or needle in s.category.lower()))]
            if not specs:
                continue
            top = QTreeWidgetItem([labels[kind]])
            self.tree.addTopLevelItem(top)
            cats: dict = {}
            for s in specs:
                cat = cats.get(s.category)
                if cat is None:
                    cat = cats[s.category] = QTreeWidgetItem([s.category + (" (user)" if s.origin == "user" else "")])
                    top.addChild(cat)
                item = QTreeWidgetItem([s.signature])
                item.setData(0, Qt.UserRole, s)
                item.setToolTip(0, self.doc_html(item))
                cat.addChild(item)
            top.setExpanded(bool(needle) or kind in ("signal", "condition"))
            for c in cats.values():
                c.setExpanded(bool(needle))

    @staticmethod
    def doc_html(item) -> str:
        """The help shown when hovering a toolbox entry (no separate help box)."""
        spec = item.data(0, Qt.UserRole) if item else None
        name = item.data(0, Qt.UserRole + 1) if item else None
        if name:
            return f"<b>{name}</b><br>Signal defined in the Signals panel. Double-click to insert it."
        if spec is None:
            return ""
        params = "".join(
            f"<li><b>{p.name}</b> ({p.type.__name__}{', default ' + repr(p.default) if not p.required else ''})"
            f"{': ' + p.doc if p.doc else ''}</li>" for p in spec.params)
        return (f"<p style='white-space:normal; width:360px'><b>{spec.signature}</b><br>"
                f"{spec.doc.replace(chr(10), '<br>')}" + (f"<ul>{params}</ul>" if params else "")
                + f"<i>{spec.kind} · {spec.category} · double-click to insert</i></p>")

    def _insert(self, item, _col=0):
        name = item.data(0, Qt.UserRole + 1)
        if name:
            self.insert_requested.emit(name)
            return
        spec = item.data(0, Qt.UserRole)
        if spec is None:
            return
        if spec.kind in ("signal", "condition"):
            args = list(spec.input_names) or (["x"] if spec.inputs != -1 else ["a", "b"])
            args += [str(p.example_value()) if p.required else f"{p.name}={p.default!r}" for p in spec.params
                     if p.required]
            self.insert_requested.emit(f"{spec.name}({', '.join(args)})")
        else:
            self.insert_requested.emit(spec.name)


class ConditionBuilder(QDialog):
    """Build `a > 1 and b == FULL` from rows, without typing syntax. Each row can open and close
    brackets and says how it joins the row above (and / or), so `(a > 1 and b == 2) or not (c < 3)`
    can be built too."""

    OPS = ["==", "!=", ">", ">=", "<", "<=", "in", "changes to", "changes from", "changes from → to"]
    HINTS = {"in": "list, e.g. 1, 2", "changes to": "value, e.g. 3", "changes from": "value, e.g. 3",
             "changes from → to": "from, to   e.g. 0, 3"}

    MULTI = ("in", "changes from → to")  # operators that take several values
    OPENS = ["", "(", "((", "not", "not (", "not (("]
    CLOSES = ["", ")", "))"]

    def __init__(self, names: list[str], parent=None, initial: str = "", enums: Optional[dict] = None):
        super().__init__(parent)
        self.setWindowTitle("Build condition")
        self.names = names
        self.enums = enums or {}  # signal -> {TEXT: value}: their texts can be picked as values
        self.value_picker: Optional[NamePicker] = None
        self.rows: list[tuple] = []
        lay = QVBoxLayout(self)
        info = QLabel("Each row is  signal  operator  value.  For 'in' give a list, e.g. 1, 2 or FULL, PARTIAL. "
                      "'changes to / from' are true only at the sample where the signal switches to or away "
                      "from the value; 'changes from → to' takes two values, e.g. 0, 3.\n"
                      "Join each row to the one above with and / or, and group rows with the brackets before "
                      "and after them (or negate them with not), e.g. (a and b) or not (c). Without brackets, "
                      "and is applied before or.\n"
                      "Right-click a row's operator, or the space around the rows, to add or delete rows.")
        info.setWordWrap(True)
        lay.addWidget(info)
        top = QHBoxLayout()  # + / − in the top right corner
        top.addStretch(1)
        for b in plus_minus(add=self.add_row, remove=lambda: self.delete_row(len(self.rows) - 1),
                            add_tip="Add a row", remove_tip="Delete the last row"):
            top.addWidget(b)
        lay.addLayout(top)
        self.rows_box = QVBoxLayout()
        lay.addLayout(self.rows_box)
        self.preview = QLabel()
        self.preview.setWordWrap(True)
        lay.addWidget(self.preview)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        self.ok_button = buttons.button(QDialogButtonBox.Ok)
        lay.addWidget(buttons)
        self.links: list[tuple] = []  # per row: (join with the row above, opening, closing brackets)
        self.resize(760, 10)
        self.add_row()
        if initial:
            self.preview.setText(f"Current: {initial}")

    def add_row(self):
        sig = QComboBox()
        sig.setEditable(True)
        sig.addItems(self.names)
        op = QComboBox()
        op.addItems(self.OPS)
        val = QLineEdit()
        val.setPlaceholderText("value")
        pick = QAction(_arrow_icon(), "Pick a value", val)
        pick.setToolTip("Pick one of the signal's enum values")
        val.addAction(pick, QLineEdit.TrailingPosition)
        pick.triggered.connect(lambda _=False, v=val: self.open_values(v))
        val.pick_action = pick
        op.currentTextChanged.connect(lambda _o, s=sig, o=op, v=val: self._update_value_field(s, o, v))
        sig.currentTextChanged.connect(lambda _t, s=sig, o=op, v=val: self._update_value_field(s, o, v))
        self._update_value_field(sig, op, val)
        join, opening, closing = QComboBox(), QComboBox(), QComboBox()
        join.addItems(["and", "or"])
        join.setToolTip("How this row joins the row above")
        opening.addItems(self.OPENS)
        opening.setToolTip("Open brackets before this row (not: negate it)")
        closing.addItems(self.CLOSES)
        closing.setToolTip("Close brackets after this row")
        policy = join.sizePolicy()
        policy.setRetainSizeWhenHidden(True)  # the first row has no join, but lines up with the others
        join.setSizePolicy(policy)
        join.setVisible(bool(self.rows))
        row = QHBoxLayout()
        for w in (join, opening, sig, op, val, closing):
            row.addWidget(w, 1 if w is val else 0)
            if hasattr(w, "currentTextChanged"):
                w.currentTextChanged.connect(self.update_preview)
            else:
                w.textChanged.connect(self.update_preview)
        self.rows_box.addLayout(row)
        self.rows.append((sig, op, val))
        self.links.append((join, opening, closing))
        self.update_preview()

    def values_of(self, signal: str) -> list[str]:
        """The enum texts of `signal` as they are written in a condition, in value order."""
        return enum_tokens(self.enums.get(signal.strip()) or {})

    def _update_value_field(self, sig: QComboBox, op: QComboBox, val: QLineEdit):
        """An enum signal's value field gets a ▾ that lists its values."""
        has = bool(self.values_of(sig.currentText()))
        val.pick_action.setVisible(has)
        hint = self.HINTS.get(op.currentText(), "value")
        val.setPlaceholderText(f"{hint}  (or pick one ▾)" if has else hint)

    def open_values(self, val: QLineEdit):
        sig, op = next((s, o) for s, o, v in self.rows if v is val)
        self.value_picker = NamePicker({f"Values of {sig.currentText().strip()}": self.values_of(sig.currentText())},
                                       self)
        self.value_picker.picked.connect(lambda name, v=val, o=op: self.pick_value(v, o.currentText(), name))
        lst = self.value_picker.list
        lst.setMinimumWidth(min(max(val.width(), lst.sizeHintForColumn(0) + 40), 900))  # full texts
        self.value_picker.popup(val.mapToGlobal(val.rect().bottomLeft()))

    def pick_value(self, val: QLineEdit, op: str, value: str):
        """Put a picked value in the field; operators taking several values get it added to the list."""
        current = val.text().strip().rstrip(",").strip()
        if op in self.MULTI and current:
            val.setText(f"{current}, {value}")
        else:
            val.setText(value)

    def delete_row(self, i: int):
        sig, op, val = self.rows.pop(i)
        join, opening, closing = self.links.pop(i)
        row = self.rows_box.takeAt(i)
        for w in (sig, op, val, join, opening, closing):
            w.setParent(None)
            w.deleteLater()
        row.layout().deleteLater()
        if not self.rows:
            self.add_row()
        self.update_preview()

    def row_at(self, pos) -> int:
        """Index of the row under `pos` (dialog coordinates), or -1."""
        for i, (sig, op, val) in enumerate(self.rows):
            top = min(w.geometry().top() for w in (sig, op, val))
            bottom = max(w.geometry().bottom() for w in (sig, op, val))
            if top - 3 <= pos.y() <= bottom + 3:
                return i
        return -1

    def menu_at(self, pos) -> QMenu:
        menu = QMenu(self)
        i = self.row_at(pos)
        if i >= 0:
            menu.addAction("Delete row").triggered.connect(lambda: self.delete_row(i))
            menu.addSeparator()
        menu.addAction("Add row").triggered.connect(self.add_row)
        return menu

    def contextMenuEvent(self, e):
        self.menu_at(e.pos()).exec(e.globalPos())

    @staticmethod
    def _is(s: str, v: str) -> str:
        """`s == v`, or `s in (a, b)` when v lists several values."""
        items = [x.strip() for x in v.strip("()[] ").split(",") if x.strip()]
        return f"{s} == {items[0]}" if len(items) == 1 else f"{s} in ({', '.join(items)})"

    @classmethod
    def row_expression(cls, s: str, o: str, v: str) -> str:
        if o == "in":
            return f"{s} in (" + v.strip("()[] ") + ("," if "," not in v else "") + ")"
        if o == "changes to":
            return f"rising({cls._is(s, v)})"
        if o == "changes from":
            return f"falling({cls._is(s, v)})"
        if o == "changes from → to":
            parts = [x.strip() for x in v.replace("->", ",").replace("→", ",").split(",") if x.strip()]
            if len(parts) != 2:
                return ""
            return f"(falling({s} == {parts[0]}) and rising({s} == {parts[1]}))"
        return f"{s} {o} {v}"

    def expression(self) -> str:
        """The condition the rows make; brackets of rows without a signal or value are left out."""
        parts = []
        for (sig, op, val), (join, opening, closing) in zip(self.rows, self.links):
            s, o, v = sig.currentText().strip(), op.currentText(), val.text().strip()
            e = self.row_expression(s, o, v) if s and v else ""
            if not e:
                continue
            lead = opening.currentText()
            piece = (lead + (" " if lead.endswith("not") else "") + e + closing.currentText())
            parts.append(piece if not parts else f"{join.currentText()} {piece}")
        return " ".join(parts)

    def bracket_problem(self) -> Optional[str]:
        """Why the brackets don't add up, or None."""
        depth = 0
        for part in re.findall(r"[()]", "".join(o.currentText() + c.currentText()
                                                for (s, op, v), (_j, o, c) in zip(self.rows, self.links)
                                                if s.currentText().strip() and v.text().strip())):
            depth += 1 if part == "(" else -1
            if depth < 0:
                return "a bracket is closed before it is opened"
        if depth:
            return f"{depth} bracket{'s' if depth > 1 else ''} not closed"
        return None

    def update_preview(self, *_):
        problem = self.bracket_problem()
        if problem:
            self.preview.setText(f"Condition: <span style='color:#e34948'>{problem}</span>")
        else:
            self.preview.setText(f"Condition: <b>{self.expression() or '—'}</b>")
        self.ok_button.setEnabled(problem is None)
        QTimer.singleShot(0, self.fit_height)

    def fit_height(self):
        """Wide enough for the values typed or picked, and tall enough for the text, rows and condition
        at that width: nothing gets squeezed."""
        for _sig, _op, val in self.rows:  # a value shows in full (up to a point)
            val.setMinimumWidth(min(val.fontMetrics().horizontalAdvance(val.text()) + 40, 560))
        lay = self.layout()
        screen = (self.screen() or QApplication.primaryScreen()).availableGeometry()
        width = min(max(self.width(), lay.totalMinimumSize().width()), int(screen.width() * 0.9))
        need = lay.totalHeightForWidth(width) if lay.hasHeightForWidth() else lay.totalSizeHint().height()
        if width > self.width() or need > self.height():
            self.resize(width, max(need, self.height()))


def make_canvas():
    """Matplotlib canvas for Qt; imported lazily so the GUI starts fast."""
    import matplotlib

    matplotlib.use("QtAgg")
    from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg, NavigationToolbar2QT
    from matplotlib.figure import Figure

    fig = Figure(figsize=(6, 4), tight_layout=True)
    canvas = FigureCanvasQTAgg(fig)
    return fig, canvas, NavigationToolbar2QT
