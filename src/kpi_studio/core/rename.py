"""Rename a signal everywhere it is used in a profile (derived signals, events, moments, KPIs, plots).

Works on the raw profile dict (as edited by the GUI). Only whole names in expressions are
replaced; quoted text, attribute access and keyword-argument names are left alone, and so are
fields that name other things (event, moment and KPI names, units, descriptions).
"""

from __future__ import annotations

import io
import tokenize

# fields whose values are not signal expressions
_SKIP_KEYS = {"name", "use", "as", "event", "description", "unit", "title", "at", "duration", "between",
              "after", "before", "offset", "reduce", "x", "y", "type", "color_by", "group_by", "enum",
              "channels", "imports", "label", "merge",
              "over", "ranges"}


def rename_in_expression(text: str, old: str, new: str) -> str:
    """`text` with every reference to the name `old` replaced by `new`."""
    if old not in text:
        return text
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(text).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return text
    lines = text.splitlines(keepends=True) or [text]
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    spans = []
    for i, tok in enumerate(tokens):
        if tok.type != tokenize.NAME or tok.string != old:
            continue
        prev = tokens[i - 1].string if i else ""
        nxt = next((t for t in tokens[i + 1:] if t.type not in (tokenize.NL, tokenize.NEWLINE)), None)
        if prev == ".":
            continue
        if nxt is not None and nxt.string == "=" and prev in ("(", ","):
            continue  # keyword argument name
        (r, c), (_, c2) = tok.start, tok.end
        spans.append((offsets[r - 1] + c, offsets[r - 1] + c2))
    for a, b in reversed(spans):
        text = text[:a] + new + text[b:]
    return text


def _walk(value, old, new, counter):
    if isinstance(value, str):
        out = rename_in_expression(value, old, new)
        if out != value:
            counter[0] += 1
        return out
    if isinstance(value, list):
        return [_walk(v, old, new, counter) for v in value]
    if isinstance(value, dict):
        return {k: (v if k in _SKIP_KEYS else _walk(v, old, new, counter)) for k, v in value.items()}
    return value


def rename_signal_references(data: dict, old: str, new: str) -> int:
    """Rewrite references to signal `old` in `data` (in place); returns how many fields changed.

    Signal / derived definitions themselves (the dict keys) are renamed by the caller."""
    counter = [0]
    derived = data.get("derived") or {}
    for k, v in list(derived.items()):
        if isinstance(v, dict):
            if "expr" in v:
                v["expr"] = _walk(v["expr"], old, new, counter)
        else:
            derived[k] = _walk(v, old, new, counter)
    events = data.get("events") or {}
    for k, ev in list(events.items()):
        events[k] = _walk(ev, old, new, counter)
    data_kpis = data.get("kpis") or []
    for i, k in enumerate(data_kpis):
        data_kpis[i] = _walk(k, old, new, counter)
    for p in data.get("plots") or []:
        if old in (p.get("signals") or []):
            p["signals"] = [new if s == old else s for s in p["signals"]]
            counter[0] += 1
    return counter[0]


def rename_moment(data: dict, event: str, old: str, new: str) -> int:
    """Point the other moments of `event` and its KPIs at moment `new` instead of `old`."""
    def swap(v):
        if v == old:
            counter[0] += 1
            return new
        return v

    counter = [0]
    ev = (data.get("events") or {}).get(event) or {}
    for m in (ev.get("moments") or {}).values():
        if not isinstance(m, dict):
            continue
        for key in ("after", "before", "offset"):
            if key in m:
                m[key] = swap(m[key])
        if isinstance(m.get("between"), list):
            m["between"] = [swap(v) for v in m["between"]]
    ranges = ev.get("ranges") or {}
    for rname, pair in list(ranges.items()):
        if isinstance(pair, (list, tuple)):
            ranges[rname] = [swap(v) for v in pair]
    for k in data.get("kpis") or []:
        if k.get("event") != event:
            continue
        if "at" in k:
            k["at"] = swap(k["at"])
        for key in ("duration", "between"):
            if isinstance(k.get(key), (list, tuple)):
                k[key] = [swap(v) for v in k[key]]
    return counter[0]


def rename_kpi(data: dict, event: str, old: str, new: str) -> int:
    """Point formulas of the same event and the plots at KPI `new` instead of `old`."""
    counter = [0]
    for k in data.get("kpis") or []:
        if k.get("event") == event and isinstance(k.get("formula"), str):
            out = rename_in_expression(k["formula"], old, new)
            if out != k["formula"]:
                k["formula"] = out
                counter[0] += 1
    for p in data.get("plots") or []:
        for key in ("x", "y", "color_by"):
            if p.get(key) == old:
                p[key] = new
                counter[0] += 1
    return counter[0]


def rename_range(data: dict, event: str, old: str, new: str) -> int:
    """Point the KPIs of `event` at range `new` instead of `old`."""
    n = 0
    for k in data.get("kpis") or []:
        if k.get("event") == event and k.get("over") == old:
            k["over"] = new
            n += 1
    return n


BUILTIN_RANGE_PAIRS = {("start", "end"): "event", ("window_start", "window_end"): "window"}


def use_named_ranges(data: dict) -> int:
    """Turn KPIs over two moments ('between: [a, b]', 'duration: [a, b]') into KPIs over a named range of
    their event ('over: <range>'; a duration becomes 'reduce: duration'), adding the ranges they need.
    Reduce and which KPIs without a range get the range they used by default. Returns KPIs changed."""
    events = data.get("events") or {}
    changed = 0
    for k in data.get("kpis") or []:
        if not isinstance(k, dict) or k.get("use") or k.get("over") is not None:
            continue
        ev = events.get(k.get("event"))
        if not isinstance(ev, dict):
            continue
        if isinstance(k.get("duration"), (list, tuple)) and len(k["duration"]) == 2:
            pair = tuple(str(m) for m in k.pop("duration"))
            k.pop("value", None)
            k["reduce"] = "duration"
        elif k.get("reduce") is not None or k.get("which") is not None:
            b = k.pop("between", None)
            if isinstance(b, (list, tuple)) and len(b) == 2:
                pair = tuple(str(m) for m in b)
            else:
                pair = ("window_start", "window_end") if k.get("reduce") is not None else ("start", "end")
        else:
            continue
        k["over"] = _range_for(ev, pair)
        changed += 1
    return changed


def _range_for(ev: dict, pair: tuple) -> str:
    """The name of the event's range from pair[0] to pair[1], added when there is none."""
    if pair in BUILTIN_RANGE_PAIRS:
        return BUILTIN_RANGE_PAIRS[pair]
    ranges = ev.get("ranges")
    if not isinstance(ranges, dict):
        ranges = ev["ranges"] = {}
    for name, p in ranges.items():
        if isinstance(p, (list, tuple)) and tuple(p) == pair:
            return name
    base = f"{pair[0]}_to_{pair[1]}"
    name, i = base, 2
    while name in ranges or name in ("event", "window"):
        name, i = f"{base}{i}", i + 1
    ranges[name] = list(pair)
    return name
