"""Bring raw per-channel samples onto one common timebase."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Optional

import numpy as np

from src.kpi_studio.core.expr import resolve_enum_member, ExprError
from src.kpi_studio.core.model import SignalDef
from src.kpi_studio.core.results import ChannelUse, FileIssue
from src.kpi_studio.toolbox.units import conversion_factor, normalize_unit


@dataclass
class RawSignal:
    name: str
    timestamps: np.ndarray
    samples: np.ndarray
    unit: str = ""
    channel: str = ""
    group: Optional[int] = None
    raw_values: Optional[np.ndarray] = None  # numeric raw values behind text (enum) samples
    labels: Optional[dict] = None            # raw value -> text, from the file's own conversion


@dataclass
class SignalFrame:
    time: np.ndarray
    values: dict = field(default_factory=dict)
    missing: dict = field(default_factory=dict)  # name -> reason


def _to_numeric(raw: RawSignal, sdef: SignalDef, enum_table: Optional[Mapping], issues: list) -> Optional[np.ndarray]:
    s = np.asarray(raw.samples)
    if s.dtype.kind in "biuf":
        return s.astype(float)
    if raw.raw_values is not None and (enum_table is None or isinstance(sdef.enum, dict)):
        # enum channel without a named enum table (none, or the file's own table): use the file's raw values
        return np.asarray(raw.raw_values, dtype=float)
    # text / bytes / object samples: decode through the enum table when there is one
    decoded = []
    unknown = set()
    for v in s.ravel():
        if isinstance(v, bytes):
            v = v.decode("utf-8", "replace")
        v = str(v).strip().strip("\x00")
        num = np.nan
        try:
            num = float(v)
        except ValueError:
            if enum_table is not None:
                try:
                    num = resolve_enum_member(v, enum_table, raw.name)
                except ExprError:
                    unknown.add(v)
            else:
                unknown.add(v)
        decoded.append(num)
    if unknown:
        sample = ", ".join(sorted(unknown)[:5])
        issues.append(FileIssue("warning", f"{raw.name}: {len(unknown)} text value(s) could not be mapped to "
                                            f"numbers ({sample}); add 'enum:' to the signal"))
    return np.asarray(decoded, dtype=float)


def clean_series(t: np.ndarray, v: np.ndarray) -> tuple[np.ndarray, np.ndarray, int]:
    """Drop non-finite timestamps, sort, drop duplicate timestamps (keep first)."""
    t = np.asarray(t, dtype=float).ravel()
    v = np.asarray(v).ravel()
    n = min(t.size, v.size)
    t, v = t[:n], v[:n]
    ok = np.isfinite(t)
    t, v = t[ok], v[ok]
    if t.size and np.any(np.diff(t) < 0):
        order = np.argsort(t, kind="stable")
        t, v = t[order], v[order]
    dup = 0
    if t.size > 1:
        keep = np.concatenate(([True], np.diff(t) > 0))
        dup = int((~keep).sum())
        t, v = t[keep], v[keep]
    return t, v, dup


def build_frame(
    raw: Mapping[str, RawSignal],
    defs: Mapping[str, SignalDef],
    rate_hz: float,
    enum_tables: Mapping[str, Mapping] | None = None,
) -> tuple[SignalFrame, list[FileIssue], list[ChannelUse]]:
    """Resample every available signal onto a grid at `rate_hz`.

    The grid spans the overlap of the *required* signals (all signals if none
    are required). Continuous signals are linearly interpolated, discrete ones
    held; nothing is extrapolated (NaN outside a signal's own range).
    """
    enum_tables = enum_tables or {}
    issues: list[FileIssue] = []
    uses: list[ChannelUse] = []
    prepared: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    missing: dict[str, str] = {}

    for name, sdef in defs.items():
        r = raw.get(name)
        if r is None:
            missing[name] = "no matching channel"
            continue
        table = sdef.enum if isinstance(sdef.enum, dict) else (enum_tables.get(sdef.enum) if sdef.enum else None)
        values = _to_numeric(r, sdef, table, issues)
        t, v, dup = clean_series(r.timestamps, values)
        if dup:
            issues.append(FileIssue("info", f"{name}: dropped {dup} duplicate timestamp(s)"))
        if t.size == 0:
            missing[name] = "channel has no samples"
            continue
        use = ChannelUse(signal=name, channel=r.channel, group=r.group, file_unit=r.unit or "")
        if sdef.unit and r.unit and normalize_unit(r.unit) != normalize_unit(sdef.unit):
            factor = conversion_factor(r.unit, sdef.unit)
            if factor is None:
                issues.append(FileIssue("warning", f"{name}: file unit '{r.unit}' differs from profile unit "
                                                    f"'{sdef.unit}' and cannot be converted; used as is"))
            else:
                v = v * factor
                use.converted_to = sdef.unit
        uses.append(use)
        prepared[name] = (t, v)

    if not prepared:
        return SignalFrame(time=np.array([]), missing=missing), issues, uses

    required = [n for n in prepared if defs[n].required] or list(prepared)
    t0 = max(prepared[n][0][0] for n in required)
    t1 = min(prepared[n][0][-1] for n in required)
    if t1 <= t0:
        issues.append(FileIssue("error", "the required signals do not overlap in time"))
        return SignalFrame(time=np.array([]), missing=missing), issues, uses

    dt = 1.0 / rate_hz
    n = int(np.floor((t1 - t0) / dt + 1e-9)) + 1
    grid = t0 + np.arange(n) * dt
    values = {}
    for name, (t, v) in prepared.items():
        if defs[name].held:
            idx = np.searchsorted(t, grid, side="right") - 1
            out = np.where(idx >= 0, v[np.clip(idx, 0, None)], np.nan)
            out[grid > t[-1]] = np.nan
        else:
            if t.size == 1:
                out = np.where(np.isclose(grid, t[0]), v[0], np.nan)
            else:
                out = np.interp(grid, t, v, left=np.nan, right=np.nan)
        values[name] = out.astype(float)
    return SignalFrame(time=grid, values=values, missing=missing), issues, uses
