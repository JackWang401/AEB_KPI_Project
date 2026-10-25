"""MF4 access: header indexing (no sample data) and reading only the mapped channels.

Channel matching follows the current tool's mf4_extractor rules (exact name,
then name without the '\\ETK'-style suffix, then base name), extended with
case-insensitivity and an optional ``Group::channel`` form to pick one raster.
"""

from __future__ import annotations

import hashlib
import html
import logging
import os
import pickle
import re
import threading
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
    comment: str = ""        # channel description stored in the file (plain text)
    discrete: bool = False   # the file maps raw values to text (an enum / state signal)
    enum: tuple = ()         # ((text, raw value), ...) of an enum channel, in value order

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
    block_addrs: Optional[dict] = None  # group -> address of its data blocks, to open only those later

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


_only_blocks = threading.local()  # data block addresses to resolve while this thread opens a file
_lazy_installed = False


def _install_lazy_blocks() -> None:
    """Opening a file normally walks the data block list of every channel group, which on a large
    file is most of the opening time. While `_only_blocks.addrs` is set, only the groups with those
    data block addresses are walked; the other groups then hold no data and must not be read."""
    global _lazy_installed
    if _lazy_installed:
        return
    from asammdf.blocks.mdf_v4 import MDF4

    original = MDF4._get_data_blocks_info

    def only_wanted(self, *args, **kwargs):
        wanted = getattr(_only_blocks, "addrs", None)
        if wanted is not None and kwargs.get("address") not in wanted:
            return iter(())
        return original(self, *args, **kwargs)

    MDF4._get_data_blocks_info = only_wanted
    _lazy_installed = True


def _open(path: str, blocks: Optional[set] = None):
    """`blocks`: data block addresses of the groups that will be read (None: all groups)."""
    from asammdf import MDF  # imported lazily: heavy, and core/tests do not need it

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        if blocks is None:
            return MDF(path)
        try:
            _install_lazy_blocks()
        except Exception:  # noqa: BLE001 - another asammdf layout: open the usual way
            return MDF(path)
        _only_blocks.addrs = set(blocks)
        try:
            return MDF(path)
        finally:
            _only_blocks.addrs = None


def _group_name(group, i: int) -> str:
    cg = getattr(group, "channel_group", None)
    for attr in ("acq_name", "comment"):
        v = getattr(cg, attr, None) if cg is not None else None
        if v:
            return str(v)
    return f"group_{i}"


INDEX_CACHE_VERSION = 2


def _cache_dir() -> Path:
    return Path(os.environ.get("KPI_STUDIO_CACHE_DIR") or Path.home() / ".kpi_studio" / "index_cache")


def _cache_file(path: str) -> Optional[Path]:
    """Cache entry of one file's index, keyed by its full path, size and modification time."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    key = f"{INDEX_CACHE_VERSION}|{Path(path).resolve()}|{st.st_size}|{st.st_mtime_ns}"
    return _cache_dir() / (hashlib.sha1(key.encode("utf-8")).hexdigest() + ".pkl")


def index_file(path: str | Path, cache: bool = True) -> RecordingIndex:
    """Read channel names/units/groups and the time range, without loading samples.
    An index is kept on disk, so a file already seen (same path, size and time) is not opened again."""
    path = str(path)
    entry = _cache_file(path) if cache else None
    if entry is not None and entry.is_file():
        try:
            with open(entry, "rb") as fh:
                idx = pickle.load(fh)
            if isinstance(idx, RecordingIndex) and idx.ok:
                idx.path = path
                return idx
        except Exception:  # noqa: BLE001
            pass
    idx = _index_file(path)
    if entry is not None and idx.ok:
        try:
            entry.parent.mkdir(parents=True, exist_ok=True)
            tmp = entry.with_suffix(f".{os.getpid()}.tmp")
            with open(tmp, "wb") as fh:
                pickle.dump(idx, fh, protocol=pickle.HIGHEST_PROTOCOL)
            os.replace(tmp, entry)
        except Exception:  # noqa: BLE001
            pass
    return idx


def _time_range(mdf, gi: int, group) -> tuple[Optional[float], Optional[float]]:
    """First and last time stamp of one channel group, reading only those two records."""
    n = int(getattr(group.channel_group, "cycles_nr", 0) or 0)
    if n <= 0:
        return None, None
    try:
        first = mdf.get_master(gi, record_offset=0, record_count=1)
        last = mdf.get_master(gi, record_offset=n - 1, record_count=1)
        if len(first) and len(last):
            return float(first[0]), float(last[-1])
    except Exception:  # noqa: BLE001
        pass
    m = mdf.get_master(gi)  # fallback: the whole time channel
    return (float(m[0]), float(m[-1])) if m is not None and len(m) else (None, None)


def _index_file(path: str) -> RecordingIndex:
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
        try:
            idx.block_addrs = {gi: int(g.data_group.data_block_addr) for gi, g in enumerate(mdf.groups)}
        except Exception:  # noqa: BLE001
            idx.block_addrs = None
        for gi, group in enumerate(mdf.groups):
            gname = _group_name(group, gi)
            master_index = getattr(group.channel_group, "master_index", None)
            for ci, ch in enumerate(group.channels):
                if getattr(ch, "channel_type", None) == 2 or ci == master_index:  # master channel
                    continue
                if ch.name in ("time", "t") and ci == 0:
                    continue
                unit, comment, discrete, enum = channel_meta(ch)
                idx.channels.append(ChannelInfo(ch.name, gi, ci, unit, gname, comment, discrete, enum))
        starts, ends = [], []
        for gi, group in enumerate(mdf.groups):
            try:
                a, b = _time_range(mdf, gi, group)
            except Exception:  # noqa: BLE001
                continue
            if a is not None:
                starts.append(a)
                ends.append(b)
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


_TEXT_CONVERSIONS = (7, 8)  # value to text, value range to text


def _plain(text) -> str:
    """Comment text without the MDF XML wrapper (<CNcomment><TX>…</TX>…)."""
    text = str(text or "")
    m = re.search(r"<TX>(.*?)</TX>", text, re.S)
    if m:
        text = m.group(1)
    elif text.lstrip().startswith("<"):
        text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(re.sub(r"\s+", " ", text)).strip()
    return text[:300] + ("…" if len(text) > 300 else "")


def _text(v) -> Optional[str]:
    if isinstance(v, bytes):
        v = v.decode("utf-8", "replace")
    if not isinstance(v, str):
        return None
    v = v.strip().strip("\x00").strip()
    return v or None


def conversion_table(conv) -> tuple:
    """((text, value), ...) of a value-to-text / range-to-text conversion that describes an enum.

    Conversions whose default is another (numeric) conversion are physical signals with a few
    special markers (e.g. 4095 = 'Fault'), not enums: they return ()."""
    if conv is None or getattr(conv, "conversion_type", None) not in _TEXT_CONVERSIONS:
        return ()
    refs = getattr(conv, "referenced_blocks", None) or {}
    default = refs.get("default_addr")
    if default is not None and not isinstance(default, (bytes, str)):
        return ()
    pairs, seen = [], set()
    i = 0
    while f"text_{i}" in refs:
        text = _text(refs[f"text_{i}"])
        value = getattr(conv, f"val_{i}", None)
        if value is None:
            value = getattr(conv, f"lower_{i}", None)
        i += 1
        if text is None or value is None or text in seen:
            continue
        seen.add(text)
        v = float(value)
        pairs.append((text, int(v) if v.is_integer() else v))
    return tuple(sorted(pairs, key=lambda p: p[1]))


def channel_meta(ch) -> tuple[str, str, bool, tuple]:
    """(unit, description, discrete, enum table) of an asammdf channel. Many loggers leave the channel
    unit empty and store it on the conversion (or on the numeric conversion behind a text marker)."""
    conv = getattr(ch, "conversion", None)
    unit = str(getattr(ch, "unit", "") or "")
    if not unit and conv is not None:
        unit = str(getattr(conv, "unit", "") or "")
        default = (getattr(conv, "referenced_blocks", None) or {}).get("default_addr")
        if not unit and default is not None and not isinstance(default, (bytes, str)):
            unit = str(getattr(default, "unit", "") or "")
    try:
        enum = conversion_table(conv)
    except Exception:  # noqa: BLE001
        enum = ()
    try:
        comment = _plain(getattr(ch, "comment", ""))
    except Exception:  # noqa: BLE001
        comment = ""
    return unit.strip(), comment, bool(enum), enum


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
    offsets: dict = field(default_factory=dict)  # file -> manual time shift [s], added on reading

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


def group_recordings(files: list, indexes: Optional[dict] = None, combine: bool = True,
                     offsets: Optional[dict] = None) -> list[Recording]:
    """Group files into recordings: same folder and same name before the logger part
    (``AADT-150453_002_ABD_Robot__…`` + ``AADT-150453_002_MOTION_1__…``). Files whose
    measurement start differs by more than MAX_START_GAP_S are kept apart. Files recorded at the
    same time (their time spans overlap, e.g. a ground-truth logger next to the vehicle's) are read
    together too, whatever their names.
    `offsets` (file -> seconds) are manual time shifts carried by the recordings."""
    out = _group(files, indexes, combine)
    if combine:
        out = _merge_overlapping(out, indexes or {}, [str(f) for f in files])
    if offsets:
        for r in out:
            r.offsets = {f: float(offsets[f]) for f in r.files if offsets.get(f)}
    return out


def _span(rec: Recording, indexes: dict) -> Optional[tuple[float, float]]:
    """The recording's time span [s since the epoch], or None when a file's start or length is unknown."""
    spans = []
    for f in rec.files:
        idx = indexes.get(f)
        if idx is None or not idx.ok or idx.abs_start is None or idx.start is None or idx.end is None:
            return None
        spans.append((idx.abs_start + idx.start, idx.abs_start + idx.end))
    return min(a for a, _ in spans), max(b for _, b in spans)


def _merge_overlapping(recs: list[Recording], indexes: dict, files: list[str]) -> list[Recording]:
    """Join recordings whose time spans overlap: they were recorded at the same time."""
    spans = [_span(r, indexes) for r in recs]
    parent = list(range(len(recs)))

    def root(i):
        while parent[i] != i:
            i = parent[i]
        return i

    for i in range(len(recs)):
        for j in range(i + 1, len(recs)):
            a, b = spans[i], spans[j]
            if a and b and a[0] < b[1] and b[0] < a[1]:
                parent[root(j)] = root(i)
    groups: dict[int, list[Recording]] = {}
    for i, r in enumerate(recs):
        groups.setdefault(root(i), []).append(r)
    order = {f: i for i, f in enumerate(files)}
    out = []
    for g in groups.values():
        if len(g) == 1:
            out.append(g[0])
            continue
        members = sorted((f for r in g for f in r.files), key=lambda f: order.get(f, 0))
        first = min(g, key=lambda r: min(order.get(f, 0) for f in r.files))
        out.append(Recording(str(Path(first.name).with_suffix("")) + f" + {len(g) - 1}", members))
    return sorted(out, key=lambda r: min(order.get(f, 0) for f in r.files))


def _group(files: list, indexes: Optional[dict], combine: bool) -> list[Recording]:
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


def _read_file(mdf, wanted: list) -> list:
    """Read channels of one open file: [(signal, ChannelInfo)] -> [(signal, Signal or Exception)].
    Channels are read with one `select` (each data group is read once, however many channels it holds)
    in raw form; physical values are computed from it, and enum channels keep the raw numbers too."""
    out = []
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        try:
            sigs = mdf.select([(ch.name, ch.group, ch.index) for _, ch in wanted], raw=True)
        except Exception:  # noqa: BLE001 - one bad channel: read them one by one
            sigs = []
            for _, ch in wanted:
                try:
                    sigs.append(mdf.get(ch.name, group=ch.group, index=ch.index, raw=True))
                except Exception as exc:  # noqa: BLE001
                    sigs.append(exc)
        for (sname, _), rsig in zip(wanted, sigs):
            if isinstance(rsig, Exception):
                out.append((sname, rsig))
                continue
            try:
                sig = rsig.physical()
                sig.raw_signal = rsig
                out.append((sname, sig))
            except Exception as exc:  # noqa: BLE001
                out.append((sname, exc))
    return out


def _complete(mdf, wanted: list, read: list) -> bool:
    """Every channel came back with as many samples as its group holds."""
    for (_, ch), (_, sig) in zip(wanted, read):
        if isinstance(sig, Exception):
            return False
        try:
            n = int(mdf.groups[ch.group].channel_group.cycles_nr)
        except Exception:  # noqa: BLE001
            return False
        if len(sig.samples) != n:
            return False
    return True


def _attach_raw_values(rs, sig) -> None:
    """Text samples from a value-to-text conversion: keep the raw numbers and the labels too."""
    if sig is None:
        return
    try:
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
    offset = {f: (indexes[f].abs_start - t0 if t0 is not None and indexes[f].ok else 0.0)
              + float(rec.offsets.get(f, 0.0)) for f in rec.files}

    mapping = resolve_recording(defs, rec, indexes)
    by_file: dict[str, list[str]] = {}
    for sname, (f, ch, note) in mapping.items():
        sdef = defs[sname]
        if ch is None:
            sev = "error" if sdef.required else "warning"
            where = f" in any of the {len(rec.files)} files" if multi else ""
            issues.append(FileIssue(sev, f"signal '{sname}': none of {sdef.channels} found{where}"))
            continue
        if note:
            issues.append(FileIssue("info", f"signal '{sname}': {note}"))
        by_file.setdefault(f, []).append(sname)

    raw: dict[str, RawSignal] = {}
    for f, names in by_file.items():
        wanted = [(n, mapping[n][1]) for n in names]
        addrs = indexes[f].block_addrs
        blocks = None if not addrs else {addrs.get(ch.group, -1) for _, ch in wanted}
        try:
            mdf = _open(f, blocks)
            read = _read_file(mdf, wanted)
            if blocks is not None and not _complete(mdf, wanted, read):
                mdf.close()  # the file is not laid out as the index says: open it the usual way
                mdf = _open(f)
                read = _read_file(mdf, wanted)
        except Exception as exc:  # noqa: BLE001
            for sname in names:
                issues.append(FileIssue("error" if defs[sname].required else "warning",
                                        f"signal '{sname}': cannot open {Path(f).name}: {exc}"))
            continue
        try:
            for sname, sig in read:
                ch, sdef = mapping[sname][1], defs[sname]
                if isinstance(sig, Exception):
                    issues.append(FileIssue("error" if sdef.required else "warning",
                                            f"signal '{sname}': failed to read '{ch.name}': {sig}"))
                    continue
                raw[sname] = RawSignal(sname, np.asarray(sig.timestamps) + offset[f], np.asarray(sig.samples),
                                       unit=str(sig.unit or ch.unit or ""),
                                       channel=f"{ch.name} ({Path(f).name})" if multi else ch.name, group=ch.group)
                if raw[sname].samples.dtype.kind in "SUO":
                    _attach_raw_values(raw[sname], getattr(sig, "raw_signal", None))
        finally:
            try:
                mdf.close()
            except Exception:  # noqa: BLE001
                pass
    return raw, issues
