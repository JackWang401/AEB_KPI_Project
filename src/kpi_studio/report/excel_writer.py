"""Write run results to one Excel workbook (written once, atomically)."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Optional

import pandas as pd
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from src.kpi_studio.core.results import Status, to_frame

FAIL_FILL = PatternFill("solid", start_color="FDE2E1", end_color="FDE2E1")
HEADER_FONT = Font(bold=True)
HEADER_ALIGN = Alignment(horizontal="center", vertical="center")
CELL_ALIGN = Alignment(horizontal="left")


class OutputLockedError(PermissionError):
    """The workbook is open in another program (common on Windows)."""


def kpi_header(name: str, unit: str) -> str:
    return f"{name} [{unit}]" if unit else name


def summary_table(outcomes: list, kpi_order: list[str], units: dict[str, str],
                  event: Optional[str] = None, bools: frozenset = frozenset()
                  ) -> tuple[pd.DataFrame, list[tuple[int, int]]]:
    """One row per occurrence, one column per KPI; `bools` KPIs (yes / no) show TRUE / FALSE.
    Returns (frame, failed cell positions)."""
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
            if k in bools and isinstance(value, (int, float)) and pd.notna(value):
                value = bool(round(value))
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
        cell.alignment = HEADER_ALIGN
    for r, c in failed:
        ws.cell(row=r + 2, column=c + 1).fill = FAIL_FILL
    for row in ws.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = CELL_ALIGN  # numbers too, so every column reads from the left
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


VERSION = "SW version"


def _with_version(df: pd.DataFrame, key: str, versions: dict) -> pd.DataFrame:
    """`df` with a software-version column right after its file column."""
    df = df.copy()
    if VERSION in df.columns:
        df = df.drop(columns=[VERSION])
    df.insert(df.columns.get_loc(key) + 1 if key in df.columns else 0, VERSION,
              [versions.get(f, "") for f in df[key]] if key in df.columns else [])
    return df


def _merge(old: Optional[pd.DataFrame], new: pd.DataFrame, key: str, replaced: set, sort: list[str]) -> pd.DataFrame:
    """Rows of `old` whose file is not in this run (`replaced`), plus `new`, sorted by file then occurrence.
    A file kept from `old` keeps its software version unless this run gives one."""
    if old is None or old.empty or key not in old.columns:
        return new
    keep = old[~old[key].astype(str).isin(replaced)]
    if keep.empty:
        return new
    cols = list(new.columns) + [c for c in keep.columns if c not in new.columns]
    both = pd.concat([keep.reindex(columns=cols), new.reindex(columns=cols)], ignore_index=True)
    by = [c for c in sort if c in both.columns]
    return both.sort_values(by, kind="stable", key=lambda c: c.astype(str) if c.name == key else c,
                            ignore_index=True) if by else both


def _failed_cells(summary: pd.DataFrame, details: pd.DataFrame) -> list[tuple[int, int]]:
    """Summary cells of KPIs whose status is not ok (from the details, so kept rows stay marked)."""
    if details.empty or summary.empty or "status" not in details.columns:
        return []
    col_of = {}
    for j, c in enumerate(summary.columns):
        col_of.setdefault(str(c).split(" [")[0], j)
    row_of = {(str(r["File"]), str(r["Event"]), int(r["Occurrence"])): i for i, r in summary.iterrows()
              if pd.notna(r["Occurrence"])}
    out = []
    for r in details[details["status"].astype(str) != Status.OK].itertuples(index=False):
        i = row_of.get((str(r.file), str(r.event), int(r.occurrence))) if pd.notna(r.occurrence) else None
        j = col_of.get(str(r.kpi))
        if i is not None and j is not None:
            out.append((i, j))
    return out


def read_previous(path: Optional[Path]) -> dict:
    """The sheets of an earlier result workbook ({} when there is none or it cannot be read)."""
    if path is None or not Path(path).exists():
        return {}
    try:
        return pd.read_excel(path, sheet_name=None)
    except Exception:  # noqa: BLE001 - a damaged old workbook is simply not merged
        return {}


def write_workbook(path: str | Path, outcomes: list, *, kpi_order: list[str], units: dict[str, str],
                   events: list[str], run_info: list[tuple[str, str]], profile_text: str,
                   previous: Optional[dict] = None, versions: Optional[dict] = None,
                   bools: frozenset = frozenset()) -> Path:
    """`previous`: sheets of the earlier workbook (read_previous). Its rows of files this run did not
    process are kept; rows of the same file (exact path) are replaced by this run's."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    previous = previous or {}
    old_files = previous.get("Files")
    kept = {}  # versions entered for earlier runs stay unless a new one is given
    if old_files is not None and VERSION in old_files.columns and "file" in old_files.columns:
        kept = {str(f): str(v) for f, v in zip(old_files["file"], old_files[VERSION]) if pd.notna(v) and str(v)}
    versions = {**kept, **{k: v for k, v in (versions or {}).items() if v}}
    replaced = {str(o.file) for o in outcomes}
    fd, tmp = tempfile.mkstemp(prefix=".kpi_results.", suffix=".xlsx", dir=str(path.parent))
    os.close(fd)
    used: set = set()
    try:
        with pd.ExcelWriter(tmp, engine="openpyxl") as xw:
            details = to_frame([r for o in outcomes for r in o.results])
            if details.empty:
                details = pd.DataFrame(columns=["file", "event", "occurrence", "kpi", "value", "unit",
                                                "moment_time", "status", "message"])
            details = _merge(previous.get("Details"), _with_version(details, "file", versions), "file", replaced,
                             ["file", "event", "occurrence"])

            summary, _ = summary_table(outcomes, kpi_order, units, bools=bools)
            summary = _merge(previous.get("Summary"), _with_version(summary, "File", versions), "File", replaced,
                             ["File", "Event", "Occurrence"])
            name = _sheet_name("Summary", used)
            summary.to_excel(xw, sheet_name=name, index=False)
            _style(xw.sheets[name], _failed_cells(summary, details))

            all_events = list(dict.fromkeys(list(events) + [str(e) for e in summary.get("Event", [])]))
            if len(all_events) > 1:
                fixed = [c for c in summary.columns if c in ("File", VERSION, "Event", "Occurrence", "Start [s]",
                                                             "End [s]")]
                for ev in all_events:
                    df = summary[summary["Event"].astype(str) == ev].reset_index(drop=True)
                    kcols = [c for c in summary.columns if c not in fixed and df[c].notna().any()]
                    df = df[fixed + kcols]
                    name = _sheet_name(ev, used)
                    df.to_excel(xw, sheet_name=name, index=False)
                    _style(xw.sheets[name], _failed_cells(df, details))

            name = _sheet_name("Details", used)
            details.to_excel(xw, sheet_name=name, index=False)
            _style(xw.sheets[name])

            moments = to_frame([m for o in outcomes for m in o.moments])
            if not moments.empty:
                moments = _with_version(moments, "file", versions)
            moments = _merge(previous.get("Moments"), moments, "file", replaced, ["file", "event", "occurrence"])
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
            files = pd.DataFrame(file_rows, columns=["file", "status", "duration [s]", "occurrences", "kpis ok",
                                                     "kpis failed", "channels", "issues"])
            files = _merge(previous.get("Files"), _with_version(files, "file", versions), "file", replaced, ["file"])
            name = _sheet_name("Files", used)
            files.to_excel(xw, sheet_name=name, index=False)
            status_col = list(files.columns).index("status")
            _style(xw.sheets[name], [(i, status_col) for i, v in enumerate(files["status"]) if v != Status.OK])

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
