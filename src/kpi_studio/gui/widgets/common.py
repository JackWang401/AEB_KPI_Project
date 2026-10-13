"""Reusable widgets: validated expression field, tool palette, condition builder, plot canvas."""

from __future__ import annotations

import fnmatch
from typing import Callable, Optional

import yaml
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFocusEvent
from PySide6.QtWidgets import (
    QComboBox,
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


class ExprEdit(QLineEdit):
    """Line edit that validates an expression as you type against the current signal names."""

    focused = Signal(object)

    def __init__(self, names_fn: Callable[[], list[str]], parent=None, *, allow_empty=True, placeholder="",
                 enums_fn: Optional[Callable[[], dict]] = None):
        super().__init__(parent)
        self.names_fn = names_fn
        self.enums_fn = enums_fn or (lambda: {})
        self.allow_empty = allow_empty
        self.error: Optional[str] = None
        self.setPlaceholderText(placeholder)
        self.textChanged.connect(self.check)

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

    OPS = ["==", "!=", ">", ">=", "<", "<=", "in"]

    def __init__(self, names: list[str], parent=None, initial: str = ""):
        super().__init__(parent)
        self.setWindowTitle("Build condition")
        self.names = names
        self.rows: list[tuple] = []
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("Each row is  signal  operator  value.  For 'in' give a list, e.g. 1, 2 or FULL, PARTIAL."))
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

    def expression(self) -> str:
        parts = []
        for sig, op, val in self.rows:
            s, o, v = sig.currentText().strip(), op.currentText(), val.text().strip()
            if not s or not v:
                continue
            if o == "in":
                v = "(" + v.strip("()[] ") + ("," if "," not in v else "") + ")"
            parts.append(f"{s} {o} {v}")
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
