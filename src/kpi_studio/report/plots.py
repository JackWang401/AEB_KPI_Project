"""Interactive Plotly HTML plots from PlotDef entries, plus a dashboard index."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from src.kpi_studio.core.model import PlotDef
from src.kpi_studio.core.results import Status, to_frame

# Validated categorical order (fixed, never cycled); 9th+ series fold to gray.
SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"]
OVERFLOW = "#8a8a85"
INK, INK_2, GRID, SURFACE = "#1f1f1e", "#5f5e5a", "#e6e5e0", "#ffffff"
WINDOW_FILL = "rgba(42,120,214,0.08)"
# one hue, light -> dark, for points coloured by a KPI's value
SEQUENTIAL = [[0.0, "#cfe0f6"], [0.5, "#2a78d6"], [1.0, "#0d3a73"]]
SYMBOLS = ["circle", "diamond", "square", "triangle-up", "x", "star"]  # one per plot in a merged chart
TEXT = "text:"  # wide-table column prefix holding a KPI's text result (which-KPIs)
BUILTIN_COLOR_BY = ("file", "event", "none")


def _color(i: int) -> str:
    return SERIES[i] if i < len(SERIES) else OVERFLOW


def _layout(fig: go.Figure, title: str, height: int = 520) -> go.Figure:
    fig.update_layout(
        title=dict(text=title, x=0.01, font=dict(size=16, color=INK)),
        paper_bgcolor=SURFACE, plot_bgcolor=SURFACE, height=height,
        font=dict(family="Inter, Helvetica Neue, Arial, sans-serif", size=12, color=INK_2),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0.01),
        hovermode="closest", margin=dict(l=60, r=24, t=80, b=50),
    )
    fig.update_xaxes(showgrid=True, gridcolor=GRID, zeroline=False, linecolor=GRID)
    fig.update_yaxes(showgrid=True, gridcolor=GRID, zeroline=False, linecolor=GRID)
    return fig


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", text).strip("_")[:80] or "plot"


def _label(kpi: str, units: dict) -> str:
    u = units.get(kpi)
    return f"{kpi} [{u}]" if u else kpi


def kpi_wide(outcomes: list) -> pd.DataFrame:
    """One row per occurrence with KPI values as columns (OK values only)."""
    res = to_frame([r for o in outcomes for r in o.results])
    if res.empty:
        return pd.DataFrame(columns=["file", "event", "occurrence"])
    res = res[res["status"] == Status.OK]
    if res.empty:
        return pd.DataFrame(columns=["file", "event", "occurrence"])
    keys = ["file", "event", "occurrence"]
    wide = res.pivot_table(index=keys, columns="kpi", values="value", aggfunc="first")
    texts = res[res["text"].fillna("") != ""] if "text" in res else res.iloc[0:0]
    if not texts.empty:  # text results (e.g. "PB + FB") next to their codes, for colouring by them
        tw = texts.pivot_table(index=keys, columns="kpi", values="text", aggfunc="first")
        tw.columns = [TEXT + c for c in tw.columns]
        wide = wide.join(tw)
    wide.columns.name = None
    return wide.reset_index()


def _short(path: str) -> str:
    return Path(path).name


def _ref_lines(fig, p: PlotDef):
    for r in p.reference_lines:
        if r.y is not None:
            fig.add_hline(y=r.y, line=dict(color=INK_2, dash="dash", width=1),
                          annotation_text=r.label, annotation_position="top left")
        else:
            fig.add_vline(x=r.x, line=dict(color=INK_2, dash="dash", width=1),
                          annotation_text=r.label, annotation_position="top right")


def _groups(df: pd.DataFrame, by: str, files: list[str]) -> list[tuple[str, pd.DataFrame]]:
    if by == "none" or df.empty:
        return [("all", df)]
    key = "file" if by == "file" else "event"
    order = files if key == "file" else sorted(df[key].unique())
    return [(k, df[df[key] == k]) for k in order if (df[key] == k).any()]


def kpi_figure(p: PlotDef, wide: pd.DataFrame, units: dict, files: list[str]) -> Optional[go.Figure]:
    title = p.title or {"scatter": f"{p.y} vs {p.x}", "box": f"{p.y} distribution",
                        "bar": f"{p.y} per occurrence", "histogram": f"{p.x} histogram"}[p.type]
    if p.type == "scatter":
        return scatter_figure([p], wide, units, files, title)
    fig = go.Figure()
    if p.type in ("box", "bar"):
        if p.y not in wide.columns:
            return None
        d = wide.dropna(subset=[p.y])
        for i, (name, g) in enumerate(_groups(d, p.group_by, files)):
            label = _short(name) if p.group_by == "file" else name
            if p.type == "box":
                fig.add_trace(go.Box(y=g[p.y], name=label, marker_color=_color(i), boxpoints="all",
                                     jitter=0.3, pointpos=0, line=dict(width=2)))
            else:
                fig.add_trace(go.Bar(x=[f"{label} #{o}" for o in g["occurrence"]], y=g[p.y], name=label,
                                     marker=dict(color=_color(i), cornerradius=4)))
        fig.update_yaxes(title_text=_label(p.y, units))
        if p.type == "bar":
            fig.update_layout(bargap=0.25)
    elif p.type == "histogram":
        if p.x not in wide.columns:
            return None
        d = wide.dropna(subset=[p.x])
        for i, (name, g) in enumerate(_groups(d, p.color_by, files)):
            fig.add_trace(go.Histogram(x=g[p.x], name=_short(name) if p.color_by == "file" else name,
                                       marker=dict(color=_color(i), line=dict(color=SURFACE, width=2)),
                                       opacity=0.85))
        fig.update_layout(barmode="overlay")
        fig.update_xaxes(title_text=_label(p.x, units))
        fig.update_yaxes(title_text="count")
    _ref_lines(fig, p)
    _axes(fig, p)
    return _layout(fig, title)


def _colour_groups(d: pd.DataFrame, p: PlotDef, files: list[str]) -> Optional[list[tuple[str, pd.DataFrame]]]:
    """Point groups for one colour each, or None when the points are coloured by a numeric KPI."""
    if p.color_by in BUILTIN_COLOR_BY:
        return [(_short(n) if p.color_by == "file" else n, g) for n, g in _groups(d, p.color_by, files)]
    col = TEXT + p.color_by
    if col in d.columns:  # a text KPI: one colour per text, "none" last
        vals = d[col].fillna("none")
        order = sorted(vals.unique(), key=lambda v: (v == "none", v))
        return [(v, d[vals == v]) for v in order]
    return None


def scatter_figure(plots: list, wide: pd.DataFrame, units: dict, files: list[str], title: str
                   ) -> Optional[go.Figure]:
    """One scatter chart of one or more plots sharing X. Merged plots are told apart by marker shape
    (and colour when they are not coloured otherwise); Y values of different units get a row each
    rather than a second Y axis."""
    plots = [p for p in plots if {p.x, p.y} <= set(wide.columns)]
    if not plots:
        return None
    merged = len(plots) > 1
    stacked = merged and len({units.get(p.y, "") for p in plots}) > 1
    fig = make_subplots(rows=len(plots), cols=1, shared_xaxes=True, vertical_spacing=0.06) if stacked \
        else go.Figure()
    colour_axes: dict[str, str] = {}  # numeric KPI -> plotly colour axis
    for j, p in enumerate(plots):
        d = wide.dropna(subset=[p.x, p.y])
        where = dict(row=j + 1, col=1) if stacked else {}
        symbol = SYMBOLS[j % len(SYMBOLS)] if merged else "circle"
        prefix = f"{p.y} · " if merged else ""
        hover = (f"{p.x}: %{{x:.3f}}<br>{p.y}: %{{y:.3f}}<br>%{{customdata[0]}} · "
                 "%{customdata[1]} #%{customdata[2]}")
        def info(g):
            return g[["file", "event", "occurrence"]].assign(file=g["file"].map(_short)).values

        groups = _colour_groups(d, p, files)
        if groups is not None:
            for i, (name, g) in enumerate(groups):
                colour = _color(j if p.color_by == "none" else i)
                label = p.y if (merged and p.color_by == "none") else prefix + name
                fig.add_trace(go.Scatter(
                    x=g[p.x], y=g[p.y], mode="markers", name=label,
                    marker=dict(size=9, color=colour, symbol=symbol, line=dict(color=SURFACE, width=2)),
                    customdata=info(g), hovertemplate=hover + "<extra></extra>"), **where)
            continue
        c = p.color_by  # a numeric KPI: a light-to-dark scale
        axis = colour_axes.setdefault(c, "coloraxis" + (str(len(colour_axes) + 1) if colour_axes else ""))
        has = d[c].notna() if c in d.columns else pd.Series(False, index=d.index)
        g = d[has]
        fig.add_trace(go.Scatter(
            x=g[p.x], y=g[p.y], mode="markers", name=p.y if merged else c, showlegend=merged,
            marker=dict(size=10, color=g[c], coloraxis=axis, symbol=symbol, line=dict(color=SURFACE, width=1)),
            customdata=info(g), hovertemplate=hover + f"<br>{c}: %{{marker.color:.3f}}<extra></extra>"), **where)
        rest = d[~has]
        if not rest.empty:
            fig.add_trace(go.Scatter(
                x=rest[p.x], y=rest[p.y], mode="markers", name=prefix + f"no {c}",
                marker=dict(size=9, color=OVERFLOW, symbol=symbol, line=dict(color=SURFACE, width=2)),
                customdata=info(rest), hovertemplate=hover + "<extra></extra>"), **where)
    for k, (c, axis) in enumerate(colour_axes.items()):
        fig.update_layout({axis: dict(colorscale=SEQUENTIAL, colorbar=dict(
            title=dict(text=_label(c, units), side="right"), x=1.02 + 0.14 * k, thickness=14))})
    first = plots[0]
    if stacked:
        for j, p in enumerate(plots, start=1):
            fig.update_yaxes(title_text=_label(p.y, units), row=j, col=1)
            if p.y_range and None not in p.y_range:
                fig.update_yaxes(range=p.y_range, row=j, col=1)
        fig.update_xaxes(title_text=_label(first.x, units), row=len(plots), col=1)
        _axes(fig, first.model_copy(update={"y_range": None}), y=False)
    else:
        unit = units.get(first.y, "")
        ytitle = _label(first.y, units) if not merged else (", ".join(p.y for p in plots) + (f" [{unit}]" if unit else ""))
        fig.update_xaxes(title_text=_label(first.x, units))
        fig.update_yaxes(title_text=ytitle)
        _axes(fig, first)
    for p in plots:
        _ref_lines(fig, p)
    height = 300 * len(plots) + 120 if stacked else 520
    return _layout(fig, title, height=height)


def _axis_values(fig: go.Figure, axis: str) -> np.ndarray:
    arrays = []
    for t in fig.data:
        v = getattr(t, axis, None)
        if v is not None:
            try:
                arrays.append(np.asarray(v, dtype=float))
            except (TypeError, ValueError):  # categorical axis (bar labels)
                return np.array([])
    vals = np.concatenate(arrays) if arrays else np.array([])
    return vals[np.isfinite(vals)]


def _widen_flat(fig: go.Figure, axis: str) -> None:
    """All values (nearly) equal: centre them in a sensible range instead of zooming into float noise."""
    vals = _axis_values(fig, axis)
    if not vals.size:
        return
    lo, hi = float(vals.min()), float(vals.max())
    mid = (lo + hi) / 2
    if hi - lo <= 1e-9 * max(abs(mid), 1.0):
        half = max(abs(mid) * 0.1, 0.5)
        (fig.update_xaxes if axis == "x" else fig.update_yaxes)(range=[mid - half, mid + half])


def _axes(fig: go.Figure, p: PlotDef, y: bool = True) -> None:
    """Apply user ranges; an end left empty is taken from the plotted data (with 5 % margin)."""
    for axis, rng in (("x", p.x_range), ("y", p.y_range))[: 2 if y else 1]:
        if not rng or rng == [None, None]:
            _widen_flat(fig, axis)
            continue
        lo, hi = rng
        if lo is None or hi is None:
            vals = _axis_values(fig, axis)
            if not vals.size:
                continue
            dmin, dmax = float(vals.min()), float(vals.max())
            pad = 0.05 * ((max(dmax, hi if hi is not None else dmax) - min(dmin, lo if lo is not None else dmin))
                          or 1.0)
            lo = dmin - pad if lo is None else lo
            hi = dmax + pad if hi is None else hi
            if not lo < hi:
                continue
        (fig.update_xaxes if axis == "x" else fig.update_yaxes)(range=[lo, hi])


def timeseries_figure(p: PlotDef, trace, occurrence, moments: list, kpis: list, units: dict) -> go.Figure:
    names = [s for s in p.signals if s in trace.signals]
    rows = max(len(names), 1)
    fig = make_subplots(rows=rows, cols=1, shared_xaxes=True, vertical_spacing=0.04,
                        subplot_titles=[_label(n, units) for n in names] or [""])
    for r, n in enumerate(names, start=1):
        fig.add_trace(go.Scatter(x=trace.time, y=trace.signals[n], mode="lines", name=n,
                                 line=dict(color=_color(r - 1), width=2),
                                 hovertemplate="t=%{x:.3f}s<br>%{y:.4g}<extra>" + n + "</extra>"),
                      row=r, col=1)
    if p.show_window and occurrence is not None:
        fig.add_vrect(x0=occurrence.t_start, x1=occurrence.t_end, fillcolor=WINDOW_FILL, line_width=0,
                      row="all", col=1)
    if p.show_moments:
        found = sorted((m for m in moments if m.time is not None), key=lambda m: m.time)
        for i, m in enumerate(found):
            fig.add_vline(x=m.time, line=dict(color=INK_2, dash="dot", width=1))
            # stagger labels so moments close in time do not overlap
            fig.add_annotation(x=m.time, y=1.0 + 0.035 * (i % 3), yref="paper", yanchor="bottom",
                               text=f"{m.moment} {m.time:.2f}s", showarrow=False, xanchor="left",
                               font=dict(size=11, color=INK_2), bgcolor="rgba(255,255,255,0.85)")
    ok = [k for k in kpis if k.status == Status.OK and k.value is not None]
    caption = "  ·  ".join(f"{k.kpi} = {k.text or f'{k.value:.4g}'}{(' ' + k.unit) if k.unit else ''}"
                           for k in ok[:8])
    title = f"{p.title or p.event}: {Path(trace.file).name} #{trace.occurrence}"
    fig.update_xaxes(title_text="time [s] (shaded: event active)", row=rows, col=1)
    height = 200 * rows + 200
    _layout(fig, title, height=height)
    fig.update_layout(showlegend=False, margin=dict(l=60, r=24, t=130, b=110))
    if caption:
        fig.add_annotation(text=caption, x=0, y=-70 / (height - 240), xref="paper", yref="paper",
                           showarrow=False, xanchor="left", yanchor="top", font=dict(size=11, color=INK_2))
    return fig


def render_plots(profile, outcomes: list, out_dir: str | Path, units: dict, files: list[str],
                 collect: Optional[list] = None) -> dict[str, str]:
    """Write every plot; returns {title: html path}. Uses one shared plotly.min.js for offline use.

    `collect`, if given, receives a FigureEntry per figure (for the dashboard)."""
    from src.kpi_studio.report.dashboard import FigureEntry

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    written: dict[str, str] = {}
    wide = kpi_wide(outcomes)
    n = 0
    merged_done: set = set()

    def save(fig, title, kind, group=""):
        nonlocal n
        n += 1
        path = out_dir / f"Fig_{n:02d} - {_slug(title)}.html"
        fig.write_html(str(path), include_plotlyjs="directory", full_html=True)
        written[title if title not in written else f"{title} ({n})"] = str(path)
        if collect is not None:
            collect.append(FigureEntry(title, str(path), fig, kind, group))

    for p in profile.plots:
        if p.type == "timeseries":
            occ_map = {(o.file, o.event, o.index): o for out in outcomes for o in out.occurrences}
            count = 0
            for out in outcomes:
                for tr in out.traces:
                    if tr.event != p.event or count >= p.max_figures:
                        continue
                    count += 1
                    occ = occ_map.get((tr.file, tr.event, tr.occurrence))
                    moms = [m for m in out.moments if m.event == tr.event and m.occurrence == tr.occurrence]
                    ks = [k for k in out.results if k.event == tr.event and k.occurrence == tr.occurrence]
                    fig = timeseries_figure(p, tr, occ, moms, ks, units)
                    save(fig, f"{Path(tr.file).stem} #{tr.occurrence}", "timeseries", p.title or p.event)
        elif p.type == "scatter" and p.merge:
            if p.merge in merged_done:
                continue
            merged_done.add(p.merge)
            group = [q for q in profile.plots if q.type == "scatter" and q.merge == p.merge]
            fig = scatter_figure(group, wide, units, files, p.merge)
            if fig is not None:
                save(fig, p.merge, p.type)
        else:
            fig = kpi_figure(p, wide, units, files)
            if fig is not None:
                save(fig, fig.layout.title.text or p.title or f"{p.type} {p.y or p.x}", p.type)
    return written
