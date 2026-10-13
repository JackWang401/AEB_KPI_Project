"""MF4 access: header indexing (no sample data) and reading only the mapped channels.

Channel matching follows the current tool's mf4_extractor rules (exact name,
then name without the '\\ETK'-style suffix, then base name), extended with
case-insensitivity and an optional ``Group::channel`` form to pick one raster.
"""

from __future__ import annotations

import logging
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

import numpy as np

from src.kpi_studio.core.align import RawSignal
from src.kpi_studio.core.model import SignalDef
from src.kpi_studio.core.results import FileIssue

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ChannelInfo:
    name: str
    group: int
    index: int
    unit: str = ""
    group_name: str = ""

    @property
    def clean(self) -> str:
        return self.name.split("\\", 1)[0]

    @property
    def base(self) -> str:
        return self.clean.replace("\\", "/").split("/")[-1]


@dataclass
class RecordingIndex:
    path: str
    channels: list = field(default_factory=list)
    start: Optional[float] = None
    end: Optional[float] = None
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error

    @property
    def duration(self) -> Optional[float]:
        if self.start is None or self.end is None:
            return None
        return self.end - self.start

    def names(self) -> list[str]:
        return sorted({c.name for c in self.channels})


def _open(path: str):
    from asammdf import MDF  # imported lazily: heavy, and core/tests do not need it

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return MDF(path)


def _group_name(group, i: int) -> str:
    cg = getattr(group, "channel_group", None)
    for attr in ("acq_name", "comment"):
        v = getattr(cg, attr, None) if cg is not None else None
        if v:
            return str(v)
    return f"group_{i}"


def index_file(path: str | Path) -> RecordingIndex:
    """Read channel names/units/groups and the time range, without loading samples."""
    path = str(path)
    idx = RecordingIndex(path=path)
    try:
        mdf = _open(path)
    except Exception as exc:  # noqa: BLE001
        idx.error = f"cannot open: {type(exc).__name__}: {exc}"
        return idx
    try:
        for gi, group in enumerate(mdf.groups):
            gname = _group_name(group, gi)
            master_index = getattr(group.channel_group, "master_index", None)
            for ci, ch in enumerate(group.channels):
                if getattr(ch, "channel_type", None) == 2 or ci == master_index:  # master channel
                    continue
                if ch.name in ("time", "t") and ci == 0:
                    continue
                idx.channels.append(ChannelInfo(ch.name, gi, ci, getattr(ch, "unit", "") or "", gname))
        starts, ends = [], []
        for gi in range(len(mdf.groups)):
            try:
                m = mdf.get_master(gi)
                if m is not None and len(m):
                    starts.append(float(m[0]))
                    ends.append(float(m[-1]))
            except Exception:  # noqa: BLE001
                continue
        if starts:
            idx.start, idx.end = min(starts), max(ends)
    except Exception as exc:  # noqa: BLE001
        idx.error = f"cannot read header: {type(exc).__name__}: {exc}"
    finally:
        try:
            mdf.close()
        except Exception:  # noqa: BLE001
            pass
    return idx


def match_channel(candidate: str, channels: Iterable[ChannelInfo]) -> tuple[Optional[ChannelInfo], int]:
    """Find `candidate` in `channels`. Returns (match, number_of_equally_good_matches)."""
    channels = list(channels)
    group_filter = None
    name = candidate.strip()
    if "::" in name:
        group_filter, name = (s.strip() for s in name.split("::", 1))
        channels = [c for c in channels if c.group_name.lower() == group_filter.lower()]
    low = name.lower()
    tiers = (
        lambda c: c.name == name,
        lambda c: c.name.lower() == low,
        lambda c: c.clean.lower() == low,
        lambda c: c.base.lower() == low,
    )
    for test in tiers:
        hits = [c for c in channels if test(c)]
        if hits:
            return hits[0], len(hits)
    return None, 0


def resolve_signals(defs: dict[str, SignalDef], index: RecordingIndex) -> dict[str, tuple[Optional[ChannelInfo], str]]:
    """signal -> (channel or None, note)."""
    out = {}
    for sname, sdef in defs.items():
        found, note = None, ""
        for cand in sdef.channels:
            ch, n = match_channel(cand, index.channels)
            if ch is not None:
                found = ch
                if n > 1:
                    note = f"'{cand}' matched {n} channels; used group {ch.group} ({ch.group_name})"
                break
        out[sname] = (found, note)
    return out


def read_signals(path: str | Path, defs: dict[str, SignalDef],
                 index: Optional[RecordingIndex] = None) -> tuple[dict[str, RawSignal], list[FileIssue]]:
    """Read only the channels the profile maps. Missing channels are reported, not raised."""
    path = str(path)
    issues: list[FileIssue] = []
    index = index or index_file(path)
    if not index.ok:
        issues.append(FileIssue("error", index.error))
        return {}, issues
    mapping = resolve_signals(defs, index)
    raw: dict[str, RawSignal] = {}
    mdf = _open(path)
    try:
        for sname, (ch, note) in mapping.items():
            sdef = defs[sname]
            if ch is None:
                sev = "error" if sdef.required else "warning"
                issues.append(FileIssue(sev, f"signal '{sname}': none of {sdef.channels} found"))
                continue
            if note:
                issues.append(FileIssue("info", f"signal '{sname}': {note}"))
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    sig = mdf.get(ch.name, group=ch.group, index=ch.index)
                raw[sname] = RawSignal(sname, np.asarray(sig.timestamps), np.asarray(sig.samples),
                                       unit=str(sig.unit or ch.unit or ""), channel=ch.name, group=ch.group)
            except Exception as exc:  # noqa: BLE001
                issues.append(FileIssue("error" if sdef.required else "warning",
                                        f"signal '{sname}': failed to read '{ch.name}': {exc}"))
    finally:
        try:
            mdf.close()
        except Exception:  # noqa: BLE001
            pass
    return raw, issues
