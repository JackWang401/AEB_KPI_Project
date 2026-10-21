"""Profile schema (pydantic). Strict: unknown keys are errors so typos are caught."""

from __future__ import annotations

import re
from typing import Any, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SCHEMA_VERSION = 1

_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_QNAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)?$")

BUILTIN_MOMENTS = ("start", "end", "window_start", "window_end")
# named ranges every event has: the event itself and its window
BUILTIN_RANGES = {"event": ("start", "end"), "window": ("window_start", "window_end")}
TIME = "time"  # built-in signal: the timebase in seconds
MOMENT_RANGE_KEYS = ("after", "before", "within", "between")


def _check_name(value: str, what: str, qualified: bool = False) -> str:
    pattern = _QNAME_RE if qualified else _NAME_RE
    if not isinstance(value, str) or not pattern.match(value):
        raise ValueError(
            f"invalid {what} name {value!r}: use letters, digits and _ (not starting with a digit)"
        )
    return value


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class SignalDef(_Strict):
    channels: list[str] = Field(min_length=1, description="candidate MF4 channel names, first match wins")
    unit: Optional[str] = None
    # boolean, enum and discrete signals are held between samples; continuous ones are interpolated
    kind: Literal["continuous", "discrete", "boolean", "enum"] = "continuous"
    # an enum table name (enum tables file), or the values themselves: {text: raw value}
    enum: Optional[Union[str, dict[str, float]]] = None
    required: bool = True
    description: str = ""

    @property
    def held(self) -> bool:
        """Held between samples (zero-order hold) rather than linearly interpolated."""
        return self.kind != "continuous"

    @field_validator("channels", mode="before")
    @classmethod
    def _one_or_many(cls, v):
        if isinstance(v, str):
            return [v]
        return v


class DerivedDef(_Strict):
    """A derived signal. In YAML either `name: <expression>` or `name: {expr, unit, description}`."""
    expr: str = ""
    unit: Optional[str] = None
    description: str = ""
    # None: from the expression (a condition is boolean, a held signal's value discrete, else continuous)
    kind: Optional[Literal["continuous", "discrete", "boolean"]] = None


def derived_to_yaml(d: "DerivedDef | str | dict"):
    """The compact YAML form: a plain expression string unless a unit, description or kind is set."""
    if isinstance(d, str):
        return d
    if isinstance(d, dict):
        d = DerivedDef.model_validate({k: d[k] for k in ("expr", "unit", "description", "kind") if d.get(k)})
    if not d.unit and not d.description and not d.kind:
        return d.expr
    out = {"expr": d.expr}
    if d.unit:
        out["unit"] = d.unit
    if d.description:
        out["description"] = d.description
    if d.kind:
        out["kind"] = d.kind
    return out


def signal_units(profile) -> dict[str, str]:
    """Unit of every signal and derived signal of a profile ('' when none is given)."""
    units = {n: (d.unit or "") for n, d in profile.signals.items()}
    units.update({n: (d.unit or "") for n, d in profile.derived.items()})
    return units


class Timebase(_Strict):
    rate_hz: float = Field(100.0, gt=0, le=10000)


class Window(_Strict):
    pre: float = Field(0.0, ge=0)
    post: float = Field(0.0, ge=0)


class PresetRef(BaseModel):
    """`preset: {name: aeb_request, of: aebRequest, post_time: 3}`; extra keys are tool params."""

    model_config = ConfigDict(extra="allow")

    name: str
    of: Union[str, list[str]]

    @property
    def inputs(self) -> list[str]:
        return [self.of] if isinstance(self.of, str) else list(self.of)

    @property
    def params(self) -> dict[str, Any]:
        return dict(self.model_extra or {})


class MomentRule(BaseModel):
    """Parsed moment definition.

    YAML forms:
      brake_onset: { first: "longActAccelFlt < -2", after: start, within: 1.5 }
      onset:       { decel_onset: "jerk(longActAccelFlt)", threshold: -20, after: start }
      early:       { offset: start, by: -0.5 }
    """

    model_config = ConfigDict(extra="forbid")

    tool: Optional[str] = None       # moment tool name (None for offset)
    expr: Optional[str] = None       # expression given to the tool
    params: dict[str, Any] = Field(default_factory=dict)
    after: Optional[str] = None
    before: Optional[str] = None
    within: Optional[float] = Field(None, gt=0)
    between: Optional[tuple[str, str]] = None
    offset: Optional[str] = None
    by: float = 0.0

    def references(self) -> list[str]:
        refs = [r for r in (self.after, self.before, self.offset) if r]
        if self.between:
            refs.extend(self.between)
        return refs


def parse_moment(raw: Any, tool_names: set[str]) -> MomentRule:
    if isinstance(raw, MomentRule):
        return raw
    if not isinstance(raw, dict):
        raise ValueError("a moment must be a mapping, e.g. { first: 'egoSpeed < 0.1', after: start }")
    raw = dict(raw)
    if "offset" in raw:
        allowed = {"offset", "by"}
        extra = set(raw) - allowed
        if extra:
            raise ValueError(f"offset moments only take 'offset' and 'by', got {sorted(extra)}")
        return MomentRule(offset=str(raw["offset"]), by=float(raw.get("by", 0.0)))
    range_args = {k: raw.pop(k) for k in MOMENT_RANGE_KEYS if k in raw}
    tool_keys = [k for k in raw if k in tool_names]
    if len(tool_keys) != 1:
        hint = ", ".join(sorted(tool_names))
        if not tool_keys:
            raise ValueError(f"no moment rule given; use one of: {hint}, or offset")
        raise ValueError(f"only one moment rule per moment, got {tool_keys}")
    tool = tool_keys[0]
    expr = raw.pop(tool)
    if "between" in range_args:
        b = range_args["between"]
        if not (isinstance(b, (list, tuple)) and len(b) == 2):
            raise ValueError("'between' needs two moment names, e.g. between: [start, end]")
        range_args["between"] = (str(b[0]), str(b[1]))
    return MomentRule(tool=tool, expr=str(expr), params=raw, **range_args)


class EventDef(_Strict):
    description: str = ""
    start: Optional[str] = None
    end: Optional[str] = None
    # which time each window starts / ends at when the condition is met more than once:
    # start first = when it first becomes true, last = the last time it becomes true before the end;
    # end first = the first time it is met after the start, last = the last time before the next start
    start_rule: Literal["first", "last"] = "first"
    end_rule: Literal["first", "last"] = "first"
    preset: Optional[PresetRef] = None
    min_duration: float = Field(0.0, ge=0)
    merge_gap: float = Field(0.0, ge=0)
    window: Window = Field(default_factory=Window)
    moments: dict[str, Any] = Field(default_factory=dict)
    # named ranges: {name: [from moment, to moment]}; KPIs reduce over them with 'over: <name>'
    ranges: dict[str, tuple[str, str]] = Field(default_factory=dict)

    @field_validator("ranges", mode="before")
    @classmethod
    def _range_pairs(cls, v):
        if not v:
            return {}
        if not isinstance(v, dict):
            raise ValueError("'ranges' maps a name to [from moment, to moment]")
        out = {}
        for name, pair in v.items():
            if not isinstance(pair, (list, tuple)) or len(pair) != 2:
                raise ValueError(f"range '{name}' needs two moment names, e.g. {name}: [start, end]")
            out[str(name)] = (str(pair[0]), str(pair[1]))
        return out

    def range_names(self) -> list[str]:
        return list(BUILTIN_RANGES) + list(self.ranges)

    def range_pair(self, name: str) -> Optional[tuple[str, str]]:
        return self.ranges.get(name) or BUILTIN_RANGES.get(name)

    @model_validator(mode="after")
    def _start_or_preset(self):
        if not self.start and not self.preset:
            raise ValueError("no trigger yet: fill in 'Start when' (a condition) or choose a preset")
        if self.start and self.preset:
            raise ValueError("an event needs exactly one of 'start' (a condition) or 'preset'")
        if self.preset and self.end:
            raise ValueError("'end' cannot be combined with 'preset'; the preset decides the end")
        if self.preset and "last" in (self.start_rule, self.end_rule):
            raise ValueError("'start_rule' / 'end_rule' cannot be combined with 'preset'")
        if self.end_rule == "last" and not self.end:
            raise ValueError("end_rule 'last' needs an 'End when' condition")
        for name in self.moments:
            _check_name(name, "moment")
            if name in BUILTIN_MOMENTS:
                raise ValueError(f"moment name '{name}' is built in and cannot be redefined")
        for name in self.ranges:
            _check_name(name, "range")
            if name in BUILTIN_RANGES:
                raise ValueError(f"range name '{name}' is built in and cannot be redefined")
        return self


class KpiDef(_Strict):
    name: Optional[str] = None
    use: Optional[str] = None        # "<library>.<kpi>" or "<library>.*"
    as_: Optional[str] = Field(None, alias="as")
    event: Optional[str] = None
    description: str = ""
    unit: Optional[str] = None
    # value form
    value: Optional[str] = None
    at: Optional[str] = None
    # duration form
    duration: Optional[tuple[str, str]] = None
    # reduce form
    reduce: Optional[str] = None
    of: Optional[str] = None
    between: Optional[tuple[str, str]] = None
    over: Optional[str] = None  # a named range of the event (instead of 'between')
    args: dict[str, Any] = Field(default_factory=dict)
    # formula form: expression over KPIs listed earlier for the same event
    formula: Optional[str] = None
    # which form: {label: condition}; the result names the conditions met anywhere in the range,
    # e.g. "PB + FB" (labels joined in this order), or "none"
    which: Optional[dict[str, str]] = None
    # a yes / no KPI: its occurrences' rows are highlighted in the Excel summary where it is true
    highlight: bool = False

    @model_validator(mode="after")
    def _one_form(self):
        if self.use:
            others = [k for k in ("name", "event", "value", "at", "duration", "reduce", "of", "between", "formula",
                                  "which", "over")
                      if getattr(self, k) is not None]
            if others:
                raise ValueError(f"'use' entries only accept 'as'; remove {others}")
            return self
        if self.as_:
            raise ValueError("'as' is only valid together with 'use'")
        if not self.name:
            raise ValueError("a KPI needs a 'name'")
        _check_name(self.name, "KPI")
        if not self.event:
            raise ValueError(f"KPI '{self.name}' needs an 'event'")
        forms = [f for f, present in (
            ("value", self.value is not None),
            ("duration", self.duration is not None),
            ("reduce", self.reduce is not None),
            ("formula", self.formula is not None),
            ("which", self.which is not None),
        ) if present]
        if len(forms) != 1:
            raise ValueError(
                f"KPI '{self.name}' needs exactly one of 'value' (+ 'at'), 'duration', 'reduce' (+ 'of') "
                f"'formula' or 'which'"
            )
        form = forms[0]
        if form == "value" and not self.at:
            raise ValueError(f"KPI '{self.name}': 'value' needs 'at' (a moment name)")
        if form != "value" and self.at:
            raise ValueError(f"KPI '{self.name}': 'at' is only used with 'value'")
        if form == "reduce" and not self.of and self.reduce != "duration":
            raise ValueError(f"KPI '{self.name}': 'reduce' needs 'of' (an expression)")
        if self.between and self.over:
            raise ValueError(f"KPI '{self.name}': use 'over' (a range) or 'between' (two moments), not both")
        if form not in ("reduce", "which") and self.over:
            raise ValueError(f"KPI '{self.name}': 'over' is only used with 'reduce' and 'which'")
        if form != "reduce" and (self.of or self.args):
            raise ValueError(f"KPI '{self.name}': 'of' and 'args' are only used with 'reduce'")
        if form not in ("reduce", "which") and self.between:
            raise ValueError(f"KPI '{self.name}': 'between' is only used with 'reduce' and 'which'")
        if form == "which" and (not self.which or not all(str(k).strip() and str(v).strip()
                                                          for k, v in self.which.items())):
            raise ValueError(f"KPI '{self.name}': 'which' needs at least one label with a condition")
        return self

    @property
    def form(self) -> str:
        if self.value is not None:
            return "value"
        if self.duration is not None:
            return "duration"
        if self.formula is not None:
            return "formula"
        if self.which is not None:
            return "which"
        return "reduce"


class RefLine(_Strict):
    x: Optional[float] = None
    y: Optional[float] = None
    label: str = ""

    @model_validator(mode="after")
    def _one_axis(self):
        if (self.x is None) == (self.y is None):
            raise ValueError("a reference line needs exactly one of x or y")
        return self


class CommonAxis(_Strict):
    """Signals the signal viewer draws in one subplot, on one value axis (right-click > Common axis)."""
    name: str = Field(min_length=1)
    signals: list[str] = Field(default_factory=list)
    range: Optional[list[float]] = None  # [min, max] of the value axis; none: fitted to the signals

    @field_validator("range")
    @classmethod
    def _range(cls, v):
        if v is not None and (len(v) != 2 or not v[0] < v[1]):
            raise ValueError("range must be [min, max] with min < max")
        return v


class PlotDef(_Strict):
    type: Literal["scatter", "box", "bar", "histogram", "timeseries"]
    title: str = ""
    x: Optional[str] = None
    y: Optional[str] = None
    color_by: str = "file"  # "file", "event", "none", or a KPI name: points coloured by that KPI's value
    also_color_by: list[str] = Field(default_factory=list, max_length=4)  # scatter: one colour per combination
    group_by: Literal["file", "event"] = "file"
    event: Optional[str] = None
    signals: list[str] = Field(default_factory=list)
    show_window: bool = True
    show_moments: bool = True
    max_figures: int = Field(50, ge=1, le=1000)
    reference_lines: list[RefLine] = Field(default_factory=list)
    x_range: Optional[list[Optional[float]]] = None  # [min, max]; an empty end adapts to the data
    y_range: Optional[list[Optional[float]]] = None
    merge: str = ""  # scatter plots with the same merge id are drawn in one chart (titled by the first title set)

    @model_validator(mode="after")
    def _fields_for_type(self):
        for key in ("x_range", "y_range"):
            r = getattr(self, key)
            if r is not None and (len(r) != 2 or (None not in r and not r[0] < r[1])):
                raise ValueError(f"{key} must be [min, max] with min < max (leave one empty for automatic)")
        t = self.type
        if t == "scatter" and not (self.x and self.y):
            raise ValueError("scatter plots need 'x' and 'y' KPI names")
        if t in ("box", "bar") and not self.y:
            raise ValueError(f"{t} plots need 'y' (a KPI name)")
        if t == "histogram" and not self.x:
            raise ValueError("histogram plots need 'x' (a KPI name)")
        if t == "timeseries" and not (self.event and self.signals):
            raise ValueError("timeseries plots need 'event' and 'signals'")
        return self


class _Body(_Strict):
    description: str = ""
    imports: list[str] = Field(default_factory=list)
    signals: dict[str, SignalDef] = Field(default_factory=dict)
    derived: dict[str, DerivedDef] = Field(default_factory=dict)
    events: dict[str, EventDef] = Field(default_factory=dict)
    kpis: list[KpiDef] = Field(default_factory=list)

    @field_validator("signals", "derived")
    @classmethod
    def _signal_names(cls, v):
        for key in v:
            _check_name(key, "signal")
        return v

    @field_validator("derived", mode="before")
    @classmethod
    def _derived_shorthand(cls, v):
        if isinstance(v, dict):
            return {k: ({"expr": d} if isinstance(d, str) or d is None else d) for k, d in v.items()}
        return v

    @field_validator("events")
    @classmethod
    def _event_names(cls, v):
        for key in v:
            _check_name(key, "event", qualified=True)  # library events are "<library>.<event>"
        return v


class Library(_Body):
    schema_version: Literal[1] = 1
    name: str

    @field_validator("name")
    @classmethod
    def _lib_name(cls, v):
        return _check_name(v, "library")


class Profile(_Body):
    schema_version: Literal[1] = SCHEMA_VERSION
    name: str = Field(min_length=1)
    timebase: Timebase = Field(default_factory=Timebase)
    plots: list[PlotDef] = Field(default_factory=list)
    common_axes: list[CommonAxis] = Field(default_factory=list)  # signal viewer groups, kept with the profile
    shown_signals: list[str] = Field(default_factory=list)  # signals (measured and derived) ticked in the viewer
