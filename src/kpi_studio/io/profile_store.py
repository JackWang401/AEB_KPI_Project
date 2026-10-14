"""Load, resolve and save profiles and KPI libraries (YAML)."""

from __future__ import annotations

import copy
import functools
import hashlib
import os
import tempfile
from pathlib import Path
from typing import Any, Optional, Union

import yaml
from pydantic import ValidationError

from src.utils.path_manager import get_resource
from src.kpi_studio.core.model import SCHEMA_VERSION, Library, Profile
from src.kpi_studio.core.validate import CompiledProfile, Issue, ProfileError, compile_profile

PROFILE_SUFFIX = ".kpi.yaml"
LIBRARY_SUFFIX = ".kpi-lib.yaml"

PathLike = Union[str, Path]


def builtin_library_dir() -> Path:
    return get_resource("kpi_studio/library")


def builtin_profile_dir() -> Path:
    return get_resource("kpi_studio/profiles")


def load_enum_tables(path: Optional[PathLike] = None) -> dict[str, dict[str, Any]]:
    """Enum groups from config/enum_definitions.yaml (the mapping sections are skipped)."""
    return _load_enum_tables(str(path) if path else str(get_resource("config/enum_definitions.yaml")))


@functools.lru_cache(maxsize=8)
def _load_enum_tables(path: str) -> dict[str, dict[str, Any]]:
    path = Path(path)
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        content = yaml.safe_load(f) or {}
    return {k: v for k, v in content.items()
            if isinstance(v, dict) and k not in {"EnumToValueMap", "ValueToEnumMap"}}


# ----------------------------------------------------------------------------- migrations
def _migrate(data: dict, where: str) -> dict:
    """Upgrade older schema versions in place. Version 1 is the first released format."""
    version = data.get("schema_version", SCHEMA_VERSION)
    if version > SCHEMA_VERSION:
        raise ProfileError([Issue("error", where, f"schema_version {version} is newer than this tool "
                                                   f"supports ({SCHEMA_VERSION}); update KPI Studio")])
    # future: if version == 1: ...; version = 2
    data["schema_version"] = SCHEMA_VERSION
    return data


def _validation_issues(exc: ValidationError, where: str) -> list[Issue]:
    issues = []
    for e in exc.errors():
        loc = ".".join(str(p) for p in e.get("loc", ()) if p not in ("function-after", "function-before"))
        msg = e.get("msg", "invalid")
        if e.get("type") == "extra_forbidden":
            msg = "unknown key (typo?)"
        issues.append(Issue("error", f"{where}:{loc}" if loc else where, msg))
    return issues


def _read_yaml(path: Path) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except FileNotFoundError:
        raise ProfileError([Issue("error", str(path), "file not found")]) from None
    except yaml.YAMLError as exc:
        raise ProfileError([Issue("error", str(path), f"not valid YAML: {exc}")]) from None
    if not isinstance(data, dict):
        raise ProfileError([Issue("error", str(path), "file must contain a mapping at the top level")])
    return data


# ----------------------------------------------------------------------------- libraries
def find_library(ref: str, base_dir: Optional[Path]) -> Path:
    """Resolve an import entry: a path (relative to the profile) or a bare library name."""
    candidates = []
    p = Path(ref)
    if base_dir is not None:
        candidates.append(base_dir / p)
    candidates.append(p)
    if not ref.endswith(".yaml"):
        names = [ref + LIBRARY_SUFFIX]
        if base_dir is not None:
            candidates.extend(base_dir / n for n in names)
            candidates.extend(base_dir / "library" / n for n in names)
        candidates.extend(builtin_library_dir() / n for n in names)
    else:
        candidates.append(builtin_library_dir() / p.name)
    for c in candidates:
        if c.is_file():
            return c.resolve()
    raise ProfileError([Issue("error", f"imports: {ref}", "library not found")])


def load_library(path: PathLike) -> Library:
    path = Path(path)
    data = _migrate(_read_yaml(path), str(path))
    data.setdefault("name", path.name.split(".")[0])
    try:
        return Library.model_validate(data)
    except ValidationError as exc:
        raise ProfileError(_validation_issues(exc, str(path))) from None


def resolve_imports(profile: Profile, base_dir: Optional[Path]) -> Profile:
    """Flatten imported libraries into one self-contained profile.

    * signals/derived from libraries are added unless the profile defines the same
      name (the profile wins, e.g. different channel names for another logger);
      two libraries defining one name differently is an error.
    * library events become '<lib>.<event>'.
    * `use: lib.kpi` / `use: lib.*` copies KPIs; `as:` renames one.
    """
    if not profile.imports:
        return profile
    issues: list[Issue] = []
    data = profile.model_dump(by_alias=True, exclude_none=True)
    signals = dict(data.get("signals", {}))
    derived = dict(data.get("derived", {}))
    events = dict(data.get("events", {}))
    origin: dict[str, str] = {}
    libs: dict[str, Library] = {}

    for ref in profile.imports:
        try:
            lib = load_library(find_library(ref, base_dir))
        except ProfileError as exc:
            issues.extend(exc.issues)
            continue
        if lib.imports:
            issues.append(Issue("error", f"library {lib.name}", "libraries cannot import other libraries"))
        if lib.name in libs:
            issues.append(Issue("error", f"imports: {ref}", f"library name '{lib.name}' imported twice"))
            continue
        libs[lib.name] = lib
        ldata = lib.model_dump(by_alias=True, exclude_none=True)
        for section, target in (("signals", signals), ("derived", derived)):
            for name, value in ldata.get(section, {}).items():
                key = f"{section}.{name}"
                if name in profile.model_dump().get(section, {}):
                    continue  # profile override
                if name in target and target[name] != value:
                    issues.append(Issue("error", key, f"defined differently in libraries "
                                                      f"'{origin.get(key)}' and '{lib.name}'; define it in the "
                                                      f"profile to choose"))
                    continue
                target[name] = value
                origin[key] = lib.name

    kpis: list[dict] = []
    used_events: set[str] = set()
    for i, k in enumerate(profile.kpis):
        if not k.use:
            kpis.append(k.model_dump(by_alias=True, exclude_none=True))
            if k.event and "." in k.event:
                used_events.add(k.event)
            continue
        lib_name, _, kname = k.use.partition(".")
        lib = libs.get(lib_name)
        if lib is None or not kname:
            issues.append(Issue("error", f"kpis[{i}].use", f"'{k.use}': library '{lib_name}' is not imported"))
            continue
        chosen = lib.kpis if kname == "*" else [x for x in lib.kpis if x.name == kname]
        if not chosen:
            issues.append(Issue("error", f"kpis[{i}].use", f"library '{lib_name}' has no KPI '{kname}'"))
            continue
        if k.as_ and len(chosen) > 1:
            issues.append(Issue("error", f"kpis[{i}].as", "'as' can only rename a single KPI"))
        for src in chosen:
            d = src.model_dump(by_alias=True, exclude_none=True)
            if k.as_:
                d["name"] = k.as_
            if src.event in lib.events:
                d["event"] = f"{lib_name}.{src.event}"
            used_events.add(d["event"])
            kpis.append(d)

    for qualified in sorted(used_events):
        lib_name, _, ename = qualified.partition(".")
        lib = libs.get(lib_name)
        if lib and ename in lib.events and qualified not in events:
            events[qualified] = lib.events[ename].model_dump(by_alias=True, exclude_none=True)

    if issues:
        raise ProfileError(issues)
    data.update(signals=signals, derived=derived, events=events, kpis=kpis, imports=[])
    try:
        return Profile.model_validate(data)
    except ValidationError as exc:
        raise ProfileError(_validation_issues(exc, "resolved profile")) from None


def inline_libraries(data: dict, base_dir: Optional[Path]) -> dict:
    """Copy everything a profile takes from libraries into the profile itself.

    The result has no `imports` and no `use:` entries; library events lose their
    '<library>.' prefix (kept only when two would clash). Libraries that cannot be
    found are dropped together with their `use:` entries."""
    data = copy.deepcopy(data)
    if not data.get("imports") and not any(isinstance(k, dict) and k.get("use") for k in data.get("kpis") or []):
        return data
    try:
        resolved = resolve_imports(parse_profile(data, "profile"), base_dir)
        flat = resolved.model_dump(by_alias=True, exclude_none=True)
        flat = {k: flat[k] for k in ("signals", "derived", "events", "kpis") if k in flat}
    except ProfileError:
        flat = {"kpis": [k for k in data.get("kpis") or [] if not (isinstance(k, dict) and k.get("use"))]}
    data.update(flat)
    data["imports"] = []
    events = data.get("events") or {}
    plain: dict[str, str] = {}
    for name in list(events):
        if "." in name:
            short = name.split(".", 1)[1]
            if short in events or short in plain.values():
                short = name.replace(".", "_")
            plain[name] = short
    if plain:
        data["events"] = {plain.get(k, k): v for k, v in events.items()}
        for item in (data.get("kpis") or []) + (data.get("plots") or []):
            if isinstance(item, dict) and item.get("event") in plain:
                item["event"] = plain[item["event"]]
    return data


# ----------------------------------------------------------------------------- profiles
def parse_profile(data: dict, where: str = "profile") -> Profile:
    data = _migrate(copy.deepcopy(data), where)
    try:
        return Profile.model_validate(data)
    except ValidationError as exc:
        raise ProfileError(_validation_issues(exc, where)) from None


def load_profile(path: PathLike) -> Profile:
    """Parse and validate the schema of a profile file (imports not resolved)."""
    path = Path(path)
    return parse_profile(_read_yaml(path), str(path))


def load_compiled(path_or_profile: Union[PathLike, Profile], base_dir: Optional[Path] = None,
                  enum_tables: Optional[dict] = None) -> tuple[Profile, CompiledProfile]:
    """Load + resolve imports + compile. Returns (resolved profile, compiled)."""
    if isinstance(path_or_profile, Profile):
        profile = path_or_profile
    else:
        path = Path(path_or_profile)
        profile = load_profile(path)
        base_dir = base_dir or path.parent
    resolved = resolve_imports(profile, base_dir)
    tables = load_enum_tables() if enum_tables is None else enum_tables
    return resolved, compile_profile(resolved, tables)


def to_yaml(model: Union[Profile, Library]) -> str:
    data = model.model_dump(by_alias=True, exclude_none=True, exclude_defaults=False)
    return yaml.safe_dump(_strip_empty(data), sort_keys=False, allow_unicode=True, width=110)


def _strip_empty(obj):
    if isinstance(obj, dict):
        return {k: _strip_empty(v) for k, v in obj.items() if v not in ({}, [], "", None)}
    if isinstance(obj, list):
        return [_strip_empty(v) for v in obj]
    if isinstance(obj, tuple):
        return [_strip_empty(v) for v in obj]
    return obj


def atomic_write_text(path: PathLike, text: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def save_profile(model: Union[Profile, Library], path: PathLike) -> None:
    atomic_write_text(path, to_yaml(model))


def profile_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
