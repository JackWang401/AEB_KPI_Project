"""Guided editor for one derived signal: pick the signal, pick the method, fill in the method's arguments.

The result is an ordinary expression (``lowpass(longActAccel, 10)``, ``egoSpeed * 3.6``), so the
profile format does not change and the Expression cell stays editable for anything the dialog
cannot express.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from typing import Optional

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QLabel,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)

from src.kpi_studio import toolbox
from src.kpi_studio.core.expr import ExprError, compile_expr
from src.kpi_studio.gui.widgets.common import ConditionBuilder, parse_condition_rows
from src.kpi_studio.toolbox.registry import Param
from src.kpi_studio.toolbox.units import COMMON_UNITS, convert_to, normalize_unit
from src.kpi_studio.toolbox.units import plain as plain_unit
from src.kpi_studio.toolbox.units import pretty as pretty_unit

NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass
class Method:
    key: str                 # tool name, or one of the arithmetic keys below
    label: str
    category: str
    doc: str
    inputs: list = field(default_factory=list)    # names of the extra signal inputs (after the first)
    params: list = field(default_factory=list)    # [Param]
    unit: str = "same"       # "same" = unit of the input signal, "" = unknown, else a unit
    variadic: bool = False
    converts_to: str = ""    # to_<unit>() methods: the unit they convert to

    @property
    def is_tool(self) -> bool:
        return self.key not in ARITHMETIC


ARITHMETIC = {
    "condition": Method("condition", "Condition  (signal, operator, value rows)", "Condition",
                        "True (1) while the condition holds, else 0.", unit=""),
    "expression": Method("expression", "Type an expression  (any formula of signals)", "Condition",
                         "Any expression of signals, e.g. gap_dist < 0.3 and braking, or v * 3.6 + 1.",
                         unit=""),
    "scale": Method("scale", "Scale  (x · factor)", "Arithmetic", "Multiply the signal by a number, e.g. 3.6.",
                    params=[Param("factor", float, example=1.0, doc="multiply by")], unit=""),
    "offset": Method("offset", "Offset  (x + offset)", "Arithmetic", "Add a constant (use a negative number to subtract).",
                     params=[Param("offset", float, example=0.0, doc="add")]),
    "scale_offset": Method("scale_offset", "Scale and offset  (x · factor + offset)", "Arithmetic",
                           "Linear conversion, e.g. factor 1.8 and offset 32 for °C → °F.",
                           params=[Param("factor", float, example=1.0), Param("offset", float, example=0.0)], unit=""),
    "add": Method("add", "Add a signal  (x + y)", "Arithmetic", "Sum of two signals.", inputs=["y"]),
    "subtract": Method("subtract", "Subtract a signal  (x − y)", "Arithmetic", "Difference of two signals.",
                       inputs=["y"]),
    "multiply": Method("multiply", "Multiply by a signal  (x · y)", "Arithmetic", "Product of two signals.",
                       inputs=["y"], unit=""),
    "divide": Method("divide", "Divide by a signal  (x / y)", "Arithmetic", "Ratio of two signals.",
                     inputs=["y"], unit=""),
}
_BINOP = {"add": "+", "subtract": "-", "multiply": "*", "divide": "/"}


def methods() -> list[Method]:
    out = list(ARITHMETIC.values())
    for spec in toolbox.tools("signal"):
        if spec.inputs == -1:
            inputs, variadic = ["x2"], True
        else:
            names = list(spec.input_names) or [f"x{i + 1}" for i in range(spec.inputs)]
            inputs, variadic = names[1:], False
        unit = spec.unit
        if unit.startswith("x") or unit == "to":
            unit = "" if unit != "to" else "to"
        out.append(Method(spec.name, f"{spec.name}  –  {spec.summary}", spec.category, spec.doc.strip(),
                          inputs=inputs, params=list(spec.params), unit=unit, variadic=variadic,
                          converts_to=spec.converts_to))
    return out


def _num(v) -> str:
    v = float(v)
    return str(int(v)) if v.is_integer() and abs(v) < 1e15 else repr(v)


def _literal(p: Param, text: str) -> str:
    if p.type is str:
        return '"' + text.replace('"', "") + '"'
    if p.type is bool:
        return "True" if text in ("1", "true", "True", "yes") else "False"
    return _num(text) if p.type is float else str(int(float(text)))


def build_expression(m: Method, signals: list[str], args: dict[str, str]) -> str:
    """Expression text for method `m` applied to `signals` (first = main input) with param texts `args`."""
    if m.key in ("expression", "condition"):
        return args.get(m.key, "").strip()
    x = signals[0]
    if m.key == "scale":
        return f"{x} * {_num(args['factor'])}"
    if m.key == "offset":
        off = float(args["offset"])
        return f"{x} - {_num(-off)}" if off < 0 else f"{x} + {_num(off)}"
    if m.key == "scale_offset":
        off = float(args["offset"])
        tail = f" - {_num(-off)}" if off < 0 else (f" + {_num(off)}" if off else "")
        return f"{x} * {_num(args['factor'])}{tail}"
    if m.key in _BINOP:
        y = signals[1]
        if m.key in ("multiply", "divide") and re.search(r"[+\-]", y):
            y = f"({y})"
        return f"{x} {_BINOP[m.key]} {y}"
    parts = list(signals)
    positional = True
    for p in m.params:
        text = (args.get(p.name) or "").strip()
        if not p.required and (text == "" or _same(p, text, p.default)):
            positional = False  # later params must then be named
            continue
        lit = _literal(p, text)
        parts.append(lit if positional else f"{p.name}={lit}")
    return f"{m.key}({', '.join(parts)})"


def _converts(target: str, unit: str) -> bool:
    try:
        convert_to(target, unit)
        return True
    except ValueError:
        return False


def _same(p: Param, text: str, value) -> bool:
    try:
        return p.coerce(text, "") == value
    except Exception:
        return False


def parse_expression(text: str, all_methods: list[Method]) -> Optional[tuple]:
    """(method, [signal names], {param: text}) when `text` is one the dialog could have built, else None."""
    try:
        node = ast.parse(text.strip(), mode="eval").body
    except SyntaxError:
        return None
    by_key = {m.key: m for m in all_methods}

    def name(n):
        return n.id if isinstance(n, ast.Name) else None

    def number(n):
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)) and not isinstance(n.value, bool):
            return float(n.value)
        if isinstance(n, ast.UnaryOp) and isinstance(n.op, ast.USub):
            v = number(n.operand)
            return None if v is None else -v
        return None

    if isinstance(node, ast.BinOp):
        left, right = node.left, node.right
        if isinstance(node.op, (ast.Add, ast.Sub)) and number(right) is not None:
            off = number(right) * (1 if isinstance(node.op, ast.Add) else -1)
            if name(left):
                return by_key["offset"], [left.id], {"offset": _num(off)}
            if isinstance(left, ast.BinOp) and isinstance(left.op, ast.Mult) and name(left.left) \
                    and number(left.right) is not None:
                return by_key["scale_offset"], [left.left.id], {"factor": _num(number(left.right)),
                                                               "offset": _num(off)}
            return None
        if isinstance(node.op, ast.Mult) and name(left) and number(right) is not None:
            return by_key["scale"], [left.id], {"factor": _num(number(right))}
        ops = {ast.Add: "add", ast.Sub: "subtract", ast.Mult: "multiply", ast.Div: "divide"}
        key = ops.get(type(node.op))
        if key and name(left) and name(right):
            return by_key[key], [left.id, right.id], {}
        return None
    if isinstance(node, ast.Call) and name(node.func) and node.func.id in by_key:
        m = by_key[node.func.id]
        n_in = 1 + len(m.inputs)
        if m.variadic:
            n_in = len(node.args)
        sigs = [name(a) for a in node.args[:n_in]]
        if len(node.args) < n_in or not all(sigs):
            return None
        args = {}
        for p, a in zip(m.params, node.args[n_in:]):
            args[p.name] = _arg_text(a)
        for kw in node.keywords:
            args[kw.arg] = _arg_text(kw.value)
        if any(v is None for v in args.values()):
            return None
        return m, sigs, args
    return None


def _arg_text(node) -> Optional[str]:
    if isinstance(node, ast.Constant):
        return str(node.value) if not isinstance(node.value, float) else _num(node.value)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub) and isinstance(node.operand, ast.Constant):
        return "-" + _arg_text(node.operand)
    return None


class DerivedBuilder(QDialog):
    """Name, input signal, method, the method's arguments, unit and description of a derived signal."""

    def __init__(self, signal_names: list[str], units: dict[str, str], parent=None, *, name: str = "",
                 expr: str = "", unit: str = "", description: str = "", taken: set = frozenset(),
                 enums_fn=None):
        super().__init__(parent)
        self.setWindowTitle("Derived signal")
        self.setMinimumWidth(560)
        self.signal_names = [n for n in signal_names if n != name]
        self.units = units
        self.enums_fn = enums_fn or (lambda: {})
        self.taken = set(taken) - {name}
        self.methods = methods()
        self.arg_widgets: dict[str, QWidget] = {}
        self.input_combos: list[QComboBox] = []
        self._unit_touched = bool(unit)

        lay = QVBoxLayout(self)
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        self.name = QLineEdit(name)
        self.name.setPlaceholderText("e.g. egoSpeedKph")
        form.addRow("Name", self.name)
        self.signal = self._signal_combo()
        form.addRow("Signal to process", self.signal)
        self.form = form
        self.method = QComboBox()
        self.method.setMaxVisibleItems(25)
        last = None
        for m in self.methods:
            if m.category != last:
                if last is not None:
                    self.method.insertSeparator(self.method.count())
                last = m.category
            self.method.addItem(f"{m.category}:  {m.label}", m.key)
        form.addRow("Method", self.method)
        lay.addLayout(form)

        self.doc = QLabel()
        self.doc.setWordWrap(True)
        self.doc.setStyleSheet("color:#5f5e5a")
        lay.addWidget(self.doc)
        self.args_box = QGroupBox("Arguments")
        self.args_form = QFormLayout(self.args_box)
        self.args_form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        lay.addWidget(self.args_box)

        form2 = QFormLayout()
        form2.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        self.unit = QLineEdit(unit)
        self.unit.setPlaceholderText("e.g. km/h")
        self.unit.textEdited.connect(lambda *_: setattr(self, "_unit_touched", True))
        form2.addRow("Unit", self.unit)
        self.description = QLineEdit(description)
        self.description.setPlaceholderText("optional: what this signal means")
        form2.addRow("Description", self.description)
        lay.addLayout(form2)

        self.result = QLabel()
        self.result.setWordWrap(True)
        self.result.setTextInteractionFlags(self.result.textInteractionFlags())
        lay.addWidget(self.result)
        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        lay.addWidget(self.buttons)

        self.name.textChanged.connect(self.update_result)
        self.signal.currentTextChanged.connect(self._signal_changed)
        self.method.currentIndexChanged.connect(self._method_changed)

        parsed = parse_expression(expr, self.methods) if expr else None
        if parsed:
            m, sigs, args = parsed
            self.signal.setCurrentText(sigs[0])
            self.set_method(m.key)
            for combo, s in zip(self.input_combos, sigs[1:]):
                combo.setCurrentText(s)
            for k, v in args.items():
                self._set_arg(k, v)
        elif expr and parse_condition_rows(expr):
            self.set_method("condition")
            self.arg_widgets["condition"].load_expression(expr)
        elif expr:
            self.set_method("expression")
            self.arg_widgets["expression"].setText(expr)
        else:
            self.set_method("scale")
        self.update_result()

    # ------------------------------------------------------------------ widgets
    def _signal_combo(self) -> QComboBox:
        c = QComboBox()
        c.setEditable(True)
        c.addItems(self.signal_names)
        c.setCurrentIndex(-1)
        c.lineEdit().setPlaceholderText("pick or type a signal")
        c.currentTextChanged.connect(self.update_result)
        return c

    def current_method(self) -> Method:
        key = self.method.currentData()
        return next(m for m in self.methods if m.key == key)

    def set_method(self, key: str):
        i = self.method.findData(key)
        if i >= 0:
            if i == self.method.currentIndex():
                self._method_changed()
            self.method.setCurrentIndex(i)

    def _method_changed(self, *_):
        m = self.current_method()
        self.doc.setText(m.doc)
        while self.args_form.rowCount():
            self.args_form.removeRow(0)
        self.arg_widgets.clear()
        self.input_combos = []
        self.form.setRowVisible(self.signal, m.key not in ("expression", "condition"))
        if m.key == "condition":
            w = ConditionBuilder(self.signal_names, self, enums=self.enums_fn(), embedded=True)
            w.changed.connect(self.update_result)
            self.args_form.addRow(w)
            self.arg_widgets["condition"] = w
            self.resize(max(self.width(), 1000), self.height())
        if m.key == "expression":
            from src.kpi_studio.gui.widgets.common import ExprEdit

            w = ExprEdit(lambda: self.signal_names, enums_fn=self.enums_fn, units_fn=lambda: self.units,
                         allow_empty=False, placeholder="e.g. fb_state == ACTIVE or pb_state == ACTIVE")
            w.textChanged.connect(self.update_result)
            self.args_form.addRow("expression", w)
            self.arg_widgets["expression"] = w
        for label in m.inputs:
            combo = self._signal_combo()
            self.input_combos.append(combo)
            self.args_form.addRow(f"{label} (signal)", combo)
        for p in m.params:
            if m.converts_to and p.name == "from_unit":
                w = QComboBox()
                w.setEditable(True)
                w.currentTextChanged.connect(self.update_result)
                self.args_form.addRow("from unit", w)
                self.arg_widgets[p.name] = w
                self._sync_from_unit()
                continue
            if p.choices:
                w = QComboBox()
                w.addItems([str(c) for c in p.choices])
                if not p.required:
                    w.setCurrentText(str(p.default))
                w.currentTextChanged.connect(self.update_result)
            elif p.type is bool:
                w = QCheckBox()
                w.setChecked(bool(p.example_value()))
                w.toggled.connect(self.update_result)
            else:
                w = QLineEdit()
                if p.required:
                    w.setPlaceholderText(f"e.g. {p.example_value()}" if p.example is not None else "required")
                else:
                    w.setText(str(p.default))
                w.textChanged.connect(self.update_result)
            label = p.name + ("" if p.required else " (optional)")
            if p.doc:
                w.setToolTip(p.doc)
            self.args_form.addRow(label, w)
            self.arg_widgets[p.name] = w
        self.args_box.setVisible(bool(m.inputs or m.params or m.key in ("expression", "condition")))
        self._suggest_unit()
        self.update_result()
        first = next(iter(self.arg_widgets.values()), None)
        if isinstance(first, QLineEdit):
            first.setFocus()

    def _set_arg(self, key, text):
        w = self.arg_widgets.get(key)
        if isinstance(w, QLineEdit):
            w.setText(text)
        elif isinstance(w, QComboBox):
            w.setCurrentText(pretty_unit(text) if key == "from_unit" else text)
        elif isinstance(w, QCheckBox):
            w.setChecked(text in ("True", "true", "1"))

    def _arg_texts(self) -> dict[str, str]:
        out = {}
        for k, w in self.arg_widgets.items():
            if isinstance(w, QLineEdit):
                out[k] = w.text().strip()
            elif isinstance(w, QComboBox):
                out[k] = plain_unit(w.currentText().strip()) if k == "from_unit" else w.currentText()
            else:
                out[k] = "True" if w.isChecked() else "False"
        return out

    def _sync_from_unit(self):
        """A to_<unit>() method converts from the signal's Unit; with none, the user picks it here."""
        w = self.arg_widgets.get("from_unit")
        m = self.current_method()
        if not isinstance(w, QComboBox) or not m.converts_to:
            return
        sig = self.signal.currentText().strip()
        unit = self.units.get(sig, "")
        choices = [u for u in COMMON_UNITS if _converts(m.converts_to, u)]
        w.blockSignals(True)
        w.clear()
        w.addItems([pretty_unit(u) for u in choices])
        w.setCurrentText(pretty_unit(unit))
        w.blockSignals(False)
        if unit:
            w.setToolTip(f"From the Unit column of {sig}")
        else:
            w.lineEdit().setPlaceholderText(f"{sig or 'the signal'} has no unit: pick the unit it is in")
            w.setToolTip("The signal has no unit in the Unit column; pick the unit its values are in")

    def _signal_changed(self, *_):
        self._sync_from_unit()
        self._suggest_unit()
        self.update_result()

    def _suggest_unit(self):
        if self._unit_touched:
            return
        m = self.current_method()
        if m.converts_to:
            try:
                u = convert_to(m.converts_to, self._from_unit())[1]
            except ValueError:
                u = m.converts_to
        elif m.unit == "same":
            u = self.units.get(self.signal.currentText().strip(), "")
        elif m.unit == "to":
            w = self.arg_widgets.get("to_unit")
            u = w.text().strip() if isinstance(w, QLineEdit) else ""
        else:
            u = m.unit
        self.unit.setText(u)

    # ------------------------------------------------------------------ result
    def problem(self) -> str:
        m = self.current_method()
        name = self.name.text().strip()
        if not NAME_RE.match(name):
            return "Give the derived signal a name (letters, digits, _; not starting with a digit)."
        if name in self.taken:
            return f"'{name}' is already used by another signal."
        if m.key == "condition":
            w = self.arg_widgets.get("condition")
            if w is None or not w.expression():
                return "Fill in a row: signal, operator and value."
            if w.bracket_problem():
                return "Brackets: " + w.bracket_problem() + "."
            try:
                compile_expr(w.expression(), names=set(self.signal_names) | {"time"}, enums=self.enums_fn(),
                             units=self.units)
            except ExprError as exc:
                return str(exc)
            return ""
        if m.key == "expression":
            w = self.arg_widgets.get("expression")
            if w is None or not w.text().strip():
                return "Type the expression, e.g. a condition such as fb_state == ACTIVE."
            return "" if w.check() else w.error
        sigs = [self.signal.currentText().strip()] + [c.currentText().strip() for c in self.input_combos]
        for s in sigs:
            if not s:
                return "Pick the signal(s) to process."
            if s not in self.signal_names:
                return f"'{s}' is not a signal of this profile."
            if s == name:
                return "A derived signal cannot use itself."
        if m.converts_to:
            if not self._from_unit():
                return f"'{sigs[0]}' has no unit in the Unit column. Pick the unit it is in under 'from unit'."
            try:
                convert_to(m.converts_to, self._from_unit())
            except ValueError as exc:
                return str(exc)[0].upper() + str(exc)[1:] + "."
        for p in m.params:
            text = self._arg_texts().get(p.name, "")
            if text == "":
                if p.required:
                    return f"Enter {p.name}" + (f" ({p.doc})" if p.doc else "") + "."
                continue
            try:
                p.coerce(text, m.key)
            except Exception as exc:
                return str(exc)
        return ""

    def expression(self) -> str:
        if self.problem():
            return ""
        if self.current_method().key == "expression":
            return self.arg_widgets["expression"].text().strip()
        if self.current_method().key == "condition":
            return self.arg_widgets["condition"].expression()
        sigs = [self.signal.currentText().strip()] + [c.currentText().strip() for c in self.input_combos]
        args = self._arg_texts()
        if self.current_method().converts_to and \
                normalize_unit(args.get("from_unit")) == normalize_unit(self.units.get(sigs[0], "")):
            args["from_unit"] = ""  # read from the Unit column when the profile runs
        return build_expression(self.current_method(), sigs, args)

    def _from_unit(self) -> str:
        return self._arg_texts().get("from_unit", "")

    def update_result(self, *_):
        if self.current_method().unit == "to" or self.current_method().converts_to:
            self._suggest_unit()
        msg = self.problem()
        ok = self.buttons.button(QDialogButtonBox.Ok)
        ok.setEnabled(not msg)
        if msg:
            self.result.setText(f"<span style='color:#c43c3b'>{msg}</span>")
        else:
            self.result.setText(f"Expression:  <b>{self.name.text().strip()} = {self.expression()}</b>")

    def values(self) -> dict:
        return {"name": self.name.text().strip(), "expr": self.expression(),
                "unit": self.unit.text().strip(), "description": self.description.text().strip()}
