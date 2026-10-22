"""A KPI written as one expression, signal first, e.g.

    gap_dist.change(braking).abs()      change(gap_dist, braking).abs()      abs(change(gap_dist, braking))
    egoSpeed @ veh_stopped              (egoSpeed @ veh_stopped).to_kph()    duration(braking)

A reducer takes the signal expression and one range of the event (plus options as keywords:
max(x, braking, k=2)); `x @ moment` is x's value at a moment (x.moment() and moment(x) read the same). Point-wise operations (abs, sign,
sqrt, to_<unit>) can follow, each taking the result so far. Two more shapes:

    which(braking, PB=pb_state == 1, FB=fb_state == 1)     the labels of the conditions met: "PB + FB"
    gap_too_small or speed_at_release > 0                  a formula of KPIs listed earlier

The profile keeps its fields (value + at, reduce + of + over + args, then, which, formula):
parse_kpi_expr fills them, kpi_expr_text writes them back."""

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
    if k.get("formula") is not None:
        return str(k["formula"])
    if k.get("which") is not None:
        items = (k.get("which") or {}).items()
        if all(re.fullmatch(r"[A-Za-z_]\w*", str(label)) for label, _ in items):
            conds = [f"{label}={cond}" for label, cond in items]
        else:  # labels that are not names: a {label: condition} mapping
            conds = ["{" + ", ".join(f"{str(label)!r}: {cond}" for label, cond in items) + "}"]
        return f"which({', '.join([k.get('over') or 'event'] + conds)})"
    if k.get("value") is not None:
        if not str(k["value"]).strip():
            return ""
        core = f"{_wrap(k['value'])} @ {k.get('at') or 'start'}"
        if then:  # (x @ moment).abs(): the operation takes the value, not the moment
            core = f"({core})"
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


def parse_kpi_expr(text: str, moments: Iterable[str], ranges: Iterable[str],
                   kpis: Iterable[str] = ()) -> dict:
    """The profile fields of a KPI expression: {value, at}, {reduce, of, over[, args]}, {which, over} or
    {formula}, plus 'then' when operations follow. `kpis`: the earlier KPIs a formula may use.
    KpiExprError says what is wrong."""
    moments, ranges, kpis = list(moments), list(ranges), set(kpis)
    try:
        tree = ast.parse(text.strip(), mode="eval").body
    except SyntaxError as exc:
        raise KpiExprError(f"not a valid expression: {exc.msg}") from None
    which = _which(tree, ranges)
    if which is not None:
        return which
    found = _parse(tree, set(moments), ranges)
    if found is None:
        used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} - {
            n.func.id for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        if used and used <= kpis:  # only earlier KPIs: a formula of them
            return {"formula": text.strip()}
        raise KpiExprError(
            "write the KPI as <signal>.<reducer>(<range>) or <signal> @ <moment>, e.g. gap_dist.change(event).abs() "
            f"or egoSpeed @ {moments[-1] if moments else 'start'} (brackets around a longer signal expression: "
            "(a - b) @ start), or a formula of earlier KPIs"
            + (f" ({', '.join(sorted(kpis))})" if kpis else "")
            + f"; reducers: {', '.join(_reducers())}; moments: {', '.join(moments)}; ranges: {', '.join(ranges)}")
    fields, then = found
    if then:
        fields["then"] = then
    return fields


def _which(node, ranges: list):
    """which(range, Label=condition, ...) or which(range, {"label": condition, ...}), else None."""
    if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "which"):
        return None
    if not node.args:
        raise KpiExprError("which takes a range and labelled conditions: which(braking, PB=pb == 1, FB=fb == 1)")
    over = _range(node.args[0], "which", ranges)
    items = {kw.arg: ast.unparse(kw.value) for kw in node.keywords if kw.arg}
    for extra in node.args[1:]:
        if not isinstance(extra, ast.Dict) or not all(isinstance(k, ast.Constant) for k in extra.keys):
            raise KpiExprError("which: give the conditions as Label=condition, e.g. which(braking, PB=pb == 1)")
        items.update({str(k.value): ast.unparse(v) for k, v in zip(extra.keys, extra.values)})
    if not items:
        raise KpiExprError("which needs at least one labelled condition: which(braking, PB=pb == 1)")
    return {"which": items, "over": over}


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
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.MatMult):  # x @ moment
        if not (isinstance(node.right, ast.Name) and node.right.id in moments):
            raise KpiExprError(f"'{ast.unparse(node.right)}' is not a moment of this event; after @ use one of "
                               f"{', '.join(sorted(moments))}")
        return {"value": ast.unparse(node.left), "at": node.right.id}
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
            raise KpiExprError(f"a moment takes only the signal: x @ {name}")
        return {"value": ast.unparse(signal), "at": name}
    return None
