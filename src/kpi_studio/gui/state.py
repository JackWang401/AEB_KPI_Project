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


class AppState(QObject):
    profile_changed = Signal()          # structure edited (pages refresh lists)
    validated = Signal()                # compile finished; see .issues / .compiled
    files_changed = Signal()
    dirty_changed = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.data: dict = copy.deepcopy(NEW_PROFILE)
        self.path: Optional[Path] = None
        self.dirty = False
        self.files: list[str] = []
        self.indexes: dict[str, RecordingIndex] = {}
        self.load_error: Optional[str] = None  # set when an unfinished (draft) profile was opened
        self.combine = True   # read files of the same recording (one per logger/bus) together
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
        merged = copy.deepcopy(NEW_PROFILE)
        merged.update(data)
        self.data = merged
        self.path = path
        self._set_dirty(False)
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
            yaml.safe_dump(self.clean_data(), sort_keys=False, allow_unicode=True)
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
                lib = profile_store.load_library(profile_store.find_library(ref, self.base_dir))
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
        if self.resolved is not None:
            return list(self.resolved.signals) + list(self.resolved.derived)
        return list(self.data.get("signals", {})) + list(self.data.get("derived", {}))

    def enum_map(self) -> dict:
        """signal name -> {MEMBER: value} for signals that declare an enum."""
        tables = profile_store.load_enum_tables()
        signals = dict(self.resolved.signals) if self.resolved is not None else {}
        out = {}
        for name, sdef in signals.items():
            if sdef.enum and sdef.enum in tables:
                out[name] = tables[sdef.enum]
        for name, sdef in (self.data.get("signals") or {}).items():
            enum = (sdef or {}).get("enum")
            if enum in tables:
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

    # ------------------------------------------------------------------ files
    def set_files(self, files: list[str]):
        self.files = list(dict.fromkeys(files))
        self.indexes = {f: i for f, i in self.indexes.items() if f in self.files}
        self.files_changed.emit()

    def set_index(self, path: str, index: RecordingIndex):
        self.indexes[path] = index
        self.files_changed.emit()

    def set_combine(self, combine: bool):
        if combine != self.combine:
            self.combine = combine
            self.files_changed.emit()

    def recordings(self) -> list[Recording]:
        """What gets processed: the files grouped into recordings (one file each when combining is off)."""
        return group_recordings(self.files, self.indexes, self.combine)
