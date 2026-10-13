"""Step 4: plots, preflight, run, open results."""

from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import QSettings, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from src.kpi_studio.app.run_service import default_workers, preflight
from src.kpi_studio.gui.state import AppState
from src.kpi_studio.gui.workers import RunWorker

PLOT_TYPES = ["scatter", "box", "bar", "histogram", "timeseries"]


class RunPage(QWidget):
    PLOT_COLS = ["Type", "Title", "X (KPI)", "Y (KPI)", "Colour / group by", "Event (timeseries)", "Signals (timeseries)"]

    def __init__(self, state: AppState, settings: QSettings, parent=None):
        super().__init__(parent)
        self.state = state
        self.settings = settings
        self.worker: RunWorker | None = None
        self.result = None
        self._loading = False

        lay = QVBoxLayout(self)

        # ---- plots
        pbox = QGroupBox("Plots (written as interactive HTML next to the Excel file)")
        pl = QVBoxLayout(pbox)
        self.plots = QTableWidget(0, len(self.PLOT_COLS))
        self.plots.setHorizontalHeaderLabels(self.PLOT_COLS)
        self.plots.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.plots.horizontalHeader().setSectionResizeMode(6, QHeaderView.Stretch)
        pl.addWidget(self.plots)
        prow = QHBoxLayout()
        self.add_plot, self.del_plot = QPushButton("Add plot"), QPushButton("Delete plot")
        prow.addWidget(self.add_plot)
        prow.addWidget(self.del_plot)
        prow.addStretch(1)
        pl.addLayout(prow)
        lay.addWidget(pbox, 1)

        # ---- run
        rbox = QGroupBox("Run")
        rl = QVBoxLayout(rbox)
        orow = QHBoxLayout()
        orow.addWidget(QLabel("Output folder"))
        self.out_dir = QLineEdit(str(settings.value("run/output_dir", str(Path.home() / "kpi_studio_output"))))
        browse = QPushButton("Browse…")
        orow.addWidget(self.out_dir, 1)
        orow.addWidget(browse)
        orow.addWidget(QLabel("Parallel files"))
        self.workers = QSpinBox()
        self.workers.setRange(1, max(os.cpu_count() or 1, 1))
        self.workers.setValue(int(settings.value("run/workers", default_workers())))
        orow.addWidget(self.workers)
        rl.addLayout(orow)
        brow = QHBoxLayout()
        self.check_btn = QPushButton("Check files")
        self.run_btn = QPushButton("RUN")
        self.run_btn.setObjectName("primary")
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setEnabled(False)
        self.open_excel = QPushButton("Open Excel")
        self.open_dash = QPushButton("Open dashboard")
        self.open_folder = QPushButton("Open folder")
        for b in (self.open_excel, self.open_dash, self.open_folder):
            b.setEnabled(False)
        for b in (self.check_btn, self.run_btn, self.cancel_btn):
            brow.addWidget(b)
        brow.addStretch(1)
        for b in (self.open_excel, self.open_dash, self.open_folder):
            brow.addWidget(b)
        rl.addLayout(brow)
        self.progress = QProgressBar()
        rl.addWidget(self.progress)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(5000)
        rl.addWidget(self.log, 1)
        lay.addWidget(rbox, 2)

        browse.clicked.connect(self.pick_out)
        self.add_plot.clicked.connect(self.new_plot)
        self.del_plot.clicked.connect(self.delete_plot)
        self.check_btn.clicked.connect(self.check)
        self.run_btn.clicked.connect(self.start_run)
        self.cancel_btn.clicked.connect(self.cancel)
        self.open_excel.clicked.connect(lambda: self._open(self.result.excel if self.result else None))
        self.open_dash.clicked.connect(lambda: self._open(self.result.dashboard if self.result else None))
        self.open_folder.clicked.connect(lambda: self._open(self.result.output_dir if self.result else None))
        self.plots.itemChanged.connect(self.plot_cell_changed)
        state.profile_changed.connect(self.refresh_plots)
        self.refresh_plots()

    # ------------------------------------------------------------------ plots
    def refresh_plots(self):
        self._loading = True
        plots = self.state.data.get("plots", []) or []
        self.plots.setRowCount(len(plots))
        for r, p in enumerate(plots):
            combo = QComboBox()
            combo.addItems(PLOT_TYPES)
            combo.setCurrentText(p.get("type", "scatter"))
            combo.currentTextChanged.connect(lambda v, row=r: self._set_plot(row, "type", v))
            self.plots.setCellWidget(r, 0, combo)
            group = p.get("color_by") if p.get("type") in ("scatter", "histogram") else p.get("group_by", "")
            for c, v in enumerate([p.get("title", ""), p.get("x", ""), p.get("y", ""), group or "",
                                   p.get("event", ""), ", ".join(p.get("signals", []) or [])], start=1):
                self.plots.setItem(r, c, QTableWidgetItem(str(v or "")))
        self._loading = False

    def _set_plot(self, row, key, value):
        plots = self.state.data.setdefault("plots", [])
        if value in ("", [], None):
            plots[row].pop(key, None)
        else:
            plots[row][key] = value
        self.state.edited()

    def plot_cell_changed(self, item):
        if self._loading:
            return
        r, c, text = item.row(), item.column(), item.text().strip()
        p = self.state.data["plots"][r]
        if c == 1:
            self._set_plot(r, "title", text)
        elif c == 2:
            self._set_plot(r, "x", text)
        elif c == 3:
            self._set_plot(r, "y", text)
        elif c == 4:
            key = "color_by" if p.get("type") in ("scatter", "histogram") else "group_by"
            self._set_plot(r, key, text.lower() if text else "")
        elif c == 5:
            self._set_plot(r, "event", text)
        elif c == 6:
            self._set_plot(r, "signals", [s.strip() for s in text.split(",") if s.strip()])

    def new_plot(self):
        kpis = [k.get("name") for k in self.state.data.get("kpis", []) if k.get("name")]
        self.state.data.setdefault("plots", []).append(
            {"type": "scatter", "title": "", "x": kpis[0] if kpis else "", "y": kpis[1] if len(kpis) > 1 else ""})
        self.state.edited(structure=True)

    def delete_plot(self):
        rows = sorted({i.row() for i in self.plots.selectedIndexes()}, reverse=True)
        for r in rows:
            del self.state.data["plots"][r]
        if rows:
            self.state.edited(structure=True)

    # ------------------------------------------------------------------ run
    def pick_out(self):
        d = QFileDialog.getExistingDirectory(self, "Output folder", self.out_dir.text())
        if d:
            self.out_dir.setText(d)

    def check(self) -> bool:
        self.state.validate_now()
        if self.state.compiled is None:
            errors = [str(i) for i in self.state.issues if i.severity == "error"]
            self.log.appendPlainText("Profile has errors:\n  " + "\n  ".join(errors))
            return False
        missing_idx = [f for f in self.state.files if f not in self.state.indexes]
        if missing_idx:
            self.log.appendPlainText("Still reading file headers; try again in a moment.")
            return False
        report = preflight(self.state.compiled, self.state.files, self.state.indexes)
        self.log.appendPlainText(report.text())
        return report.ok

    def start_run(self):
        if self.worker is not None:
            return
        if not self.check():
            if QMessageBox.question(self, "Run", "The check found problems (see the log). Run anyway?") \
                    != QMessageBox.Yes:
                return
            if self.state.compiled is None:
                return
        self.settings.setValue("run/output_dir", self.out_dir.text())
        self.settings.setValue("run/workers", self.workers.value())
        self.log.appendPlainText("— run started —")
        self.worker = RunWorker(self.state.resolved, list(self.state.files), self.out_dir.text(),
                                self.workers.value(), base_dir=self.state.base_dir,
                                indexes=dict(self.state.indexes), parent=self)
        self.worker.progress.connect(self.on_progress)
        self.worker.log.connect(self.log.appendPlainText)
        self.worker.finished_ok.connect(self.on_done)
        self.worker.failed.connect(self.on_failed)
        self.worker.finished.connect(self._worker_finished)
        self.run_btn.setEnabled(False)
        self.cancel_btn.setEnabled(True)
        self.worker.start()

    def cancel(self):
        if self.worker:
            self.worker.cancel()
            self.log.appendPlainText("Cancelling after the current file…")

    def on_progress(self, stage, done, total, message):
        self.progress.setMaximum(max(total, 1))
        self.progress.setValue(done)
        self.progress.setFormat(f"{stage}: {message}")
        self.log.appendPlainText(message)

    def on_done(self, result):
        self.result = result
        ok = result.n_ok
        self.log.appendPlainText(f"Finished: {ok}/{len(result.outcomes)} file(s) OK"
                                 + (" (cancelled)" if result.cancelled else "") + f"\nExcel: {result.excel}")
        for b in (self.open_excel, self.open_folder):
            b.setEnabled(True)
        self.open_dash.setEnabled(bool(result.dashboard))

    def on_failed(self, msg):
        self.log.appendPlainText("Run failed: " + msg)
        QMessageBox.warning(self, "Run failed", msg.splitlines()[0])

    def _worker_finished(self):
        self.worker = None
        self.run_btn.setEnabled(True)
        self.cancel_btn.setEnabled(False)

    def _open(self, path):
        if not path:
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def shutdown(self):
        if self.worker:
            self.worker.cancel()
            self.worker.wait(10000)
