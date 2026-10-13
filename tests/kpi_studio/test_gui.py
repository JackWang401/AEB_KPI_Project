"""GUI smoke tests (offscreen): load files and a profile, preview, run."""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("pytestqt")

from PySide6.QtCore import QSettings  # noqa: E402

from src.kpi_studio.gui.main_window import MainWindow  # noqa: E402
from src.kpi_studio.io import profile_store  # noqa: E402


@pytest.fixture
def window(qtbot, tmp_path):
    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.IniFormat)
    w = MainWindow(settings=settings)
    w._confirm_discard = lambda: True  # no modal "save changes?" prompt during tests
    qtbot.addWidget(w)
    yield w


def _load(qtbot, w, mf4_dir):
    folder, _ = mf4_dir
    w.files_page.add_files([str(folder / "a.mf4"), str(folder / "b.mf4")])
    qtbot.waitUntil(lambda: len(w.state.indexes) == 2, timeout=10000)
    w.open_profile(profile_store.builtin_profile_dir() / "aeb_basic.kpi.yaml")


def test_load_profile_and_files(qtbot, window, mf4_dir):
    _load(qtbot, window, mf4_dir)
    assert window.state.compiled is not None, [str(i) for i in window.state.issues]
    assert window.signals_page.tree.topLevelItemCount() > 0
    assert window.signals_page.sig_table.rowCount() >= 9  # library signals shown
    assert window.kpi_page.kpis.count() == 2  # use: aeb_core.* + peakDecel


def test_edit_event_and_kpi_validates(qtbot, window, mf4_dir):
    _load(qtbot, window, mf4_dir)
    page = window.kpi_page
    page.new_event()
    ev = window.state.data["events"]
    name = next(iter(ev))
    page.event_form.start.setText("egoSpeed > 1000")
    page.event_form.start.editingFinished.emit()
    page.new_kpi()
    k = window.state.data["kpis"][-1]
    k.update(event=name, value="egoSpeedKph", at="start")
    window.state.edited(structure=True)
    window.state.validate_now()
    assert window.state.compiled is not None, [str(i) for i in window.state.issues]
    page.event_form.load(name)
    page.event_form.start.setText("egoSpeed >")
    assert not page.event_form.start.check()


def test_preview_and_run(qtbot, window, mf4_dir, tmp_path):
    _load(qtbot, window, mf4_dir)
    page = window.kpi_page
    page.refresh_preview_choices()
    page.pv_event.setCurrentText("aeb_core.aeb")
    page.run_preview()
    qtbot.waitUntil(lambda: page.preview_data is not None, timeout=20000)
    assert page.pv_table.rowCount() > 10
    run = window.run_page
    run.out_dir.setText(str(tmp_path / "out"))
    run.workers.setValue(1)
    assert run.check()
    run.start_run()
    qtbot.waitUntil(lambda: run.result is not None, timeout=60000)
    assert run.result.n_ok == 2 and run.result.excel.exists()


def test_save_and_reopen(qtbot, window, mf4_dir, tmp_path):
    _load(qtbot, window, mf4_dir)
    target = tmp_path / "mine.kpi.yaml"
    assert window._save(target)
    window.open_profile(target)
    assert window.state.compiled is not None
