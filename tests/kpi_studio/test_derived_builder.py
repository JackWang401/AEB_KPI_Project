import pytest

from src.kpi_studio.gui.widgets.derived_builder import (
    DerivedBuilder, build_expression, methods, parse_expression)

M = {m.key: m for m in methods()}


@pytest.mark.parametrize("key,sigs,args,expr", [
    ("scale", ["v"], {"factor": "3.6"}, "v * 3.6"),
    ("offset", ["v"], {"offset": "-2"}, "v - 2"),
    ("scale_offset", ["t"], {"factor": "1.8", "offset": "32"}, "t * 1.8 + 32"),
    ("subtract", ["a", "b"], {}, "a - b"),
    ("lowpass", ["a"], {"cutoff_hz": "10", "order": "4"}, "lowpass(a, 10)"),
    ("lowpass", ["a"], {"cutoff_hz": "10", "order": "2"}, "lowpass(a, 10, 2)"),
    ("clip", ["a"], {"lo": "-1", "hi": "1"}, "clip(a, -1, 1)"),
    ("convert", ["p"], {"from_unit": "bar", "to_unit": "kpa"}, 'convert(p, "bar", "kpa")'),
])
def test_build_and_parse_round_trip(key, sigs, args, expr):
    if key not in M:
        pytest.skip(f"no tool {key}")
    assert build_expression(M[key], sigs, args) == expr
    m, s, a = parse_expression(expr, list(M.values()))
    assert m.key == key and s == sigs
    assert build_expression(m, s, {**args, **a}) == expr


def test_dialog_scale_prompts_for_factor(qtbot):
    dlg = DerivedBuilder(["VehicleSpeed", "accel"], {"VehicleSpeed": "m/s"}, taken={"VehicleSpeed", "accel"})
    qtbot.addWidget(dlg)
    dlg.name.setText("VehSpdKph")
    dlg.signal.setCurrentText("VehicleSpeed")
    dlg.set_method("scale")
    assert "factor" in dlg.arg_widgets and dlg.expression() == ""  # factor still empty
    assert "factor" in dlg.problem()
    dlg.arg_widgets["factor"].setText("3.6")
    assert dlg.values() == {"name": "VehSpdKph", "expr": "VehicleSpeed * 3.6", "unit": "", "description": ""}
    dlg.set_method("lowpass")
    assert dlg.unit.text() == "m/s"  # same unit as the input
    assert set(dlg.arg_widgets) == {"cutoff_hz", "order"}


def test_dialog_prefills_from_existing_expression(qtbot):
    dlg = DerivedBuilder(["v", "w"], {}, name="vKph", expr="v * 3.6", unit="km/h")
    qtbot.addWidget(dlg)
    assert dlg.current_method().key == "scale"
    assert dlg.signal.currentText() == "v" and dlg.arg_widgets["factor"].text() == "3.6"
    assert dlg.unit.text() == "km/h"
    assert dlg.values()["expr"] == "v * 3.6"


@pytest.mark.parametrize("m", [m for m in methods() if m.key not in ("expression", "condition")],
                         ids=lambda m: m.key)
def test_every_method_builds_a_valid_expression(m):
    from src.kpi_studio.core.expr import compile_expr

    sigs = ["a", "b", "c"][:1 + len(m.inputs)]
    args = {p.name: str(p.example_value()) for p in m.params}
    expr = build_expression(m, sigs, args)
    compile_expr(expr, names={"a", "b", "c"})
    parsed = parse_expression(expr, methods())
    assert parsed is not None, expr
    assert build_expression(parsed[0], parsed[1], {**args, **parsed[2]}) == expr  # same (or equivalent) method


def test_dialog_converter_reads_unit_column_and_asks_when_empty(qtbot):
    dlg = DerivedBuilder(["speed", "raw", "yaw"], {"speed": "m/s", "raw": "", "yaw": "rad/s"},
                         taken={"speed", "raw", "yaw"})
    qtbot.addWidget(dlg)
    dlg.name.setText("speedKph")
    dlg.signal.setCurrentText("speed")
    dlg.set_method("to_kph")
    assert dlg.arg_widgets["from_unit"].currentText() == "m/s"
    assert dlg.values()["expr"] == "to_kph(speed)" and dlg.unit.text() == "km/h"
    dlg.signal.setCurrentText("raw")  # no unit: the user is asked for it
    assert dlg.arg_widgets["from_unit"].currentText() == ""
    assert "has no unit" in dlg.problem() and dlg.expression() == ""
    dlg.arg_widgets["from_unit"].setCurrentText("mph")
    assert dlg.values()["expr"] == 'to_kph(raw, "mph")'
    dlg.arg_widgets["from_unit"].setCurrentText("deg")
    assert "Cannot convert deg to km/h" in dlg.problem()
    dlg.signal.setCurrentText("yaw")
    dlg.set_method("to_deg")
    assert dlg.values()["expr"] == "to_deg(yaw)" and dlg.unit.text() == "deg/s"


def test_dialog_expression_method_takes_a_condition(qtbot):
    enums = {"fb": {"ACTIVE": 2, "OFF": 0}, "pb": {"ACTIVE": 2, "OFF": 0}}
    dlg = DerivedBuilder(["fb", "pb", "gap"], {}, taken={"fb", "pb", "gap"}, enums_fn=lambda: enums)
    qtbot.addWidget(dlg)
    dlg.name.setText("braking")
    dlg.set_method("expression")
    assert dlg.form.isRowVisible(dlg.signal) is False  # no single signal to process
    assert "Type the expression" in dlg.problem()
    dlg.arg_widgets["expression"].setText("fb == ACTIVE or pb == BOGUS")
    assert dlg.problem() and dlg.expression() == ""
    dlg.arg_widgets["expression"].setText("fb == ACTIVE or pb == ACTIVE")
    assert dlg.values()["expr"] == "fb == ACTIVE or pb == ACTIVE"
    dlg.set_method("scale")
    assert dlg.form.isRowVisible(dlg.signal)
    again = DerivedBuilder(["fb", "pb"], {}, name="braking", expr="fb == ACTIVE or pb == ACTIVE",
                           enums_fn=lambda: enums)
    qtbot.addWidget(again)
    assert again.current_method().key == "condition"  # a condition that fits rows reopens as rows
    assert again.values()["expr"] == "fb == ACTIVE or pb == ACTIVE"
    typed = DerivedBuilder(["fb", "gap"], {}, name="x", expr="gap * 2 + fb", enums_fn=lambda: enums)
    qtbot.addWidget(typed)
    assert typed.current_method().key == "expression"


def test_dialog_condition_rows_with_enum_values_and_xor(qtbot):
    import numpy as np

    from src.kpi_studio.core.expr import Env, compile_expr

    enums = {"fb": {"ACTIVE": 2, "OFF": 0}, "pb": {"ACTIVE": 2, "OFF": 0}}
    dlg = DerivedBuilder(["fb", "pb", "gap"], {}, taken={"fb", "pb", "gap"}, enums_fn=lambda: enums)
    qtbot.addWidget(dlg)
    dlg.name.setText("one_braking")
    dlg.set_method("condition")
    cb = dlg.arg_widgets["condition"]
    assert "Fill in a row" in dlg.problem()
    cb.add_row()
    for (sig, op, val), name in zip(cb.rows, ("fb", "pb")):
        sig.setCurrentText(name)
        op.setCurrentText("==")
        assert val.pick_action.isVisible() or not dlg.isVisible()  # enum values can be picked
        cb.pick_value(val, "==", "ACTIVE")
    cb.links[1][0].setCurrentText("xor")
    expr = dlg.values()["expr"]
    assert expr == "(fb == ACTIVE) ^ (pb == ACTIVE)"
    c = compile_expr(expr, names={"fb", "pb"}, enums=enums)
    out = c.fn(Env(time=np.arange(4.0), values={"fb": np.array([2, 2, 0, 0]), "pb": np.array([2, 0, 2, 0])}))
    assert list(out) == [False, True, True, False]
    again = DerivedBuilder(["fb", "pb"], {}, name="one_braking", expr=expr, enums_fn=lambda: enums)
    qtbot.addWidget(again)
    assert again.current_method().key == "condition"
    assert again.arg_widgets["condition"].links[1][0].currentText() == "xor"
    assert again.values()["expr"] == expr


def test_dialog_height_follows_the_condition_rows(qtbot):
    dlg = DerivedBuilder(["a", "b"], {}, taken=set(), name="x", expr="a == 1 or b == 2")
    qtbot.addWidget(dlg)
    dlg.show()
    qtbot.wait(150)
    cb, start = dlg.arg_widgets["condition"], dlg.height()
    cb.add_row()
    cb.add_row()
    qtbot.wait(150)
    assert dlg.height() > start
    lay = dlg.layout()
    assert dlg.height() >= lay.totalHeightForWidth(dlg.width())  # nothing cut off
    cb.delete_row(3)
    cb.delete_row(2)
    qtbot.wait(150)
    assert dlg.height() == start  # and it shrinks back
