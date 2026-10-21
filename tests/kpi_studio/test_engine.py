import numpy as np
import pytest
import yaml

from src.kpi_studio.core.align import RawSignal, build_frame
from src.kpi_studio.core.engine import edges_to_intervals, evaluate_frame, sample_at, clean_intervals
from src.kpi_studio.core.model import Profile
from src.kpi_studio.core.results import Status
from src.kpi_studio.core.validate import ProfileError, compile_profile

BASE = """
name: t
signals:
  speed: {channels: [v], unit: m/s}
  req:   {channels: [r], kind: discrete}
  opt:   {channels: [o], required: false}
events:
  e:
    start: req > 0
    window: {pre: 1, post: 1}
    moments:
      half:  {crossing: speed, level: 5, direction: falling, after: start}
      later: {offset: half, by: 0.5}
      nope:  {first: speed > 1000}
      dep:   {first: speed < 1, after: nope}
kpis:
  - {name: v0, event: e, value: speed, at: start}
  - {name: t_half, event: e, duration: [start, half]}
  - {name: t_later, event: e, duration: [start, later]}
  - {name: vmax, event: e, reduce: max, of: speed}
  - {name: twice, event: e, formula: v0 * 2}
  - {name: never, event: e, value: speed, at: nope}
  - {name: chained, event: e, value: speed, at: dep}
  - {name: optional, event: e, value: opt, at: start}
  - {name: needs_never, event: e, formula: never + 1}
"""


def compile_yaml(text, **over):
    data = yaml.safe_load(text)
    data.update(over)
    return compile_profile(Profile.model_validate(data))


def frame(cp):
    t = np.arange(0, 10, 0.01)
    req = ((t > 2) & (t < 4)).astype(float)
    speed = np.clip(10 - np.clip(t - 2, 0, None) * 5, 0, None)
    raw = {"speed": RawSignal("speed", t, speed, "m/s"), "req": RawSignal("req", t, req)}
    fr, _, _ = build_frame(raw, cp.profile.signals, 100)
    return fr


def test_full_flow_statuses():
    cp = compile_yaml(BASE)
    out = evaluate_frame(cp, frame(cp), "f")
    r = {x.kpi: x for x in out.results}
    assert len(out.occurrences) == 1
    assert r["v0"].value == pytest.approx(10.0, abs=0.06)
    assert r["t_half"].value == pytest.approx(1.0, abs=0.02)
    assert r["t_later"].value == pytest.approx(1.5, abs=0.02)
    assert r["vmax"].value == pytest.approx(10.0)
    assert r["twice"].value == pytest.approx(2 * r["v0"].value)
    assert r["never"].status == Status.MOMENT_NOT_FOUND
    assert r["chained"].status == Status.MOMENT_NOT_FOUND
    assert r["optional"].status == Status.MISSING_SIGNAL
    assert r["needs_never"].status == Status.MOMENT_NOT_FOUND
    assert out.status == Status.OK


def test_validation_collects_all_errors():
    bad = BASE.replace("at: start}", "at: strat}", 1).replace("reduce: max", "reduce: maxx") \
              .replace("speed > 1000", "sped > 1000")
    with pytest.raises(ProfileError) as exc:
        compile_yaml(bad)
    text = str(exc.value)
    assert "strat" in text and "maxx" in text and "sped" in text


def test_unknown_key_is_rejected():
    with pytest.raises(Exception, match="Extra inputs|extra"):
        Profile.model_validate({"name": "x", "signals": {"a": {"channels": ["a"], "uint": "m"}}})


def test_moment_cycle_detected():
    text = BASE.replace("later: {offset: half, by: 0.5}", "later: {offset: dep, by: 0.5}") \
               .replace("dep:   {first: speed < 1, after: nope}", "dep:   {first: speed < 1, after: later}")
    with pytest.raises(ProfileError, match="loop"):
        compile_yaml(text)


def test_formula_may_only_use_earlier_kpis():
    text = BASE + "  - {name: early, event: e, formula: late + 1}\n  - {name: late, event: e, formula: v0}\n"
    with pytest.raises(ProfileError, match="listed earlier"):
        compile_yaml(text)


def test_edges_and_merge():
    t = np.arange(10.0)
    c = np.array([0, 1, 1, 0, 1, 0, 0, 0, 1, 1], bool)
    assert edges_to_intervals(t, c, None) == [(1, 3), (4, 5), (8, 9)]
    assert clean_intervals([(1, 3), (4, 5), (8, 9)], merge_gap=1.0, min_duration=0) == [(1, 5), (8, 9)]
    assert clean_intervals([(1, 3), (4, 5)], merge_gap=0, min_duration=1.5) == [(1, 3)]


def test_several_windows_per_file_with_first_or_last_rules():
    t = np.arange(20.0)
    start = np.isin(np.arange(20), [1, 3, 10, 12])
    end = np.isin(np.arange(20), [5, 7, 15, 17])
    assert edges_to_intervals(t, start, end) == [(1, 5), (10, 15)]
    assert edges_to_intervals(t, start, end, start_rule="last") == [(3, 5), (12, 15)]
    assert edges_to_intervals(t, start, end, end_rule="last") == [(1, 7), (10, 17)]
    assert edges_to_intervals(t, start, end, "last", "last") == [(3, 7), (12, 17)]


def test_end_rule_last_needs_an_end():
    import pytest
    from src.kpi_studio.core.model import EventDef
    with pytest.raises(ValueError):
        EventDef(start="a > 1", end_rule="last")
    assert EventDef(start="a > 1", end="b > 1", start_rule="last", end_rule="last").end_rule == "last"


def test_sample_at_hold_vs_interp():
    t = np.array([0.0, 1.0])
    v = np.array([0.0, 10.0])
    assert sample_at(t, v, 0.5, hold=False) == 5.0
    assert sample_at(t, v, 0.5, hold=True) == 0.0
    assert np.isnan(sample_at(t, v, 2.0, hold=False))


def test_align_converts_units_and_holds_discrete():
    from src.kpi_studio.core.model import SignalDef

    defs = {"v": SignalDef(channels=["v"], unit="m/s"), "s": SignalDef(channels=["s"], kind="discrete", required=False)}
    raw = {"v": RawSignal("v", np.array([0.0, 1.0, 2.0]), np.array([36.0, 36.0, 72.0]), "km/h"),
           "s": RawSignal("s", np.array([0.0, 1.0, 1.0, 0.5]), np.array([1.0, 2.0, 9.0, 5.0]))}
    fr, issues, uses = build_frame(raw, defs, rate_hz=2)
    np.testing.assert_allclose(fr.values["v"], [10, 10, 10, 15, 20])
    # unsorted + duplicate timestamps cleaned, then held
    np.testing.assert_allclose(fr.values["s"][:3], [1, 5, 2])
    assert any("duplicate" in i.message for i in issues)
    assert uses[0].converted_to == "m/s"


def test_text_enum_samples_are_decoded():
    from src.kpi_studio.core.model import SignalDef

    defs = {"r": SignalDef(channels=["r"], kind="discrete", enum="E")}
    raw = {"r": RawSignal("r", np.arange(3.0), np.array([b"E_NONE", b"E_FULL", b"???"]))}
    fr, issues, _ = build_frame(raw, defs, 1, {"E": {"E_NONE": 0, "E_FULL": 2}})
    assert fr.values["r"][:2].tolist() == [0.0, 2.0] and np.isnan(fr.values["r"][2])
    assert any("could not be mapped" in i.message for i in issues)


def test_no_occurrences_says_why():
    import numpy as np

    from src.kpi_studio.core.align import SignalFrame
    from src.kpi_studio.core.engine import evaluate_frame
    from src.kpi_studio.core.validate import compile_profile
    from src.kpi_studio.io.profile_store import parse_profile

    prof = parse_profile({"name": "p", "signals": {"ax": {"channels": ["ax"]}},
                          "events": {"e": {"start": "rising(ax == 3)"}}}, "t")
    cp = compile_profile(prof)
    t = np.arange(0, 1, 0.1)
    frame = SignalFrame(time=t, values={"ax": np.linspace(4, -10, t.size)})
    out = evaluate_frame(cp, frame, "f")
    msg = [i.message for i in out.issues if "no occurrences" in i.message][0]
    assert "never true" in msg and "rising(ax == 3)" in msg


def test_kpi_unit_comes_from_its_signal():
    from src.kpi_studio.core.model import KpiDef
    from src.kpi_studio.core.validate import expr_unit, kpi_unit

    units = {"v": "kph", "a": "m/s^2", "b": "m/s^2", "n": ""}
    assert expr_unit("v", units) == "kph"
    assert expr_unit("abs(lowpass(a, 5))", units) == "m/s^2"
    assert expr_unit("a - b", units) == "m/s^2"
    assert expr_unit("v * 3.6", units) is None
    assert expr_unit("to_kph(a)", units) == "km/h"
    assert kpi_unit(KpiDef(name="k", event="e", value="v", at="start", unit="m/s"), units) == "kph"  # signal wins
    assert kpi_unit(KpiDef(name="k", event="e", value="n", at="start", unit="-"), units) == "-"  # fallback
    assert kpi_unit(KpiDef(name="k", event="e", duration=("start", "end")), units) == "s"
    assert kpi_unit(KpiDef(name="k", event="e", reduce="max", of="a"), units) == "m/s^2"
    assert kpi_unit(KpiDef(name="k", event="e", reduce="count_rising", of="v > 1"), units) == ""


def test_end_never_met_is_reported():
    from src.kpi_studio.core.align import SignalFrame
    from src.kpi_studio.core.validate import compile_profile
    from src.kpi_studio.io.profile_store import parse_profile

    prof = parse_profile({"name": "p", "signals": {"fb": {"channels": ["fb"]}, "pb": {"channels": ["pb"]}},
                          "events": {"e": {"start": "rising(fb == 4)",
                                           "end": "falling(fb == 4) and falling(pb == 4)"}}}, "t")
    cp = compile_profile(prof)
    t = np.arange(0, 10, 1.0)
    fb = np.array([1, 4, 4, 1, 1, 4, 4, 1, 1, 1], float)
    frame = SignalFrame(time=t, values={"fb": fb, "pb": np.full(t.size, 4.0)})  # pb never falls
    out = evaluate_frame(cp, frame, "f")
    assert len(out.occurrences) == 1 and out.occurrences[0].t_end == 9.0  # runs to the end of the file
    msg = next(i.message for i in out.issues if "end condition" in i.message)
    assert "never true in this file" in msg and "runs to the end of the file" in msg
    prof = parse_profile({"name": "p", "signals": {"fb": {"channels": ["fb"]}},
                          "events": {"e": {"start": "rising(fb == 4)", "end": "falling(fb == 4)"}}}, "t")
    out = evaluate_frame(compile_profile(prof), SignalFrame(time=t, values={"fb": fb}), "f")
    assert [(o.t_start, o.t_end) for o in out.occurrences] == [(1.0, 3.0), (5.0, 7.0)]
    assert not [i for i in out.issues if "end condition" in i.message]


def test_which_kpi_names_the_conditions_met():
    from src.kpi_studio.core.align import SignalFrame
    from src.kpi_studio.core.validate import compile_profile
    from src.kpi_studio.io.profile_store import parse_profile
    from src.kpi_studio.report.excel_writer import summary_table

    states = {"IDLE": 1, "ACTIVE": 4}
    prof = parse_profile({
        "name": "p",
        "signals": {"fb_state": {"channels": ["fb"], "kind": "enum", "enum": states},
                    "pb_state": {"channels": ["pb"], "kind": "enum", "enum": states}},
        "events": {"e": {"start": "rising(fb_state == ACTIVE) or rising(pb_state == ACTIVE)",
                         "end": "fb_state != ACTIVE and pb_state != ACTIVE"}},
        "kpis": [{"name": "braking_type", "event": "e",
                  "which": {"PB": "pb_state == ACTIVE", "FB": "fb_state == ACTIVE"}}]}, "t")
    cp = compile_profile(prof)
    t = np.arange(0, 16, 1.0)
    fb = np.array([1, 4, 4, 1, 1, 1, 1, 1, 4, 4, 4, 1, 1, 1, 1, 1], float)
    pb = np.array([1, 1, 1, 1, 4, 4, 1, 1, 1, 4, 4, 1, 1, 1, 1, 1], float)
    out = evaluate_frame(cp, SignalFrame(time=t, values={"fb_state": fb, "pb_state": pb}), "f")
    assert [r.text for r in out.results] == ["FB", "PB", "PB + FB"]
    assert [r.value for r in out.results] == [2.0, 1.0, 3.0]  # a code for formulas: PB = 1, FB = 2
    table, _ = summary_table([out], ["braking_type"], {})
    assert list(table["braking_type"]) == ["FB", "PB", "PB + FB"]


def test_kpis_over_named_ranges():
    from src.kpi_studio.core.align import SignalFrame
    from src.kpi_studio.core.validate import compile_profile
    from src.kpi_studio.io.profile_store import parse_profile

    prof = parse_profile({
        "name": "p",
        "signals": {"on": {"channels": ["on"]}, "v": {"channels": ["v"]}},
        "events": {"e": {"start": "on > 0", "end": "on < 1", "window": {"pre": 1.0, "post": 1.0},
                         "moments": {"slow": {"first": "v < 3", "after": "start"}},
                         "ranges": {"braking": ["start", "slow"]}}},
        "kpis": [{"name": "vmax", "event": "e", "reduce": "max", "of": "v", "over": "braking"},
                 {"name": "len", "event": "e", "reduce": "duration", "over": "braking"},
                 {"name": "evlen", "event": "e", "reduce": "duration", "over": "event"},
                 {"name": "moving", "event": "e", "reduce": "all", "of": "v > 0", "over": "window"}]}, "t")
    cp = compile_profile(prof)
    assert [k.unit for k in cp.kpis][1:3] == ["s", "s"]
    t = np.arange(0, 10, 1.0)
    on = np.array([0, 0, 1, 1, 1, 1, 0, 0, 0, 0], float)
    v = np.array([9, 9, 8, 6, 2, 1, 0, 0, 0, 0], float)
    out = evaluate_frame(cp, SignalFrame(time=t, values={"on": on, "v": v}), "f")
    assert {r.kpi: r.value for r in out.results} == {"vmax": 8.0, "len": 2.0, "evlen": 4.0, "moving": 0.0}


def test_change_is_the_difference_at_the_two_moments():
    from src.kpi_studio.core.align import SignalFrame
    from src.kpi_studio.core.validate import compile_profile
    from src.kpi_studio.io.profile_store import parse_profile

    prof = parse_profile({
        "name": "p",
        "signals": {"on": {"channels": ["on"]}, "v": {"channels": ["v"], "unit": "km/h"}},
        "events": {"e": {"start": "on > 0", "end": "on < 1",
                         "moments": {"slow": {"first": "v < 3", "after": "start"},
                                     "mid": {"offset": "start", "by": 0.5}},
                         "ranges": {"braking": ["start", "slow"], "half": ["start", "mid"]}}},
        "kpis": [{"name": "dv", "event": "e", "reduce": "change", "of": "v", "over": "braking"},
                 {"name": "dv_half", "event": "e", "reduce": "change", "of": "v", "over": "half"}]}, "t")
    cp = compile_profile(prof)
    assert cp.kpis[0].unit == "km/h"
    t = np.arange(0, 10, 1.0)
    on = np.array([0, 0, 1, 1, 1, 1, 0, 0, 0, 0], float)
    v = np.array([9, 9, 8, 6, 2, 1, 0, 0, 0, 0], float)
    out = evaluate_frame(cp, SignalFrame(time=t, values={"on": on, "v": v}), "f")
    assert {r.kpi: r.value for r in out.results} == {"dv": -6.0, "dv_half": -1.0}  # 2 - 8; 7 (interpolated) - 8


def test_unknown_range_is_an_error():
    from src.kpi_studio.core.validate import compile_profile
    from src.kpi_studio.io.profile_store import parse_profile

    prof = parse_profile({
        "name": "p", "signals": {"v": {"channels": ["v"]}},
        "events": {"e": {"start": "v > 0", "ranges": {"r": ["start", "nowhere"]}}},
        "kpis": [{"name": "k", "event": "e", "reduce": "max", "of": "v", "over": "nope"}]}, "t")
    with pytest.raises(Exception) as exc:
        compile_profile(prof)
    assert "unknown range 'nope'" in str(exc.value) and "unknown moment 'nowhere'" in str(exc.value)


@pytest.mark.parametrize("unit, speed, metres", [("km/h", 36.0, 40.0), ("m/s", 10.0, 40.0), ("mph", 10.0, 17.8816),
                                                ("", 10.0, 40.0)])
def test_distance_converts_speed_from_its_unit(unit, speed, metres):
    from src.kpi_studio.core.align import SignalFrame
    from src.kpi_studio.io.profile_store import parse_profile

    prof = parse_profile({
        "name": "p",
        "signals": {"v": {"channels": ["v"], "unit": unit or None}, "on": {"channels": ["on"]}},
        "events": {"e": {"start": "on > 0", "end": "on < 1"}},
        "kpis": [{"name": "dist", "event": "e", "reduce": "distance", "of": "v", "between": ["start", "end"]}]}, "t")
    cp = compile_profile(prof)
    assert cp.kpis[0].unit == "m"
    assert bool(cp.warnings) == (unit == "")  # an unknown unit is taken as m/s, with a warning
    t = np.arange(0, 10, 1.0)
    on = np.array([0, 1, 1, 1, 1, 0, 0, 0, 0, 0], float)  # 1 s .. 5 s
    out = evaluate_frame(cp, SignalFrame(time=t, values={"v": np.full_like(t, speed), "on": on}), "f")
    assert out.results[0].value == pytest.approx(metres)


def test_distance_rejects_a_value_that_is_not_a_speed():
    from src.kpi_studio.io.profile_store import parse_profile

    prof = parse_profile({
        "name": "p", "signals": {"a": {"channels": ["a"], "unit": "m/s^2"}},
        "events": {"e": {"start": "a > 0"}},
        "kpis": [{"name": "dist", "event": "e", "reduce": "distance", "of": "a"}]}, "t")
    with pytest.raises(ProfileError, match="distance needs a value in m/s"):
        compile_profile(prof)


@pytest.mark.parametrize("a_on, b_on, says", [([0, 1, 1, 1, 0, 0], [0, 1, 1, 1, 0, 0], "are at the same time"),
                                               ([0, 0, 1, 1, 0, 0], [0, 1, 1, 1, 0, 0], "the range is reversed")])
def test_reduce_over_an_empty_range_says_why(a_on, b_on, says):
    from src.kpi_studio.core.align import SignalFrame
    from src.kpi_studio.io.profile_store import parse_profile

    prof = parse_profile({
        "name": "p", "signals": {n: {"channels": [n]} for n in ("v", "on", "ma", "mb")},
        "events": {"e": {"start": "on > 0", "end": "on < 1",
                         "moments": {"a": {"first": "ma > 0"}, "b": {"first": "mb > 0"}}}},
        "kpis": [{"name": "dist", "event": "e", "reduce": "integral", "of": "v", "between": ["a", "b"]}]}, "t")
    cp = compile_profile(prof)
    t = np.arange(0, 6, 1.0)
    vals = {"v": np.full_like(t, 5.0), "on": np.array([0, 1, 1, 1, 0, 0], float),
            "ma": np.array(a_on, float), "mb": np.array(b_on, float)}
    r = evaluate_frame(cp, SignalFrame(time=t, values=vals), "f").results[0]
    assert r.value is None and says in r.message, r.message


def test_unit_converters_read_the_unit_column():
    import numpy as np

    from src.kpi_studio import toolbox
    from src.kpi_studio.core.expr import Env, ExprError, compile_expr
    from src.kpi_studio.toolbox.units import CONVERTERS

    offered = {s.name for s in toolbox.tools("signal") if s.category == "Units"}
    assert set(CONVERTERS) <= offered
    units = {"v": "km/h", "a": "g", "r": "rad/s", "n": ""}
    t = np.array([0.0, 1.0])

    def run(src):
        c = compile_expr(src, names=set(units), units=units)
        return c.fn(Env(time=t, values={k: np.array([36.0, 72.0]) for k in units}))

    assert np.allclose(run("to_mps(v)"), [10, 20])
    assert np.allclose(run("to_mps2(a)"), [36 * 9.80665, 72 * 9.80665])
    assert np.allclose(run("to_deg(r)"), np.degrees([36, 72]))  # a rate stays a rate: deg/s
    assert np.allclose(run('to_cm(n, "m")'), [3600, 7200])  # no unit: given in the call
    with pytest.raises(ExprError, match="has no unit"):
        run("to_cm(n)")
    with pytest.raises(ExprError, match="cannot convert"):
        run("to_km(v)")
    assert np.allclose(run("to_kph(n)"), [36 * 3.6, 72 * 3.6])  # older profiles: m/s assumed
