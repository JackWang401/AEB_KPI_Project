"""dashboard.html: run overview, KPI statistics and every plot inline (no frames, no scrolling boxes).

Figures are embedded as JSON and drawn by plotly.js when they scroll into view, so a run with
many time-series figures still opens quickly. plotly.min.js is the copy written next to the
standalone plots, so the dashboard works offline.
"""

from __future__ import annotations

import html
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.utils import PlotlyJSONEncoder

from src.kpi_studio.core.results import Status, to_frame


@dataclass
class FigureEntry:
    title: str
    path: str               # standalone HTML file
    fig: go.Figure
    kind: str               # scatter | box | bar | histogram | timeseries
    group: str = ""         # timeseries: the plot's title / event, used to group the figures


def _fmt(v) -> str:
    if v is None or (isinstance(v, float) and not np.isfinite(v)):
        return "–"
    return f"{v:.3f}"


def _text_counts(d: pd.DataFrame) -> str:
    """Text results (e.g. 'PB + FB') counted over the OK occurrences, most frequent first; '' for numbers."""
    if d.empty or "text" not in d:
        return ""
    texts = d[(d["status"] == Status.OK) & (d["text"].fillna("") != "")]["text"]
    return ", ".join(f"{t} ×{n}" for t, n in texts.value_counts().items())


def kpi_stats(outcomes: list, kpi_order: list[str], units: dict) -> list[dict]:
    """One row per KPI: event, unit, OK / failed counts and mean, min, max of the OK values."""
    res = to_frame([r for o in outcomes for r in o.results])
    rows = []
    for name in kpi_order:
        d = res[res["kpi"] == name] if not res.empty else res
        ok = d[d["status"] == Status.OK]["value"].dropna().astype(float) if not d.empty else pd.Series(dtype=float)
        rows.append({
            "kpi": name, "event": ", ".join(sorted(set(d["event"]))) if not d.empty else "",
            "unit": units.get(name, ""), "n": int(ok.size), "failed": int(len(d) - ok.size),
            "mean": ok.mean() if ok.size else None, "min": ok.min() if ok.size else None,
            "max": ok.max() if ok.size else None,
            "counts": _text_counts(d),
        })
    return rows


def _figure_json(fig: go.Figure, kind: str) -> str:
    f = go.Figure(fig)
    f.update_layout(title=None, autosize=True, width=None)
    if kind != "timeseries":
        f.update_layout(height=KPI_FIG_HEIGHT, margin=dict(l=60, r=20, t=36, b=50))
    data = json.dumps({"data": f.to_plotly_json()["data"], "layout": f.to_plotly_json()["layout"]},
                      cls=PlotlyJSONEncoder)
    return data.replace("</", "<\\/")  # safe inside <script>


KPI_FIG_HEIGHT = 420


def _card(i: int, e: FigureEntry, rel: str) -> str:
    height = KPI_FIG_HEIGHT if e.kind != "timeseries" else int(e.fig.layout.height or 480)
    return f"""<section class="card">
  <div class="card-head"><h3>{html.escape(e.title)}</h3>
    <a class="open" href="{html.escape(rel)}" target="_blank" rel="noopener">Open full size ↗</a></div>
  <div class="fig" id="fig{i}" style="height:{height}px"></div>
  <script type="application/json" id="fig{i}-data">{_figure_json(e.fig, e.kind)}</script>
</section>"""


def build_dashboard(entries: list[FigureEntry], out_dir: str | Path, *, outcomes: list, kpi_order: list[str],
                    units: dict, profile_name: str, run_time: str = "",
                    plotly_js: Optional[str | Path] = None) -> str:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / "dashboard.html"

    n_files = len(outcomes)
    n_ok = sum(1 for o in outcomes if o.status == Status.OK)
    n_occ = sum(len(o.occurrences) for o in outcomes)
    stats = kpi_stats(outcomes, kpi_order, units)
    tiles = [("Recordings", f"{n_ok} / {n_files}", "processed OK"),
             ("Occurrences", str(n_occ), "events found"),
             ("KPIs", str(len(kpi_order)), f"{sum(s['n'] for s in stats)} values")]
    tiles_html = "".join(f'<div class="tile"><div class="tile-label">{a}</div><div class="tile-value">{b}</div>'
                         f'<div class="tile-sub">{c}</div></div>' for a, b, c in tiles)

    stat_rows = "".join(
        f"<tr><td class='name'>{html.escape(s['kpi'])}</td><td>{html.escape(s['event'])}</td>"
        f"<td>{html.escape(s['unit'])}</td><td class='num'>{s['n']}</td>"
        f"<td class='num{' bad' if s['failed'] else ''}'>{s['failed']}</td>"
        + (f"<td colspan='3'>{html.escape(s['counts'])}</td></tr>" if s.get("counts") else
           f"<td class='num'>{_fmt(s['mean'])}</td><td class='num'>{_fmt(s['min'])}</td>"
           f"<td class='num'>{_fmt(s['max'])}</td></tr>") for s in stats)
    stats_html = (f"""<section class="card"><div class="card-head">
  <span class="muted">Over all occurrences. failed = the KPI could not be computed (see the Excel Details sheet).</span></div>
  <div class="table-wrap"><table><thead><tr><th>KPI</th><th>Event</th><th>Unit</th><th class='num'>n</th>
  <th class='num'>failed</th><th class='num'>mean</th><th class='num'>min</th><th class='num'>max</th></tr></thead>
  <tbody>{stat_rows}</tbody></table></div></section>""" if stats else "")

    def rel(p):
        return os.path.relpath(p, start=out_dir)

    kpi_figs = [e for e in entries if e.kind != "timeseries"]
    ts_figs = [e for e in entries if e.kind == "timeseries"]
    i = 0
    kpi_cards = []
    for e in kpi_figs:
        kpi_cards.append(_card(i, e, rel(e.path)))
        i += 1
    groups: dict[str, list[str]] = {}
    for e in ts_figs:
        groups.setdefault(e.group or "Time series", []).append(_card(i, e, rel(e.path)))
        i += 1
    ts_html = "".join(
        f"""<details class="group"{' open' if gi == 0 else ''}><summary>{html.escape(g)}
  <span class="muted">{len(cards)} figure{'s' if len(cards) != 1 else ''}</span></summary>
  <div class="stack">{''.join(cards)}</div></details>""" for gi, (g, cards) in enumerate(groups.items()))

    js_src = rel(plotly_js) if plotly_js else "plots/plotly.min.js"
    body = f"""
<header class="top">
  <div><h1>KPI Dashboard</h1><div class="muted">{html.escape(profile_name)}{(' · ' + html.escape(run_time)) if run_time else ''}</div></div>
  <a class="open" href="kpi_results.xlsx">kpi_results.xlsx</a>
</header>
<main>
  <div class="tiles">{tiles_html}</div>
  {'<h2>KPI plots</h2><div class="grid">' + ''.join(kpi_cards) + '</div>' if kpi_cards else ''}
  <h2>KPI statistics</h2>
  {stats_html}
  {'<h2>Time series</h2>' + ts_html if ts_html else ''}
  {'' if entries else '<p class="muted">No plots defined in the profile (tab 4 → Add plot).</p>'}
</main>"""

    page = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>KPI Dashboard</title>
<style>
:root {{ color-scheme: light; --bg:#f6f6f3; --surface:#ffffff; --ink:#1f1f1e; --ink-2:#5f5e5a; --muted:#8a8a85;
  --line:#e6e5e0; --accent:#2a78d6; --bad:#c43c3b; }}
* {{ box-sizing: border-box; }}
body {{ margin:0; background:var(--bg); color:var(--ink);
  font: 14px/1.5 Inter, "Helvetica Neue", Arial, sans-serif; }}
.top {{ display:flex; justify-content:space-between; align-items:flex-end; gap:16px; flex-wrap:wrap;
  padding:28px clamp(16px,4vw,48px) 12px; }}
h1 {{ margin:0; font-size:26px; font-weight:650; letter-spacing:-0.01em; }}
h2 {{ margin:36px 0 12px; font-size:17px; font-weight:600; color:var(--ink-2); }}
h3 {{ margin:0; font-size:15px; font-weight:600; }}
main {{ padding:0 clamp(16px,4vw,48px) 48px; max-width:1600px; }}
.muted {{ color:var(--muted); font-size:13px; font-weight:400; }}
.tiles {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); gap:12px; margin:8px 0 16px; }}
.tile, .card {{ background:var(--surface); border:1px solid var(--line); border-radius:10px; }}
.tile {{ padding:14px 16px; }}
.tile-label {{ color:var(--ink-2); font-size:12px; text-transform:uppercase; letter-spacing:.04em; }}
.tile-value {{ font-size:26px; font-weight:650; margin-top:2px; font-variant-numeric:tabular-nums; }}
.tile-sub {{ color:var(--muted); font-size:12px; }}
.card {{ padding:16px 18px 10px; margin-bottom:16px; min-width:0; }}
.card-head {{ display:flex; justify-content:space-between; align-items:baseline; gap:12px; flex-wrap:wrap;
  margin-bottom:6px; }}
a.open {{ color:var(--accent); text-decoration:none; font-size:13px; white-space:nowrap; }}
a.open:hover {{ text-decoration:underline; }}
.grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(min(100%,640px),1fr)); gap:16px; }}
.grid .card {{ margin-bottom:0; }}
.fig {{ width:100%; }}
.table-wrap {{ overflow-x:auto; }}
table {{ border-collapse:collapse; width:100%; font-variant-numeric:tabular-nums; }}
th, td {{ text-align:left; padding:7px 10px; border-bottom:1px solid var(--line); white-space:nowrap; }}
th {{ color:var(--ink-2); font-weight:600; font-size:12px; }}
td.num, th.num {{ text-align:right; }}
td.name {{ font-weight:600; }}
td.bad {{ color:var(--bad); font-weight:600; }}
tbody tr:hover {{ background:#fafaf8; }}
details.group {{ background:var(--surface); border:1px solid var(--line); border-radius:10px; margin-bottom:12px; }}
details.group > summary {{ cursor:pointer; padding:12px 18px; font-weight:600; list-style-position:inside; }}
details.group .stack {{ padding:0 12px 4px; }}
details.group .card {{ border:none; border-top:1px solid var(--line); border-radius:0; margin:0; }}
</style>
<script src="{html.escape(js_src)}"></script>
</head><body>{body}
<script>
(function () {{
  const config = {{responsive: true, displaylogo: false}};
  function draw(div) {{
    if (div.dataset.drawn) return;
    div.dataset.drawn = "1";
    const spec = JSON.parse(document.getElementById(div.id + "-data").textContent);
    Plotly.newPlot(div, spec.data, spec.layout, config);
  }}
  const figs = document.querySelectorAll(".fig");
  if (!("IntersectionObserver" in window) || typeof Plotly === "undefined") {{
    if (typeof Plotly !== "undefined") figs.forEach(draw);
    return;
  }}
  const io = new IntersectionObserver((items) => items.forEach((it) => {{
    if (it.isIntersecting) {{ draw(it.target); io.unobserve(it.target); }}
  }}), {{rootMargin: "400px"}});
  figs.forEach((d) => io.observe(d));
}})();
</script>
</body></html>"""
    tmp = target.with_suffix(".tmp")
    tmp.write_text(page, encoding="utf-8")
    os.replace(tmp, target)
    return str(target)
