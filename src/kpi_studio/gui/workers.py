"""Background workers so the window never freezes (header indexing, preview, run)."""

from __future__ import annotations

import logging
import threading
import traceback
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from src.kpi_studio.app.run_service import RunService
from src.kpi_studio.core.align import build_frame
from src.kpi_studio.core.engine import FileEvaluator
from src.kpi_studio.core.results import FileOutcome
from src.kpi_studio.io import profile_store
from src.kpi_studio.io.mf4 import index_file, read_signals


class IndexWorker(QThread):
    indexed = Signal(str, object)  # path, RecordingIndex

    def __init__(self, files: list[str], parent=None):
        super().__init__(parent)
        self.files = list(files)

    def run(self):
        for f in self.files:
            if self.isInterruptionRequested():
                return
            self.indexed.emit(f, index_file(f))


class PreviewWorker(QThread):
    """Evaluate the compiled profile on one file; returns frame values for plotting."""

    done = Signal(object)    # dict(outcome=FileOutcome, time=..., values=...)
    failed = Signal(str)

    def __init__(self, compiled, path: str, index=None, parent=None):
        super().__init__(parent)
        self.cp = compiled
        self.path = path
        self.index = index

    def run(self):
        try:
            outcome = FileOutcome(file=self.path)
            raw, issues = read_signals(self.path, self.cp.profile.signals, self.index)
            outcome.issues.extend(issues)
            frame, align_issues, uses = build_frame(raw, self.cp.profile.signals, self.cp.profile.timebase.rate_hz,
                                                    profile_store.load_enum_tables())
            outcome.issues.extend(align_issues)
            outcome.channels.extend(uses)
            ev = FileEvaluator(self.cp, frame, self.path)
            ev.run(outcome)
            self.done.emit({"outcome": outcome, "time": ev.time, "values": dict(ev.env.values)})
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}")


class _QtLogHandler(logging.Handler):
    def __init__(self, emit):
        super().__init__(logging.INFO)
        self._emit = emit
        self.setFormatter(logging.Formatter("%(levelname)s %(message)s"))

    def emit(self, record):
        try:
            self._emit(self.format(record))
        except Exception:  # noqa: BLE001
            pass


class RunWorker(QThread):
    progress = Signal(str, int, int, str)
    log = Signal(str)
    finished_ok = Signal(object)  # RunResult
    failed = Signal(str)

    def __init__(self, profile, files: list[str], output_root: str, workers: int, base_dir=None,
                 indexes=None, parent=None):
        super().__init__(parent)
        self.profile = profile
        self.files = files
        self.output_root = output_root
        self.workers = workers
        self.base_dir = base_dir
        self.indexes = indexes or {}
        self.cancel_event = threading.Event()

    def cancel(self):
        self.cancel_event.set()

    def run(self):
        handler = _QtLogHandler(self.log.emit)
        root = logging.getLogger("src.kpi_studio")
        root.addHandler(handler)
        try:
            svc = RunService(self.profile, self.files, self.output_root, workers=self.workers,
                             progress=lambda *a: self.progress.emit(*a), cancel=self.cancel_event,
                             base_dir=self.base_dir)
            svc.indexes.update(self.indexes)
            result = svc.run()
            for o in result.outcomes:
                for i in o.issues:
                    if i.severity != "info":
                        self.log.emit(f"{Path(o.file).name}: [{i.severity}] {i.message}")
            self.finished_ok.emit(result)
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}")
        finally:
            root.removeHandler(handler)
