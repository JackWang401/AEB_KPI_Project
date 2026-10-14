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


def test_viewer_cursors_read_signal_values(qtbot, window, mf4_dir):
    from types import SimpleNamespace

    import numpy as np

    _load(qtbot, window, mf4_dir)
    viewer = window.signals_page.viewer
    viewer.plot_signals(["egoSpeed"])
    qtbot.waitUntil(lambda: bool(viewer.cache) and viewer.worker is None, timeout=30000)
    ax = viewer.fig.axes[0]
    data = next(iter(viewer.cache.values()))
    t, y = data["time"], np.asarray(data["values"]["egoSpeed"], dtype=float)
    # click places cursor A, shift+click cursor B
    viewer._on_press(SimpleNamespace(inaxes=ax, xdata=t[10], button=1, key=None))
    viewer._on_release(None)
    viewer._on_press(SimpleNamespace(inaxes=ax, xdata=t[50], button=1, key="shift"))
    viewer._on_release(None)
    assert viewer.cursors == [t[10], t[50]]
    text = viewer.readouts()[ax][0]
    assert f"A {y[10]:.4g}" in text and f"B {y[50]:.4g}" in text and "Δ" in text
    assert "Δt" in viewer.cursor_info.text()
    assert len(ax.get_lines()) == 3  # signal + two cursor lines
    # cursors survive a redraw, and right-click clears them
    viewer.draw()
    assert len(viewer.fig.axes[0].get_lines()) == 3
    viewer._on_press(SimpleNamespace(inaxes=viewer.fig.axes[0], xdata=t[5], button=3, key=None))
    assert viewer.cursors == [None, None]
    assert viewer.value_at(t, y, t[-1] + 100) is None


def test_condition_builder_value_changes_and_moment(qtbot, window, mf4_dir):
    from src.kpi_studio.gui.widgets.common import ConditionBuilder

    rx = ConditionBuilder.row_expression
    assert rx("sig", "changes to", "3") == "rising(sig == 3)"
    assert rx("sig", "changes from", "FULL") == "falling(sig == FULL)"
    assert rx("sig", "changes to", "1, 2") == "rising(sig in (1, 2))"
    assert rx("sig", "changes from → to", "0 -> 3") == "(falling(sig == 0) and rising(sig == 3))"
    assert rx("sig", "changes from → to", "0") == ""
    dlg = ConditionBuilder(["sig", "other"])
    qtbot.addWidget(dlg)
    sig, op, val = dlg.rows[0]
    sig.setCurrentText("sig")
    op.setCurrentText("changes from → to")
    assert "from, to" in val.placeholderText()
    val.setText("0, 3")
    dlg.add_row()
    dlg.rows[1][0].setCurrentText("other")
    dlg.rows[1][1].setCurrentText(">")
    dlg.rows[1][2].setText("5")
    assert dlg.expression() == "(falling(sig == 0) and rising(sig == 3)) and other > 5"

    _load(qtbot, window, mf4_dir)
    window.state.data.setdefault("events", {})["changes"] = {"start": "egoSpeed > 1"}
    form = window.kpi_page.event_form
    form.load("changes")
    assert form.name is not None
    n = form.moments.rowCount()
    form.apply_moment_condition(None, "rising(egoSpeed > 5)")
    assert form.moments.rowCount() == n + 1
    moments = form.ev["moments"]
    new = list(moments)[-1]
    assert moments[new]["first"] == "rising(egoSpeed > 5)"


def test_moment_expression_signal_picker(qtbot, window, mf4_dir):
    from src.kpi_studio.gui.widgets.common import NamePicker

    _load(qtbot, window, mf4_dir)
    window.state.data.setdefault("events", {})["pick"] = {"start": "egoSpeed > 1",
                                                          "moments": {"m1": {"first": "", "after": "start"}}}
    form = window.kpi_page.event_form
    form.load("pick")
    cell = form.moments.cellWidget(0, 2)
    sections = cell.picker_sections()
    assert "egoSpeed" in sections["Signals"]
    assert sections["Moments"][:2] == ["start", "end"] and "m1" not in sections["Moments"]
    cell.open_picker()
    picker = cell.picker
    picker.filter.setText("ego*d")
    assert picker.names() == ["egoSpeed", "egoSpeedKph"]
    picker.filter.returnPressed.emit()  # Enter picks the first match
    assert cell.text() == "egoSpeed"
    assert form.ev["moments"]["m1"]["first"] == "egoSpeed"
    cell.setText("rising( == 3)")
    cell.setCursorPosition(len("rising("))
    cell.insert_name("longActAccel")
    assert cell.text() == "rising(longActAccel == 3)"
    menu = NamePicker({"Signals": ["a", "b"], "Moments": []})
    qtbot.addWidget(menu)
    assert menu.list.count() == 3  # one header + two names; empty sections are left out


def test_moment_rule_tooltips(qtbot, window):
    from src.kpi_studio.gui.pages.kpi_page import EventForm

    assert "First time the condition is true" in EventForm.rule_help("first")
    assert EventForm.rule_help("offset").startswith("offset")


def test_moment_rule_options_and_build_action(qtbot, window, mf4_dir, monkeypatch):
    from src.kpi_studio.gui.widgets import common

    _load(qtbot, window, mf4_dir)
    window.state.data.setdefault("events", {})["vc"] = {
        "start": "egoSpeed > 1", "moments": {"m1": {"decel_onset": "longJerk", "after": "start", "threshold": -20}}}
    form = window.kpi_page.event_form
    form.load("vc")
    form.moments.cellWidget(0, 1).setCurrentText("crossing")
    m1 = form.ev["moments"]["m1"]
    assert m1["after"] == "start" and m1["level"] == 0.0 and "threshold" not in m1
    assert "crossing" in m1
    # the ▾ list offers the condition builder; picking it fills the cell and switches to 'first'
    monkeypatch.setattr(common.ConditionBuilder, "exec", lambda self: common.QDialog.Accepted)
    monkeypatch.setattr(common.ConditionBuilder, "expression", lambda self: "rising(egoSpeed == 3)")
    cell = form.moments.cellWidget(0, 2)
    cell.open_picker()
    first = cell.picker.list.item(0)
    assert first.text().startswith("➜ Build condition")
    cell.picker._pick(first)
    m1 = form.ev["moments"]["m1"]
    assert m1.get("first") == "rising(egoSpeed == 3)" and "level" not in m1 and m1["after"] == "start"


def test_unfinished_profile_saves_as_draft_and_reopens(qtbot, window, tmp_path):
    state = window.state
    state.data["kpis"] = [{"name": "kpi2", "event": "missing_event"}]  # no value/duration/... yet
    path = tmp_path / "draft.kpi.yaml"
    import pytest

    from src.kpi_studio.core.validate import ProfileError

    with pytest.raises(ProfileError):
        state.save(path)
    state.save_draft(path)
    assert path.read_text().startswith("# Draft") and not state.dirty
    window.open_profile(path, quiet=True)
    assert state.load_error and "kpi2" in state.load_error
    assert state.data["kpis"][0]["name"] == "kpi2" and state.compiled is None


def test_plot_axes_dropdowns_and_ranges(qtbot, window, mf4_dir, tmp_path):
    from src.kpi_studio.gui.pages.run_page import AxesDialog

    _load(qtbot, window, mf4_dir)
    page = window.run_page
    window.state.data["plots"] = []
    page.new_plot()
    kpis = window.state.kpi_names()
    assert len(kpis) >= 2
    x, y = page.plots.cellWidget(0, 2), page.plots.cellWidget(0, 3)
    assert kpis[0] in [x.itemText(i) for i in range(x.count())]
    x.setCurrentText(kpis[1])
    y.setCurrentText(kpis[0])
    plot = window.state.data["plots"][0]
    assert plot["x"] == kpis[1] and plot["y"] == kpis[0]
    page.plots.cellWidget(0, 4).setCurrentText("event")
    assert plot["color_by"] == "event"

    dlg = AxesDialog(plot)
    qtbot.addWidget(dlg)
    assert not hasattr(dlg, "x_label")  # titles come from the KPIs
    dlg.ranges["y_range"][0].setText("0")
    dlg.ranges["y_range"][1].setText("50")
    dlg.ranges["x_range"][0].setText("5")  # max left empty: fits the data
    page.apply_axes(0, dlg.values())
    assert plot["y_range"] == [0.0, 50.0] and plot["x_range"] == [5.0, None]
    dlg.ranges["x_range"][1].setText("1")
    import pytest

    with pytest.raises(ValueError, match="min must be below max"):
        dlg.values()
    dlg.ranges["x_range"][1].setText("abc")
    with pytest.raises(ValueError, match="not a number"):
        dlg.values()
    window.state.validate_now()
    assert window.state.compiled is not None, [str(i) for i in window.state.issues]


def test_plot_axes_applied_to_figure():
    import pandas as pd
    import pytest

    from src.kpi_studio.core.model import PlotDef
    from src.kpi_studio.report.plots import kpi_figure

    wide = pd.DataFrame({"file": ["f", "f"], "event": ["e", "e"], "occurrence": [1, 2],
                         "a": [1.0, 3.0], "b": [2.0, 4.0]})
    fig = kpi_figure(PlotDef(type="scatter", x="a", y="b", y_range=[0, 10], x_range=[None, 5]), wide, {}, ["f"])
    assert fig.layout.xaxis.title.text.startswith("a")  # title from the KPI
    assert list(fig.layout.yaxis.range) == [0, 10]
    lo, hi = fig.layout.xaxis.range
    assert hi == 5 and lo < 1.0  # min fitted to the data with a margin
    with pytest.raises(ValueError):
        PlotDef(type="scatter", x="a", y="b", x_range=[5, 1])


def test_flat_kpi_axis_is_widened():
    import pandas as pd

    from src.kpi_studio.core.model import PlotDef
    from src.kpi_studio.report.plots import kpi_figure

    wide = pd.DataFrame({"file": ["f", "f"], "event": ["e", "e"], "occurrence": [1, 2],
                         "a": [40.0, 60.0], "b": [0.11, 0.11 + 1e-15]})
    fig = kpi_figure(PlotDef(type="scatter", x="a", y="b"), wide, {}, ["f"])
    lo, hi = fig.layout.yaxis.range
    assert lo < 0.11 < hi and hi - lo >= 0.9  # not zoomed into float noise
    assert fig.layout.xaxis.range is None     # a normal spread stays automatic
