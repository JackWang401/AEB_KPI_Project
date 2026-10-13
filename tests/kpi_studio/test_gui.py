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


def test_user_tools_button_loads_folder(window, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QFileDialog, QMessageBox
    from src.kpi_studio import toolbox

    (tmp_path / "my_tools.py").write_text(
        "from src.kpi_studio.toolbox.registry import tool\n"
        "@tool('signal', 'Math')\n"
        "def gui_double(ctx, x):\n"
        "    '''Twice the input.'''\n"
        "    return 2 * x\n")
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *a, **k: str(tmp_path))
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)
    window.pick_user_tools()
    assert toolbox.get("signal", "gui_double").origin == "user"
    assert window.settings.value("tools/user_enabled", False, type=bool)


def test_signal_viewer_works_while_an_event_is_incomplete(qtbot, window, mf4_dir):
    _load(qtbot, window, mf4_dir)
    window.state.data["events"]["unfinished"] = {"window": {"pre": 1, "post": 1}}  # no start yet
    window.state.edited(structure=True)
    window.state.validate_now()
    assert window.state.compiled is None
    viewer = window.signals_page.viewer
    viewer.plot_signals(["egoSpeed", "longJerk"])  # a raw and a derived signal
    qtbot.waitUntil(lambda: viewer.data is not None and viewer.worker is None, timeout=20000)
    assert {"egoSpeed", "longJerk"} <= set(viewer.data["values"])
    assert len(window.signals_page.viewer.fig.axes) == 2


def test_preview_shows_whole_file_when_event_never_fires(qtbot, window, mf4_dir):
    _load(qtbot, window, mf4_dir)
    window.state.data["events"]["never"] = {"start": "egoSpeedKph > 1000", "window": {"pre": 1, "post": 1}}
    window.state.edited(structure=True)
    window.state.validate_now()
    page = window.kpi_page
    page.refresh_preview_choices()
    page.pv_event.setCurrentText("never")
    page.run_preview()
    qtbot.waitUntil(lambda: page.preview_data is not None, timeout=20000)
    assert page.fig.axes and "Whole file" in page.fig.axes[0].get_title()


def test_files_of_one_recording_are_combined(qtbot, window, tmp_path):
    from tests.kpi_studio.test_recordings import _split_recording

    files = _split_recording(tmp_path)
    window.files_page.add_files(files)
    qtbot.waitUntil(lambda: len(window.state.indexes) == 2, timeout=10000)
    window.open_profile(profile_store.builtin_profile_dir() / "aeb_basic.kpi.yaml")
    assert len(window.state.recordings()) == 1
    assert window.files_page.table.item(0, 1).text() == "AADT-1_002"
    viewer = window.signals_page.viewer
    viewer.plot_signals(["egoSpeed", "aebTargetDecel"])  # one from each file
    qtbot.waitUntil(lambda: viewer.data is not None and viewer.worker is None, timeout=20000)
    assert {"egoSpeed", "aebTargetDecel"} <= set(viewer.data["values"])
    window.files_page.combine.setChecked(False)
    assert len(window.state.recordings()) == 2


def test_new_signals_reach_tab3_while_an_event_is_unfinished(qtbot, window, mf4_dir):
    _load(qtbot, window, mf4_dir)
    page = window.kpi_page
    page.new_event()  # no trigger yet -> profile has an error
    window.state.validate_now()
    assert window.state.compiled is None
    window.state.data["signals"]["myNewSignal"] = {"channels": ["speedESC"]}
    window.state.edited(structure=True)
    window.state.validate_now()
    assert "myNewSignal" in window.state.signal_names()
    pv = [page.pv_signals.item(i).text() for i in range(page.pv_signals.count())]
    assert "myNewSignal" in pv
    top = page.palette.tree.topLevelItem(0)
    assert top.text(0).startswith("Signals")
    assert "myNewSignal" in [top.child(i).text(0) for i in range(top.childCount())]
    # double-clicking a signal in the toolbox fills the event's start field
    item = next(top.child(i) for i in range(top.childCount()) if top.child(i).text(0) == "myNewSignal")
    page.palette._insert(item)
    assert "myNewSignal" in page.event_form.start.text()


def test_wildcard_search():
    from src.kpi_studio.gui.widgets.common import matches

    assert matches("lmc*it", "Group::LmcAxTrgtLimit")
    assert matches("veh*sp?ed", "VehicleSpeedQF")
    assert matches("SPEED", "vehicleSpeed") and matches("", "x")
    assert not matches("veh*xyz", "VehicleSpeed")


def test_channel_filter_and_viewer_overlay_two_recordings(qtbot, window, mf4_dir):
    _load(qtbot, window, mf4_dir)
    sp = window.signals_page
    sp.search.setText("acc*[0]")
    shown = [sp.tree.topLevelItem(i).child(j).text(0) for i in range(sp.tree.topLevelItemCount())
             for j in range(sp.tree.topLevelItem(i).childCount())]
    assert shown == ["acceleration[0]"]
    viewer = sp.viewer
    viewer.select_recordings([r.name for r in window.state.recordings()])
    viewer.plot_signals(["egoSpeed"])
    qtbot.waitUntil(lambda: len(viewer.cache) == 2 and viewer.worker is None, timeout=30000)
    lines = viewer.fig.axes[0].get_lines()
    assert len(lines) == 2  # one line per recording, overlaid
    assert viewer.fig.axes[0].get_legend() is not None
