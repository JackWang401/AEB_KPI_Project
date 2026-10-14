"""KPI Studio main window: Files -> Signals -> Events & KPIs -> Run."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSettings, Qt
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QDockWidget,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QSplitter,
    QTabWidget,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from src.kpi_studio import __version__, toolbox
from src.kpi_studio.core.validate import ProfileError
from src.kpi_studio.gui.pages.files_page import FilesPage
from src.kpi_studio.gui.pages.kpi_page import KpiPage
from src.kpi_studio.gui.pages.run_page import RunPage
from src.kpi_studio.gui.pages.signals_page import SignalsPage
from src.kpi_studio.gui.state import AppState
from src.kpi_studio.gui.widgets.common import PAGE_MARGINS, ExprEdit
from src.kpi_studio.gui.widgets.signal_viewer import SignalViewer
from src.kpi_studio.io import profile_store

# Used with the Fusion style (set in app.py) so the window looks the same on macOS, Windows and Linux.
STYLE = """
QWidget { font-size: 13px; color: #1d1d1f; }
QMainWindow, QTabWidget::pane, QStackedWidget { background: #f5f5f7; }
QTabWidget::pane { border: none; border-top: 1px solid #e5e5ea; }
QTabWidget::tab-bar { alignment: left; left: 12px; }
QTabBar::tab { padding: 10px 20px; font-weight: 600; color: #6e6e73; background: transparent;
               border: none; border-bottom: 2px solid transparent; }
QTabBar::tab:selected { color: #007aff; border-bottom: 2px solid #007aff; }
QTabBar::tab:hover { color: #1d1d1f; }
QPushButton { padding: 5px 14px; border: 1px solid #d2d2d7; border-radius: 7px; background: #ffffff; }
QPushButton:hover { background: #f2f6fd; }
QPushButton:pressed { background: #e6eefb; }
QPushButton:disabled { color: #aeaeb2; }
QPushButton#primary { background: #007aff; color: white; border: none; font-weight: 600; padding: 7px 22px; }
QPushButton#primary:hover { background: #0068d9; }
QPushButton#primary:disabled { background: #a6cdfb; }
QLineEdit, QComboBox, QDoubleSpinBox, QSpinBox, QPlainTextEdit, QTextEdit {
    min-height: 24px; padding: 2px 8px; background: #ffffff;
    border: 1px solid #d2d2d7; border-radius: 6px; }
QLineEdit:focus, QComboBox:focus, QDoubleSpinBox:focus, QSpinBox:focus, QPlainTextEdit:focus, QTextEdit:focus {
    border: 1px solid #007aff; }
QListWidget, QTableWidget, QTableView, QTreeWidget, QTreeView {
    background: #ffffff; border: 1px solid #e5e5ea; border-radius: 8px; outline: none;
    alternate-background-color: #fafafc; selection-background-color: #007aff; selection-color: white; }
QListWidget::item { padding: 5px 8px; border-radius: 5px; }
QListWidget::item:selected { background: #007aff; color: white; }
QGroupBox { font-weight: 600; background: #ffffff; border: 1px solid #e5e5ea; border-radius: 10px;
            margin-top: 22px; padding: 10px 8px 8px 8px; }
QGroupBox::title { subcontrol-origin: margin; subcontrol-position: top left; left: 4px; padding: 0 0 4px 0;
                   color: #1d1d1f; }
QLabel { background: transparent; }
QLabel#hint { color: #6e6e73; }
QLabel#sectionTitle { font-weight: 700; }
QToolBar { spacing: 6px; padding: 6px 12px; background: #f5f5f7; border: none; border-bottom: 1px solid #e5e5ea; }
QToolBar QToolButton { padding: 4px 10px; border-radius: 6px; }
QToolBar QToolButton:hover { background: #e8e8ed; }
QHeaderView::section { padding: 6px 8px; background: #fafafc; border: none; border-bottom: 1px solid #e5e5ea;
                       color: #6e6e73; font-weight: 600; }
QSplitter::handle { background: transparent; }
QSplitter::handle:horizontal { width: 14px; }
QSplitter::handle:vertical { height: 12px; }
QDockWidget { font-weight: 600; }
QDockWidget::title { padding: 8px 20px; }
"""


class MainWindow(QMainWindow):
    SETTINGS_ORG = "JackWang401"
    SETTINGS_APP = "KPI_Studio"

    def __init__(self, settings: QSettings | None = None, parent=None):
        super().__init__(parent)
        self.settings = settings or QSettings(self.SETTINGS_ORG, self.SETTINGS_APP)
        if self.settings.value("tools/user_enabled", False, type=bool):
            folder = self.settings.value("tools/user_folder", "")
            if folder:
                toolbox.load_user_tools(folder)
        self.state = AppState(self)
        self.setStyleSheet(STYLE)
        self.resize(1400, 900)

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.files_page = FilesPage(self.state, self.settings)
        # one signal panel shared by tab 2 (signals) and tab 3 (event / KPI preview), shown below the tabs
        self.viewer = SignalViewer(self.state)
        self.signals_page = SignalsPage(self.state, viewer=self.viewer)
        self.kpi_page = KpiPage(self.state, viewer=self.viewer)
        self.run_page = RunPage(self.state, self.settings)
        self.tabs.addTab(self.files_page, "1  Files")
        self.tabs.addTab(self.signals_page, "2  Signals")
        self.tabs.addTab(self.kpi_page, "3  Events && KPIs")
        self.tabs.addTab(self.run_page, "4  Plots && Run")
        self.viewer_box = QGroupBox("Signal viewer && preview")
        vb = QVBoxLayout(self.viewer_box)
        vb.setContentsMargins(10, 10, 10, 10)
        vb.addWidget(self.viewer)
        # the viewer sits in its own padded frame so it lines up with the page margins above
        self.viewer_wrap = QWidget()
        wl = QVBoxLayout(self.viewer_wrap)
        wl.setContentsMargins(PAGE_MARGINS[0], 0, PAGE_MARGINS[2], PAGE_MARGINS[3])
        wl.addWidget(self.viewer_box)
        self.central_split = QSplitter(Qt.Vertical)
        self.central_split.setChildrenCollapsible(False)
        self.central_split.addWidget(self.tabs)
        self.tabs.setMinimumHeight(150)  # the tabs give way to the plots when the window is small
        self.central_split.addWidget(self.viewer_wrap)
        self.central_split.setStretchFactor(0, 1)
        self.central_split.setStretchFactor(1, 1)
        self.central_split.setSizes([430, 470])
        self.setCentralWidget(self.central_split)
        self.tabs.currentChanged.connect(self._tab_changed)
        # the viewer plots the recording of the file selected on the Files tab
        self.files_page.table.currentCellChanged.connect(
            lambda row, *_: self.viewer.show_recording_of(self.state.files[row])
            if 0 <= row < len(self.state.files) else None)
        self._tab_changed(self.tabs.currentIndex())

        self._build_toolbar()
        self._build_issues_dock()
        self.state.dirty_changed.connect(lambda *_: self._update_title())
        self.state.validated.connect(self._show_issues)
        self.state.validated.connect(self._recheck_expressions)
        self.state.profile_changed.connect(self._load_name)

        last = self.settings.value("profile/last", "")
        if last and Path(last).exists():
            self.open_profile(last, quiet=True)
        else:
            self.state.new()
        self._update_title()

    # ------------------------------------------------------------------ chrome
    def _build_toolbar(self):
        tb = QToolBar("Profile")
        tb.setMovable(False)
        self.addToolBar(tb)
        for text, slot, key in (("New", self.new_profile, QKeySequence.New),
                                ("Open…", self.pick_profile, QKeySequence.Open),
                                ("Save", self.save_profile, QKeySequence.Save),
                                ("Save as…", self.save_profile_as, QKeySequence.SaveAs)):
            act = QAction(text, self)
            act.setShortcut(key)
            act.triggered.connect(slot)
            tb.addAction(act)
        examples = QAction("Open example", self)
        examples.triggered.connect(self.open_example)
        tb.addAction(examples)
        user_tools = QAction("User tools…", self)
        user_tools.setToolTip("Load your own @tool functions from a folder of .py files")
        user_tools.triggered.connect(self.pick_user_tools)
        tb.addAction(user_tools)
        tb.addSeparator()
        box = QWidget()
        lay = QHBoxLayout(box)
        lay.setContentsMargins(12, 0, 12, 0)
        lay.setSpacing(8)
        lay.addWidget(QLabel("Profile name"))
        self.name_edit = QLineEdit()
        self.name_edit.setMinimumWidth(260)
        self.name_edit.editingFinished.connect(self._name_changed)
        lay.addWidget(self.name_edit)
        lay.addWidget(QLabel("Rate [Hz]"))
        self.rate_edit = QLineEdit()
        self.rate_edit.setMaximumWidth(70)
        self.rate_edit.editingFinished.connect(self._rate_changed)
        lay.addWidget(self.rate_edit)
        tb.addWidget(box)

    def _build_issues_dock(self):
        dock = QDockWidget("Profile check", self)
        dock.setObjectName("issues")
        self.issues = QListWidget()
        self.issues.setToolTip("Click a problem to go to the place it is about")
        self.issues.setCursor(Qt.PointingHandCursor)
        self.issues.setMinimumHeight(44)
        self.issues.itemClicked.connect(lambda item: self.go_to(item.data(Qt.UserRole) or ""))
        holder = QWidget()
        hl = QVBoxLayout(holder)
        hl.setContentsMargins(PAGE_MARGINS[0], 0, PAGE_MARGINS[2], PAGE_MARGINS[3])
        hl.addWidget(self.issues)
        dock.setWidget(holder)
        dock.setFeatures(QDockWidget.DockWidgetMovable | QDockWidget.DockWidgetFloatable)
        self.addDockWidget(Qt.BottomDockWidgetArea, dock)
        self.resizeDocks([dock], [80], Qt.Vertical)

    def _show_issues(self):
        self.issues.clear()
        if not self.state.issues:
            self.issues.addItem(QListWidgetItem("✓ Profile is valid"))
            return
        errors = [i for i in self.state.issues if i.severity == "error"]
        for i in errors + [i for i in self.state.issues if i.severity != "error"]:
            item = QListWidgetItem(("✖ " if i.severity == "error" else "⚠ ") + f"{i.where}: {i.message}")
            item.setForeground(Qt.red if i.severity == "error" else Qt.darkYellow)
            item.setData(Qt.UserRole, i.where)
            self.issues.addItem(item)

    def go_to(self, where: str):
        """Open the tab and item a Profile check message is about, e.g. 'kpis.stop_gap.at'."""
        parts = where.split(".")
        if len(parts) < 2:
            return
        section, name, field = parts[0], parts[1], (parts[2] if len(parts) > 2 else "")
        if section == "kpis":
            kpis = self.state.data.get("kpis", [])
            row = next((i for i, k in enumerate(kpis) if k.get("name") == name or k.get("use") == name), None)
            if row is None and name.isdigit() and int(name) < len(kpis):
                row = int(name)
            if row is None:
                return
            self.tabs.setCurrentWidget(self.kpi_page)
            self.kpi_page.kpis.setCurrentRow(row)
            form = self.kpi_page.kpi_form
            target = {"at": form.at, "duration": form.m1, "between": form.m1, "value": form.expr, "of": form.expr,
                      "formula": form.formula, "reduce": form.reducer, "event": form.event}.get(field)
            if target is not None and target.isVisible():
                target.setFocus()
        elif section == "events":
            self.tabs.setCurrentWidget(self.kpi_page)
            self.kpi_page.select_event(name)
        elif section in ("signals", "derived"):
            self.tabs.setCurrentWidget(self.signals_page)
            table = self.signals_page.sig_table if section == "signals" else self.signals_page.der_table
            for r in range(table.rowCount()):
                if table.item(r, 0) is not None and table.item(r, 0).text() == name:
                    table.setCurrentCell(r, 0)
                    table.scrollToItem(table.item(r, 0))
                    break
        elif section == "plots":
            self.tabs.setCurrentWidget(self.run_page)

    def _load_name(self):
        self.name_edit.setText(self.state.data.get("name", ""))
        self.rate_edit.setText(str((self.state.data.get("timebase") or {}).get("rate_hz", 100)))

    def _name_changed(self):
        if self.name_edit.text().strip() and self.name_edit.text().strip() != self.state.data.get("name"):
            self.state.data["name"] = self.name_edit.text().strip()
            self.state.edited()

    def _rate_changed(self):
        try:
            rate = float(self.rate_edit.text())
        except ValueError:
            self._load_name()
            return
        self.state.data.setdefault("timebase", {})["rate_hz"] = rate
        self.state.edited()

    def _update_title(self):
        name = self.state.path.name if self.state.path else "unsaved profile"
        self.setWindowTitle(f"KPI Studio {__version__} — {name}{' *' if self.state.dirty else ''}")

    def pick_user_tools(self):
        start = self.settings.value("tools/user_folder", "") or str(Path.home())
        folder = QFileDialog.getExistingDirectory(self, "Folder with your tool .py files", start)
        if not folder:
            return
        loaded = toolbox.load_user_tools(folder)
        self.settings.setValue("tools/user_folder", folder)
        self.settings.setValue("tools/user_enabled", True)
        self.kpi_page.palette.populate()
        self.state.validate_now()
        QMessageBox.information(self, "User tools",
                                f"Loaded {len(loaded)} file(s) from {folder}.\n"
                                "They load again automatically next time KPI Studio starts.")

    # ------------------------------------------------------------------ profile files
    def _confirm_discard(self) -> bool:
        if not self.state.dirty:
            return True
        r = QMessageBox.question(self, "Unsaved changes", "Save changes to the current profile first?",
                                 QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel)
        if r == QMessageBox.Save:
            return self.save_profile()
        return r == QMessageBox.Discard

    def new_profile(self):
        if self._confirm_discard():
            self.state.new()
            self._update_title()

    def pick_profile(self):
        if not self._confirm_discard():
            return
        start = str(self.state.base_dir or profile_store.builtin_profile_dir())
        path, _ = QFileDialog.getOpenFileName(self, "Open profile", start, "KPI profile (*.kpi.yaml *.yaml)")
        if path:
            self.open_profile(path)

    def open_example(self):
        if self._confirm_discard():
            self.open_profile(profile_store.builtin_profile_dir() / "aeb_basic.kpi.yaml")
            self.state.path = None  # save-as to keep the example untouched
            self._update_title()

    def open_profile(self, path, quiet=False):
        try:
            self.state.load(path)
            if self.state.load_error and not quiet:
                QMessageBox.information(self, "Open profile", "Opened an unfinished profile (draft). "
                                        "Fix these to run it:\n\n" + self.state.load_error)
        except ProfileError as exc:
            if not quiet:
                QMessageBox.warning(self, "Open profile", str(exc))
            self.state.new()
            return
        except Exception as exc:  # noqa: BLE001
            if not quiet:
                QMessageBox.warning(self, "Open profile", f"{type(exc).__name__}: {exc}")
            self.state.new()
            return
        self.settings.setValue("profile/last", str(path))
        self._update_title()

    def save_profile(self) -> bool:
        if self.state.path is None:
            return self.save_profile_as()
        return self._save(self.state.path)

    def save_profile_as(self) -> bool:
        start = str(self.state.base_dir or Path.home())
        default = self.state.data.get("name", "profile").replace(" ", "_") + profile_store.PROFILE_SUFFIX
        path, _ = QFileDialog.getSaveFileName(self, "Save profile", str(Path(start) / default),
                                              "KPI profile (*.kpi.yaml)")
        if not path:
            return False
        if not path.endswith(".yaml"):
            path += profile_store.PROFILE_SUFFIX
        return self._save(Path(path))

    def _save(self, path: Path) -> bool:
        try:
            self.state.save(path)
        except ProfileError as exc:
            box = QMessageBox(QMessageBox.Warning, "Save profile",
                              "The profile is not finished yet:\n\n" + str(exc) +
                              "\n\nSave it as a draft? You can reopen it and keep editing; "
                              "Run stays blocked until these are fixed.", parent=self)
            draft = box.addButton("Save draft", QMessageBox.AcceptRole)
            box.addButton(QMessageBox.Cancel)
            box.setDefaultButton(draft)
            box.exec()
            if box.clickedButton() is not draft:
                return False
            self.state.save_draft(path)
        self.settings.setValue("profile/last", str(path))
        self._update_title()
        return True

    def _recheck_expressions(self):
        """Fields are checked as you type; re-check them once names may have changed (renames, imports)."""
        for edit in self.findChildren(ExprEdit):
            edit.check()

    VIEWER_TABS = (1, 2)  # Signals, Events & KPIs

    def _tab_changed(self, index: int):
        self.viewer_wrap.setVisible(index in self.VIEWER_TABS)

    def closeEvent(self, event):
        if not self._confirm_discard():
            event.ignore()
            return
        self.files_page.shutdown()
        self.viewer.shutdown()
        self.run_page.shutdown()
        event.accept()
