"""Moment tools: find one instant inside a search range.

The engine slices ``ctx.time`` and ``x`` to the search range before calling, so
each tool only has to return the time it found (or None).
"""

from __future__ import annotations

from typing import Optional

import numpy as np

from src.utils.event_detector.as_long.decel import detect_brake_jerk_end, detect_decel_onset
from src.kpi_studio.toolbox.condition_tools import _b, _run_lengths
from src.kpi_studio.toolbox.registry import Param, tool


def _f(x) -> np.ndarray:
    return np.asarray(x, dtype=float)


def _t(ctx, idx) -> Optional[float]:
    if idx is None:
        return None
    return float(ctx.time[int(idx)])


@tool("moment", "Search", input_names=("cond",), params=(
    Param("hold", float, 0.0, min=0.0, doc="cond must stay true this long (s)"),
))
def first(ctx, cond, hold):
    """First time the condition is true (optionally staying true for `hold` seconds)."""
    c = _b(cond, ctx.time.shape)
    n = ctx.seconds_to_samples(hold)
    if n > 0:
        ok = np.where(_run_lengths(c) >= n + 1)[0]
        return _t(ctx, ok[0] - n) if ok.size else None
    idx = np.flatnonzero(c)
    return _t(ctx, idx[0]) if idx.size else None


@tool("moment", "Search", input_names=("cond",))
def last(ctx, cond):
    """Last time the condition is true."""
    idx = np.flatnonzero(_b(cond, ctx.time.shape))
    return _t(ctx, idx[-1]) if idx.size else None


@tool("moment", "Search", input_names=("cond",))
def last_segment_start(ctx, cond):
    """Start of the last continuous true segment (e.g. 'stable detection' of a target)."""
    c = _b(cond, ctx.time.shape)
    idx = np.flatnonzero(c)
    if not idx.size:
        return None
    end = idx[-1]
    start = end
    while start > 0 and c[start - 1]:
        start -= 1
    return _t(ctx, start)


@tool("moment", "Extremes")
def argmax(ctx, x):
    """Time of the maximum value (ignores NaN)."""
    x = _f(x)
    if not np.isfinite(x).any():
        return None
    return _t(ctx, np.nanargmax(x))


@tool("moment", "Extremes")
def argmin(ctx, x):
    """Time of the minimum value (ignores NaN)."""
    x = _f(x)
    if not np.isfinite(x).any():
        return None
    return _t(ctx, np.nanargmin(x))


@tool("moment", "Search", params=(
    Param("level", float, example=0.0),
    Param("direction", str, "any", choices=("any", "rising", "falling")),
    Param("which", str, "first", choices=("first", "last")),
))
def crossing(ctx, x, level, direction, which):
    """Time x crosses `level`, linearly interpolated between samples."""
    x = _f(x)
    if x.size < 2:
        return None
    a, b = x[:-1] - level, x[1:] - level
    with np.errstate(invalid="ignore"):
        up = (a < 0) & (b >= 0)
        down = (a > 0) & (b <= 0)
    mask = up if direction == "rising" else down if direction == "falling" else (up | down)
    idx = np.flatnonzero(mask)
    if not idx.size:
        return None
    i = int(idx[0] if which == "first" else idx[-1])
    t0, t1 = ctx.time[i], ctx.time[i + 1]
    denom = b[i] - a[i]
    frac = 0.0 if denom == 0 else -a[i] / denom
    return float(t0 + frac * (t1 - t0))


@tool("moment", "Search", params=(Param("tol", float, 0.0, min=0.0),))
def first_nonzero(ctx, x, tol):
    """First time |x| > tol (e.g. first detection distance from a gap signal)."""
    x = _f(x)
    with np.errstate(invalid="ignore"):
        idx = np.flatnonzero(np.abs(x) > tol)
    return _t(ctx, idx[0]) if idx.size else None


@tool("moment", "Braking", hidden=True, input_names=("jerk",), params=(
    Param("threshold", float, example=-20.0, doc="negative jerk threshold (m/s^3)"),
))
def decel_onset(ctx, jerk, threshold):
    """First time jerk drops below `threshold` (current tool's detect_decel_onset)."""
    j = _f(jerk)
    return _t(ctx, detect_decel_onset(j, threshold))


@tool("moment", "Braking", hidden=True, input_names=("jerk",), params=(
    Param("threshold", float, example=20.0, doc="positive jerk threshold (m/s^3)"),
))
def jerk_end(ctx, jerk, threshold):
    """Last time jerk exceeds `threshold` after the onset (current tool's detect_brake_jerk_end)."""
    j = _f(jerk)
    return _t(ctx, detect_brake_jerk_end(j, threshold, -1))


@tool("moment", "Braking", hidden=True, input_names=("speed",), params=(
    Param("threshold", float, 0.01, min=0.0, doc="speed at or below this counts as stopped"),
    Param("hold", float, 0.0, min=0.0, doc="must stay stopped this long (s)"),
))
def vehicle_stop(ctx, speed, threshold, hold):
    """First time the vehicle is stopped (speed <= threshold, held for `hold` s)."""
    v = _f(speed)
    with np.errstate(invalid="ignore"):
        stopped = np.abs(v) <= threshold
    return first(ctx, stopped, hold)


@tool("moment", "Search", params=(
    Param("tol", float, min=0.0, example=0.1),
    Param("seconds", float, min=0.0, example=0.5),
))
def settled(ctx, x, tol, seconds):
    """First time from which x stays within a `tol` band for `seconds`."""
    x = _f(x)
    n = max(ctx.seconds_to_samples(seconds), 1)
    if x.size < n:
        return None
    from numpy.lib.stride_tricks import sliding_window_view

    win = sliding_window_view(x, n)
    with np.errstate(invalid="ignore"):
        ok = np.flatnonzero((np.max(win, axis=1) - np.min(win, axis=1)) <= tol)
    return _t(ctx, ok[0]) if ok.size else None

