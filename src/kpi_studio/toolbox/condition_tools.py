"""Condition tools: return boolean arrays. Usable in event start/end and first/last moments."""

from __future__ import annotations

import numpy as np

from src.kpi_studio.toolbox.registry import Param, tool


def _b(x, shape) -> np.ndarray:
    arr = np.asarray(x)
    if arr.dtype != bool:
        with np.errstate(invalid="ignore"):
            arr = np.where(np.isnan(arr.astype(float)), False, arr.astype(float) != 0)
    return np.broadcast_to(arr, shape).astype(bool)


def _f(x) -> np.ndarray:
    return np.asarray(x, dtype=float)


def _run_lengths(cond: np.ndarray) -> np.ndarray:
    """For each sample, how many consecutive samples (ending here) the condition has been true."""
    out = np.zeros(cond.size, dtype=np.int64)
    if cond.size == 0:
        return out
    idx = np.arange(cond.size)
    last_false = np.where(~cond, idx, -1)
    np.maximum.accumulate(last_false, out=last_false)
    out = np.where(cond, idx - last_false, 0)
    return out


@tool("condition", "Edges", input_names=("cond",))
def rising(ctx, cond):
    """True only on the sample where cond turns from false to true."""
    c = _b(cond, ctx.time.shape)
    out = np.zeros_like(c)
    out[1:] = c[1:] & ~c[:-1]
    return out


@tool("condition", "Edges", input_names=("cond",))
def falling(ctx, cond):
    """True only on the sample where cond turns from true to false."""
    c = _b(cond, ctx.time.shape)
    out = np.zeros_like(c)
    out[1:] = ~c[1:] & c[:-1]
    return out


@tool("condition", "Edges")
def changed(ctx, x):
    """True on samples where the value differs from the previous sample (NaN-safe)."""
    x = np.broadcast_to(_f(x), ctx.time.shape)
    out = np.zeros(x.shape, dtype=bool)
    if x.size > 1:
        a, b = x[1:], x[:-1]
        out[1:] = ~((a == b) | (np.isnan(a) & np.isnan(b)))
    return out


@tool("condition", "Timing", input_names=("cond",), params=(
    Param("seconds", float, min=0.0, example=0.2, doc="how long cond must already be true"),
))
def hold(ctx, cond, seconds):
    """On-delay: true once cond has been continuously true for `seconds`."""
    c = _b(cond, ctx.time.shape)
    n = ctx.seconds_to_samples(seconds)
    return _run_lengths(c) >= n + 1 if n > 0 else c.copy()


@tool("condition", "Timing", input_names=("cond",), params=(
    Param("on_s", float, 0.0, min=0.0, doc="cond must be true this long to switch on"),
    Param("off_s", float, 0.0, min=0.0, doc="cond must be false this long to switch off"),
))
def debounce(ctx, cond, on_s, off_s):
    """Hysteresis timer: ignores glitches shorter than on_s (on) / off_s (off)."""
    c = _b(cond, ctx.time.shape)
    n_on = ctx.seconds_to_samples(on_s)
    n_off = ctx.seconds_to_samples(off_s)
    on_ok = _run_lengths(c) >= n_on + 1 if n_on > 0 else c
    off_ok = _run_lengths(~c) >= n_off + 1 if n_off > 0 else ~c
    out = np.zeros(c.shape, dtype=bool)
    state = False
    for i in range(c.size):
        if not state and on_ok[i]:
            state = True
        elif state and off_ok[i]:
            state = False
        out[i] = state
    return out


@tool("condition", "Comparison", params=(Param("lo", float, example=0.0), Param("hi", float, example=1.0)))
def between(ctx, x, lo, hi):
    """True where lo <= x <= hi."""
    x = _f(x)
    with np.errstate(invalid="ignore"):
        return (x >= lo) & (x <= hi)


@tool("condition", "Comparison", params=(
    Param("ref", float, example=-6.0),
    Param("tol", float, 0.1, min=0.0),
))
def in_band(ctx, x, ref, tol):
    """True where |x - ref| <= tol (e.g. target decel at the partial-braking level)."""
    x = _f(x)
    with np.errstate(invalid="ignore"):
        return np.abs(x - ref) <= tol


@tool("condition", "Comparison")
def valid(ctx, x):
    """True where x is a finite number."""
    return np.isfinite(_f(x))


@tool("condition", "Timing", params=(
    Param("tol", float, min=0.0, example=0.1, doc="max peak-to-peak variation"),
    Param("seconds", float, min=0.0, example=0.5, doc="trailing window length"),
))
def stable(ctx, x, tol, seconds):
    """True where x varied by at most `tol` over the trailing `seconds`."""
    x = np.broadcast_to(_f(x), ctx.time.shape)
    n = max(ctx.seconds_to_samples(seconds), 1)
    out = np.zeros(x.shape, dtype=bool)
    if x.size < n:
        return out
    from numpy.lib.stride_tricks import sliding_window_view

    win = sliding_window_view(x, n)
    with np.errstate(invalid="ignore"):
        ptp = np.max(win, axis=1) - np.min(win, axis=1)
    out[n - 1:] = ptp <= tol
    return out
