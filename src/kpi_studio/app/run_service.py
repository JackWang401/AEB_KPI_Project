"""Orchestrates a run: preflight, per-file processing (optionally in parallel), outputs.

Used by both the GUI and the CLI, so behaviour is identical in both.
"""

from __future__ import annotations

import datetime as _dt
import logging
import multiprocessing
import os
import platform
import re
import threading
import traceback
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional, Union

from src.kpi_studio import __version__
from src.kpi_studio import toolbox
from src.kpi_studio.core.align import build_frame
from src.kpi_studio.core.engine import evaluate_frame
from src.kpi_studio.core.model import Profile
from src.kpi_studio.core.results import FileIssue, FileOutcome, Status
from src.kpi_studio.core.validate import CompiledProfile
from src.kpi_studio.io import profile_store
from src.kpi_studio.io.mf4 import RecordingIndex, index_file, read_signals, resolve_signals
from src.kpi_studio.report.excel_writer import write_workbook
from src.kpi_studio.report.plots import build_dashboard, render_plots

log = logging.getLogger(__name__)

ProgressFn = Callable[[str, int, int, str], None]  # (stage, done, total, message)


# ----------------------------------------------------------------------------- preflight
@dataclass
class SignalCoverage:
    signal: str
    required: bool
    found_in: list = field(default_factory=list)   # (file, channel)
    missing_in: list = field(default_factory=list)


@dataclass
class PreflightReport:
    files: list
    indexes: dict
    coverage: list
    errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def text(self) -> str:
        lines = [f"{len(self.files)} file(s)"]
        for c in self.coverage:
            mark = "OK " if not c.missing_in else ("ERR" if c.required else "WRN")
            lines.append(f"  [{mark}] {c.signal}: found in {len(c.found_in)}/{len(self.files)}"
                         + (f"; missing in {', '.join(Path(f).name for f in c.missing_in)}" if c.missing_in else ""))
        lines += [f"ERROR: {e}" for e in self.errors] + [f"WARNING: {w}" for w in self.warnings]
        return "\n".join(lines)


def preflight(cp: CompiledProfile, files: list[str], indexes: Optional[dict] = None) -> PreflightReport:
    indexes = dict(indexes or {})
    for f in files:
        if f not in indexes:
            indexes[f] = index_file(f)
    report = PreflightReport(files=list(files), indexes=indexes, coverage=[])
    report.warnings.extend(str(w) for w in cp.warnings)
    defs = cp.profile.signals
    per_file = {f: resolve_signals(defs, indexes[f]) for f in files if indexes[f].ok}
    for f in files:
        if not indexes[f].ok:
            report.errors.append(f"{Path(f).name}: {indexes[f].error}")
    for name, sdef in defs.items():
        cov = SignalCoverage(name, sdef.required)
        for f, mapping in per_file.items():
            ch, _ = mapping[name]
            (cov.found_in if ch else cov.missing_in).append((f, ch.name) if ch else f)
        report.coverage.append(cov)
        if cov.missing_in and sdef.required:
            msg = (f"required signal '{name}' not found in {len(cov.missing_in)} file(s); "
                   f"those files will be skipped (tried {sdef.channels})")
            (report.errors if len(cov.missing_in) == len(per_file) else report.warnings).append(msg)
    if not files:
        report.errors.append("no files selected")
    return report


# ----------------------------------------------------------------------------- per-file work
def _capture_spec(profile: Profile) -> dict:
    cap: dict = {}
    for p in profile.plots:
        if p.type == "timeseries" and p.event:
            c = cap.setdefault(p.event, {"signals": [], "max": 0})
            c["signals"] = list(dict.fromkeys(c["signals"] + p.signals))
            c["max"] = max(c["max"], p.max_figures)
    return cap


def process_file(cp: CompiledProfile, path: str, index: Optional[RecordingIndex] = None) -> FileOutcome:
    """Read, align and evaluate one file. Never raises: failures become statuses."""
    outcome = FileOutcome(file=path)
    try:
        raw, issues = read_signals(path, cp.profile.signals, index)
        outcome.issues.extend(issues)
        missing_required = [n for n, s in cp.profile.signals.items() if s.required and n not in raw]
        if missing_required:
            return outcome.error(f"skipped: required signal(s) missing: {', '.join(missing_required)}")
        frame, align_issues, uses = build_frame(raw, cp.profile.signals, cp.profile.timebase.rate_hz,
                                                profile_store.load_enum_tables())
        outcome.issues.extend(align_issues)
        outcome.channels.extend(uses)
        if any(i.severity == "error" for i in align_issues):
            outcome.status = Status.ERROR
            return outcome
        return evaluate_frame(cp, frame, path, outcome, capture=_capture_spec(cp.profile))
    except Exception as exc:  # noqa: BLE001
        log.debug("processing %s failed\n%s", path, traceback.format_exc())
        return outcome.error(f"unexpected error: {type(exc).__name__}: {exc}")


def _worker(profile_dict: dict, path: str, user_tool_files: list[str]) -> FileOutcome:
    # runs in a child process: rebuild the compiled profile from plain data
    for f in user_tool_files:
        toolbox.load_user_tools(Path(f).parent)
    profile = Profile.model_validate(profile_dict)
    _, cp = profile_store.load_compiled(profile)
    return process_file(cp, path)


# ----------------------------------------------------------------------------- run
@dataclass
class RunResult:
    output_dir: Path
    excel: Optional[Path]
    plots: dict
    dashboard: Optional[str]
    outcomes: list
    cancelled: bool = False

    @property
    def n_ok(self) -> int:
        return sum(1 for o in self.outcomes if o.status == Status.OK)


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", text).strip("_")[:60] or "profile"


class RunService:
    def __init__(self, profile: Union[str, Path, Profile], files: list, output_root: Union[str, Path],
                 *, workers: int = 1, progress: Optional[ProgressFn] = None,
                 cancel: Optional[threading.Event] = None, base_dir: Optional[Path] = None):
        self.profile_src = profile
        if isinstance(profile, (str, Path)):
            base_dir = base_dir or Path(profile).parent
        self.resolved, self.cp = profile_store.load_compiled(profile, base_dir=base_dir)
        self.files = [str(Path(f)) for f in files]
        self.output_root = Path(output_root)
        self.workers = max(1, int(workers))
        self.progress = progress or (lambda *a: None)
        self.cancel = cancel or threading.Event()
        self.indexes: dict = {}

    def preflight(self) -> PreflightReport:
        total = len(self.files)
        for i, f in enumerate(self.files, start=1):
            self.progress("index", i - 1, total, f"Reading header of {Path(f).name}")
            self.indexes[f] = index_file(f)
        self.progress("index", total, total, "Headers read")
        return preflight(self.cp, self.files, self.indexes)

    def _process_all(self) -> list[FileOutcome]:
        total = len(self.files)
        outcomes: dict[str, FileOutcome] = {}
        if self.workers > 1 and total > 1:
            data = self.resolved.model_dump(by_alias=True, exclude_none=True)
            user_files = toolbox.loaded_user_tool_files()
            # spawn (not fork): same behaviour on Windows/macOS/Linux and safe with Qt threads
            ctx = multiprocessing.get_context("spawn")
            with ProcessPoolExecutor(max_workers=min(self.workers, total), mp_context=ctx) as pool:
                futures = {pool.submit(_worker, data, f, user_files): f for f in self.files}
                done = 0
                for fut in as_completed(futures):
                    f = futures[fut]
                    try:
                        outcomes[f] = fut.result()
                    except Exception as exc:  # noqa: BLE001
                        outcomes[f] = FileOutcome(file=f).error(f"worker failed: {exc}")
                    done += 1
                    self.progress("process", done, total, f"Finished {Path(f).name}")
                    if self.cancel.is_set():
                        for other in futures:
                            other.cancel()
                        break
        else:
            for i, f in enumerate(self.files):
                if self.cancel.is_set():
                    break
                self.progress("process", i, total, f"Processing {Path(f).name}")
                outcomes[f] = process_file(self.cp, f, self.indexes.get(f))
            self.progress("process", len(outcomes), total, "Files processed")
        return [outcomes[f] for f in self.files if f in outcomes]

    def run(self, output_dir: Optional[Path] = None) -> RunResult:
        stamp = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        out_dir = Path(output_dir) if output_dir else self.output_root / f"{_slug(self.resolved.name)}_{stamp}"
        out_dir.mkdir(parents=True, exist_ok=True)

        outcomes = self._process_all()
        cancelled = self.cancel.is_set()
        if cancelled:
            for f in self.files:
                if f not in {o.file for o in outcomes}:
                    outcomes.append(FileOutcome(file=f, issues=[FileIssue("warning", "cancelled before processing")],
                                                status=Status.ERROR))

        profile_text = profile_store.to_yaml(self.resolved)
        profile_store.atomic_write_text(out_dir / "profile.resolved.kpi.yaml", profile_text)
        units = {k.defn.name: k.defn.unit or "" for k in self.cp.kpis}
        kpi_order = [k.defn.name for k in self.cp.kpis]
        run_info = [
            ("profile", self.resolved.name),
            ("profile source", str(self.profile_src) if not isinstance(self.profile_src, Profile) else "(in memory)"),
            ("profile sha256", profile_store.profile_hash(profile_text)),
            ("tool version", __version__),
            ("toolbox version", toolbox.TOOLBOX_VERSION),
            ("user tools", ", ".join(toolbox.loaded_user_tool_files()) or "none"),
            ("run time", _dt.datetime.now().isoformat(timespec="seconds")),
            ("host", f"{platform.node()} / Python {platform.python_version()}"),
            ("files", "\n".join(self.files)),
            ("cancelled", "yes" if cancelled else "no"),
        ]
        self.progress("excel", 0, 1, "Writing Excel")
        excel = write_workbook(out_dir / "kpi_results.xlsx", outcomes, kpi_order=kpi_order, units=units,
                               events=list(self.cp.events), run_info=run_info, profile_text=profile_text)
        self.progress("plots", 0, 1, "Rendering plots")
        plots = render_plots(self.resolved, outcomes, out_dir / "plots", units, self.files)
        dashboard = build_dashboard(plots, out_dir) if plots else None
        self.progress("done", 1, 1, f"Done: {out_dir}")
        return RunResult(out_dir, excel, plots, dashboard, outcomes, cancelled)


def default_workers() -> int:
    return max(1, min(4, (os.cpu_count() or 2) - 1))
