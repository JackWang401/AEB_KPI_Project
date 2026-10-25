"""Shared GUI state: the profile being edited, the loaded files, live validation."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Optional

import yaml
from PySide6.QtCore import QObject, QTimer, Signal

from src.kpi_studio.core.model import Profile
from src.kpi_studio.core.validate import CompiledProfile, Issue, ProfileError
from src.kpi_studio.io import profile_store
from src.kpi_studio.io.mf4 import Recording, RecordingIndex, group_recordings

NEW_PROFILE = {
    "schema_version": 1,
    "name": "New profile",
    "description": "",
    "imports": [],
    "timebase": {"rate_hz": 100.0},
    "signals": {},
    "derived": {},
    "events": {},
    "kpis": [],
    "plots": [],
}


_LIB_CACHE: dict = {}


class AppState(QObject):
    profile_changed = Signal()          # structure edited (pages refresh lists)
    validated = Signal()                # compile finished; see .issues / .compiled
    files_changed = Signal()
    dirty_changed = Signal(bool)
    offsets_changed = Signal(list)      # files whose manual time offset changed

    def __init__(self, parent=None):
        super().__init__(parent)
        self.data: dict = copy.deepcopy(NEW_PROFILE)
        self.path: Optional[Path] = None
        self.dirty = False
        self.files: list[str] = []          # every file in the Files panel
        self.unchecked: set[str] = set()    # files the user unticked: not analysed
        self.indexes: dict[str, RecordingIndex] = {}
        self.load_error: Optional[str] = None  # set when an unfinished (draft) profile was opened
        self.offsets: dict[str, float] = {}  # file -> manual time shift [s] (viewer and KPI run; not saved)
        self.together: list[list[str]] = []  # files the user reads as one recording (Files: right-click)
        self.versions: dict[str, str] = {}  # file -> software version, written next to its results
        self.issues: list[Issue] = []
        self.compiled: Optional[CompiledProfile] = None
        self.resolved: Optional[Profile] = None
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(300)
        self._timer.timeout.connect(self.validate_now)

    # ------------------------------------------------------------------ profile
    @property
    def base_dir(self) -> Optional[Path]:
        return self.path.parent if self.path else None

    def new(self):
        self.data = copy.deepcopy(NEW_PROFILE)
        self.path = None
        self._set_dirty(False)
        self.profile_changed.emit()
        self.validate_now()

    def load(self, path: str | Path):
        path = Path(path)
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        if not isinstance(data, dict):
            raise ProfileError(f"{path}: not a KPI profile (expected a mapping at the top level)")
        self.load_error = None
        try:
            profile_store.parse_profile(data, str(path))
        except ProfileError as exc:  # an unfinished draft: open it, the issues show in Profile check
            self.load_error = str(exc)
        flat = profile_store.inline_libraries(data, path.parent)  # KPI Studio has no libraries
        merged = copy.deepcopy(NEW_PROFILE)
        merged.update(flat)
        from src.kpi_studio.core.rename import use_named_ranges

        moved = use_named_ranges(merged)  # KPIs over two moments -> over a named range of the event
        self.data = merged
        self.path = path
        self._set_dirty(flat != data or bool(moved))
        self.profile_changed.emit()
        self.validate_now()

    def save(self, path: Optional[str | Path] = None):
        path = Path(path) if path else self.path
        if path is None:
            raise ValueError("no file name")
        profile = self.as_profile()
        profile_store.save_profile(profile, path)
        self.path = path
        self._set_dirty(False)

    def save_draft(self, path: Optional[str | Path] = None):
        """Write the profile as it is, unfinished parts included (Run stays blocked until fixed)."""
        path = Path(path) if path else self.path
        if path is None:
            raise ValueError("no file name")
        text = "# Draft: this profile still has errors; KPI Studio shows them in Profile check.\n" + \
            profile_store.profile_text(self.clean_data())
        profile_store.atomic_write_text(path, text)
        self.path = path
        self._set_dirty(False)

    def as_profile(self) -> Profile:
        return profile_store.parse_profile(self.clean_data(), "profile")

    def clean_data(self) -> dict:
        def strip(obj):
            if isinstance(obj, dict):
                return {k: strip(v) for k, v in obj.items() if v not in (None, "", [], {}) or k in ("signals",)}
            if isinstance(obj, list):
                return [strip(v) for v in obj]
            return obj

        data = strip(copy.deepcopy(self.data))
        data.setdefault("name", "Unnamed")
        return data

    def edited(self, structure: bool = False):
        """Call after changing self.data. `structure`: names/lists changed, pages should refresh."""
        self._set_dirty(True)
        if structure:
            self.profile_changed.emit()
        self._timer.start()

    def _set_dirty(self, value: bool):
        if self.dirty != value:
            self.dirty = value
            self.dirty_changed.emit(value)

    def validate_now(self):
        self._timer.stop()
        try:
            profile = self.as_profile()
            self.resolved, self.compiled = profile_store.load_compiled(profile, base_dir=self.base_dir)
            self.issues = list(self.compiled.warnings)
        except ProfileError as exc:
            self.compiled = None
            self.issues = list(exc.issues)
        except Exception as exc:  # noqa: BLE001
            self.compiled = None
            self.issues = [Issue("error", "profile", f"{type(exc).__name__}: {exc}")]
        if self.compiled is None:
            # an unfinished event or KPI must not hide new signals from the other tabs:
            # resolve the signals (and imported libraries) on their own
            resolved, _ = self._signals_only()
            if resolved is not None:
                self.resolved = resolved
        self.validated.emit()

    def signals_only_compiled(self):
        """Compile only the signals and derived signals (events, KPIs and plots left out).

        Lets the signal viewer work while an event or KPI is still incomplete.
        Returns the compiled profile, or None if the signals themselves have errors.
        """
        return self._signals_only()[1]

    def _signals_only(self):
        data = self.clean_data()
        for key in ("events", "kpis", "plots"):
            data.pop(key, None)
        try:
            profile = profile_store.parse_profile(data, "profile")
            return profile_store.load_compiled(profile, base_dir=self.base_dir)
        except Exception:  # noqa: BLE001
            return None, None

    def library_definitions(self) -> tuple[dict, dict]:
        """(signals, derived) defined by the imported libraries, read from the library files."""
        signals, derived = {}, {}
        for ref in self.data.get("imports", []) or []:
            try:
                path = profile_store.find_library(ref, self.base_dir)
                key = (str(path), path.stat().st_mtime)
                lib = _LIB_CACHE.get(key)
                if lib is None:
                    lib = _LIB_CACHE[key] = profile_store.load_library(path)
            except Exception:  # noqa: BLE001 - reported by validation
                continue
            signals.update(lib.signals)
            derived.update(lib.derived)
        return signals, derived

    def taken_names(self) -> set[str]:
        """Every signal / derived name currently defined (own and imported), from the live data."""
        lib_sig, lib_der = self.library_definitions()
        return set(self.data.get("signals", {}) or {}) | set(self.data.get("derived", {}) or {}) \
            | set(lib_sig) | set(lib_der)

    # names usable in expressions (after resolving imports when possible)
    def signal_names(self) -> list[str]:
        """Signal and derived names as they are now (the last validation can lag behind a rename), in
        alphabetical order: every list to pick a signal from shows them that way."""
        live = self.taken_names()
        names = []
        if self.resolved is not None:
            names = [n for n in list(self.resolved.signals) + list(self.resolved.derived) if n in live]
        lib_sig, lib_der = self.library_definitions()
        for n in list(lib_sig) + list(self.data.get("signals", {}) or {}) + list(lib_der) \
                + list(self.data.get("derived", {}) or {}):
            if n not in names:
                names.append(n)
        return sorted(names, key=str.lower)

    def derived_kind(self, name: str, *, inferred: bool = False, _seen: frozenset = frozenset()) -> str:
        """A derived signal's Kind: the one set in the table, else the one it gets from its inputs: a
        condition is boolean, one input signal passes on its Kind (an enum's values become discrete),
        several inputs give continuous unless all are held. inferred=True: always the latter."""
        from src.kpi_studio.core.expr import compile_expr

        value = (self.data.get("derived") or {}).get(name)
        if value is None and self.resolved is not None and name in self.resolved.derived:
            value = self.resolved.derived[name].model_dump(exclude_none=True)
        if isinstance(value, dict) and value.get("kind") and not inferred:
            return value["kind"]
        expr = value.get("expr", "") if isinstance(value, dict) else str(value or "")
        held = {n for n, d in self.resolved.signals.items() if d.held} if self.resolved is not None else set()
        try:
            c = compile_expr(expr, names=set(self.signal_names()) | {"time"}, enums=self.enum_map(), discrete=held)
        except Exception:
            return "continuous"
        if c.is_bool:
            return "boolean"
        kinds = {self.input_kind(d, _seen | {name}) for d in c.deps if d != "time"}
        if len(kinds) == 1:
            kind = kinds.pop()
            return "discrete" if kind in ("enum", "boolean") else kind  # not a condition: no longer 0/1
        return "discrete" if c.hold else "continuous"

    def input_kind(self, name: str, seen: frozenset = frozenset()) -> str:
        """Kind of a signal or derived signal an expression reads."""
        sig = (self.data.get("signals") or {}).get(name)
        if isinstance(sig, dict):
            return sig.get("kind") or "continuous"
        if self.resolved is not None and name in self.resolved.signals:
            return self.resolved.signals[name].kind
        if name in seen:
            return "continuous"
        return self.derived_kind(name, _seen=seen)

    def kind_source(self, name: str) -> str:
        """Which inputs a derived signal's inferred Kind comes from, e.g. 'egoSpeed', for messages."""
        from src.kpi_studio.core.expr import compile_expr

        value = (self.data.get("derived") or {}).get(name)
        expr = value.get("expr", "") if isinstance(value, dict) else str(value or "")
        try:
            deps = compile_expr(expr, names=set(self.signal_names()) | {"time"}, enums=self.enum_map()).deps
        except Exception:
            return ""
        return ", ".join(sorted(d for d in deps if d != "time"))

    def unit_map(self) -> dict:
        """signal / derived name -> its Unit ('' when none), from the live data."""
        from src.kpi_studio.core.model import signal_units

        units = signal_units(self.resolved) if self.resolved is not None else {}
        for name, sdef in (self.data.get("signals") or {}).items():
            units[name] = str((sdef or {}).get("unit") or "") if isinstance(sdef, dict) else ""
        for name, ddef in (self.data.get("derived") or {}).items():
            units[name] = str(ddef.get("unit") or "") if isinstance(ddef, dict) else units.get(name, "")
        return units

    def enum_map(self) -> dict:
        """signal name -> {MEMBER: value} for signals that declare an enum."""
        tables = profile_store.load_enum_tables()
        signals = dict(self.resolved.signals) if self.resolved is not None else {}
        out = {}
        for name, sdef in signals.items():
            if isinstance(sdef.enum, dict):
                out[name] = dict(sdef.enum)
            elif sdef.enum and sdef.enum in tables:
                out[name] = tables[sdef.enum]
        for name, sdef in (self.data.get("signals") or {}).items():
            enum = (sdef or {}).get("enum")
            if isinstance(enum, dict):
                out[name] = dict(enum)
            elif enum in tables:
                out[name] = tables[enum]
        return out

    def kpi_names(self) -> list[str]:
        names = [k.get("name") for k in self.data.get("kpis", []) or [] if k.get("name")]
        if self.resolved is not None:
            names += [k.name for k in self.resolved.kpis if k.name not in names]
        return names

    def event_names(self) -> list[str]:
        names = list(self.data.get("events", {}))
        if self.resolved is not None:
            names += [e for e in self.resolved.events if e not in names]
        return names

    def moments_of(self, event: str) -> list[str]:
        from src.kpi_studio.core.model import BUILTIN_MOMENTS

        ev = self.data.get("events", {}).get(event)
        if ev is None and self.resolved is not None and event in self.resolved.events:
            return list(BUILTIN_MOMENTS) + list(self.resolved.events[event].moments)
        return list(BUILTIN_MOMENTS) + list((ev or {}).get("moments", {}) or {})

    def ranges_of(self, event: str) -> list[str]:
        from src.kpi_studio.core.model import BUILTIN_RANGES

        ev = self.data.get("events", {}).get(event)
        if ev is None and self.resolved is not None and event in self.resolved.events:
            return list(BUILTIN_RANGES) + list(self.resolved.events[event].ranges)
        return list(BUILTIN_RANGES) + list((ev or {}).get("ranges", {}) or {})

    # ------------------------------------------------------------------ files
    def set_files(self, files: list[str]):
        self.files = list(dict.fromkeys(files))
        self.indexes = {f: i for f, i in self.indexes.items() if f in self.files}
        self.offsets = {f: v for f, v in self.offsets.items() if f in self.files}
        self.together = [g for g in ([f for f in g if f in self.files] for g in self.together) if len(g) > 1]
        self.unchecked &= set(self.files)
        self.files_changed.emit()

    @property
    def checked_files(self) -> list[str]:
        """The files to analyse: the ticked ones, in the Files panel's order."""
        return [f for f in self.files if f not in self.unchecked]

    def set_checked(self, files, checked: bool):
        """Tick (analyse) or untick files."""
        before = set(self.unchecked)
        if checked:
            self.unchecked -= set(files)
        else:
            self.unchecked |= set(files) & set(self.files)
        if self.unchecked != before:
            self.files_changed.emit()

    def set_index(self, path: str, index: RecordingIndex):
        self.indexes[path] = index
        self.files_changed.emit()

    def recordings(self) -> list[Recording]:
        """What gets processed: the files grouped into recordings (files of one recording are always combined)."""
        return group_recordings(self.checked_files, self.indexes, offsets=self.offsets, together=self.together)

    def read_together(self, files: list[str]):
        """Read `files` as one recording from now on (lined up by their measurement start)."""
        files = [f for f in dict.fromkeys(files) if f in self.files]
        if len(files) < 2:
            return
        self.together = [g for g in ([f for f in g if f not in files] for g in self.together) if len(g) > 1]
        self.together.append(files)
        self.files_changed.emit()

    def read_apart(self, files: list[str]):
        """Undo read_together for `files`: each goes back to its automatic recording."""
        before = [list(g) for g in self.together]
        self.together = [g for g in ([f for f in g if f not in files] for g in self.together) if len(g) > 1]
        if self.together != before:
            self.files_changed.emit()

    def set_offsets(self, changes: dict[str, float]):
        """Set the manual time shift [s] of files (0 removes it); positive moves a file later."""
        changed = []
        for f, v in changes.items():
            v = round(float(v), 6)
            if v != self.offsets.get(f, 0.0):
                changed.append(f)
                if v:
                    self.offsets[f] = v
                else:
                    self.offsets.pop(f, None)
        if changed:
            self.offsets_changed.emit(changed)
