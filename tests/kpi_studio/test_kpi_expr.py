import pytest

from src.kpi_studio.core.kpi_expr import KpiExprError, kpi_expr_text, parse_kpi_expr

M, R = ["start", "end", "veh_stopped"], ["event", "window", "braking"]
KPIS = ["too_close", "speed", "gap_kpi"]  # KPIs listed earlier: a formula may use them


@pytest.mark.parametrize("text, fields, canonical", [
    ("gap.change(event).abs()", {"reduce": "change", "of": "gap", "over": "event", "then": ["abs"]}, None),
    ("change(gap, event).abs()", {"reduce": "change", "of": "gap", "over": "event", "then": ["abs"]},
     "gap.change(event).abs()"),
    ("abs(change(gap, braking))", {"reduce": "change", "of": "gap", "over": "braking", "then": ["abs"]},
     "gap.change(braking).abs()"),
    ("abs(gap).change(event)", {"reduce": "change", "of": "abs(gap)", "over": "event"}, None),
    ("(a - b).max(window, k=2)", {"reduce": "max", "of": "a - b", "over": "window", "args": {"k": 2}}, None),
    ("egoSpeed @ veh_stopped", {"value": "egoSpeed", "at": "veh_stopped"}, None),
    ("(a - b) @ start", {"value": "a - b", "at": "start"}, None),
    ("(egoSpeed @ veh_stopped).to_kph()", {"value": "egoSpeed", "at": "veh_stopped", "then": ["to_kph"]}, None),
    ("abs(egoSpeed @ veh_stopped)", {"value": "egoSpeed", "at": "veh_stopped", "then": ["abs"]},
     "(egoSpeed @ veh_stopped).abs()"),
    ("egoSpeed.veh_stopped().to_kph()", {"value": "egoSpeed", "at": "veh_stopped", "then": ["to_kph"]},
     "(egoSpeed @ veh_stopped).to_kph()"),  # the earlier spellings still read
    ("veh_stopped(egoSpeed)", {"value": "egoSpeed", "at": "veh_stopped"}, "egoSpeed @ veh_stopped"),
    ("duration(braking)", {"reduce": "duration", "over": "braking"}, None),
    ("which(braking, PB=pb == 1, FB=fb == 1)", {"which": {"PB": "pb == 1", "FB": "fb == 1"}, "over": "braking"},
     None),
    ("which(event, {'Full brake': fb == 1})", {"which": {"Full brake": "fb == 1"}, "over": "event"}, None),
    ("too_close or speed > 0", {"formula": "too_close or speed > 0"}, None),
    ("abs(speed - gap_kpi)", {"formula": "abs(speed - gap_kpi)"}, None),
])
def test_kpi_expression_round_trip(text, fields, canonical):
    assert parse_kpi_expr(text, M, R, KPIS) == fields
    assert kpi_expr_text(fields) == (canonical or text)


@pytest.mark.parametrize("text, message", [
    ("gap.change()", "takes a signal and a range"),
    ("gap.change(nowhere)", "unknown range 'nowhere'"),
    ("gap", "write the KPI as"),
    ("gap.veh_stopped(1)", "a moment takes only the signal"),
    ("gap @ nowhere", "'nowhere' is not a moment"),
    ("gap.change(event", "not a valid expression"),
    ("which(braking)", "at least one labelled condition"),
    ("too_close or later_kpi", "write the KPI as"),
])
def test_kpi_expression_errors(text, message):
    with pytest.raises(KpiExprError, match=message):
        parse_kpi_expr(text, M, R, KPIS)
