"""Known-answer tests for toolbox functions, including parity with the code they came from."""

import numpy as np
import pytest

from src.kpi_studio import toolbox
from src.kpi_studio.toolbox.registry import ToolContext
from src.utils.event_detector.as_long.aeb import detect_aeb_events, find_aeb_intv_end, find_aeb_intv_start
from src.utils.event_detector.as_long.decel import detect_decel_onset
from src.utils.event_detector.as_long.fcw import detect_fcw_events
from src.utils.signal_filters import accel_filter

T = np.arange(0, 10, 0.01)
CTX = ToolContext(T)


def f(kind, name, *xs, **params):
    spec = toolbox.get(kind, name)
    bound = spec.bind_params([], params)
    return spec.func(CTX, *xs, **bound)


def test_lowpass_matches_accel_filter():
    x = np.sin(T * 3) + np.random.default_rng(1).normal(scale=0.3, size=T.size)
    np.testing.assert_allclose(f("signal", "lowpass", x, cutoff_hz=10), accel_filter(T, x, 10))


def test_lowpass_keeps_nan_positions():
    x = np.sin(T)
    x[100:120] = np.nan
    out = f("signal", "lowpass", x, cutoff_hz=5)
    assert np.isnan(out[100:120]).all() and np.isfinite(out[:100]).all()


def test_derivative_and_integral():
    np.testing.assert_allclose(f("signal", "derivative", 2 * T)[1:-1], 2.0)
    assert f("signal", "integral", np.ones_like(T))[-1] == pytest.approx(T[-1])


def test_hold_and_debounce():
    c = (T > 2) & (T < 2.05) | (T > 5)
    held = f("condition", "hold", c, seconds=0.1)
    assert not held[(T > 2) & (T < 3)].any()
    assert held[T > 5.11].all()
    deb = f("condition", "debounce", c, on_s=0.1, off_s=0.0)
    assert not deb[T < 4].any() and deb[T > 5.2].all()


def test_crossing_is_interpolated():
    x = T - 3.333
    assert f("moment", "crossing", x, level=0.0) == pytest.approx(3.333, abs=1e-9)
    assert f("moment", "crossing", x, level=0.0, direction="falling") is None


def test_first_with_hold():
    c = ((T > 1) & (T < 1.02)) | (T > 4)
    assert f("moment", "first", c) == pytest.approx(1.01)
    assert f("moment", "first", c, hold=0.1) == pytest.approx(4.01)


def test_decel_onset_matches_legacy():
    jerk = np.where(T > 2.5, -30.0, 0.0)
    assert f("moment", "decel_onset", jerk, threshold=-25) == pytest.approx(T[detect_decel_onset(jerk, -25)])


def test_time_mean_and_distance():
    assert f("reducer", "time_mean", np.full_like(T, -7.0)) == pytest.approx(-7.0)
    assert f("reducer", "distance", np.full_like(T, 10.0)) == pytest.approx(10.0 * T[-1])
    assert f("reducer", "time_true", T > 5) == pytest.approx(T[-1] - 5.0, abs=0.02)


def test_aeb_request_preset_matches_detector():
    req = np.zeros_like(T)
    req[(T > 1) & (T < 2)] = 2
    req[(T > 4) & (T < 6)] = 1
    s, e = detect_aeb_events(T, req)
    assert f("event", "aeb_request", req) == list(zip(s.tolist(), e.tolist()))


def test_fcw_preset_matches_detector():
    req = np.zeros_like(T)
    req[(T > 1) & (T < 2)] = 3
    req[(T > 2.5) & (T < 3)] = 2
    req[(T > 7) & (T < 8)] = 2
    s, e = detect_fcw_events(T, req, merge_window=2.0)
    assert f("event", "fcw_request", req, merge_window=2.0) == list(zip(s.tolist(), e.tolist()))


def test_target_decel_window_matches_find_aeb_intv():
    decel = np.zeros_like(T)
    decel[(T >= 3) & (T < 6)] = -6.0
    speed = np.where(T < 5.5, 10.0, 0.0)
    events = f("event", "target_decel_window", decel, speed)
    i0, t0 = find_aeb_intv_start({"aebTargetDecel": decel, "time": T}, -6.0)
    _, i1, t1 = find_aeb_intv_end({"time": T, "egoSpeed": speed, "aebTargetDecel": decel}, i0, -4.9)
    assert events == [(pytest.approx(t0), pytest.approx(t1))]


def test_ttc_is_nan_when_not_closing():
    out = f("signal", "ttc", np.full_like(T, 20.0), np.where(T < 5, 10.0, -1.0))
    assert out[0] == pytest.approx(2.0) and np.isnan(out[-1])


def test_units_conversion():
    from src.kpi_studio.toolbox.units import conversion_factor

    assert conversion_factor("km/h", "m/s") == pytest.approx(1 / 3.6)
    assert conversion_factor("[m/s2]", "g") == pytest.approx(1 / 9.80665)
    assert conversion_factor("deg", "rad") == pytest.approx(np.pi / 180)
    assert conversion_factor("m", "s") is None

