"""Event presets: whole-recording detectors returning (t_start, t_end) intervals.

Most presets wrap the current tool's event detectors so profiles get exactly the
same occurrences as the feature pipelines.
"""

from __future__ import annotations

import contextlib
import io
import logging
import warnings

import numpy as np

from src.utils.event_detector.as_lat.lka import detect_lka_events
from src.utils.event_detector.as_long.aeb import detect_aeb_events
from src.utils.event_detector.as_long.fcw import detect_fcw_events
from src.utils.event_detector.as_long.lsaeb import detect_lsaeb_events
from src.kpi_studio.toolbox.registry import Param, tool

log = logging.getLogger(__name__)


def _quiet(fn, *args, **kwargs):
    """Run a legacy detector, routing its prints/warnings to the debug log."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), warnings.catch_warnings():
        warnings.simplefilter("ignore")
        out = fn(*args, **kwargs)
    text = buf.getvalue().strip()
    if text:
        log.debug(text)
    return out


def _pairs(starts, ends) -> list[tuple[float, float]]:
    return [(float(s), float(e)) for s, e in zip(np.asarray(starts), np.asarray(ends))]


def _clean(x) -> np.ndarray:
    # legacy detectors compare with == on floats; NaN never matches, which is what we want
    return np.asarray(x, dtype=float)


@tool("event", "Longitudinal", input_names=("aeb_request",), params=(
    Param("post_time", float, 3.0, min=0.0, doc="end used when no falling edge is found (s after start)"),
))
def aeb_request(ctx, aeb_request, post_time):
    """AEB request: start when request becomes 1/2, end when it returns to 0 (detect_aeb_events)."""
    x = _clean(aeb_request)
    if x.size == 0:
        return []
    return _pairs(*_quiet(detect_aeb_events, ctx.time, x, post_time=post_time))


@tool("event", "Longitudinal", input_names=("fcw_request",), params=(
    Param("merge_window", float, 2.0, min=0.0, doc="merge activations closer than this (s)"),
))
def fcw_request(ctx, fcw_request, merge_window):
    """FCW request: 0 -> 2/3 until back to 0, close activations merged (detect_fcw_events)."""
    x = _clean(fcw_request)
    if x.size == 0:
        return []
    return _pairs(*_quiet(detect_fcw_events, ctx.time, x, merge_window=merge_window))


@tool("event", "Longitudinal", input_names=("cpm_event_type",), params=(
    Param("merge_window", float, 2.0, min=0.0),
))
def lsaeb_cpm(ctx, cpm_event_type, merge_window):
    """LSAEB: CPM event type 0 -> 1/2 until back to 0, merged (detect_lsaeb_events)."""
    x = _clean(cpm_event_type)
    if x.size == 0:
        return []
    return _pairs(*_quiet(detect_lsaeb_events, ctx.time, x, merge_window=merge_window, output="times"))


@tool("event", "Lateral", input_names=("lka_status",))
def lka_intervention(ctx, lka_status):
    """LKA intervention: status 0 -> 1 until 1 -> 0 (detect_lka_events)."""
    x = _clean(lka_status)
    if x.size == 0:
        return []
    return _pairs(*_quiet(detect_lka_events, ctx.time, x, output="times"))


@tool("event", "Longitudinal", inputs=2, input_names=("target_decel", "speed"), params=(
    Param("start_level", float, -6.0, doc="start when target decel goes below start_level + tol"),
    Param("tol", float, 0.1, min=0.0),
    Param("end_level", float, -4.9, doc="end when target decel rises above this"),
    Param("stop_speed", float, 0.0, min=0.0, doc="... or when |speed| <= stop_speed"),
))
def target_decel_window(ctx, target_decel, speed, start_level, tol, end_level, stop_speed):
    """Autonomous-braking window: target decel request edge until release or vehicle stop.

    Same rule as find_aeb_intv_start / find_aeb_intv_end, applied to every
    activation in the recording instead of one pre-cut chunk.
    """
    d = _clean(target_decel)
    v = _clean(speed)
    t = ctx.time
    if d.size == 0:
        return []
    with np.errstate(invalid="ignore"):
        active = d < (start_level + tol)
        release = d > end_level
        stopped = np.abs(v) <= stop_speed
    starts = np.flatnonzero(active & ~np.concatenate(([False], active[:-1])))
    out = []
    next_allowed = -1
    for s in starts:
        if s <= next_allowed:
            continue
        rel = np.flatnonzero(release[s:])
        stp = np.flatnonzero(stopped[s:])
        cands = [c for c in (rel[0] if rel.size else None, stp[0] if stp.size else None) if c is not None]
        e = s + min(cands) if cands else d.size - 1
        out.append((float(t[s]), float(t[e])))
        next_allowed = e
    return out
