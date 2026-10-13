"""MF4 access: header indexing (no sample data) and reading only the mapped channels.

Channel matching follows the current tool's mf4_extractor rules (exact name,
then name without the '\\ETK'-style suffix, then base name), extended with
case-insensitivity and an optional ``Group::channel`` form to pick one raster.
"""

from __future__ import annotations

import logging
import re
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
    abs_start: Optional[float] = None   # measurement start (header), seconds since the epoch

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
        try:
            idx.abs_start = float(mdf.header.start_time.timestamp())
        except Exception:  # noqa: BLE001
            idx.abs_start = None
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


# ----------------------------------------------------------------------------- recordings
@dataclass
class Recording:
    """One drive/test. Loggers often write it as several MF4 files (one per bus or ECU);
    their signals are read together as if they were one file."""

    name: str
    files: list

    @property
    def label(self) -> str:
        n = Path(self.name).name
        return n if len(self.files) == 1 else f"{n}  ({len(self.files)} files)"


def as_recording(source) -> Recording:
    if isinstance(source, Recording):
        return source
    return Recording(str(source), [str(source)])


# <test>_<run>_<logger>__<YYYYMMDDTHHMMSS>  e.g. AADT-150453_002_MOTION_1__20260826T151158
_SPLIT_NAME = re.compile(r"^(?P<key>.+?_\d+)_.+__\d{8}T\d{4,6}$")
MAX_START_GAP_S = 300.0


def recording_key(path: str | Path) -> str:
    """Name shared by all files of one recording; a file's own stem when the name has no logger part."""
    stem = Path(path).stem
    m = _SPLIT_NAME.match(stem)
    return m.group("key") if m else stem


def group_recordings(files: list, indexes: Optional[dict] = None, combine: bool = True) -> list[Recording]:
    """Group files into recordings: same folder and same name before the logger part
    (``AADT-150453_002_ABD_Robot__…`` + ``AADT-150453_002_MOTION_1__…``). Files whose
    measurement start differs by more than MAX_START_GAP_S are kept apart."""
    files = [str(f) for f in files]
    if not combine:
        return [Recording(f, [f]) for f in files]
    indexes = indexes or {}
    groups: dict[tuple, list[str]] = {}
    for f in files:
        groups.setdefault((str(Path(f).parent), recording_key(f)), []).append(f)
    out: list[Recording] = []
    for (folder, key), members in groups.items():
        clusters: list[list[str]] = []
        for f in members:
            t = getattr(indexes.get(f), "abs_start", None)
            for c in clusters:
                t0 = getattr(indexes.get(c[0]), "abs_start", None)
                if t is None or t0 is None or abs(t - t0) <= MAX_START_GAP_S:
                    c.append(f)
                    break
            else:
                clusters.append([f])
        for c in clusters:
            if len(c) == 1:
                out.append(Recording(c[0], c))
            else:
                name = str(Path(folder) / key)
                if any(r.name == name for r in out):
                    name += f"_{len(out)}"
                out.append(Recording(name, c))
    order = {f: i for i, f in enumerate(files)}
    return sorted(out, key=lambda r: min(order[f] for f in r.files))


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


def resolve_recording(defs: dict[str, SignalDef], rec: Recording, indexes: dict) \
        -> dict[str, tuple[Optional[str], Optional[ChannelInfo], str]]:
    """signal -> (file, channel, note). Channel candidates keep their priority across files:
    the first candidate found in any file of the recording wins."""
    ok = [f for f in rec.files if indexes.get(f) is not None and indexes[f].ok]
    out = {}
    for sname, sdef in defs.items():
        hit = (None, None, "")
        for cand in sdef.channels:
            for f in ok:
                ch, n = match_channel(cand, indexes[f].channels)
                if ch is not None:
                    note = f"'{cand}' matched {n} channels; used group {ch.group} ({ch.group_name})" if n > 1 else ""
                    hit = (f, ch, note)
                    break
            if hit[1] is not None:
                break
        out[sname] = hit
    return out


def _attach_raw_values(rs, mdf, ch) -> None:
    """Text samples from a value-to-text conversion: keep the raw numbers and the labels too."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            sig = mdf.get(ch.name, group=ch.group, index=ch.index, raw=True)
        vals = np.asarray(sig.samples)
        if vals.dtype.kind not in "biuf" or len(vals) != len(rs.samples):
            return
        rs.raw_values = vals
        labels = {}
        for u in np.unique(vals)[:256]:
            text = sig.conversion.convert(np.array([u]))[0] if sig.conversion is not None else u
            if isinstance(text, bytes):
                text = text.decode("utf-8", "replace")
            labels[float(u)] = str(text).strip().strip("\x00")
        rs.labels = labels
    except Exception:  # noqa: BLE001
        rs.raw_values = None


def read_signals(source, defs: dict[str, SignalDef],
                 index=None) -> tuple[dict[str, RawSignal], list[FileIssue]]:
    """Read only the channels the profile maps, from one file or from all files of a Recording.

    `index` is a RecordingIndex (one file) or a dict path -> RecordingIndex; missing ones are read.
    Files of one recording are put on a common time axis using their measurement start times.
    Missing channels are reported, not raised.
    """
    rec = as_recording(source)
    multi = len(rec.files) > 1
    if isinstance(index, RecordingIndex):
        indexes = {rec.files[0]: index}
    else:
        indexes = dict(index or {})
    issues: list[FileIssue] = []
    for f in rec.files:
        if indexes.get(f) is None:
            indexes[f] = index_file(f)
        if not indexes[f].ok:
            issues.append(FileIssue("error", f"{Path(f).name}: {indexes[f].error}" if multi else indexes[f].error))
    if not any(indexes[f].ok for f in rec.files):
        return {}, issues
    starts = [indexes[f].abs_start for f in rec.files if indexes[f].ok]
    t0 = min(starts) if starts and all(t is not None for t in starts) else None
    offset = {f: (indexes[f].abs_start - t0 if t0 is not None and indexes[f].ok else 0.0) for f in rec.files}

    mapping = resolve_recording(defs, rec, indexes)
    raw: dict[str, RawSignal] = {}
    opened: dict[str, object] = {}
    try:
        for sname, (f, ch, note) in mapping.items():
            sdef = defs[sname]
            if ch is None:
                sev = "error" if sdef.required else "warning"
                where = f" in any of the {len(rec.files)} files" if multi else ""
                issues.append(FileIssue(sev, f"signal '{sname}': none of {sdef.channels} found{where}"))
                continue
            if note:
                issues.append(FileIssue("info", f"signal '{sname}': {note}"))
            try:
                if f not in opened:
                    opened[f] = _open(f)
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    sig = opened[f].get(ch.name, group=ch.group, index=ch.index)
                raw[sname] = RawSignal(sname, np.asarray(sig.timestamps) + offset[f], np.asarray(sig.samples),
                                       unit=str(sig.unit or ch.unit or ""),
                                       channel=f"{ch.name} ({Path(f).name})" if multi else ch.name, group=ch.group)
                if raw[sname].samples.dtype.kind in "SUO":
                    _attach_raw_values(raw[sname], opened[f], ch)
            except Exception as exc:  # noqa: BLE001
                issues.append(FileIssue("error" if sdef.required else "warning",
                                        f"signal '{sname}': failed to read '{ch.name}': {exc}"))
    finally:
        for mdf in opened.values():
            try:
                mdf.close()
            except Exception:  # noqa: BLE001
                pass
    return raw, issues
