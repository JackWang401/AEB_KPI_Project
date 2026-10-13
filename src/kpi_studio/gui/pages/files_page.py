"""Step 1: choose the MF4 files."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from src.kpi_studio.gui.state import AppState
from src.kpi_studio.gui.workers import IndexWorker


class FilesPage(QWidget):
    COLS = ["File", "Recording", "Folder", "Duration [s]", "Channels", "Status"]

    def __init__(self, state: AppState, settings: QSettings, parent=None):
        super().__init__(parent)
        self.state = state
        self.settings = settings
        self.workers: list[IndexWorker] = []
        self.setAcceptDrops(True)

        lay = QVBoxLayout(self)
        intro = QLabel("Add the MF4 recordings to process (two or more is typical). Drag and drop works too. "
                       "Headers are read in the background so channel names are available in the next step.")
        intro.setWordWrap(True)
        lay.addWidget(intro)

        bar = QHBoxLayout()
        self.add_files_btn = QPushButton("Add files…")
        self.add_folder_btn = QPushButton("Add folder…")
        self.remove_btn = QPushButton("Remove selected")
        self.clear_btn = QPushButton("Clear")
        for b in (self.add_files_btn, self.add_folder_btn, self.remove_btn, self.clear_btn):
            bar.addWidget(b)
        bar.addStretch(1)
        self.combine = QCheckBox("Combine files of the same recording")
        self.combine.setToolTip(
            "Loggers often write one drive as several files, one per bus or ECU, e.g.\n"
            "  AADT-150453_002_ABD_Robot__20260826T151159.mf4\n"
            "  AADT-150453_002_MOTION_1__20260826T151158.mf4\n"
            "Files in the same folder with the same name before the logger part (and start times within\n"
            "5 minutes) are read together as one recording, on one time axis. KPIs can then use signals\n"
            "from all of them.")
        self.combine.setChecked(self.settings.value("files/combine", True, type=bool))
        self.state.set_combine(self.combine.isChecked())
        bar.addWidget(self.combine)
        lay.addLayout(bar)

        self.table = QTableWidget(0, len(self.COLS))
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        lay.addWidget(self.table, 1)
        self.summary = QLabel()
        lay.addWidget(self.summary)

        self.add_files_btn.clicked.connect(self.pick_files)
        self.add_folder_btn.clicked.connect(self.pick_folder)
        self.remove_btn.clicked.connect(self.remove_selected)
        self.clear_btn.clicked.connect(lambda: self.state.set_files([]))
        self.combine.toggled.connect(self._combine_toggled)
        state.files_changed.connect(self.refresh)
        self.refresh()

    # ------------------------------------------------------------------ actions
    def _last_dir(self) -> str:
        return str(self.settings.value("files/last_dir", str(Path.home())))

    def pick_files(self):
        files, _ = QFileDialog.getOpenFileNames(self, "Add MF4 files", self._last_dir(), "MF4 files (*.mf4 *.MF4 *.mdf *.dat)")
        if files:
            self.settings.setValue("files/last_dir", str(Path(files[0]).parent))
            self.add_files(files)

    def pick_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Add all MF4 files in a folder", self._last_dir())
        if folder:
            self.settings.setValue("files/last_dir", folder)
            files = sorted(str(p) for p in Path(folder).iterdir() if p.suffix.lower() in (".mf4", ".mdf"))
            self.add_files(files)

    def add_files(self, files: list[str]):
        new = [str(Path(f).resolve()) for f in files if str(Path(f).resolve()) not in self.state.files]
        if not new:
            return
        self.state.set_files(self.state.files + new)
        worker = IndexWorker(new, self)
        worker.indexed.connect(self.state.set_index)
        worker.finished.connect(lambda w=worker: self.workers.remove(w) if w in self.workers else None)
        self.workers.append(worker)
        worker.start()

    def remove_selected(self):
        rows = sorted({i.row() for i in self.table.selectedIndexes()})
        keep = [f for i, f in enumerate(self.state.files) if i not in rows]
        self.state.set_files(keep)

    def _combine_toggled(self, on: bool):
        self.settings.setValue("files/combine", on)
        self.state.set_combine(on)

    # drag and drop
    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        paths = []
        for url in e.mimeData().urls():
            p = Path(url.toLocalFile())
            if p.is_dir():
                paths += [str(x) for x in p.iterdir() if x.suffix.lower() in (".mf4", ".mdf")]
            elif p.suffix.lower() in (".mf4", ".mdf", ".dat"):
                paths.append(str(p))
        self.add_files(sorted(paths))

    # ------------------------------------------------------------------ view
    def refresh(self):
        self.table.setRowCount(len(self.state.files))
        indexed = 0
        recs = self.state.recordings()
        rec_of = {f: r for r in recs for f in r.files}
        for r, f in enumerate(self.state.files):
            idx = self.state.indexes.get(f)
            p = Path(f)
            if idx is None:
                status, dur, chans = "reading header…", "", ""
            elif idx.ok:
                indexed += 1
                status = "ready"
                dur = f"{idx.duration:.1f}" if idx.duration is not None else "?"
                chans = str(len(idx.channels))
            else:
                status, dur, chans = idx.error, "", ""
            rec = rec_of.get(f)
            rec_text = Path(rec.name).name if rec is not None and len(rec.files) > 1 else "(own)"
            for c, text in enumerate([p.name, rec_text, str(p.parent), dur, chans, status]):
                item = QTableWidgetItem(text)
                if c == 5 and idx is not None and not idx.ok:
                    item.setForeground(Qt.red)
                self.table.setItem(r, c, item)
        multi = sum(1 for r in recs if len(r.files) > 1)
        self.summary.setText(f"{len(self.state.files)} file(s), {indexed} ready  ·  {len(recs)} recording(s) to process"
                             + (f", {multi} of them combined from several files" if multi else ""))

    def shutdown(self):
        for w in list(self.workers):
            w.requestInterruption()
            w.wait(3000)
