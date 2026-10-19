"""Reducer tools: one number over a time range (already sliced by the engine)."""

from __future__ import annotations

import numpy as np

from src.kpi_studio.toolbox.condition_tools import _b
from src.kpi_studio.toolbox.registry import tool


def _f(x) -> np.ndarray:
    return np.asarray(x, dtype=float)


def _finite(x) -> np.ndarray:
    x = _f(x)
    return x[np.isfinite(x)]


def _trapz(y, t) -> float:
    fn = getattr(np, "trapezoid", None) or np.trapz
    return float(fn(y, t))


@tool("reducer", "Statistics", unit="same", name="max")
def max_(ctx, x):
    """Maximum value (NaN ignored)."""
    v = _finite(x)
    return float(v.max()) if v.size else float("nan")


@tool("reducer", "Statistics", unit="same", name="min")
def min_(ctx, x):
    """Minimum value (NaN ignored)."""
    v = _finite(x)
    return float(v.min()) if v.size else float("nan")


@tool("reducer", "Statistics", unit="same")
def abs_max(ctx, x):
    """Maximum absolute value (NaN ignored)."""
    v = _finite(x)
    return float(np.abs(v).max()) if v.size else float("nan")


@tool("reducer", "Statistics", unit="same")
def mean(ctx, x):
    """Arithmetic mean of samples (NaN ignored)."""
    v = _finite(x)
    return float(v.mean()) if v.size else float("nan")


@tool("reducer", "Statistics", unit="same")
def time_mean(ctx, x):
    """Time-weighted mean: trapezoidal integral / duration (current tool's average decel)."""
    x = _f(x)
    t = ctx.time
    m = np.isfinite(x) & np.isfinite(t)
    if m.sum() < 2:
        return float("nan")
    t, x = t[m], x[m]
    dur = float(t[-1] - t[0])
    if dur <= 0:
        return float("nan")
    return _trapz(x, t) / dur


@tool("reducer", "Statistics", unit="same")
def std(ctx, x):
    """Standard deviation (NaN ignored)."""
    v = _finite(x)
    return float(v.std()) if v.size else float("nan")


@tool("reducer", "Statistics", unit="same")
def rms(ctx, x):
    """Root mean square (NaN ignored)."""
    v = _finite(x)
    return float(np.sqrt(np.mean(v ** 2))) if v.size else float("nan")


@tool("reducer", "Statistics", unit="same")
def peak_to_peak(ctx, x):
    """max - min (NaN ignored)."""
    v = _finite(x)
    return float(v.max() - v.min()) if v.size else float("nan")


@tool("reducer", "Statistics", unit="same")
def first_value(ctx, x):
    """First finite value in the range."""
    v = _finite(x)
    return float(v[0]) if v.size else float("nan")


@tool("reducer", "Statistics", unit="same")
def last_value(ctx, x):
    """Last finite value in the range."""
    v = _finite(x)
    return float(v[-1]) if v.size else float("nan")


@tool("reducer", "Statistics", unit="same")
def change(ctx, x):
    """Value at the end of the range minus the value at its start (e.g. speed lost between two moments)."""
    v = _finite(x)
    return float(v[-1] - v[0]) if v.size else float("nan")


@tool("reducer", "Integrals", unit="x*s")
def integral(ctx, x):
    """Trapezoidal integral over the range (NaN samples dropped)."""
    x = _f(x)
    m = np.isfinite(x)
    if m.sum() < 2:
        return float("nan")
    return _trapz(x[m], ctx.time[m])


@tool("reducer", "Integrals", unit="m", input_names=("speed",), input_unit="m/s")
def distance(ctx, speed):
    """Distance travelled over the range, in m, from a speed in any unit (m/s, km/h, mph: converted
    from the signal's unit)."""
    return integral(ctx, speed)


@tool("reducer", "Conditions", unit="s", input_names=("cond",))
def time_true(ctx, cond):
    """Total time the condition is true within the range."""
    c = _b(cond, ctx.time.shape)
    if c.size < 2:
        return 0.0
    dt = np.diff(ctx.time)
    return float(np.sum(dt[c[:-1]]))


@tool("reducer", "Conditions", unit="count", input_names=("cond",))
def count_rising(ctx, cond):
    """Number of false->true transitions within the range."""
    c = _b(cond, ctx.time.shape)
    if c.size < 2:
        return 0.0
    return float(np.count_nonzero(c[1:] & ~c[:-1]))


@tool("reducer", "Conditions", unit="bool", input_names=("cond",), name="any")
def any_(ctx, cond):
    """1 if the condition is true at any sample in the range, else 0."""
    return float(_b(cond, ctx.time.shape).any())


@tool("reducer", "Conditions", unit="bool", input_names=("cond",), name="all")
def all_(ctx, cond):
    """1 if the condition is true at every sample in the range, else 0."""
    c = _b(cond, ctx.time.shape)
    return float(c.all()) if c.size else float("nan")
