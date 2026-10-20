"""Signal tools: array in, array out. Usable in `derived` and any expression."""

from __future__ import annotations

import numpy as np
from scipy.ndimage import median_filter as _median_filter
from scipy.signal import butter, filtfilt

from src.utils.signal_filters import accel_filter
from src.kpi_studio.toolbox import units as _units
from src.kpi_studio.toolbox.registry import Param, tool


def _f(x) -> np.ndarray:
    return np.asarray(x, dtype=float)


def _fill_nan(x: np.ndarray, t: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Linearly bridge NaN gaps so filters can run; returns (filled, nan_mask)."""
    mask = ~np.isfinite(x)
    if not mask.any():
        return x, mask
    if mask.all():
        return x, mask
    filled = x.copy()
    filled[mask] = np.interp(t[mask], t[~mask], x[~mask])
    return filled, mask


# --------------------------------------------------------------------------- filters
@tool("signal", "Filters", unit="same", params=(
    Param("cutoff_hz", float, min=1e-6, example=10.0, doc="Cutoff frequency in Hz"),
    Param("order", int, 4, min=1, max=10, doc="Butterworth order"),
))
def lowpass(ctx, x, cutoff_hz, order):
    """Zero-phase Butterworth low-pass filter (same filter as the current tool's accel_filter).

    NaN gaps are bridged for filtering and restored afterwards; signals too short
    for the filter are returned unchanged.
    """
    x = _f(x)
    if x.size == 0:
        return x
    filled, mask = _fill_nan(x, ctx.time)
    if mask.all():
        return x
    out = np.asarray(accel_filter(ctx.time, filled, cutoff_hz, order=order), dtype=float)
    out[mask] = np.nan
    return out


@tool("signal", "Filters", unit="same", params=(
    Param("cutoff_hz", float, min=1e-6, example=1.0),
    Param("order", int, 2, min=1, max=10),
))
def highpass(ctx, x, cutoff_hz, order):
    """Zero-phase Butterworth high-pass filter."""
    x = _f(x)
    if x.size < 3 * (order + 1) * 3:
        return x
    filled, mask = _fill_nan(x, ctx.time)
    if mask.all():
        return x
    dt = ctx.dt
    if not np.isfinite(dt) or dt <= 0:
        return x
    nyq = 0.5 / dt
    wn = min(cutoff_hz / nyq, 0.99)
    b, a = butter(order, wn, btype="high")
    try:
        out = filtfilt(b, a, filled)
    except ValueError:
        return x
    out[mask] = np.nan
    return out


@tool("signal", "Filters", unit="same", params=(Param("window_s", float, min=0.0, example=0.1),))
def moving_average(ctx, x, window_s):
    """Centered moving average over `window_s` seconds (NaN-aware)."""
    x = _f(x)
    n = min(max(ctx.seconds_to_samples(window_s), 1), x.size)
    if n <= 1 or x.size == 0:
        return x
    valid = np.isfinite(x)
    xs = np.where(valid, x, 0.0)
    kernel = np.ones(n)
    num = np.convolve(xs, kernel, mode="same")
    den = np.convolve(valid.astype(float), kernel, mode="same")
    with np.errstate(invalid="ignore", divide="ignore"):
        out = num / den
    out[den == 0] = np.nan
    return out


@tool("signal", "Filters", unit="same", params=(Param("window_s", float, min=0.0, example=0.1),))
def median(ctx, x, window_s):
    """Centered median filter over `window_s` seconds (removes spikes)."""
    x = _f(x)
    n = max(ctx.seconds_to_samples(window_s), 1)
    if n <= 1 or x.size == 0:
        return x
    filled, mask = _fill_nan(x, ctx.time)
    if mask.all():
        return x
    out = _median_filter(filled, size=n, mode="nearest").astype(float)
    out[mask] = np.nan
    return out


# --------------------------------------------------------------------------- calculus
@tool("signal", "Calculus", unit="x/s")
def derivative(ctx, x):
    """Time derivative using central differences (np.gradient on the timebase)."""
    x = _f(x)
    if x.size < 2:
        return np.full_like(x, np.nan)
    return np.gradient(x, ctx.time)


@tool("signal", "Calculus", hidden=True, unit="m/s^3", input_names=("accel",))
def jerk(ctx, accel):
    """Jerk = derivative of acceleration, as used by the current latency/brake-jerk KPIs."""
    return derivative(ctx, accel)


@tool("signal", "Calculus", unit="x*s")
def integral(ctx, x):
    """Cumulative trapezoidal integral from the start of the recording (NaN contributes 0)."""
    x = _f(x)
    if x.size == 0:
        return x
    xs = np.where(np.isfinite(x), x, 0.0)
    out = np.zeros_like(xs)
    if x.size > 1:
        out[1:] = np.cumsum(0.5 * (xs[1:] + xs[:-1]) * np.diff(ctx.time))
    return out


@tool("signal", "Time", unit="same", params=(Param("seconds", float, example=0.1, doc="positive = later"),))
def delay(ctx, x, seconds):
    """Shift a signal in time; positive delays it. Samples shifted in from outside are NaN."""
    x = _f(x)
    n = ctx.seconds_to_samples(abs(seconds))
    if n == 0 or x.size == 0:
        return x
    out = np.full_like(x, np.nan)
    if n >= x.size:
        return out
    if seconds > 0:
        out[n:] = x[:-n]
    else:
        out[:-n] = x[n:]
    return out


@tool("signal", "Cleanup", unit="same")
def fill_hold(ctx, x):
    """Replace NaN with the last valid value (leading NaN stay NaN)."""
    x = _f(x)
    if x.size == 0:
        return x
    idx = np.where(np.isfinite(x), np.arange(x.size), -1)
    np.maximum.accumulate(idx, out=idx)
    out = np.where(idx >= 0, x[np.clip(idx, 0, None)], np.nan)
    return out


@tool("signal", "Cleanup", unit="same", params=(Param("value", float, 0.0),))
def fill(ctx, x, value):
    """Replace NaN with a constant."""
    x = _f(x)
    return np.where(np.isfinite(x), x, value)


# --------------------------------------------------------------------------- math
@tool("signal", "Math", unit="same")
def abs_(ctx, x):
    """Absolute value."""
    return np.abs(_f(x))


@tool("signal", "Math", unit="same")
def sign(ctx, x):
    """Sign of x (-1, 0, 1)."""
    return np.sign(_f(x))


@tool("signal", "Math", unit="same")
def sqrt(ctx, x):
    """Square root (NaN for negative input)."""
    x = _f(x)
    with np.errstate(invalid="ignore"):
        return np.sqrt(x)


@tool("signal", "Math", inputs=-1, unit="same", name="min")
def min_(ctx, *xs):
    """Element-wise minimum of several signals/numbers; NaN only where all inputs are NaN."""
    return np.fmin.reduce([np.broadcast_to(_f(x), ctx.time.shape) for x in xs])


@tool("signal", "Math", inputs=-1, unit="same", name="max")
def max_(ctx, *xs):
    """Element-wise maximum of several signals/numbers; NaN only where all inputs are NaN."""
    return np.fmax.reduce([np.broadcast_to(_f(x), ctx.time.shape) for x in xs])


@tool("signal", "Math", unit="same", params=(Param("lo", float, example=0.0), Param("hi", float, example=1.0)))
def clip(ctx, x, lo, hi):
    """Limit x to [lo, hi]."""
    return np.clip(_f(x), lo, hi)


@tool("signal", "Math", inputs=3, unit="same", input_names=("cond", "a", "b"))
def where(ctx, cond, a, b):
    """Pick a where cond is true, b elsewhere."""
    c = np.broadcast_to(np.asarray(cond, dtype=bool), ctx.time.shape)
    return np.where(c, np.broadcast_to(_f(a), ctx.time.shape), np.broadcast_to(_f(b), ctx.time.shape))


@tool("signal", "Math", inputs=2, unit="same", input_names=("x", "y"))
def hypot(ctx, x, y):
    """Euclidean norm sqrt(x^2 + y^2), e.g. distance from longitudinal and lateral gaps."""
    return np.hypot(_f(x), _f(y))


# --------------------------------------------------------------------------- units
@tool("signal", "Units", hidden=True, unit="km/h", input_names=("speed_mps",))
def to_kph(ctx, speed_mps):
    """m/s -> km/h."""
    return _f(speed_mps) * 3.6


@tool("signal", "Units", hidden=True, unit="m/s", input_names=("speed_kph",))
def to_mps(ctx, speed_kph):
    """km/h -> m/s."""
    return _f(speed_kph) / 3.6


@tool("signal", "Units", unit="deg")
def to_deg(ctx, x):
    """rad -> deg (also rad/s -> deg/s)."""
    return np.degrees(_f(x))


@tool("signal", "Units", unit="rad")
def to_rad(ctx, x):
    """deg -> rad (also deg/s -> rad/s)."""
    return np.radians(_f(x))


@tool("signal", "Units", hidden=True, unit="g")
def to_g(ctx, x):
    """m/s^2 -> g."""
    return _f(x) / 9.80665


@tool("signal", "Units", unit="to", params=(
    Param("from_unit", str, example="km/h"),
    Param("to_unit", str, example="m/s"),
))
def convert(ctx, x, from_unit, to_unit):
    """Convert between any two known units of the same dimension, e.g. convert(x, "bar", "kpa")."""
    return _units.convert(_f(x), from_unit, to_unit)


# --------------------------------------------------------------------------- kinematics
@tool("signal", "Kinematics", hidden=True, inputs=2, unit="m/s", input_names=("ego_speed", "obj_speed"))
def rel_speed(ctx, ego_speed, obj_speed):
    """Closing speed ego - object (positive = approaching), same unit as inputs."""
    return _f(ego_speed) - _f(obj_speed)


@tool("signal", "Kinematics", hidden=True, inputs=2, unit="s", input_names=("distance", "closing_speed"), params=(
    Param("min_closing", float, 0.1, min=0.0, doc="below this closing speed TTC is NaN"),
    Param("max_ttc", float, 20.0, min=0.0, doc="values above are clipped to NaN"),
))
def ttc(ctx, distance, closing_speed, min_closing, max_ttc):
    """Time to collision = distance / closing speed (NaN when not closing)."""
    d = np.broadcast_to(_f(distance), ctx.time.shape)
    v = np.broadcast_to(_f(closing_speed), ctx.time.shape)
    with np.errstate(divide="ignore", invalid="ignore"):
        out = d / v
    out = np.where((v > min_closing) & (d >= 0), out, np.nan)
    out = np.where(out <= max_ttc, out, np.nan)
    return out


@tool("signal", "Kinematics", hidden=True, unit="m", input_names=("speed_mps",))
def distance_travelled(ctx, speed_mps):
    """Cumulative distance from speed (m/s), trapezoidal integration."""
    return integral(ctx, speed_mps)
