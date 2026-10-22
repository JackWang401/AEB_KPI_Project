"""A KPI written as one expression, signal first, e.g.

    gap_dist.change(braking).abs()      change(gap_dist, braking).abs()      abs(change(gap_dist, braking))
    egoSpeed.veh_stopped()              veh_stopped(egoSpeed)                duration(braking)

A reducer takes the signal expression and one range of the event (plus options as keywords:
max(x, braking, k=2)); a moment takes the signal expression only. Point-wise operations (abs, sign,
sqrt, to_<unit>) can follow, each taking the result so far. The profile keeps its fields (value + at,
reduce + of + over + args, then): parse_kpi_expr fills them, kpi_expr_text writes them back."""

from __future__ import annotations

import ast
import re
from typing import Iterable

from src.kpi_studio import toolbox

_SIMPLE = re.compile(r"[A-Za-z_]\w*(\.[A-Za-z_]\w*)*(\(.*\))?|-?[\d.]+")


class KpiExprError(ValueError):
    pass


def _ops() -> set[str]:
    return {s.name for s in toolbox.tools("signal") if s.pointwise}


def _reducers() -> list[str]:
    return toolbox.names("reducer")


def _wrap(expr: str) -> str:
    """`expr` ready to take .method(): a name or call stays, anything else gets brackets."""
    expr = expr.strip()
    if _SIMPLE.fullmatch(expr):
        try:
            if isinstance(ast.parse(expr, mode="eval").body, (ast.Name, ast.Call, ast.Attribute, ast.Constant)):
                return expr
        except SyntaxError:
            pass
    return f"({expr})"


def kpi_expr_text(k: dict) -> str:
    """The KPI's expression from its profile fields ("" when it has none of them yet)."""
    then = (["abs"] if k.get("absolute") else []) + list(k.get("then") or [])
    if k.get("value") is not None:
        if not str(k["value"]).strip():
            return ""
        core = f"{_wrap(k['value'])}.{k.get('at') or 'start'}()"
    elif k.get("reduce") == "duration":
        core = f"duration({k.get('over') or 'event'})"
    elif k.get("reduce"):
        if not str(k.get("of") or "").strip():
            return ""
        args = [k.get("over") or "event"] + [f"{a}={v!r}" for a, v in (k.get("args") or {}).items()]
        core = f"{_wrap(k['of'])}.{k['reduce']}({', '.join(args)})"
    else:
        return ""
    return core + "".join(f".{op}()" for op in then)


def parse_kpi_expr(text: str, moments: Iterable[str], ranges: Iterable[str]) -> dict:
    """The profile fields of a KPI expression: {value, at} or {reduce, of, over[, args]}, plus 'then'
    when operations follow. KpiExprError says what is wrong."""
    moments, ranges = list(moments), list(ranges)
    try:
        tree = ast.parse(text.strip(), mode="eval").body
    except SyntaxError as exc:
        raise KpiExprError(f"not a valid expression: {exc.msg}") from None
    found = _parse(tree, set(moments), ranges)
    if found is None:
        raise KpiExprError(
            "write the KPI as <signal>.<reducer>(<range>) or <signal>.<moment>(), e.g. gap_dist.change(event).abs() "
            f"or egoSpeed.{moments[-1] if moments else 'start'}(); reducers: {', '.join(_reducers())}; "
            f"moments: {', '.join(moments)}; ranges: {', '.join(ranges)}")
    fields, then = found
    if then:
        fields["then"] = then
    return fields


def _parse(node, moments: set, ranges: list):
    core = _core(node, moments, ranges)
    if core is not None:
        return core, []
    if isinstance(node, ast.Call) and not node.keywords:
        f = node.func
        if isinstance(f, ast.Name) and f.id in _ops() and len(node.args) == 1:
            inner = _parse(node.args[0], moments, ranges)
            if inner is not None:
                return inner[0], inner[1] + [f.id]
        if isinstance(f, ast.Attribute) and f.attr in _ops() and not node.args:
            inner = _parse(f.value, moments, ranges)
            if inner is not None:
                return inner[0], inner[1] + [f.attr]
    return None


def _range(node, reducer: str, ranges: list) -> str:
    if not isinstance(node, ast.Name):
        raise KpiExprError(f"{reducer}: the range must be the name of one of the event's ranges "
                           f"({', '.join(ranges)}), not '{ast.unparse(node)}'")
    if node.id not in ranges:
        raise KpiExprError(f"{reducer}: unknown range '{node.id}'; ranges of this event: {', '.join(ranges)}")
    return node.id


def _options(node, reducer: str) -> dict:
    out = {}
    for kw in node.keywords:
        try:
            out[kw.arg] = ast.literal_eval(kw.value)
        except ValueError:
            raise KpiExprError(f"{reducer}: option {kw.arg} must be a number or text") from None
    return out


def _core(node, moments: set, ranges: list):
    """The reducer or moment the expression ends in, or None."""
    if not isinstance(node, ast.Call):
        return None
    f, args = node.func, node.args
    if isinstance(f, ast.Name):
        name, signal = f.id, (args[0] if args else None)
        rest = args[1:]
    elif isinstance(f, ast.Attribute):
        name, signal, rest = f.attr, f.value, args
    else:
        return None
    if name == "duration" and isinstance(f, ast.Name):
        if len(args) != 1 or node.keywords:
            raise KpiExprError("duration takes one range: duration(braking)")
        return {"reduce": "duration", "over": _range(args[0], "duration", ranges)}
    if name in _reducers():
        if signal is None or len(rest) != 1:
            raise KpiExprError(f"{name} takes a signal and a range: x.{name}(event) or {name}(x, event)")
        fields = {"reduce": name, "of": ast.unparse(signal), "over": _range(rest[0], name, ranges)}
        options = _options(node, name)
        if options:
            fields["args"] = options
        return fields
    if name in moments:
        if signal is None or rest or node.keywords:
            raise KpiExprError(f"a moment takes only the signal: x.{name}() or {name}(x)")
        return {"value": ast.unparse(signal), "at": name}
    return None
