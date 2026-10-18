"""Generic KPI engine: derived signals -> occurrences -> moments -> KPI values.

Pure numpy; no file access, no GUI. Every failure is captured as a status on the
smallest unit it affects (file, event, moment or KPI) so one problem never stops
the rest of the run.
"""

from __future__ import annotations

import logging
import traceback
from typing import Iterable, Optional

import numpy as np

from src.kpi_studio.core.align import SignalFrame
from src.kpi_studio.core.expr import Compiled, Env, MissingSignal, as_bool
from src.kpi_studio.core.model import TIME
from src.kpi_studio.core.results import (
    FileIssue,
    FileOutcome,
    KpiResult,
    MomentRecord,
    Occurrence,
    Status,
    Trace,
)
from src.kpi_studio.core.validate import CompiledEvent, CompiledKpi, CompiledProfile
from src.kpi_studio.toolbox.registry import ToolContext

log = logging.getLogger(__name__)


class _MomentFailure(Exception):
    def __init__(self, status: str, message: str):
        self.status = status
        super().__init__(message)


# ----------------------------------------------------------------------------- helpers
def sample_at(time: np.ndarray, values: np.ndarray, t: float, hold: bool) -> float:
    """Value of `values` at time `t`: linear interpolation, or zero-order hold."""
    if time.size == 0 or t < time[0] - 1e-9 or t > time[-1] + 1e-9:
        return float("nan")
    values = np.asarray(values)
    if values.dtype == bool:
        hold = True
        values = values.astype(float)
    i = int(np.searchsorted(time, t, side="right") - 1)
    i = min(max(i, 0), time.size - 1)
    if hold or i == time.size - 1 or abs(time[i] - t) < 1e-12:
        return float(values[i])
    t0, t1 = time[i], time[i + 1]
    v0, v1 = float(values[i]), float(values[i + 1])
    if not (np.isfinite(v0) and np.isfinite(v1)):
        return v0 if abs(t - t0) <= abs(t1 - t) else v1
    return v0 + (v1 - v0) * (t - t0) / (t1 - t0)


def _slice(time: np.ndarray, lo: float, hi: float) -> slice:
    i0 = int(np.searchsorted(time, lo - 1e-9, side="left"))
    i1 = int(np.searchsorted(time, hi + 1e-9, side="right"))
    return slice(i0, i1)


def edges_to_intervals(time: np.ndarray, start: np.ndarray, end: Optional[np.ndarray],
                       start_rule: str = "first", end_rule: str = "first") -> list[tuple[float, float]]:
    """Occurrences (every one in the file) from a start condition (rising edges) and optional end condition.

    start_rule 'last': a window starts at the last time the start condition becomes true before its end.
    end_rule 'last': it ends at the last time the end condition is met before the next start (or the file end)."""
    start = as_bool(start)
    n = start.size
    if n == 0:
        return []
    rise = np.flatnonzero(start & ~np.concatenate(([False], start[:-1])))
    end_hits = np.flatnonzero(as_bool(end)) if end is not None else None
    intervals = []
    last_end = -1
    for s in rise:
        if s <= last_end:
            continue
        if end_hits is not None:
            after = end_hits[end_hits > s]
            e = int(after[0]) if after.size else n - 1
            if end_rule == "last" and after.size:
                nxt = rise[rise > e]
                limit = int(nxt[0]) if nxt.size else n
                e = int(after[after < limit][-1])
        else:
            f = np.flatnonzero(~start[s:])
            e = s + f[0] if f.size else n - 1
        if start_rule == "last":
            s = int(rise[(rise >= s) & (rise < e)][-1]) if np.any((rise >= s) & (rise < e)) else s
        intervals.append((float(time[s]), float(time[e])))
        last_end = e
    return intervals


def clean_intervals(intervals: Iterable[tuple[float, float]], merge_gap: float, min_duration: float):
    out: list[list[float]] = []
    for s, e in sorted(intervals):
        if e < s:
            s, e = e, s
        if out and s - out[-1][1] <= merge_gap:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return [(s, e) for s, e in out if e - s >= min_duration]


# ----------------------------------------------------------------------------- engine
class FileEvaluator:
    def __init__(self, cp: CompiledProfile, frame: SignalFrame, file_label: str,
                 capture: Optional[dict] = None):
        self.cp = cp
        self.file = file_label
        self.time = frame.time
        self.env = Env(time=frame.time, values=dict(frame.values), missing=dict(frame.missing))
        self.env.values[TIME] = frame.time
        self._cache: dict[str, object] = {}  # expr source -> array | MissingSignal
        self.capture = capture or {}
        self.texts: dict[str, str] = {}  # text results of 'which' KPIs, picked up with their value  # event -> {"signals": [...], "max": n}

    # ---- expressions are evaluated once per file over the whole recording
    def full(self, c: Compiled) -> np.ndarray:
        key = c.source
        hit = self._cache.get(key)
        if hit is None:
            try:
                hit = c.evaluate(self.env)
            except MissingSignal as exc:
                hit = exc
            self._cache[key] = hit
        if isinstance(hit, MissingSignal):
            raise hit
        return hit

    def compute_derived(self, outcome: FileOutcome) -> None:
        for name, c in self.cp.derived:
            try:
                self.env.values[name] = np.asarray(self.full(c), dtype=float) if not c.is_bool \
                    else np.asarray(self.full(c))
            except MissingSignal as exc:
                self.env.missing[name] = f"needs {exc.name}"
            except Exception as exc:  # noqa: BLE001
                self.env.missing[name] = f"failed: {exc}"
                outcome.issues.append(FileIssue("warning", f"derived '{name}' failed: {exc}"))

    # ---- occurrences
    def detect(self, ev: CompiledEvent) -> list[tuple[float, float]]:
        if ev.preset is not None:
            inputs = [self.full(c) for c in ev.preset_inputs]
            intervals = ev.preset.func(ToolContext(self.time), *inputs, **ev.preset_params)
        else:
            start = self.full(ev.start)
            end = self.full(ev.end) if ev.end is not None else None
            intervals = edges_to_intervals(self.time, start, end, ev.defn.start_rule, ev.defn.end_rule)
        return clean_intervals(intervals, ev.defn.merge_gap, ev.defn.min_duration)

    def _open_end(self, ev: CompiledEvent, t_start: float) -> str:
        """Why the last window runs to the end of the file (its end condition is never met), or ''."""
        hits = as_bool(self.full(ev.end))
        if hits[self.time > t_start].any():
            return ""
        never = not hits.any()
        return (f"the end condition '{ev.defn.end}' is {'never true in this file' if never else 'not met after'}"
                f"{'' if never else f' the start at {t_start:.2f} s'}, so the window from {t_start:.2f} s runs "
                "to the end of the file and no later window can start")

    def _why_none(self, ev: CompiledEvent) -> str:
        """A hint for 'no occurrences': was the start condition ever true?"""
        if ev.preset is not None or ev.start is None:
            return ""
        try:
            hits = int(as_bool(self.full(ev.start)).sum())
        except Exception:  # noqa: BLE001
            return ""
        if hits == 0:
            return (f" (the start condition '{ev.defn.start}' is never true in this file; "
                    f"'==' needs an exact value, use < or > for continuous signals)")
        return f" (start condition true on {hits} samples, but shorter than 'ignore shorter than')"

    # ---- moments
    def resolve_moments(self, ev: CompiledEvent, occ: Occurrence) -> dict[str, object]:
        """name -> time (float) or _MomentFailure."""
        times: dict[str, object] = {
            "start": occ.t_start,
            "end": occ.t_end,
            "window_start": occ.t_window_start,
            "window_end": occ.t_window_end,
        }
        for name in ev.moment_order:
            cm = ev.moments[name]
            try:
                times[name] = self._one_moment(cm, times, occ)
            except _MomentFailure as exc:
                times[name] = exc
        return times

    @staticmethod
    def _ref(times: dict, name: str) -> float:
        v = times[name]
        if isinstance(v, _MomentFailure):
            raise _MomentFailure(v.status if v.status != Status.OK else Status.MOMENT_NOT_FOUND,
                                 f"depends on '{name}', which was not found")
        return float(v)

    def _one_moment(self, cm, times: dict, occ: Occurrence) -> float:
        rule = cm.rule
        if rule.offset:
            return self._ref(times, rule.offset) + rule.by
        lo, hi = occ.t_window_start, occ.t_window_end
        if rule.between:
            lo, hi = self._ref(times, rule.between[0]), self._ref(times, rule.between[1])
        if rule.after:
            lo = max(lo, self._ref(times, rule.after))
        if rule.before:
            hi = min(hi, self._ref(times, rule.before))
        if rule.within is not None:
            if rule.before and not rule.after:
                lo = max(lo, hi - rule.within)
            else:
                hi = min(hi, lo + rule.within)
        if hi < lo:
            raise _MomentFailure(Status.MOMENT_NOT_FOUND, f"empty search range {lo:.3f}s..{hi:.3f}s")
        try:
            x = self.full(cm.expr)
        except MissingSignal as exc:
            raise _MomentFailure(Status.MISSING_SIGNAL, str(exc)) from None
        sl = _slice(self.time, lo, hi)
        if sl.stop - sl.start == 0:
            raise _MomentFailure(Status.NO_SAMPLES, f"no samples in {lo:.3f}s..{hi:.3f}s")
        t = cm.tool.func(ToolContext(self.time[sl]), x[sl], **cm.params)
        if t is None or not np.isfinite(t):
            raise _MomentFailure(Status.MOMENT_NOT_FOUND,
                                 f"{cm.tool.name} found nothing in {lo:.3f}s..{hi:.3f}s")
        return float(t) + rule.by

    # ---- KPIs
    def kpi(self, k: CompiledKpi, times: dict, done: dict) -> tuple[Optional[float], Optional[float], str, str]:
        """Returns (value, moment_time, status, message). `done` holds earlier KPI results."""
        d = k.defn
        try:
            if d.form == "formula":
                inputs = {}
                for name in k.value.deps:
                    value, status = done[name]
                    if status != Status.OK:
                        return None, None, status, f"depends on KPI '{name}' ({status})"
                    inputs[name] = np.array([value], dtype=float)
                env = Env(time=np.zeros(1), values=inputs)
                v = float(np.asarray(k.value.evaluate(env), dtype=float)[0])
                if not np.isfinite(v):
                    return None, None, Status.NO_SAMPLES, "formula result is not a number"
                return v, None, Status.OK, ""
            if d.form == "value":
                t = self._ref_kpi(times, d.at)
                arr = self.full(k.value)
                v = sample_at(self.time, arr, t, hold=k.value.hold)
                if not np.isfinite(v):
                    return None, t, Status.NO_SAMPLES, f"'{d.value}' has no valid value at {t:.3f}s"
                return v, t, Status.OK, ""
            if d.form == "duration":
                a, b = self._ref_kpi(times, d.duration[0]), self._ref_kpi(times, d.duration[1])
                return b - a, b, Status.OK, ""
            if d.form == "which":  # the labels of the conditions met anywhere in the range
                lo_name, hi_name = d.between or ("start", "end")
                lo, hi = self._ref_kpi(times, lo_name), self._ref_kpi(times, hi_name)
                sl = _slice(self.time, lo, hi)
                if sl.stop - sl.start == 0:
                    return None, None, Status.NO_SAMPLES, f"no samples in {lo:.3f}s..{hi:.3f}s"
                met = [i for i, (_label, c) in enumerate(k.which) if as_bool(self.full(c)[sl]).any()]
                self.texts[d.name] = " + ".join(k.which[i][0] for i in met) or "none"
                return float(sum(2 ** i for i in met)), hi, Status.OK, ""
            # reduce
            lo_name, hi_name = d.between or ("window_start", "window_end")
            lo, hi = self._ref_kpi(times, lo_name), self._ref_kpi(times, hi_name)
            arr = self.full(k.of)
            sl = _slice(self.time, lo, hi)
            if sl.stop - sl.start == 0:
                return None, None, Status.NO_SAMPLES, f"no samples in {lo:.3f}s..{hi:.3f}s"
            v = k.reducer.func(ToolContext(self.time[sl]), arr[sl], **k.args)
            if v is None or not np.isfinite(v):
                return None, hi, Status.NO_SAMPLES, f"{d.reduce} of '{d.of}' has no valid samples"
            return float(v), hi, Status.OK, ""
        except _MomentFailure as exc:
            return None, None, exc.status, str(exc)
        except MissingSignal as exc:
            return None, None, Status.MISSING_SIGNAL, str(exc)
        except Exception as exc:  # noqa: BLE001
            log.debug("KPI %s failed", d.name, exc_info=True)
            return None, None, Status.ERROR, f"{type(exc).__name__}: {exc}"

    @staticmethod
    def _ref_kpi(times: dict, name: str) -> float:
        v = times[name]
        if isinstance(v, _MomentFailure):
            raise _MomentFailure(v.status, f"moment '{name}': {v}")
        return float(v)

    # ---- driver
    def run(self, outcome: FileOutcome) -> FileOutcome:
        if self.time.size == 0:
            return outcome.error("no usable samples after alignment")
        outcome.duration_s = float(self.time[-1] - self.time[0])
        self.compute_derived(outcome)
        t_min, t_max = float(self.time[0]), float(self.time[-1])

        for ename, ev in self.cp.events.items():
            kpis = self.cp.kpis_for(ename)
            try:
                intervals = self.detect(ev)
            except MissingSignal as exc:
                outcome.issues.append(FileIssue("warning", f"event '{ename}' skipped: {exc}"))
                continue
            except Exception as exc:  # noqa: BLE001
                outcome.issues.append(FileIssue("error", f"event '{ename}' failed: {type(exc).__name__}: {exc}"))
                log.debug("event detection failed\n%s", traceback.format_exc())
                continue
            if not intervals:
                outcome.issues.append(FileIssue("info", f"event '{ename}': no occurrences" + self._why_none(ev)))
            elif ev.preset is None and ev.end is not None:
                open_end = self._open_end(ev, intervals[-1][0])
                if open_end:
                    outcome.issues.append(FileIssue("warning", f"event '{ename}': {open_end}"))
            cap = self.capture.get(ename)
            for i, (s, e) in enumerate(intervals, start=1):
                w = ev.defn.window
                occ = Occurrence(self.file, ename, i, s, e, max(s - w.pre, t_min), min(e + w.post, t_max))
                outcome.occurrences.append(occ)
                times = self.resolve_moments(ev, occ)
                for mname in ev.moment_order:
                    v = times[mname]
                    if isinstance(v, _MomentFailure):
                        outcome.moments.append(MomentRecord(self.file, ename, i, mname, None, v.status, str(v)))
                    else:
                        outcome.moments.append(MomentRecord(self.file, ename, i, mname, float(v), Status.OK))
                done: dict = {}
                for k in kpis:
                    value, mt, status, msg = self.kpi(k, times, done)
                    done[k.defn.name] = (value, status)
                    outcome.results.append(KpiResult(self.file, ename, i, k.defn.name, value,
                                                     k.unit, mt, status, msg, self.texts.pop(k.defn.name, "")))
                if cap and i <= cap.get("max", 50):
                    outcome.traces.append(self._trace(occ, cap.get("signals", [])))
        return outcome

    def _trace(self, occ: Occurrence, signals: list[str]) -> Trace:
        sl = _slice(self.time, occ.t_window_start, occ.t_window_end)
        data = {}
        for s in signals:
            arr = self.env.values.get(s)
            if arr is not None:
                data[s] = np.asarray(arr[sl], dtype=float).tolist()
        return Trace(occ.file, occ.event, occ.index, self.time[sl].tolist(), data)


def evaluate_frame(cp: CompiledProfile, frame: SignalFrame, file_label: str,
                   outcome: Optional[FileOutcome] = None, capture: Optional[dict] = None) -> FileOutcome:
    outcome = outcome or FileOutcome(file=file_label)
    try:
        return FileEvaluator(cp, frame, file_label, capture).run(outcome)
    except Exception as exc:  # noqa: BLE001  (last-resort isolation per file)
        log.debug("file evaluation failed\n%s", traceback.format_exc())
        return outcome.error(f"unexpected error: {type(exc).__name__}: {exc}")
