"""Write run results to one Excel workbook (written once, atomically)."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Optional

import pandas as pd
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

from src.kpi_studio.core.results import Status, to_frame

FAIL_FILL = PatternFill("solid", start_color="FDE2E1", end_color="FDE2E1")
HEADER_FONT = Font(bold=True)


class OutputLockedError(PermissionError):
    """The workbook is open in another program (common on Windows)."""


def kpi_header(name: str, unit: str) -> str:
    return f"{name} [{unit}]" if unit else name


def summary_table(outcomes: list, kpi_order: list[str], units: dict[str, str],
                  event: Optional[str] = None) -> tuple[pd.DataFrame, list[tuple[int, int]]]:
    """One row per occurrence, one column per KPI. Returns (frame, failed cell positions)."""
    occ = to_frame([o for out in outcomes for o in out.occurrences])
    res = to_frame([r for out in outcomes for r in out.results])
    base_cols = ["File", "Event", "Occurrence", "Start [s]", "End [s]"]
    if occ.empty:
        return pd.DataFrame(columns=base_cols + [kpi_header(k, units.get(k, "")) for k in kpi_order]), []
    if event:
        occ = occ[occ["event"] == event]
        res = res[res["event"] == event] if not res.empty else res
    rows = occ.rename(columns={"file": "File", "event": "Event", "index": "Occurrence",
                               "t_start": "Start [s]", "t_end": "End [s]"})[base_cols].reset_index(drop=True)
    kpis = [k for k in kpi_order if res.empty or k in set(res["kpi"])] if event else kpi_order
    failed: list[tuple[int, int]] = []
    lookup = {}
    if not res.empty:
        for r in res.itertuples(index=False):
            text = getattr(r, "text", "")
            lookup[(r.file, r.event, r.occurrence, r.kpi)] = (text if isinstance(text, str) and text else r.value,
                                                              r.status)
    for j, k in enumerate(kpis):
        col = []
        for i, row in rows.iterrows():
            value, status = lookup.get((row["File"], row["Event"], row["Occurrence"], k), (None, None))
            col.append(value)
            if status is not None and status != Status.OK:
                failed.append((i, len(base_cols) + j))
        rows[kpi_header(k, units.get(k, ""))] = col
    return rows, failed


NUMBER_FORMAT = "0.000"


def _style(ws, failed: list[tuple[int, int]] = ()):
    ws.freeze_panes = "A2"
    for cell in ws[1]:
        cell.font = HEADER_FONT
    for r, c in failed:
        ws.cell(row=r + 2, column=c + 1).fill = FAIL_FILL
    for row in ws.iter_rows(min_row=2):
        for cell in row:
            if isinstance(cell.value, float):
                cell.number_format = NUMBER_FORMAT  # display only; the full value stays in the cell
    for i, col in enumerate(ws.iter_cols(min_row=1, max_row=min(ws.max_row, 200)), start=1):
        width = max((len(f"{c.value:.3f}" if isinstance(c.value, float) else str(c.value))
                     for c in col if c.value is not None), default=8)
        ws.column_dimensions[get_column_letter(i)].width = min(max(width + 2, 8), 60)


def _sheet_name(name: str, used: set) -> str:
    bad = '[]:*?/\\'
    base = "".join("_" if ch in bad else ch for ch in name)[:31] or "sheet"
    out, n = base, 1
    while out.lower() in used:
        n += 1
        out = f"{base[:28]}_{n}"
    used.add(out.lower())
    return out


def write_workbook(path: str | Path, outcomes: list, *, kpi_order: list[str], units: dict[str, str],
                   events: list[str], run_info: list[tuple[str, str]], profile_text: str) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".kpi_results.", suffix=".xlsx", dir=str(path.parent))
    os.close(fd)
    used: set = set()
    try:
        with pd.ExcelWriter(tmp, engine="openpyxl") as xw:
            summary, failed = summary_table(outcomes, kpi_order, units)
            name = _sheet_name("Summary", used)
            summary.to_excel(xw, sheet_name=name, index=False)
            _style(xw.sheets[name], failed)

            if len(events) > 1:
                for ev in events:
                    df, f = summary_table(outcomes, kpi_order, units, event=ev)
                    name = _sheet_name(ev, used)
                    df.to_excel(xw, sheet_name=name, index=False)
                    _style(xw.sheets[name], f)

            details = to_frame([r for o in outcomes for r in o.results])
            if details.empty:
                details = pd.DataFrame(columns=["file", "event", "occurrence", "kpi", "value", "unit",
                                                "moment_time", "status", "message"])
            name = _sheet_name("Details", used)
            details.to_excel(xw, sheet_name=name, index=False)
            _style(xw.sheets[name])

            moments = to_frame([m for o in outcomes for m in o.moments])
            if not moments.empty:
                name = _sheet_name("Moments", used)
                moments.to_excel(xw, sheet_name=name, index=False)
                _style(xw.sheets[name])

            file_rows = []
            for o in outcomes:
                file_rows.append({
                    "file": o.file,
                    "status": o.status,
                    "duration [s]": o.duration_s,
                    "occurrences": len(o.occurrences),
                    "kpis ok": sum(1 for r in o.results if r.status == Status.OK),
                    "kpis failed": sum(1 for r in o.results if r.status != Status.OK),
                    "channels": "; ".join(
                        f"{c.signal} <- {c.channel}" + (f" ({c.file_unit} -> {c.converted_to})" if c.converted_to else "")
                        for c in o.channels),
                    "issues": "\n".join(f"[{i.severity}] {i.message}" for i in o.issues),
                })
            name = _sheet_name("Files", used)
            pd.DataFrame(file_rows).to_excel(xw, sheet_name=name, index=False)
            _style(xw.sheets[name], [(i, 1) for i, r in enumerate(file_rows) if r["status"] != Status.OK])

            info = pd.DataFrame(run_info, columns=["key", "value"])
            prof = pd.DataFrame({"key": ["profile"] + [""] * (len(profile_text.splitlines()) - 1),
                                 "value": profile_text.splitlines() or [""]})
            name = _sheet_name("RunInfo", used)
            pd.concat([info, pd.DataFrame([{"key": "", "value": ""}]), prof]).to_excel(
                xw, sheet_name=name, index=False)
            _style(xw.sheets[name])
        try:
            os.replace(tmp, path)
        except PermissionError as exc:
            raise OutputLockedError(
                f"cannot write {path}: it is probably open in Excel; close it and run again") from exc
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return path
