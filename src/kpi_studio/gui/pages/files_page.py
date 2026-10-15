"""The MF4 files (side panel "Files") and their time offsets (side panel "Time offset")."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSettings, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
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
from src.kpi_studio.gui.widgets import context_menu
from src.kpi_studio.gui.workers import IndexWorker


class FilesPage(QWidget):
    COLS = ["File", "Recording", "Duration [s]", "Channels", "Status", "Folder"]

    def __init__(self, state: AppState, settings: QSettings, parent=None):
        super().__init__(parent)
        self.state = state
        self.settings = settings
        self.workers: list[IndexWorker] = []
        self.setAcceptDrops(True)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        intro = QLabel("Check the files to analyse. Right-click to add or remove files; drag and drop works too. "
                       "Files of the same recording (one per logger) are read together.")
        intro.setWordWrap(True)
        intro.setObjectName("hint")
        lay.addWidget(intro)

        self.table = QTableWidget(0, len(self.COLS))
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setDefaultSectionSize(22)
        for c in range(len(self.COLS) - 1):
            self.table.horizontalHeader().setSectionResizeMode(c, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setToolTip("Check the files to analyse; select one to show its recording in the signal viewer")
        lay.addWidget(self.table, 1)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        self.summary.setObjectName("hint")
        foot = QHBoxLayout()
        foot.addWidget(self.summary, 1)
        for text, value in (("Check all", True), ("Uncheck all", False)):
            b = QPushButton(text)
            b.clicked.connect(lambda *_, v=value: self.check_all(v))
            foot.addWidget(b)
        lay.addLayout(foot)

        self.header_buttons = context_menu.plus_minus(
            remove=self.remove_selected, add_tip="Add files or a folder", remove_tip="Remove the selected files",
            add_menu=[("Add files…", self.pick_files), ("Add folder…", self.pick_folder)])
        context_menu.install(
            self.table,
            add=[("Add files…", self.pick_files), ("Add folder…", self.pick_folder),
                 ("Check all files", lambda: self.check_all(True)),
                 ("Uncheck all files", lambda: self.check_all(False)),
                 ("Remove all files", lambda: self.state.set_files([]))],
            item=lambda rows: [("Check selected", lambda: self.check_selected(True)),
                               ("Uncheck selected", lambda: self.check_selected(False)),
                               ("Remove file" if len(rows) == 1 else f"Remove {len(rows)} files",
                                self.remove_selected)])
        self.table.itemChanged.connect(self._item_changed)
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

    def check_selected(self, checked: bool):
        rows = {i.row() for i in self.table.selectedIndexes()}
        self.state.set_checked([f for i, f in enumerate(self.state.files) if i in rows], checked)

    def check_all(self, checked: bool):
        self.state.set_checked(self.state.files, checked)

    def _item_changed(self, item):
        if item.column() == 0 and 0 <= item.row() < len(self.state.files):
            self.state.set_checked([self.state.files[item.row()]], item.checkState() == Qt.Checked)

    def remove_selected(self):
        rows = sorted({i.row() for i in self.table.selectedIndexes()})
        keep = [f for i, f in enumerate(self.state.files) if i not in rows]
        self.state.set_files(keep)

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
        self.table.blockSignals(True)
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
            for c, text in enumerate([p.name, rec_text, dur, chans, status, str(p.parent)]):
                item = QTableWidgetItem(text)
                item.setToolTip(f)
                if c == 0:
                    item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
                    item.setCheckState(Qt.Unchecked if f in self.state.unchecked else Qt.Checked)
                elif f in self.state.unchecked:
                    item.setForeground(QColor("#aeaeb2"))  # not analysed
                if c == 4 and idx is not None and not idx.ok:
                    item.setForeground(Qt.red)
                self.table.setItem(r, c, item)
        self.table.blockSignals(False)
        multi = sum(1 for r in recs if len(r.files) > 1)
        ticked = len(self.state.checked_files)
        self.summary.setText(f"{ticked} of {len(self.state.files)} file(s) checked, {indexed} ready · "
                             f"{len(recs)} recording(s) to process"
                             + (f", {multi} of them combined from several files" if multi else ""))

    def shutdown(self):
        for w in list(self.workers):
            w.requestInterruption()
            w.wait(3000)


class OffsetPanel(QWidget):
    """Time shift per file in seconds: for files (or recordings) that are not time-synced."""

    COLS = ["File", "Offset [s]"]
    OFFSET_COL = 1

    def __init__(self, state: AppState, parent=None):
        super().__init__(parent)
        self.state = state
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        hint = QLabel("Double-click an offset to edit it; positive moves the file later. It applies to the "
                      "signal viewer, the event preview and the KPI run. Not saved with the profile.")
        hint.setWordWrap(True)
        hint.setObjectName("hint")
        lay.addWidget(hint)
        self.table = QTableWidget(0, len(self.COLS))
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.verticalHeader().setDefaultSectionSize(22)
        self.table.setEditTriggers(QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed
                                   | QAbstractItemView.AnyKeyPressed)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        lay.addWidget(self.table, 1)
        self.table.itemChanged.connect(self._item_edited)
        state.files_changed.connect(self.refresh)
        state.offsets_changed.connect(lambda *_: self.refresh())
        self.refresh()

    def _item_edited(self, item):
        if item.column() != self.OFFSET_COL or not (0 <= item.row() < len(self.state.files)):
            return
        f = self.state.files[item.row()]
        try:
            value = float(item.text().strip().replace(",", ".") or 0)
        except ValueError:
            value = self.state.offsets.get(f, 0.0)
        self.state.set_offsets({f: value})
        self.refresh()  # normalise the text (and undo invalid input)

    def refresh(self):
        self.table.blockSignals(True)
        self.table.setRowCount(len(self.state.files))
        for r, f in enumerate(self.state.files):
            name = QTableWidgetItem(Path(f).name)
            name.setToolTip(f)
            name.setFlags(name.flags() & ~Qt.ItemIsEditable)
            off = self.state.offsets.get(f, 0.0)
            value = QTableWidgetItem(f"{off:+.3f}" if off else "0")
            value.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            if off:
                value.setForeground(QColor("#007aff"))
            self.table.setItem(r, 0, name)
            self.table.setItem(r, self.OFFSET_COL, value)
        self.table.blockSignals(False)
