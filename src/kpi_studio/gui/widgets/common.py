"""Reusable widgets: validated expression field, tool palette, condition builder, plot canvas."""

from __future__ import annotations

import fnmatch
from typing import Callable, Optional

import yaml
from PySide6.QtCore import QPointF, Qt, Signal
from PySide6.QtGui import QAction, QColor, QFocusEvent, QIcon, QPainter, QPixmap, QPolygonF
from PySide6.QtWidgets import (
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
    QPushButton,
    QTextBrowser,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from src.kpi_studio import toolbox
from src.kpi_studio.core.expr import ExprError, compile_expr

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
        sections = {"Signals": list(self.names_fn())}
        sections.update(self.extras_fn() or {})
        return sections

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
        self.doc = QTextBrowser()
        self.doc.setMaximumHeight(140)
        lay.addWidget(title)
        lay.addWidget(self.search)
        lay.addWidget(self.tree, 1)
        lay.addWidget(self.doc)
        self.search.textChanged.connect(self.populate)
        self.tree.currentItemChanged.connect(self._show_doc)
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
            top = QTreeWidgetItem([f"Signals ({len(self._names)}, from tab 2)"])
            top.setToolTip(0, "Double-click a signal to insert its name into the field you are editing")
            self.tree.addTopLevelItem(top)
            for n in shown:
                item = QTreeWidgetItem([n])
                item.setData(0, Qt.UserRole + 1, n)
                top.addChild(item)
            if not self._names:
                top.addChild(QTreeWidgetItem(["(add signals on the Signals tab)"]))
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
                item.setToolTip(0, s.summary)
                cat.addChild(item)
            top.setExpanded(bool(needle) or kind in ("signal", "condition"))
            for c in cats.values():
                c.setExpanded(bool(needle))

    def _show_doc(self, item, _prev=None):
        spec = item.data(0, Qt.UserRole) if item else None
        name = item.data(0, Qt.UserRole + 1) if item else None
        if name:
            self.doc.setHtml(f"<b>{name}</b><br>Signal defined on the Signals tab. Double-click to insert it.")
            return
        if spec is None:
            self.doc.clear()
            return
        params = "".join(
            f"<li><b>{p.name}</b> ({p.type.__name__}{', default ' + repr(p.default) if not p.required else ''})"
            f"{': ' + p.doc if p.doc else ''}</li>" for p in spec.params)
        self.doc.setHtml(f"<b>{spec.signature}</b><br>{spec.doc.replace(chr(10), '<br>')}"
                         + (f"<ul>{params}</ul>" if params else "")
                         + f"<i>{spec.kind} · {spec.category}</i>")

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
    """Build `a > 1 and b == FULL` from rows, without typing syntax."""

    OPS = ["==", "!=", ">", ">=", "<", "<=", "in", "changes to", "changes from", "changes from → to"]
    HINTS = {"in": "list, e.g. 1, 2", "changes to": "value, e.g. 3", "changes from": "value, e.g. 3",
             "changes from → to": "from, to   e.g. 0, 3"}

    def __init__(self, names: list[str], parent=None, initial: str = ""):
        super().__init__(parent)
        self.setWindowTitle("Build condition")
        self.names = names
        self.rows: list[tuple] = []
        lay = QVBoxLayout(self)
        info = QLabel("Each row is  signal  operator  value.  For 'in' give a list, e.g. 1, 2 or FULL, PARTIAL.\n"
                      "'changes to / from' are true only at the sample where the signal switches to or away "
                      "from the value;\n'changes from → to' takes two values, e.g. 0, 3.")
        lay.addWidget(info)
        self.rows_box = QVBoxLayout()
        lay.addLayout(self.rows_box)
        bar = QHBoxLayout()
        add = QPushButton("Add row")
        add.clicked.connect(self.add_row)
        self.join = QComboBox()
        self.join.addItems(["and", "or"])
        bar.addWidget(add)
        bar.addWidget(QLabel("Join rows with"))
        bar.addWidget(self.join)
        bar.addStretch(1)
        lay.addLayout(bar)
        self.preview = QLabel()
        self.preview.setWordWrap(True)
        lay.addWidget(self.preview)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)
        self.join.currentIndexChanged.connect(self.update_preview)
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
        op.currentTextChanged.connect(lambda o, v=val: v.setPlaceholderText(self.HINTS.get(o, "value")))
        row = QHBoxLayout()
        for w in (sig, op, val):
            row.addWidget(w)
            if hasattr(w, "currentTextChanged"):
                w.currentTextChanged.connect(self.update_preview)
            else:
                w.textChanged.connect(self.update_preview)
        self.rows_box.addLayout(row)
        self.rows.append((sig, op, val))
        self.update_preview()

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
        parts = []
        for sig, op, val in self.rows:
            s, o, v = sig.currentText().strip(), op.currentText(), val.text().strip()
            if not s or not v:
                continue
            e = self.row_expression(s, o, v)
            if e:
                parts.append(e)
        return f" {self.join.currentText()} ".join(parts)

    def update_preview(self, *_):
        self.preview.setText(f"Condition: <b>{self.expression() or '—'}</b>")


def make_canvas():
    """Matplotlib canvas for Qt; imported lazily so the GUI starts fast."""
    import matplotlib

    matplotlib.use("QtAgg")
    from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg, NavigationToolbar2QT
    from matplotlib.figure import Figure

    fig = Figure(figsize=(6, 4), tight_layout=True)
    canvas = FigureCanvasQTAgg(fig)
    return fig, canvas, NavigationToolbar2QT
