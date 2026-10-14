"""Turn a (resolved) Profile into a CompiledProfile, collecting every problem at once."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

from src.kpi_studio import toolbox
from src.kpi_studio.core.expr import Compiled, ExprError, compile_expr
from src.kpi_studio.core.model import BUILTIN_MOMENTS, TIME, EventDef, KpiDef, MomentRule, Profile, parse_moment
from src.kpi_studio.toolbox.registry import ToolError, ToolSpec


@dataclass(frozen=True)
class Issue:
    severity: str  # "error" | "warning"
    where: str
    message: str

    def __str__(self) -> str:
        return f"{self.severity.upper()} {self.where}: {self.message}"


class ProfileError(ValueError):
    def __init__(self, issues: list[Issue]):
        self.issues = issues
        errors = [i for i in issues if i.severity == "error"]
        super().__init__("profile has errors:\n" + "\n".join(f"  - {i}" for i in errors))


@dataclass
class CompiledMoment:
    name: str
    rule: MomentRule
    tool: Optional[ToolSpec] = None
    expr: Optional[Compiled] = None
    params: dict = field(default_factory=dict)


@dataclass
class CompiledEvent:
    name: str
    defn: EventDef
    start: Optional[Compiled] = None
    end: Optional[Compiled] = None
    preset: Optional[ToolSpec] = None
    preset_inputs: list = field(default_factory=list)
    preset_params: dict = field(default_factory=dict)
    moments: dict = field(default_factory=dict)
    moment_order: list = field(default_factory=list)


@dataclass
class CompiledKpi:
    defn: KpiDef
    value: Optional[Compiled] = None
    reducer: Optional[ToolSpec] = None
    of: Optional[Compiled] = None
    args: dict = field(default_factory=dict)

    @property
    def name(self) -> str:
        return self.defn.name


@dataclass
class CompiledProfile:
    profile: Profile
    signal_names: list
    derived: list  # [(name, Compiled)] in evaluation order
    events: dict
    kpis: list
    enums: dict  # signal name -> {MEMBER: value}
    discrete: set
    warnings: list

    def kpis_for(self, event: str) -> list:
        return [k for k in self.kpis if k.defn.event == event]

    def all_names(self) -> set:
        return set(self.signal_names) | {n for n, _ in self.derived}


def _toposort(nodes: dict[str, set[str]]) -> tuple[list[str], list[str]]:
    """Return (order, cycle_members)."""
    order, state = [], {}
    cycle: list[str] = []

    def visit(n, stack):
        if state.get(n) == 2:
            return
        if state.get(n) == 1:
            cycle.extend(stack[stack.index(n):])
            return
        state[n] = 1
        for d in sorted(nodes.get(n, ())):
            if d in nodes:
                visit(d, stack + [d])
        state[n] = 2
        order.append(n)

    for n in sorted(nodes):
        visit(n, [n])
    return order, sorted(set(cycle))


def compile_profile(profile: Profile, enum_tables: Optional[Mapping[str, Mapping[str, Any]]] = None) -> CompiledProfile:
    enum_tables = enum_tables or {}
    issues: list[Issue] = []

    def err(where, msg):
        issues.append(Issue("error", where, msg))

    def warn(where, msg):
        issues.append(Issue("warning", where, msg))

    # ---------------- signals
    enums: dict = {}
    for name, sdef in profile.signals.items():
        if isinstance(sdef.enum, dict):
            enums[name] = dict(sdef.enum)
        elif sdef.enum:
            if sdef.enum in enum_tables:
                enums[name] = dict(enum_tables[sdef.enum])
            else:
                err(f"signals.{name}.enum", f"unknown enum '{sdef.enum}'")
    discrete = {n for n, s in profile.signals.items() if s.kind == "discrete"}

    for name in profile.derived:
        if name in profile.signals:
            err(f"derived.{name}", "a derived signal cannot have the same name as a signal")
    for name in list(profile.signals) + list(profile.derived):
        if name == TIME:
            err(f"signals.{name}", f"'{TIME}' is built in (the timebase in seconds); choose another name")
    names = set(profile.signals) | set(profile.derived) | {TIME}

    def cexpr(where, source) -> Optional[Compiled]:
        try:
            return compile_expr(source, names=names, enums=enums, discrete=discrete)
        except ExprError as exc:
            err(where, str(exc))
            return None

    # ---------------- derived
    compiled_derived: dict[str, Compiled] = {}
    for name, source in profile.derived.items():
        c = cexpr(f"derived.{name}", source.expr)
        if c is not None:
            compiled_derived[name] = c
    order, cycle = _toposort({n: set(c.deps) for n, c in compiled_derived.items()})
    if cycle:
        err("derived", f"derived signals depend on each other in a loop: {', '.join(cycle)}")
    derived = [(n, compiled_derived[n]) for n in order if n in compiled_derived and n not in cycle]
    for n, c in derived:
        if c.hold:
            discrete.add(n)

    # ---------------- events
    moment_tools = set(toolbox.names("moment"))
    events: dict[str, CompiledEvent] = {}
    for ename, edef in profile.events.items():
        ce = CompiledEvent(name=ename, defn=edef)
        where = f"events.{ename}"
        if edef.start:
            ce.start = cexpr(f"{where}.start", edef.start)
        if edef.end:
            ce.end = cexpr(f"{where}.end", edef.end)
        if edef.preset:
            try:
                spec = toolbox.get("event", edef.preset.name)
                spec.check_arity(len(edef.preset.inputs))
                ce.preset = spec
                ce.preset_params = spec.bind_params([], edef.preset.params)
                ce.preset_inputs = [cexpr(f"{where}.preset.of", s) for s in edef.preset.inputs]
            except ToolError as exc:
                err(f"{where}.preset", str(exc))

        refs: dict[str, set[str]] = {}
        for mname, raw in edef.moments.items():
            mwhere = f"{where}.moments.{mname}"
            try:
                rule = parse_moment(raw, moment_tools)
            except (ValueError, TypeError) as exc:
                err(mwhere, str(exc))
                continue
            cm = CompiledMoment(name=mname, rule=rule)
            if rule.tool:
                try:
                    cm.tool = toolbox.get("moment", rule.tool)
                    cm.params = cm.tool.bind_params([], rule.params)
                except ToolError as exc:
                    err(mwhere, str(exc))
                cm.expr = cexpr(mwhere, rule.expr)
            for r in rule.references():
                if r not in BUILTIN_MOMENTS and r not in edef.moments:
                    err(mwhere, f"refers to unknown moment '{r}'; use one of "
                                f"{', '.join(list(BUILTIN_MOMENTS) + list(edef.moments))}")
            if rule.within is not None and not (rule.after or rule.before):
                warn(mwhere, "'within' without 'after'/'before' counts from the window start")
            ce.moments[mname] = cm
            refs[mname] = {r for r in rule.references() if r in edef.moments}
        morder, mcycle = _toposort(refs)
        if mcycle:
            err(f"{where}.moments", f"moments refer to each other in a loop: {', '.join(mcycle)}")
        ce.moment_order = [m for m in morder if m in ce.moments and m not in mcycle]
        events[ename] = ce

    # ---------------- kpis
    kpis: list[CompiledKpi] = []
    seen: set[str] = set()
    seen_by_event: dict[str, set[str]] = {}
    for i, kdef in enumerate(profile.kpis):
        if kdef.use:
            err(f"kpis[{i}]", f"'use: {kdef.use}' was not resolved; load the profile with profile_store")
            continue
        where = f"kpis.{kdef.name}"
        if kdef.name in seen:
            err(where, "duplicate KPI name")
        seen.add(kdef.name)
        ev = events.get(kdef.event)
        if ev is None:
            err(where, f"unknown event '{kdef.event}'; events: {', '.join(events) or 'none'}")
            continue
        valid_moments = set(BUILTIN_MOMENTS) | set(ev.defn.moments)

        def check_moment(m, key):
            if m not in valid_moments:
                err(f"{where}.{key}", f"unknown moment '{m}' in event '{kdef.event}'; "
                                      f"use one of {', '.join(sorted(valid_moments))}")

        ck = CompiledKpi(defn=kdef)
        if kdef.form == "value":
            ck.value = cexpr(f"{where}.value", kdef.value)
            check_moment(kdef.at, "at")
        elif kdef.form == "duration":
            for m in kdef.duration:
                check_moment(m, "duration")
        elif kdef.form == "formula":
            earlier = seen_by_event.get(kdef.event, set())
            try:
                ck.value = compile_expr(kdef.formula, names=earlier)
            except ExprError as exc:
                err(f"{where}.formula", f"{exc} (a formula may use KPIs listed earlier for event "
                                        f"'{kdef.event}': {', '.join(sorted(earlier)) or 'none'})")
        else:
            try:
                ck.reducer = toolbox.get("reducer", kdef.reduce)
                ck.args = ck.reducer.bind_params([], kdef.args)
            except ToolError as exc:
                err(f"{where}.reduce", str(exc))
            ck.of = cexpr(f"{where}.of", kdef.of)
            for m in kdef.between or ():
                check_moment(m, "between")
        kpis.append(ck)
        seen_by_event.setdefault(kdef.event, set()).add(kdef.name)

    # ---------------- plots
    kpi_names = {k.defn.name for k in kpis}
    for i, p in enumerate(profile.plots):
        where = f"plots[{i}]" + (f" '{p.title}'" if p.title else "")
        for key in ("x", "y"):
            v = getattr(p, key)
            if v and p.type != "timeseries" and v not in kpi_names:
                err(where, f"{key}: unknown KPI '{v}'")
        if p.type == "timeseries":
            if p.event not in events:
                err(where, f"unknown event '{p.event}'")
            for s in p.signals:
                if s not in names:
                    err(where, f"unknown signal '{s}'")

    # ---------------- unused signals (warning only)
    used: set[str] = set()
    for _, c in derived:
        used |= c.deps
    for ce in events.values():
        for c in [ce.start, ce.end, *ce.preset_inputs] + [m.expr for m in ce.moments.values()]:
            if c is not None:
                used |= c.deps
    for k in kpis:
        if k.defn.form == "formula":
            continue
        for c in (k.value, k.of):
            if c is not None:
                used |= c.deps
    for p in profile.plots:
        used |= set(p.signals)
    for name in profile.signals:
        if name not in used:
            warn(f"signals.{name}", "defined but not used")
    if not profile.events:
        warn("events", "no events defined; nothing will be extracted")

    if any(i.severity == "error" for i in issues):
        raise ProfileError(issues)
    return CompiledProfile(
        profile=profile,
        signal_names=list(profile.signals),
        derived=derived,
        events=events,
        kpis=kpis,
        enums=enums,
        discrete=discrete,
        warnings=[i for i in issues if i.severity == "warning"],
    )
