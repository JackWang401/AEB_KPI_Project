import numpy as np
import pytest

from src.kpi_studio.core.expr import Env, ExprError, MissingSignal, compile_expr

T = np.arange(5, dtype=float)
ENUM = {"aebRequest": {"AUTO_EMERGENCY_BRAKING_REQUEST_NONE": 0, "AUTO_EMERGENCY_BRAKING_REQUEST_PARTIAL": 1,
                       "AUTO_EMERGENCY_BRAKING_REQUEST_FULL": 2}}
ENV = Env(time=T, values={"v": np.array([0, 1, 2, 3, 4.0]), "aebRequest": np.array([0, 1, 2, 0, 2.0])})


def ev(src, names=("v", "aebRequest")):
    return compile_expr(src, names=set(names), enums=ENUM).evaluate(ENV)


def test_arithmetic_and_compare():
    np.testing.assert_allclose(ev("v * 2 + 1"), [1, 3, 5, 7, 9])
    assert ev("1 < v <= 3").tolist() == [False, False, True, True, False]
    assert ev("v > 1 and not v > 3").tolist() == [False, False, True, True, False]


def test_enum_names_and_in():
    assert ev("aebRequest == FULL").tolist() == [False, False, True, False, True]
    assert ev("aebRequest in (FULL, 'PARTIAL')").tolist() == [False, True, True, False, True]
    assert ev("aebRequest not in (0,)").tolist() == [False, True, True, False, True]
    with pytest.raises(ExprError, match="not a value"):
        ev("aebRequest == BOGUS")


def test_tools_in_expressions():
    np.testing.assert_allclose(ev("max(v, 2)"), [2, 2, 2, 3, 4])
    np.testing.assert_allclose(ev("abs(v - 2)"), [2, 1, 0, 1, 2])
    assert ev("rising(v > 1.5)").tolist() == [False, False, True, False, False]


def test_scalar_result_is_broadcast():
    assert ev("3").shape == T.shape


@pytest.mark.parametrize("bad", [
    "__import__('os')", "v.real", "v[0]", "lambda: 1", "[x for x in v]", "open('f')",
    "v if v else 1", "lowpass(v)", "lowpass(v, cutoff_hz=-1)", "unknown_sig + 1", "'text'",
])
def test_rejected(bad):
    with pytest.raises(ExprError):
        ev(bad)


def test_suggests_close_names():
    with pytest.raises(ExprError, match="did you mean v"):
        compile_expr("vv + 1", names={"v"})


def test_missing_signal_raised_at_runtime():
    c = compile_expr("w + 1", names={"w"})
    with pytest.raises(MissingSignal):
        c.evaluate(ENV)
    assert c.deps == {"w"}


def test_value_change_conditions():
    env = Env(time=T, values={"s": np.array([0, 3, 3, 1, 3.0])})
    run = lambda src: compile_expr(src, names={"s"}, enums={}).evaluate(env).tolist()  # noqa: E731
    assert run("rising(s == 3)") == [False, True, False, False, True]            # changes to 3
    assert run("falling(s == 3)") == [False, False, False, True, False]          # changes from 3
    assert run("(falling(s == 0) and rising(s == 3))") == [False, True, False, False, False]  # 0 -> 3
    assert run("rising(s in (1, 3))") == [False, True, False, False, False]
