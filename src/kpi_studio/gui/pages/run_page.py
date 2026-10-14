"""Step 4: plots, preflight, run, open results."""

from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import QSettings, QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
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
from src.kpi_studio.gui.widgets.common import PAGE_MARGINS
from src.kpi_studio.gui.workers import RunWorker

PLOT_TYPES = ["scatter", "box", "bar", "histogram", "timeseries"]
COLOR_BY = ["file", "event", "none"]
GROUP_BY = ["file", "event"]


class AxesDialog(QDialog):
    """Axis ranges of one plot. Titles come from the X / Y KPIs; an empty min or max adapts to the data."""

    def __init__(self, plot: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Axis ranges")
        form = QFormLayout(self)
        self.ranges = {}
        hint = QLabel("Leave min or max empty to fit the data.")
        hint.setObjectName("hint")
        form.addRow(hint)
        for key, axis in (("x_range", "x"), ("y_range", "y")):
            label = f"{axis.upper()}: {plot.get(axis) or '(no KPI)'}"
            lo, hi = (plot.get(key) or [None, None])
            row = QHBoxLayout()
            edits = (QLineEdit("" if lo is None else str(lo)), QLineEdit("" if hi is None else str(hi)))
            edits[0].setPlaceholderText("min (auto)")
            edits[1].setPlaceholderText("max (auto)")
            if axis == "x" and plot.get("type") in ("box", "bar"):
                for e in edits:
                    e.setEnabled(False)
                    e.setPlaceholderText("n/a")
            row.addWidget(edits[0])
            row.addWidget(QLabel("to"))
            row.addWidget(edits[1])
            form.addRow(label, row)
            self.ranges[key] = edits
        self.error = QLabel()
        self.error.setStyleSheet("color: #e34948")
        form.addRow(self.error)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def values(self) -> dict:
        """{key: [min, max] or None}; an empty end is None. Raises ValueError for bad numbers."""
        out = {}
        for key, (lo, hi) in self.ranges.items():
            ends = []
            for e, what in ((lo, "min"), (hi, "max")):
                text = e.text().strip()
                try:
                    ends.append(float(text) if text else None)
                except ValueError:
                    raise ValueError(f"{key[0].upper()} {what}: '{text}' is not a number") from None
            if ends == [None, None]:
                out[key] = None
                continue
            if None not in ends and not ends[0] < ends[1]:
                raise ValueError(f"{key[0].upper()} range: min must be below max")
            out[key] = ends
        return out

    def _accept(self):
        try:
            self.values()
        except ValueError as exc:
            self.error.setText(str(exc))
            return
        self.accept()


class RunPage(QWidget):
    PLOT_COLS = ["Type", "Title", "X (KPI)", "Y (KPI)", "Colour / group by", "Event (timeseries)",
                 "Signals (timeseries)", "Axis ranges"]

    def __init__(self, state: AppState, settings: QSettings, parent=None):
        super().__init__(parent)
        self.state = state
        self.settings = settings
        self.worker: RunWorker | None = None
        self.result = None
        self._loading = False

        lay = QVBoxLayout(self)
        lay.setContentsMargins(*PAGE_MARGINS)
        lay.setSpacing(12)

        # ---- plots
        pbox = QGroupBox("Plots (written as interactive HTML next to the Excel file)")
        pl = QVBoxLayout(pbox)
        self.plots = QTableWidget(0, len(self.PLOT_COLS))
        self.plots.setHorizontalHeaderLabels(self.PLOT_COLS)
        self.plots.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.plots.horizontalHeader().setSectionResizeMode(6, QHeaderView.Stretch)
        for col, width in ((0, 110), (2, 190), (3, 190), (4, 130), (5, 170), (7, 100)):
            self.plots.setColumnWidth(col, width)
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
        orow.addWidget(QLabel("Output"))
        self.out_mode = QComboBox()
        self.out_mode.addItem("Next to the input data (replaced each run)", "input")
        self.out_mode.addItem("Custom folder (new timestamped folder each run)", "custom")
        self.out_mode.setCurrentIndex(0 if settings.value("run/output_mode", "input") == "input" else 1)
        orow.addWidget(self.out_mode)
        self.out_dir = QLineEdit(str(settings.value("run/output_dir", str(Path.home() / "kpi_studio_output"))))
        self.browse = browse = QPushButton("Browse…")
        orow.addWidget(self.out_dir, 1)
        orow.addWidget(browse)
        orow.addWidget(QLabel("Parallel files"))
        self.workers = QSpinBox()
        self.workers.setRange(1, max(os.cpu_count() or 1, 1))
        self.workers.setValue(int(settings.value("run/workers", default_workers())))
        orow.addWidget(self.workers)
        rl.addLayout(orow)
        self.out_hint = QLabel()
        self.out_hint.setObjectName("hint")
        self.out_hint.setWordWrap(True)
        rl.addWidget(self.out_hint)
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
        self.out_mode.currentIndexChanged.connect(lambda *_: self.update_output_hint())
        state.files_changed.connect(self.update_output_hint)
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
        self.update_output_hint()

    # ------------------------------------------------------------------ plots
    def refresh_plots(self):
        self._loading = True
        plots = self.state.data.get("plots", []) or []
        self.plots.setRowCount(len(plots))
        for r, p in enumerate(plots):
            combo = QComboBox()
            combo.addItems(PLOT_TYPES)
            combo.setCurrentText(p.get("type", "scatter"))
            combo.currentTextChanged.connect(lambda v, row=r: self._type_changed(row, v))
            self.plots.setCellWidget(r, 0, combo)
            group = p.get("color_by") if p.get("type") in ("scatter", "histogram") else p.get("group_by", "")
            kpis = self.state.kpi_names()
            events = self.state.event_names()
            ptype = p.get("type", "scatter")
            for c, v in ((1, p.get("title", "")), (6, ", ".join(p.get("signals", []) or []))):
                self.plots.setItem(r, c, QTableWidgetItem(str(v or "")))
            for c, key, choices in ((2, "x", kpis), (3, "y", kpis), (5, "event", events)):
                self.plots.setCellWidget(r, c, self._choice(r, key, choices, p.get(key, ""), editable=True))
            if ptype in ("scatter", "histogram"):
                gkey, gchoices = "color_by", COLOR_BY
            elif ptype in ("box", "bar"):
                gkey, gchoices = "group_by", GROUP_BY
            else:
                gkey, gchoices = None, []
            if gkey:
                self.plots.setCellWidget(r, 4, self._choice(r, gkey, gchoices, group or gchoices[0]))
            else:
                self.plots.removeCellWidget(r, 4)
                item = QTableWidgetItem("")
                self.plots.setItem(r, 4, item)
            axes = QPushButton("Ranges…")
            axes.setToolTip("Axis ranges (axis titles come from the X / Y KPIs)")
            axes.setEnabled(ptype != "timeseries")
            axes.clicked.connect(lambda *_, row=r: self.edit_axes(row))
            self.plots.setCellWidget(r, 7, axes)
        self._loading = False

    def _choice(self, row: int, key: str, choices: list[str], current: str, editable=False) -> QComboBox:
        combo = QComboBox()
        combo.setEditable(editable)
        if editable:
            combo.addItem("")
        combo.addItems(choices)
        if current and combo.findText(current) < 0:
            combo.addItem(current)  # a name that does not exist (yet) stays visible
        combo.setCurrentText(current or "")
        combo.currentTextChanged.connect(lambda v, k=key: self._set_plot(row, k, v.strip()))
        return combo

    def edit_axes(self, row: int):
        plot = self.state.data["plots"][row]
        dlg = AxesDialog(plot, self)
        if dlg.exec() != QDialog.Accepted:
            return
        self.apply_axes(row, dlg.values())

    def apply_axes(self, row: int, values: dict):
        plot = self.state.data["plots"][row]
        plot.pop("x_label", None)
        plot.pop("y_label", None)
        for k, v in values.items():
            if v is None:
                plot.pop(k, None)
            else:
                plot[k] = v
        self.state.edited()

    def showEvent(self, event):
        super().showEvent(event)
        self.refresh_plots()  # KPI and event names may have changed on tab 3

    def _type_changed(self, row, value):
        self._set_plot(row, "type", value)
        QTimer.singleShot(0, self.refresh_plots)  # rebuild after this combo's signal returns

    def _set_plot(self, row, key, value):
        if self._loading:
            return
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
        kpis = self.state.kpi_names()
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
    def output_root(self):
        """None = next to the input data; else the custom folder."""
        return None if self.out_mode.currentData() == "input" else self.out_dir.text()

    def update_output_hint(self):
        custom = self.out_mode.currentData() == "custom"
        self.out_dir.setEnabled(custom)
        self.browse.setEnabled(custom)
        if custom:
            self.out_hint.setText("Each run writes a new folder named after the profile and the time.")
            return
        from src.kpi_studio.app.run_service import input_results_dir

        if not self.state.files:
            self.out_hint.setText("Results go to a 'kpi_results' folder next to the input files.")
            return
        target = input_results_dir(self.state.files)
        self.out_hint.setText(f"Results go to {target}"
                              + (" (replaces the existing result)" if target.exists() else ""))

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
        self.settings.setValue("run/output_mode", self.out_mode.currentData())
        self.settings.setValue("run/workers", self.workers.value())
        self.log.appendPlainText("— run started —")
        self.worker = RunWorker(self.state.resolved, list(self.state.files), self.output_root(),
                                self.workers.value(), base_dir=self.state.base_dir,
                                indexes=dict(self.state.indexes),
                                offsets=dict(self.state.offsets), parent=self)
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
        self.log.appendPlainText(f"Finished: {ok}/{len(result.outcomes)} recording(s) OK"
                                 + (" (cancelled; the previous result was kept)" if result.cancelled and not result.excel
                                    else " (cancelled)" if result.cancelled else "")
                                 + (f"\nExcel: {result.excel}" if result.excel else ""))
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
