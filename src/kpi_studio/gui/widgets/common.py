"""Reusable widgets: validated expression field, tool palette, condition builder, plot canvas."""

from __future__ import annotations

import ast
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
        self.list.setItemDelegate(HighlightDelegate(lambda: self.filter.text(), self.list))  # matches
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
        self.list.setMinimumWidth(min(max(280, self.list.sizeHintForColumn(0) + 40), 900))  # full names
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
                 build_label: str = "Edit condition"):
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

    KEYWORDS = {"and", "or", "not", "in", "True", "False", "true", "false"}

    def name_at_cursor(self) -> Optional[tuple[int, int]]:
        """(start, end) of the signal / value name the cursor is in or right next to, if any."""
        text, pos = self.text(), self.cursorPosition()
        for m in re.finditer(r"[A-Za-z_]\w*", text):
            if m.start() <= pos <= m.end() and m.group() not in self.KEYWORDS \
                    and not text[m.end():].lstrip().startswith("("):  # a function name stays
                return m.start(), m.end()
        return None

    def insert_name(self, name: str):
        """Put `name` in the field: it replaces a selection, else the name the cursor is in or next to
        (picking another signal swaps it), else it is inserted at the cursor with spaces as needed."""
        text, pos = self.text(), self.cursorPosition()
        if self.hasSelectedText():
            start = self.selectionStart()
            text = text[:start] + text[start + len(self.selectedText()):]
            pos = start
        elif (span := self.name_at_cursor()) is not None:
            text = text[:span[0]] + text[span[1]:]
            pos = span[0]
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

    def __init__(self, kinds=("signal", "condition", "moment", "reducer"), parent=None,
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
        self.tree.setItemDelegate(HighlightDelegate(lambda: self.search.text(), self.tree))  # matches
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


def parse_condition_rows(text: str) -> Optional[list[dict]]:
    """Split a condition into ConditionBuilder rows: [{join, open, sig, op, val, close}], or None when
    it can't be shown as rows (brackets nested deeper than two, arithmetic, other functions...)."""
    try:
        tree = ast.parse(text.strip(), mode="eval").body
    except SyntaxError:
        return None
    cmp_ops = {ast.Eq: "==", ast.NotEq: "!=", ast.Gt: ">", ast.GtE: ">=", ast.Lt: "<", ast.LtE: "<="}

    def src(node) -> str:
        return ast.get_source_segment(text.strip(), node) or ast.unparse(node)

    def values(node) -> Optional[str]:
        if isinstance(node, (ast.Tuple, ast.List)):
            return ", ".join(src(e) for e in node.elts)
        return None

    def is_test(node):
        """`s == v` or `s in (a, b)`: (signal, value text), else None."""
        if not (isinstance(node, ast.Compare) and len(node.ops) == 1 and isinstance(node.left, ast.Name)):
            return None
        op, right = node.ops[0], node.comparators[0]
        if isinstance(op, ast.Eq):
            return node.left.id, src(right)
        if isinstance(op, ast.In) and values(right) is not None:
            return node.left.id, values(right)
        return None

    def edge(node):
        """rising(test) / falling(test): (function, signal, value), else None."""
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ("rising", "falling")
                and len(node.args) == 1 and not node.keywords):
            t = is_test(node.args[0])
            if t is not None:
                return node.func.id, *t
        return None

    def leaf(node) -> Optional[dict]:
        if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.And) and len(node.values) == 2:
            a, b = edge(node.values[0]), edge(node.values[1])
            if a and b and a[0] == "falling" and b[0] == "rising" and a[1] == b[1] and "," not in a[2] + b[2]:
                return {"sig": a[1], "op": "changes from → to", "val": f"{a[2]}, {b[2]}"}
        if isinstance(node, ast.Name):  # a flag on its own: true while it is not 0
            return {"sig": node.id, "op": "!=", "val": "0"}
        e = edge(node)
        if e is not None:
            return {"sig": e[1], "op": "changes to" if e[0] == "rising" else "changes from", "val": e[2]}
        if isinstance(node, ast.Compare) and len(node.ops) == 1 and isinstance(node.left, ast.Name):
            op, right = node.ops[0], node.comparators[0]
            if type(op) in cmp_ops:
                return {"sig": node.left.id, "op": cmp_ops[type(op)], "val": src(right)}
            if isinstance(op, ast.In) and values(right) is not None:
                return {"sig": node.left.id, "op": "in", "val": values(right)}
        return None

    def rows_of(node) -> Optional[list[dict]]:
        one = leaf(node)
        if one is not None:
            return [dict(one, join="and", nots=0, opens=0, close=0)]
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            inner = rows_of(node.operand)
            if inner is None:
                return None
            if len(inner) > 1:  # not (group)
                inner = bracket(inner)
            if inner[0]["nots"]:
                return None
            inner[0]["nots"] = 1
            return inner
        if isinstance(node, ast.BoolOp):
            out = []
            join = "and" if isinstance(node.op, ast.And) else "or"
            for child in node.values:
                part = rows_of(child)
                if part is None:
                    return None
                if len(part) > 1 and isinstance(child, ast.BoolOp) and leaf(child) is None:
                    part = bracket(part)  # (a or b) and c
                if out:
                    part[0]["join"] = join
                out += part
            return out
        return None

    def bracket(rows):
        if rows[0]["nots"]:  # (not a ...): a row's not always comes before its brackets
            raise ValueError
        rows[0]["opens"] += 1
        rows[-1]["close"] += 1
        return rows

    try:
        rows = rows_of(tree)
    except ValueError:
        return None
    if rows is None:
        return None
    out = []
    for r in rows:
        if r["opens"] > 2 or r["close"] > 2:
            return None
        opening = ("not " if r["nots"] else "") + "(" * r["opens"]
        out.append({"join": r["join"], "open": opening.strip(), "sig": r["sig"], "op": r["op"],
                    "val": r["val"], "close": ")" * r["close"]})
    return out


class ConditionRow(QWidget):
    """One condition as  signal  operator  value  (the same fields as a row of Edit condition), with the
    signal's enum values to pick. A condition these fields can't show is kept as typed ('expression')."""

    changed = Signal()
    EXPRESSION = "expression"

    def __init__(self, names_fn: Callable[[], list[str]], enums_fn: Callable[[], dict], parent=None,
                 *, laid_out: bool = True):
        """laid_out=False leaves sig / op / val unplaced, e.g. for a table that shows each in its own column."""
        super().__init__(parent)
        self.names_fn, self.enums_fn = names_fn, enums_fn
        self.value_picker: Optional[NamePicker] = None
        self.sig = QComboBox()
        self.sig.setEditable(True)
        self.sig.addItems(sorted(names_fn(), key=str.lower))
        self.sig.setCurrentText("")
        self.sig.setMinimumWidth(130)
        self.op = QComboBox()
        self.op.addItems(ConditionBuilder.OPS + [self.EXPRESSION])
        self.op.setMinimumWidth(70)
        if laid_out:  # in a table the column decides the width
            self.op.setMaximumWidth(110)
        self.op.setItemData(self.op.count() - 1, "Type the whole condition in the value field", Qt.ToolTipRole)
        self.val = QLineEdit()
        self.val.setMinimumWidth(70)
        pick = QAction(_arrow_icon(), "Pick a value", self.val)
        pick.setToolTip("Pick one of the signal's enum values")
        self.val.addAction(pick, QLineEdit.TrailingPosition)
        pick.triggered.connect(self.open_values)
        self.pick_action = pick
        if laid_out:
            lay = QHBoxLayout(self)
            lay.setContentsMargins(0, 0, 0, 0)
            lay.setSpacing(4)
            for w, stretch in ((self.sig, 3), (self.op, 0), (self.val, 2)):
                lay.addWidget(w, stretch)
        self.sig.currentTextChanged.connect(self._update)
        self.op.currentTextChanged.connect(self._update)
        self.sig.activated.connect(lambda *_: self.changed.emit())
        self.op.activated.connect(lambda *_: self.changed.emit())
        self.val.editingFinished.connect(self.changed.emit)
        self._update()

    def values_of(self, signal: str) -> list[str]:
        return enum_tokens(self.enums_fn().get(signal.strip()) or {})

    def _update(self, *_):
        expression = self.op.currentText() == self.EXPRESSION
        self.sig.setEnabled(not expression)
        has = not expression and bool(self.values_of(self.sig.currentText()))
        self.pick_action.setVisible(has)
        hint = "a condition, e.g. a > 1 and b == 2" if expression else \
            ConditionBuilder.HINTS.get(self.op.currentText(), "value")
        self.val.setPlaceholderText(f"{hint}  (or pick one ▾)" if has else hint)

    def open_values(self):
        sig = self.sig.currentText().strip()
        self.value_picker = NamePicker({f"Values of {sig}": self.values_of(sig)}, self)
        self.value_picker.picked.connect(self.pick_value)
        lst = self.value_picker.list
        lst.setMinimumWidth(min(max(self.val.width(), lst.sizeHintForColumn(0) + 40), 900))
        self.value_picker.popup(self.val.mapToGlobal(self.val.rect().bottomLeft()))

    def pick_value(self, value: str):
        current = self.val.text().strip().rstrip(",").strip()
        self.val.setText(f"{current}, {value}" if self.op.currentText() in ConditionBuilder.MULTI and current
                         else value)
        self.changed.emit()

    def expression(self) -> str:
        o, v = self.op.currentText(), self.val.text().strip()
        if o == self.EXPRESSION:
            return v
        s = self.sig.currentText().strip()
        return ConditionBuilder.row_expression(s, o, v) if s and v else ""

    def set_expression(self, text: str):
        rows = parse_condition_rows(text) if text.strip() else None
        simple = rows is not None and len(rows) == 1 and not rows[0]["open"] and not rows[0]["close"]
        for w in (self.sig, self.op, self.val):
            w.blockSignals(True)
        if simple:
            self.sig.setCurrentText(rows[0]["sig"])
            self.op.setCurrentText(rows[0]["op"])
            self.val.setText(rows[0]["val"])
        else:
            self.sig.setCurrentText("")
            self.op.setCurrentText(self.EXPRESSION if text.strip() else "==")
            self.val.setText(text.strip())
        for w in (self.sig, self.op, self.val):
            w.blockSignals(False)
        self._update()


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
        self.setWindowTitle("Edit condition")
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
        if initial and not self.load_expression(initial):
            self.preview.setText(f"Current: {initial}<br><span style='color:#c27a00'>This condition can't be "
                                 "split into rows. Edit it in the field instead, or build a new one here "
                                 "(OK replaces it).</span>")

    def load_expression(self, text: str) -> bool:
        """Fill the rows from an existing condition, e.g. one built here before. False (rows left as
        they were) when it uses something the rows can't show, such as arithmetic or other functions."""
        rows = parse_condition_rows(text)
        if not rows:
            return False
        while len(self.rows) > 1:
            self.delete_row(len(self.rows) - 1)
        for i, r in enumerate(rows):
            if i:
                self.add_row()
            (sig, op, val), (join, opening, closing) = self.rows[i], self.links[i]
            join.setCurrentText(r["join"])
            opening.setCurrentText(r["open"])
            sig.setCurrentText(r["sig"])
            op.setCurrentText(r["op"])
            val.setText(r["val"])
            closing.setCurrentText(r["close"])
        self.update_preview()
        return True

    def add_row(self):
        sig = QComboBox()
        sig.setEditable(True)
        sig.addItems(sorted(self.names, key=str.lower))  # alphabetical, whatever the profile order
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
        width = max(opening.sizeHint().width(), closing.sizeHint().width())
        opening.setFixedWidth(width)  # the bracket boxes line up as one width
        closing.setFixedWidth(width)
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
