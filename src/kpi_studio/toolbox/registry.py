"""Toolbox registry.

Every reusable building block (signal transform, condition, moment finder,
reducer, event preset) is a plain numpy function registered here with the
``@tool`` decorator. The expression compiler, the profile validator and the
GUI tool palette all read this registry, so registering a function is the only
step needed to make it usable everywhere.

Calling conventions per kind
----------------------------
signal / condition : func(ctx, *inputs, **params) -> ndarray (len == len(ctx.time))
moment             : func(ctx, x, **params) -> float time | None
                     (ctx.time and x are already restricted to the search range)
reducer            : func(ctx, x, **params) -> float
event              : func(ctx, x, **params) -> list[(t_start, t_end)]
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import numpy as np

KINDS = ("signal", "condition", "moment", "reducer", "event")

_REQUIRED = object()


class ToolError(ValueError):
    """Raised for invalid tool usage (unknown tool, bad parameter)."""


@dataclass(frozen=True)
class ToolContext:
    """Timebase information handed to every tool call."""

    time: np.ndarray

    @property
    def dt(self) -> float:
        if self.time.size < 2:
            return float("nan")
        return float(np.median(np.diff(self.time)))

    def seconds_to_samples(self, seconds: float) -> int:
        dt = self.dt
        if not np.isfinite(dt) or dt <= 0:
            return 0
        return max(int(round(float(seconds) / dt)), 0)


@dataclass(frozen=True)
class Param:
    name: str
    type: type = float
    default: Any = _REQUIRED
    min: Optional[float] = None
    max: Optional[float] = None
    choices: Optional[tuple] = None
    doc: str = ""
    example: Any = None  # used by contract tests and GUI forms when no default

    @property
    def required(self) -> bool:
        return self.default is _REQUIRED

    def coerce(self, value: Any, tool_name: str) -> Any:
        try:
            if self.type is bool:
                if isinstance(value, str):
                    value = value.strip().lower() in {"1", "true", "yes", "on"}
                else:
                    value = bool(value)
            elif self.type is int:
                if isinstance(value, float) and not value.is_integer():
                    raise ValueError("expected an integer")
                value = int(value)
            elif self.type is float:
                value = float(value)
            elif self.type is str:
                value = str(value)
        except (TypeError, ValueError) as exc:
            raise ToolError(
                f"{tool_name}: parameter '{self.name}' expects {self.type.__name__}, got {value!r}"
            ) from exc
        if self.choices is not None and value not in self.choices:
            raise ToolError(
                f"{tool_name}: parameter '{self.name}' must be one of {list(self.choices)}, got {value!r}"
            )
        if self.min is not None and value < self.min:
            raise ToolError(f"{tool_name}: parameter '{self.name}' must be >= {self.min}, got {value}")
        if self.max is not None and value > self.max:
            raise ToolError(f"{tool_name}: parameter '{self.name}' must be <= {self.max}, got {value}")
        return value

    def example_value(self) -> Any:
        if not self.required:
            return self.default
        if self.example is not None:
            return self.example
        if self.choices:
            return self.choices[0]
        return {float: 1.0, int: 1, bool: False, str: ""}.get(self.type, 1.0)


@dataclass(frozen=True)
class ToolSpec:
    name: str
    kind: str
    category: str
    func: Callable
    inputs: int  # number of array inputs; -1 = variadic (>= 1)
    params: tuple = ()
    doc: str = ""
    unit: str = ""  # free text: "same", "s", "m", ...
    origin: str = "builtin"
    input_names: tuple = field(default=())
    input_unit: str = ""  # the unit the input is converted to before the call, e.g. "m/s" (reducers)
    # not offered in the toolbox (domain-specific helpers kept so older profiles still run)
    hidden: bool = False

    @property
    def summary(self) -> str:
        return self.doc.strip().splitlines()[0] if self.doc.strip() else ""

    @property
    def signature(self) -> str:
        if self.inputs == -1:
            args = ["x1", "x2", "..."]
        else:
            args = list(self.input_names or [f"x{i + 1}" if self.inputs > 1 else "x" for i in range(self.inputs)])
        for p in self.params:
            args.append(p.name if p.required else f"{p.name}={p.default!r}")
        return f"{self.name}({', '.join(args)})"

    def bind_params(self, positional: list, keywords: dict) -> dict:
        """Map extra positional + keyword arguments onto declared params, validated."""
        if len(positional) > len(self.params):
            raise ToolError(
                f"{self.name}: too many arguments; signature is {self.signature}"
            )
        bound: dict = {}
        for p, value in zip(self.params, positional):
            bound[p.name] = value
        known = {p.name for p in self.params}
        for key, value in keywords.items():
            if key not in known:
                raise ToolError(
                    f"{self.name}: unknown parameter '{key}'; signature is {self.signature}"
                )
            if key in bound:
                raise ToolError(f"{self.name}: parameter '{key}' given twice")
            bound[key] = value
        out = {}
        for p in self.params:
            if p.name in bound:
                out[p.name] = p.coerce(bound[p.name], self.name)
            elif p.required:
                raise ToolError(f"{self.name}: missing parameter '{p.name}'; signature is {self.signature}")
            else:
                out[p.name] = p.default
        return out

    def check_arity(self, n_inputs: int) -> None:
        if self.inputs == -1:
            if n_inputs < 1:
                raise ToolError(f"{self.name}: needs at least one input")
        elif n_inputs != self.inputs:
            raise ToolError(
                f"{self.name}: expects {self.inputs} signal input(s), got {n_inputs}; signature is {self.signature}"
            )


_REGISTRY: dict[str, dict[str, ToolSpec]] = {k: {} for k in KINDS}


def tool(
    kind: str,
    category: str,
    *,
    inputs: int = 1,
    params: tuple = (),
    name: Optional[str] = None,
    unit: str = "",
    input_names: tuple = (),
    origin: str = "builtin",
    input_unit: str = "",
    hidden: bool = False,
):
    """Register a toolbox function."""
    if kind not in KINDS:
        raise ValueError(f"unknown tool kind {kind!r}")

    def deco(func: Callable) -> Callable:
        tool_name = name or func.__name__.rstrip("_")
        spec = ToolSpec(
            name=tool_name,
            kind=kind,
            category=category,
            func=func,
            inputs=inputs,
            params=tuple(params),
            doc=inspect.getdoc(func) or "",
            unit=unit,
            origin=origin,
            input_names=tuple(input_names),
            input_unit=input_unit,
            hidden=hidden,
        )
        register(spec)
        return func

    return deco


def register(spec: ToolSpec, *, replace: bool = False) -> None:
    table = _REGISTRY[spec.kind]
    if spec.name in table and not replace:
        raise ValueError(f"{spec.kind} tool '{spec.name}' is already registered")
    # signal and condition tools share one namespace inside expressions
    if spec.kind in ("signal", "condition"):
        other = "condition" if spec.kind == "signal" else "signal"
        if spec.name in _REGISTRY[other] and not replace:
            raise ValueError(f"expression tool '{spec.name}' is already registered as {other}")
    table[spec.name] = spec


def get(kind: str, name: str) -> ToolSpec:
    try:
        return _REGISTRY[kind][name]
    except KeyError:
        raise ToolError(f"unknown {kind} tool '{name}'") from None


def find_expression_tool(name: str) -> Optional[ToolSpec]:
    return _REGISTRY["signal"].get(name) or _REGISTRY["condition"].get(name)


def tools(kind: Optional[str] = None, include_hidden: bool = False) -> list[ToolSpec]:
    """The tools offered (of one kind); `include_hidden` adds the ones kept only for older profiles."""
    kinds = [kind] if kind else list(KINDS)
    out = []
    for k in kinds:
        out.extend(sorted((s for s in _REGISTRY[k].values() if include_hidden or not s.hidden),
                          key=lambda s: (s.category, s.name)))
    return out


def names(kind: str, include_hidden: bool = False) -> list[str]:
    return sorted(n for n, s in _REGISTRY[kind].items() if include_hidden or not s.hidden)
