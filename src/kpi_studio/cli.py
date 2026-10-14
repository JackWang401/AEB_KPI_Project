"""Command-line interface.

    python -m src.kpi_studio.cli run PROFILE FILE [FILE ...] [-o OUT] [--workers N]
    python -m src.kpi_studio.cli validate PROFILE
    python -m src.kpi_studio.cli preflight PROFILE FILE [FILE ...]
    python -m src.kpi_studio.cli channels FILE [--grep TEXT]
    python -m src.kpi_studio.cli tools [--kind KIND]
"""

from __future__ import annotations

import argparse
import glob
import logging
import multiprocessing
import sys
from pathlib import Path

from src.kpi_studio import toolbox


def _expand(files: list[str]) -> list[str]:
    out = []
    for f in files:
        p = Path(f)
        if p.is_dir():
            out.extend(sorted(str(x) for x in p.glob("*.mf4")) + sorted(str(x) for x in p.glob("*.MF4")))
        elif any(ch in f for ch in "*?["):
            out.extend(sorted(glob.glob(f)))
        else:
            out.append(f)
    return list(dict.fromkeys(out))


def _progress(stage, done, total, message):
    print(f"[{stage}] {done}/{total} {message}", flush=True)


def cmd_validate(args) -> int:
    from src.kpi_studio.core.validate import ProfileError
    from src.kpi_studio.io import profile_store

    try:
        resolved, cp = profile_store.load_compiled(args.profile)
    except ProfileError as exc:
        for issue in exc.issues:
            print(issue)
        return 1
    for w in cp.warnings:
        print(w)
    print(f"OK: '{resolved.name}' — {len(cp.profile.signals)} signals, {len(cp.derived)} derived, "
          f"{len(cp.events)} events, {len(cp.kpis)} KPIs, {len(resolved.plots)} plots")
    return 0


def _offsets(specs, files: list[str]) -> dict:
    """--offset FILE=SECONDS (FILE: a path or just the file name) -> {file: seconds}."""
    out = {}
    for spec in specs or []:
        name, _, sec = spec.rpartition("=")
        try:
            value = float(sec)
        except ValueError:
            raise SystemExit(f"--offset {spec}: expected FILE=SECONDS")
        hits = [f for f in files if Path(f).name == name or Path(f).resolve() == Path(name).resolve()]
        if not hits:
            raise SystemExit(f"--offset {spec}: no input file matches '{name}'")
        out.update({str(Path(f)): value for f in hits})
    return out


def cmd_preflight(args) -> int:
    from src.kpi_studio.app.run_service import RunService

    svc = RunService(args.profile, _expand(args.files), ".")
    report = svc.preflight()
    print(report.text())
    return 0 if report.ok else 1


def cmd_run(args) -> int:
    from src.kpi_studio.app.run_service import RunService
    from src.kpi_studio.core.validate import ProfileError

    files = _expand(args.files)
    try:
        svc = RunService(args.profile, files, args.output, workers=args.workers, progress=_progress,
                         offsets=_offsets(args.offset, files))
    except ProfileError as exc:
        for issue in exc.issues:
            print(issue)
        return 1
    report = svc.preflight()
    print(report.text())
    if not report.ok and not args.force:
        print("Preflight failed; fix the errors above or pass --force to run anyway.")
        return 1
    result = svc.run()
    print(f"\nExcel:     {result.excel}")
    if result.dashboard:
        print(f"Dashboard: {result.dashboard}")
    print(f"Recordings OK: {result.n_ok}/{len(result.outcomes)}")
    for o in result.outcomes:
        for i in o.issues:
            if i.severity != "info":
                print(f"  {Path(o.file).name}: [{i.severity}] {i.message}")
    return 0 if result.n_ok == len(result.outcomes) else 2


def cmd_channels(args) -> int:
    from src.kpi_studio.io.mf4 import index_file

    idx = index_file(args.file)
    if not idx.ok:
        print(idx.error)
        return 1
    needle = (args.grep or "").lower()
    for c in idx.channels:
        if needle in c.name.lower() or needle in c.group_name.lower():
            print(f"{c.group_name}::{c.name}\t[{c.unit}]")
    return 0


def cmd_tools(args) -> int:
    for spec in toolbox.tools(args.kind):
        print(f"{spec.kind:<9} {spec.category:<12} {spec.signature}\n          {spec.summary}")
    return 0




def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="kpi_studio", description="Configurable KPI extraction from MF4 files")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--user-tools", help="folder with user tool .py files to load")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("run", help="extract KPIs and write Excel + plots")
    p.add_argument("profile")
    p.add_argument("files", nargs="+", help="MF4 files, folders or glob patterns")
    p.add_argument("-o", "--output", default=None,
                   help="custom output folder (a new timestamped folder per run); default: "
                        "'kpi_results' next to the input files, replaced on every run")
    p.add_argument("--workers", type=int, default=1)
    p.add_argument("--force", action="store_true", help="run even if preflight reports errors")
    p.add_argument("--offset", action="append", metavar="FILE=SECONDS",
                   help="shift a file in time when files are not time-synced (positive: later); repeatable")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("validate", help="check a profile")
    p.add_argument("profile")
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("preflight", help="check signal coverage of files")
    p.add_argument("profile")
    p.add_argument("files", nargs="+")
    p.set_defaults(func=cmd_preflight)

    p = sub.add_parser("channels", help="list channels in an MF4 file")
    p.add_argument("file")
    p.add_argument("--grep")
    p.set_defaults(func=cmd_channels)

    p = sub.add_parser("tools", help="list toolbox functions")
    p.add_argument("--kind", choices=toolbox.KINDS)
    p.set_defaults(func=cmd_tools)

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")
    if args.user_tools:
        toolbox.load_user_tools(args.user_tools)
    return args.func(args)


if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(main())
