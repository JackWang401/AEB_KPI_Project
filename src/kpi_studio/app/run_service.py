"""Orchestrates a run: preflight, per-file processing (optionally in parallel), outputs.

Used by both the GUI and the CLI, so behaviour is identical in both.
"""

from __future__ import annotations

import datetime as _dt
import logging
import multiprocessing
import os
import shutil
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
from src.kpi_studio.core.model import Profile, signal_units
from src.kpi_studio.core.results import FileIssue, FileOutcome, Status
from src.kpi_studio.core.validate import CompiledProfile
from src.kpi_studio.io import profile_store
from src.kpi_studio.io.mf4 import Recording, as_recording, group_recordings, index_file, read_signals, resolve_recording
from src.kpi_studio.report.excel_writer import read_previous, write_workbook
from src.kpi_studio.report.dashboard import build_dashboard
from src.kpi_studio.report.plots import render_plots

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
    files: list          # recording names (a single-file recording is named by its path)
    indexes: dict
    coverage: list
    errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    recordings: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def text(self) -> str:
        n_files = sum(len(r.files) for r in self.recordings) or len(self.files)
        lines = [f"{len(self.files)} recording(s) from {n_files} file(s)"]
        for r in self.recordings:
            if len(r.files) > 1:
                lines.append(f"  {r.label}: " + ", ".join(Path(f).name for f in r.files))
        for c in self.coverage:
            mark = "OK " if not c.missing_in else ("ERR" if c.required else "WRN")
            lines.append(f"  [{mark}] {c.signal}: found in {len(c.found_in)}/{len(self.files)}"
                         + (f"; missing in {', '.join(Path(f).name for f in c.missing_in)}" if c.missing_in else ""))
        lines += [f"ERROR: {e}" for e in self.errors] + [f"WARNING: {w}" for w in self.warnings]
        return "\n".join(lines)


def preflight(cp: CompiledProfile, files: list[str], indexes: Optional[dict] = None,
              combine: bool = True, together: Optional[list] = None) -> PreflightReport:
    indexes = dict(indexes or {})
    for f in files:
        if f not in indexes:
            indexes[f] = index_file(f)
    recordings = group_recordings(files, indexes, combine, together=together)
    report = PreflightReport(files=[r.name for r in recordings], indexes=indexes, coverage=[], recordings=recordings)
    report.warnings.extend(str(w) for w in cp.warnings)
    defs = cp.profile.signals
    for f in files:
        if not indexes[f].ok:
            report.errors.append(f"{Path(f).name}: {indexes[f].error}")
    per_rec = {r.name: resolve_recording(defs, r, indexes) for r in recordings
               if any(indexes[f].ok for f in r.files)}
    for name, sdef in defs.items():
        cov = SignalCoverage(name, sdef.required)
        for rname, mapping in per_rec.items():
            f, ch, _ = mapping[name]
            (cov.found_in if ch else cov.missing_in).append((rname, ch.name) if ch else rname)
        report.coverage.append(cov)
        if cov.missing_in and sdef.required:
            msg = (f"required signal '{name}' not found in {len(cov.missing_in)} recording(s); "
                   f"those will be skipped (tried {sdef.channels})")
            (report.errors if len(cov.missing_in) == len(per_rec) else report.warnings).append(msg)
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


def process_file(cp: CompiledProfile, source, index=None) -> FileOutcome:
    """Read, align and evaluate one recording (a file path or a Recording). Never raises."""
    rec = as_recording(source)
    path = rec.name
    outcome = FileOutcome(file=path)
    try:
        raw, issues = read_signals(rec, cp.profile.signals, index)
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


def _worker(profile_dict: dict, name: str, files: list[str], user_tool_files: list[str],
            offsets: Optional[dict] = None, indexes: Optional[dict] = None) -> FileOutcome:
    # runs in a child process: rebuild the compiled profile from plain data
    for f in user_tool_files:
        toolbox.load_user_tools(Path(f).parent)
    profile = Profile.model_validate(profile_dict)
    _, cp = profile_store.load_compiled(profile)
    return process_file(cp, Recording(name, files, dict(offsets or {})), indexes)


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


RESULTS_DIRNAME = "kpi_results"
OUTPUT_MARKER = ".kpi_studio_output"  # written into every output folder; only such folders are replaced


def input_results_dir(files: list) -> Path:
    """`kpi_results` in the folder holding the input files (their common folder if they are spread out)."""
    parents = [str(Path(f).resolve().parent) for f in files]
    if not parents:
        raise ValueError("no input files")
    return Path(os.path.commonpath(parents)) / RESULTS_DIRNAME


def _is_ours(folder: Path) -> bool:
    return (folder / OUTPUT_MARKER).exists() or (folder / "kpi_results.xlsx").exists()


class RunService:
    """`output_root=None`: write to `kpi_results` next to the input data, replacing the previous result."""

    def __init__(self, profile: Union[str, Path, Profile], files: list, output_root: Union[str, Path, None],
                 *, workers: int = 1, progress: Optional[ProgressFn] = None,
                 cancel: Optional[threading.Event] = None, base_dir: Optional[Path] = None,
                 combine: bool = True, offsets: Optional[dict] = None, versions: Optional[dict] = None,
                 together: Optional[list] = None):
        self.profile_src = profile
        if isinstance(profile, (str, Path)):
            base_dir = base_dir or Path(profile).parent
        self.resolved, self.cp = profile_store.load_compiled(profile, base_dir=base_dir)
        self.files = [str(Path(f)) for f in files]
        self.output_root = Path(output_root) if output_root else None
        self.workers = max(1, int(workers))
        self.progress = progress or (lambda *a: None)
        self.cancel = cancel or threading.Event()
        self.indexes: dict = {}
        self.combine = combine
        self.offsets = {str(Path(f)): float(v) for f, v in (offsets or {}).items() if v}  # file -> time shift [s]
        self.versions = {str(Path(f)): str(v).strip() for f, v in (versions or {}).items() if str(v).strip()}
        self.together = [[str(Path(f)) for f in g] for g in (together or [])]  # files read as one recording
        self.recordings: list[Recording] = []

    def preflight(self) -> PreflightReport:
        total = len(self.files)
        for i, f in enumerate(self.files, start=1):
            self.progress("index", i - 1, total, f"Reading header of {Path(f).name}")
            self.indexes[f] = index_file(f)
        self.progress("index", total, total, "Headers read")
        report = preflight(self.cp, self.files, self.indexes, self.combine, self.together)
        self.recordings = report.recordings
        self._attach_offsets()
        return report

    def _attach_offsets(self):
        for r in self.recordings:
            r.offsets = {f: self.offsets[f] for f in r.files if f in self.offsets}

    def _process_all(self) -> list[FileOutcome]:
        if not self.recordings:
            self.recordings = group_recordings(self.files, self.indexes, self.combine, together=self.together)
            self._attach_offsets()
        recs = self.recordings
        total = len(recs)
        outcomes: dict[str, FileOutcome] = {}
        if self.workers > 1 and total > 1:
            data = self.resolved.model_dump(by_alias=True, exclude_none=True)
            user_files = toolbox.loaded_user_tool_files()
            # spawn (not fork): same behaviour on Windows/macOS/Linux and safe with Qt threads
            ctx = multiprocessing.get_context("spawn")
            with ProcessPoolExecutor(max_workers=min(self.workers, total), mp_context=ctx) as pool:
                futures = {pool.submit(_worker, data, r.name, r.files, user_files, r.offsets,
                                       {f: self.indexes[f] for f in r.files if f in self.indexes}): r
                           for r in recs}
                done = 0
                for fut in as_completed(futures):
                    r = futures[fut]
                    try:
                        outcomes[r.name] = fut.result()
                    except Exception as exc:  # noqa: BLE001
                        outcomes[r.name] = FileOutcome(file=r.name).error(f"worker failed: {exc}")
                    done += 1
                    self.progress("process", done, total, f"Finished {r.label}")
                    if self.cancel.is_set():
                        for other in futures:
                            other.cancel()
                        break
        else:
            for i, r in enumerate(recs):
                if self.cancel.is_set():
                    break
                self.progress("process", i, total, f"Processing {r.label}")
                outcomes[r.name] = process_file(self.cp, r, self.indexes)
            self.progress("process", len(outcomes), total, "Recordings processed")
        return [outcomes[r.name] for r in recs if r.name in outcomes]

    def target_dir(self) -> Path:
        """Where the result ends up: `kpi_results` next to the input data, or in the chosen folder."""
        return input_results_dir(self.files) if self.output_root is None else self.output_root / RESULTS_DIRNAME

    def run(self, output_dir: Optional[Path] = None) -> RunResult:
        final_dir = None
        if output_dir:
            out_dir = Path(output_dir)
        else:
            # always the same kpi_results folder (so the same Excel file), replaced by each run: built
            # beside the old result, then swapped in, so a failed run never leaves a half-written folder
            final_dir = self.target_dir()
            if final_dir.exists() and not _is_ours(final_dir):
                raise RuntimeError(f"{final_dir} exists but was not written by KPI Studio; "
                                   "rename or move it, or choose another output folder")
            out_dir = final_dir.with_name(f"{final_dir.name}.tmp-{os.getpid()}")
            if out_dir.exists():
                shutil.rmtree(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / OUTPUT_MARKER).write_text("Written by KPI Studio; replaced by the next run.\n", encoding="utf-8")
        result = self._run_into(out_dir)
        if final_dir is not None:
            if result.cancelled:  # keep the previous complete result rather than a partial one
                shutil.rmtree(out_dir, ignore_errors=True)
                return RunResult(final_dir, None, {}, None, result.outcomes, True)
            result = self._swap_into(out_dir, final_dir, result)
        return result

    @staticmethod
    def _swap_into(tmp: Path, final: Path, result: "RunResult") -> "RunResult":
        old = None
        if final.exists():
            old = final.with_name(f"{final.name}.old-{os.getpid()}")
            if old.exists():
                shutil.rmtree(old)
            final.rename(old)
        tmp.rename(final)
        if old is not None:
            shutil.rmtree(old, ignore_errors=True)

        def moved(p):
            return None if p is None else str(final / Path(p).relative_to(tmp))

        result.output_dir = final
        result.excel = None if result.excel is None else Path(moved(result.excel))
        result.dashboard = moved(result.dashboard)
        result.plots = {k: moved(v) for k, v in result.plots.items()}
        return result

    def _run_into(self, out_dir: Path) -> RunResult:

        outcomes = self._process_all()
        cancelled = self.cancel.is_set()
        if cancelled:
            for r in self.recordings:
                if r.name not in {o.file for o in outcomes}:
                    outcomes.append(FileOutcome(file=r.name, issues=[FileIssue("warning", "cancelled before processing")],
                                                status=Status.ERROR))

        profile_text = profile_store.to_yaml(self.resolved)
        profile_store.atomic_write_text(out_dir / "profile.resolved.kpi.yaml", profile_text)
        units = {k.defn.name: k.unit for k in self.cp.kpis}
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
            ("recordings", "\n".join(f"{r.label}: " + ", ".join(Path(f).name for f in r.files)
                                     for r in self.recordings if len(r.files) > 1) or "one file each"),
            ("cancelled", "yes" if cancelled else "no"),
        ]
        self.progress("excel", 0, 1, "Writing Excel")
        # one workbook for every run into this folder: earlier files' rows are kept, this run's files replaced
        previous = read_previous(self.target_dir() / "kpi_results.xlsx")
        rec_versions = {}  # recording -> software version of its files
        for r in self.recordings:
            v = ", ".join(dict.fromkeys(self.versions[f] for f in r.files if f in self.versions))
            if v:
                rec_versions[r.name] = v
        excel = write_workbook(out_dir / "kpi_results.xlsx", outcomes, kpi_order=kpi_order, units=units,
                               events=list(self.cp.events), run_info=run_info, profile_text=profile_text,
                               previous=previous, versions=rec_versions,
                               bools=frozenset(k.defn.name for k in self.cp.kpis if k.is_bool),
                               highlight=frozenset(k.defn.name for k in self.cp.kpis if k.is_bool and k.defn.highlight))
        details = read_previous(excel).get("Details")  # every file's KPIs, for the KPI plots
        self.progress("plots", 0, 1, "Rendering plots")
        figures: list = []
        plot_units = {**signal_units(self.resolved), **units}  # time series panels show signal units
        plots = render_plots(self.resolved, outcomes, out_dir / "plots", plot_units, [r.name for r in self.recordings],
                             collect=figures, details=details)
        dashboard = build_dashboard(figures, out_dir, outcomes=outcomes, kpi_order=kpi_order, units=units,
                                    profile_name=self.resolved.name, run_time=dict(run_info)["run time"],
                                    plotly_js=out_dir / "plots" / "plotly.min.js")
        self.progress("done", 1, 1, f"Done: {out_dir}")
        return RunResult(out_dir, excel, plots, dashboard, outcomes, cancelled)


def default_workers() -> int:
    return max(1, min(4, (os.cpu_count() or 2) - 1))
