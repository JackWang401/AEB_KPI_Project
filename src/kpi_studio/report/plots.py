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
    wide = res.pivot_table(index=["file", "event", "occurrence"], columns="kpi", values="value", aggfunc="first")
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
    fig = go.Figure()
    if p.type == "scatter":
        cols = [p.x, p.y]
        if not set(cols) <= set(wide.columns):
            return None
        d = wide.dropna(subset=cols)
        for i, (name, g) in enumerate(_groups(d, p.color_by, files)):
            fig.add_trace(go.Scatter(
                x=g[p.x], y=g[p.y], mode="markers", name=_short(name) if p.color_by == "file" else name,
                marker=dict(size=9, color=_color(i), line=dict(color=SURFACE, width=2)),
                customdata=g[["file", "event", "occurrence"]].assign(file=g["file"].map(_short)).values,
                hovertemplate=f"{p.x}: %{{x:.3f}}<br>{p.y}: %{{y:.3f}}<br>%{{customdata[0]}} · "
                              "%{customdata[1]} #%{customdata[2]}<extra></extra>"))
        fig.update_xaxes(title_text=_label(p.x, units))
        fig.update_yaxes(title_text=_label(p.y, units))
    elif p.type in ("box", "bar"):
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


def _axes(fig: go.Figure, p: PlotDef) -> None:
    """Apply user ranges; an end left empty is taken from the plotted data (with 5 % margin)."""
    for axis, rng in (("x", p.x_range), ("y", p.y_range)):
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
    caption = "  ·  ".join(f"{k.kpi} = {k.value:.4g}{(' ' + k.unit) if k.unit else ''}" for k in ok[:8])
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
        else:
            fig = kpi_figure(p, wide, units, files)
            if fig is not None:
                save(fig, fig.layout.title.text or p.title or f"{p.type} {p.y or p.x}", p.type)
    return written
