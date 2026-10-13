"""Safe expression language for conditions and values.

Expressions are parsed with :mod:`ast` and only a whitelist of node types is
accepted; they are compiled into closures over numpy arrays. Nothing is ever
passed to ``eval``/``exec``.

Supported: signal names, numbers, True/False, + - * / ** %, unary -, comparisons
(chained too), ``and``/``or``/``not``, ``x in (1, 2)``, enum member names when
compared with a signal that declares an ``enum``, and calls to toolbox signal or
condition tools, e.g. ``lowpass(longActAccel, 10)``.
"""

from __future__ import annotations

import ast
import difflib
import operator
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional

import numpy as np

from src.kpi_studio import toolbox
from src.kpi_studio.toolbox.registry import ToolContext, ToolError


class ExprError(ValueError):
    """Invalid expression; message names where it occurred."""


class MissingSignal(Exception):
    """A signal needed by an expression is not available in this file."""

    def __init__(self, name: str, reason: str = ""):
        self.name = name
        self.reason = reason
        super().__init__(f"missing signal '{name}'" + (f" ({reason})" if reason else ""))


@dataclass
class Env:
    """Evaluation environment for one recording."""

    time: np.ndarray
    values: dict[str, np.ndarray] = field(default_factory=dict)
    missing: dict[str, str] = field(default_factory=dict)  # name -> reason

    @property
    def ctx(self) -> ToolContext:
        return ToolContext(self.time)

    def get(self, name: str) -> np.ndarray:
        if name in self.values:
            return self.values[name]
        raise MissingSignal(name, self.missing.get(name, ""))


Fn = Callable[[Env], Any]


@dataclass(frozen=True)
class Compiled:
    source: str
    deps: frozenset
    fn: Fn
    is_bool: bool
    hold: bool  # sample with zero-order hold instead of interpolation

    def evaluate(self, env: Env) -> np.ndarray:
        out = self.fn(env)
        arr = np.asarray(out)
        if arr.ndim == 0:
            arr = np.full(env.time.shape, arr.item(), dtype=bool if arr.dtype == bool else float)
        elif arr.shape != env.time.shape:
            arr = np.broadcast_to(arr, env.time.shape)
        return arr


def as_bool(x) -> np.ndarray:
    arr = np.asarray(x)
    if arr.dtype == bool:
        return arr
    f = arr.astype(float)
    with np.errstate(invalid="ignore"):
        return np.where(np.isnan(f), False, f != 0)


def _num(x):
    arr = np.asarray(x)
    if arr.dtype == bool:
        return arr.astype(float)
    return arr.astype(float) if arr.dtype.kind in "iuf" else arr


_BINOPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Pow: operator.pow,
    ast.Mod: operator.mod,
}
_CMPOPS = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
}


def resolve_enum_member(token: str, table: Mapping[str, Any], signal: str) -> float:
    """Match `token` to an enum member: exact, case-insensitive, or unique '_'-suffix."""
    if token in table:
        return float(table[token])
    low = token.lower()
    exact = [k for k in table if k.lower() == low]
    if len(exact) == 1:
        return float(table[exact[0]])
    suffix = [k for k in table if k.lower().endswith("_" + low)]
    if len(suffix) == 1:
        return float(table[suffix[0]])
    options = ", ".join(sorted(table))
    if len(suffix) > 1:
        raise ExprError(f"'{token}' is ambiguous for signal '{signal}'; matches {sorted(suffix)}")
    raise ExprError(f"'{token}' is not a value of signal '{signal}'; options: {options}")


class _Compiler:
    def __init__(self, source: str, names: set[str], enums: Mapping[str, Mapping[str, Any]],
                 discrete: set[str]):
        self.source = source
        self.names = names
        self.enums = enums
        self.discrete = discrete
        self.deps: set[str] = set()

    # ---- helpers
    def fail(self, node: ast.AST, msg: str):
        col = getattr(node, "col_offset", None)
        where = f" (at column {col + 1})" if col is not None else ""
        raise ExprError(f"{msg}{where} in: {self.source}")

    def unknown_name(self, node: ast.Name):
        candidates = sorted(self.names) + [s.name for s in toolbox.tools("signal") + toolbox.tools("condition")]
        close = difflib.get_close_matches(node.id, candidates, n=3)
        hint = f"; did you mean {', '.join(close)}?" if close else ""
        self.fail(node, f"unknown signal '{node.id}'{hint}")

    # ---- entry
    def compile(self, node: ast.AST) -> tuple[Fn, bool, bool]:
        """Returns (fn, is_bool, hold)."""
        method = getattr(self, f"c_{type(node).__name__}", None)
        if method is None:
            self.fail(node, f"'{type(node).__name__}' is not allowed")
        return method(node)

    def c_Expression(self, node):
        return self.compile(node.body)

    def c_Constant(self, node):
        v = node.value
        if isinstance(v, bool):
            return (lambda env, v=v: np.bool_(v)), True, True
        if isinstance(v, (int, float)):
            return (lambda env, v=float(v): v), False, True
        self.fail(node, f"constant {v!r} is not a number; strings are only allowed as enum values or tool options")

    def c_Name(self, node):
        name = node.id
        if name in self.names:
            self.deps.add(name)
            return (lambda env, n=name: env.get(n)), False, name in self.discrete
        self.unknown_name(node)

    def c_UnaryOp(self, node):
        fn, is_bool, hold = self.compile(node.operand)
        if isinstance(node.op, ast.Not):
            return (lambda env: ~as_bool(fn(env))), True, True
        if isinstance(node.op, ast.USub):
            return (lambda env: -_num(fn(env))), False, hold
        if isinstance(node.op, ast.UAdd):
            return (lambda env: _num(fn(env))), False, hold
        self.fail(node, "unsupported unary operator")

    def c_BinOp(self, node):
        op = _BINOPS.get(type(node.op))
        if op is None:
            self.fail(node, "unsupported operator")
        lf, _, lh = self.compile(node.left)
        rf, _, rh = self.compile(node.right)

        def run(env):
            with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
                return op(_num(lf(env)), _num(rf(env)))

        return run, False, lh and rh

    def c_BoolOp(self, node):
        parts = [self.compile(v)[0] for v in node.values]
        combine = np.logical_and if isinstance(node.op, ast.And) else np.logical_or

        def run(env):
            out = as_bool(parts[0](env))
            for p in parts[1:]:
                out = combine(out, as_bool(p(env)))
            return out

        return run, True, True

    def _enum_table_for(self, node) -> tuple[Optional[str], Optional[Mapping]]:
        if isinstance(node, ast.Name) and node.id in self.enums:
            return node.id, self.enums[node.id]
        return None, None

    def _operand(self, node, enum_signal, enum_table) -> Fn:
        """Compile a comparison operand, allowing enum member names / strings."""
        if enum_table is not None:
            if isinstance(node, ast.Name) and node.id not in self.names:
                v = resolve_enum_member(node.id, enum_table, enum_signal)
                return lambda env, v=v: v
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                v = resolve_enum_member(node.value, enum_table, enum_signal)
                return lambda env, v=v: v
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            self.fail(node, f"text value {node.value!r} only works when compared with a signal that has an 'enum'")
        return self.compile(node)[0]

    def _members(self, node, enum_signal, enum_table) -> list[float]:
        if not isinstance(node, (ast.Tuple, ast.List, ast.Set)):
            self.fail(node, "'in' needs a list of values, e.g. x in (1, 2)")
        values = []
        for elt in node.elts:
            if isinstance(elt, ast.Constant) and isinstance(elt.value, (int, float)) and not isinstance(elt.value, bool):
                values.append(float(elt.value))
            elif isinstance(elt, ast.UnaryOp) and isinstance(elt.op, ast.USub) and isinstance(elt.operand, ast.Constant):
                values.append(-float(elt.operand.value))
            elif enum_table is not None and isinstance(elt, (ast.Name, ast.Constant)):
                token = elt.id if isinstance(elt, ast.Name) else str(elt.value)
                values.append(resolve_enum_member(token, enum_table, enum_signal))
            else:
                self.fail(elt, "'in' lists may only contain numbers or enum values")
        return values

    def c_Compare(self, node):
        operands = [node.left] + list(node.comparators)
        steps: list[Fn] = []
        for i, op in enumerate(node.ops):
            left, right = operands[i], operands[i + 1]
            sig, table = self._enum_table_for(left)
            if table is None:
                sig, table = self._enum_table_for(right)
            if isinstance(op, (ast.In, ast.NotIn)):
                lf = self._operand(left, sig, table)
                members = np.asarray(self._members(right, sig, table), dtype=float)
                negate = isinstance(op, ast.NotIn)

                def step(env, lf=lf, members=members, negate=negate):
                    res = np.isin(_num(lf(env)), members)
                    return ~res if negate else res
            else:
                cmp = _CMPOPS.get(type(op))
                if cmp is None:
                    self.fail(node, "unsupported comparison")
                lf = self._operand(left, sig, table)
                rf = self._operand(right, sig, table)

                def step(env, lf=lf, rf=rf, cmp=cmp):
                    with np.errstate(invalid="ignore"):
                        return np.asarray(cmp(_num(lf(env)), _num(rf(env))))
            steps.append(step)

        def run(env):
            out = as_bool(steps[0](env))
            for s in steps[1:]:
                out = np.logical_and(out, as_bool(s(env)))
            return out

        return run, True, True

    def _const_arg(self, node) -> Any:
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub) and isinstance(node.operand, ast.Constant) \
                and isinstance(node.operand.value, (int, float)):
            return -node.operand.value
        self.fail(node, "tool options must be plain numbers, text or True/False")

    def c_Call(self, node):
        if not isinstance(node.func, ast.Name):
            self.fail(node, "only toolbox functions can be called, e.g. lowpass(x, 10)")
        spec = toolbox.find_expression_tool(node.func.id)
        if spec is None:
            close = difflib.get_close_matches(
                node.func.id, [s.name for s in toolbox.tools("signal") + toolbox.tools("condition")], n=3)
            hint = f"; did you mean {', '.join(close)}?" if close else ""
            self.fail(node, f"unknown function '{node.func.id}'{hint}")
        n_in = len(node.args) if spec.inputs == -1 else spec.inputs
        if len(node.args) < n_in:
            self.fail(node, f"{spec.name} expects {n_in} signal input(s); signature is {spec.signature}")
        input_fns = [self.compile(a)[0] for a in node.args[:n_in]]
        try:
            spec.check_arity(len(input_fns))
            params = spec.bind_params(
                [self._const_arg(a) for a in node.args[n_in:]],
                {kw.arg: self._const_arg(kw.value) for kw in node.keywords if kw.arg},
            )
        except ToolError as exc:
            self.fail(node, str(exc))
        if any(kw.arg is None for kw in node.keywords):
            self.fail(node, "**kwargs is not allowed")

        def run(env, spec=spec, input_fns=input_fns, params=params):
            ctx = env.ctx
            inputs = [np.broadcast_to(np.asarray(f(env)), env.time.shape) for f in input_fns]
            return spec.func(ctx, *inputs, **params)

        return run, spec.kind == "condition", spec.kind == "condition"


def compile_expr(
    source: str,
    *,
    names: set[str],
    enums: Optional[Mapping[str, Mapping[str, Any]]] = None,
    discrete: Optional[set[str]] = None,
) -> Compiled:
    """Compile `source`. `names` are the signal names it may use; `enums` maps a
    signal name to its {MEMBER: value} table; `discrete` lists held signals."""
    if source is None or not str(source).strip():
        raise ExprError("empty expression")
    text = str(source).strip()
    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError as exc:
        raise ExprError(f"syntax error at column {exc.offset}: {exc.msg} in: {text}") from None
    comp = _Compiler(text, set(names), enums or {}, set(discrete or ()))
    fn, is_bool, hold = comp.compile(tree)
    return Compiled(source=text, deps=frozenset(comp.deps), fn=fn, is_bool=is_bool, hold=hold)
