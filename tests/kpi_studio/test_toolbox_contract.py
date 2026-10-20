"""Contract tests: every registered tool must survive awkward input without crashing
and return the right shape/type. New tools are covered automatically."""

import numpy as np
import pytest

from src.kpi_studio import toolbox
from src.kpi_studio.toolbox.registry import ToolContext


def _inputs(n):
    t = np.arange(n) * 0.01
    cases = {
        "constant": np.full(n, 3.0),
        "step": np.where(np.arange(n) >= n // 2, 1.0, 0.0),
        "ramp": np.linspace(-5, 5, n),
        "noise": np.random.default_rng(0).normal(size=n),
        "nan_gap": np.where((np.arange(n) > n // 3) & (np.arange(n) < n // 2), np.nan, np.linspace(0, 1, n)),
        "all_nan": np.full(n, np.nan),
        "bool": np.arange(n) % 7 < 3,
    }
    return t, cases


def _call(spec, t, x):
    ctx = ToolContext(t)
    params = {p.name: p.example_value() for p in spec.params}
    n_in = 2 if spec.inputs == -1 else spec.inputs
    return spec.func(ctx, *([x] * n_in), **params)


ALL = [s for s in toolbox.tools(include_hidden=True)]  # hidden ones still run older profiles


@pytest.mark.parametrize("spec", ALL, ids=lambda s: f"{s.kind}:{s.name}")
@pytest.mark.parametrize("n", [0, 1, 2, 7, 500])
def test_tool_survives_awkward_input(spec, n):
    t, cases = _inputs(n)
    for case, x in cases.items():
        out = _call(spec, t, x)
        where = f"{spec.kind}:{spec.name} on {case} (n={n})"
        if spec.kind in ("signal", "condition"):
            arr = np.asarray(out)
            assert arr.shape == (n,), where
            if spec.kind == "condition":
                assert arr.dtype == bool, where
        elif spec.kind == "moment":
            assert out is None or (isinstance(out, float) and (n == 0 or t[0] - 1 <= out <= t[-1] + 1)), where
        elif spec.kind == "reducer":
            assert isinstance(out, float), where
        elif spec.kind == "event":
            assert isinstance(out, list), where
            for s, e in out:
                assert s <= e, where


@pytest.mark.parametrize("spec", ALL, ids=lambda s: f"{s.kind}:{s.name}")
def test_tool_is_documented(spec):
    assert spec.summary, f"{spec.name} needs a docstring; the GUI shows it"
    for p in spec.params:
        assert p.required is False or p.example is not None or p.type in (float, int, bool), p.name


def test_bind_params_validates():
    spec = toolbox.get("signal", "lowpass")
    assert spec.bind_params([10], {}) == {"cutoff_hz": 10.0, "order": 4}
    with pytest.raises(toolbox.ToolError):
        spec.bind_params([], {})
    with pytest.raises(toolbox.ToolError):
        spec.bind_params([-1], {})
    with pytest.raises(toolbox.ToolError):
        spec.bind_params([10], {"nope": 1})


def test_user_tools_are_loaded_and_marked(tmp_path):
    (tmp_path / "mine.py").write_text(
        "from src.kpi_studio.toolbox import tool\n"
        "@tool('signal', 'Mine')\n"
        "def double_it(ctx, x):\n"
        "    '''Twice x.'''\n"
        "    return x * 2\n")
    loaded = toolbox.load_user_tools(tmp_path)
    assert loaded and toolbox.get("signal", "double_it").origin == "user"


def test_domain_specific_tools_are_not_offered_but_still_run():
    from src.kpi_studio.core.expr import compile_expr

    hidden = {"decel_onset", "jerk_end", "vehicle_stop", "jerk", "to_kph", "to_mps", "to_g", "rel_speed", "ttc",
              "distance_travelled"}
    offered = {s.name for s in toolbox.tools()}
    assert not hidden & offered
    assert hidden <= {s.name for s in toolbox.tools(include_hidden=True)}
    assert "Braking" not in {s.category for s in toolbox.tools("moment")}
    compile_expr("ttc(d, v) + jerk(a)", names={"d", "v", "a"})  # an older profile's expressions still compile
