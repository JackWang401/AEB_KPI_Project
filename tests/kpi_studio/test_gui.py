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
    assert window.kpi_page.kpis.item(1).text() == "aeb_core.aeb.peakDecel"  # event.kpi
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
    viewer = window.viewer
    assert window.kpi_page.viewer is viewer and window.signals_page.viewer is viewer
    viewer.set_event("aeb_core.aeb")
    viewer.run_preview()
    qtbot.waitUntil(lambda: bool(viewer.preview is not None and viewer.preview_worker is None
                                 and viewer.worker is None), timeout=20000)
    assert viewer.kpi_table.rowCount() > 10
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
    viewer = window.viewer
    viewer.set_event("never")
    viewer.run_preview()
    qtbot.waitUntil(lambda: bool(viewer.preview is not None and viewer.preview_worker is None
                                 and viewer.worker is None), timeout=20000)
    assert viewer.fig.axes and "Whole file" in viewer.fig.axes[0].get_title()


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
    pv = [window.viewer.list.item(i).text() for i in range(window.viewer.list.count())]
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
    ax = viewer.fig.axes[0]
    labels = [lab for lab, *_ in viewer._legend[ax]]  # side list names each recording
    assert len(labels) == 2 and all(" · " in lab for lab in labels)
    texts = [t.get_text() for t in ax.texts]
    assert any(lab in t for lab in labels for t in texts)


def test_viewer_time_offset_shifts_one_recording(qtbot, window, mf4_dir):
    import numpy as np

    _load(qtbot, window, mf4_dir)
    viewer = window.signals_page.viewer
    recs = window.state.recordings()
    assert not viewer.files.isVisible()  # no recordings list in the panel; a compact picker instead
    viewer.rec_combo.setCurrentIndex(1)
    assert viewer.selected_names() == [recs[1].name]
    viewer.select_recordings([r.name for r in recs])
    viewer.plot_signals(["egoSpeed"])
    qtbot.waitUntil(lambda: len(viewer.cache) == 2 and viewer.worker is None, timeout=30000)
    second = recs[1].name
    t0 = np.asarray(viewer.cache[second]["time"], dtype=float)
    window.files_page.table.item(1, 2).setText("1.5")  # Offset [s] on the Files tab
    assert window.state.offsets == {recs[1].files[0]: 1.5}
    assert window.state.recordings()[1].offsets == {recs[1].files[0]: 1.5}  # what the KPI run reads
    assert "+1.500 s" in viewer.rec_combo.itemText(1) and "s)" not in viewer.rec_combo.itemText(0)
    assert window.files_page.table.item(1, 2).text() == "+1.500"
    # the shifted recording is read again with the offset applied
    qtbot.waitUntil(lambda: second in viewer.cache and viewer.worker is None, timeout=30000)
    assert np.allclose(np.asarray(viewer.cache[second]["time"], dtype=float)[:5], t0[:5] + 1.5)
    window.files_page.table.item(1, 2).setText("0")  # zero removes the shift
    assert window.state.offsets == {} and "s)" not in viewer.rec_combo.itemText(1)
    window.files_page.table.item(0, 2).setText("abc")  # invalid input is ignored
    assert window.state.offsets == {} and window.files_page.table.item(0, 2).text() == "0"


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


def test_signal_panel_is_shared_by_tabs_2_and_3(window):
    window.show()
    for index, visible in [(0, False), (1, True), (2, True), (3, False)]:
        window.tabs.setCurrentIndex(index)
        assert window.viewer_box.isVisible() == visible


def test_derived_unit_and_description_columns(qtbot, window, mf4_dir):
    _load(qtbot, window, mf4_dir)
    page = window.signals_page
    window.state.data.setdefault("derived", {})["vKph"] = "egoSpeed * 3.6"
    window.state.edited(structure=True)
    window.state.validate_now()
    row = next(r for r in range(page.der_table.rowCount()) if page.der_table.item(r, 0).text() == "vKph")
    page.der_table.item(row, 2).setText("km/h")
    page.der_table.item(row, 3).setText("ego speed")
    assert window.state.data["derived"]["vKph"] == {"expr": "egoSpeed * 3.6", "unit": "km/h",
                                                    "description": "ego speed"}
    window.state.validate_now()
    assert window.state.compiled is not None
    viewer = window.viewer
    viewer.plot_signals(["vKph"])
    qtbot.waitUntil(lambda: bool(viewer.data is not None and viewer.worker is None), timeout=20000)
    assert viewer.data["units"]["vKph"] == "km/h"


def test_add_derived_through_the_builder(qtbot, window, mf4_dir, monkeypatch):
    from src.kpi_studio.gui.widgets.derived_builder import DerivedBuilder

    _load(qtbot, window, mf4_dir)
    page = window.signals_page

    def fake_exec(dlg):
        dlg.name.setText("vKph")
        dlg.signal.setCurrentText("egoSpeed")
        dlg.set_method("scale")
        dlg.arg_widgets["factor"].setText("3.6")
        dlg.unit.setText("km/h")
        return DerivedBuilder.Accepted

    monkeypatch.setattr(DerivedBuilder, "exec", fake_exec)
    page.add_derived()
    assert window.state.data["derived"]["vKph"] == {"expr": "egoSpeed * 3.6", "unit": "km/h"}
    window.state.validate_now()
    assert "vKph" in {n for n, _ in window.state.compiled.derived}


def test_renaming_a_signal_updates_events_and_kpis(qtbot, window, mf4_dir):
    _load(qtbot, window, mf4_dir)
    state = window.state
    state.data["signals"]["motType"] = {"channels": ["speedESC"]}
    state.data["events"]["served"] = {"start": "rising(motType == 3)", "end": "falling(motType == 3)",
                                      "moments": {"m1": {"first": "rising(motType == 3)"}}}
    state.data["kpis"].append({"name": "spd", "event": "served", "value": "motType", "at": "m1"})
    state.edited(structure=True)
    state.validate_now()
    page = window.signals_page
    row = next(r for r in range(page.sig_table.rowCount()) if page.sig_table.item(r, 0).text() == "motType")
    page.sig_table.item(row, 0).setText("motTypeSrvd")
    ev = state.data["events"]["served"]
    assert ev["start"] == "rising(motTypeSrvd == 3)" and ev["moments"]["m1"]["first"] == "rising(motTypeSrvd == 3)"
    assert state.data["kpis"][-1]["value"] == "motTypeSrvd"
    state.validate_now()
    assert state.compiled is not None, [str(i) for i in state.issues]


def test_rename_twice_quickly_and_no_ghost_rows(qtbot, window, mf4_dir, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: warnings.append(a[2]))
    _load(qtbot, window, mf4_dir)
    state = window.state
    state.data["signals"]["LmcMotTypeServedLong"] = {"channels": ["speedESC"]}
    state.data["events"]["served"] = {"start": "rising(LmcMotTypeServedLong == 3)"}
    state.edited(structure=True)
    state.validate_now()
    page = window.signals_page

    def rename(old, new):
        row = next(r for r in range(page.sig_table.rowCount()) if page.sig_table.item(r, 0).text() == old)
        page.sig_table.item(row, 0).setText(new)

    rename("LmcMotTypeServedLong", "MotTypeSrvdLong")  # no validation in between
    names = [page.sig_table.item(r, 0).text() for r in range(page.sig_table.rowCount())]
    assert "LmcMotTypeServedLong" not in names  # the old name does not linger as a library row
    rename("MotTypeSrvdLong", "LmcMotTypeSrvdLong")
    assert state.data["events"]["served"]["start"] == "rising(LmcMotTypeSrvdLong == 3)"
    rename("LmcMotTypeSrvdLong", "egoSpeed")  # taken by the vehicle library
    assert warnings and "LmcMotTypeSrvdLong" in state.data["signals"]


def test_event_fields_not_red_after_a_rename(qtbot, window, mf4_dir):
    _load(qtbot, window, mf4_dir)
    state = window.state
    state.data["signals"]["motType"] = {"channels": ["speedESC"]}
    state.data["events"]["served"] = {"start": "rising(motType == 3)", "end": "falling(motType == 3)"}
    state.edited(structure=True)
    state.validate_now()
    kp = window.kpi_page
    kp.select_event("served")
    assert kp.event_form.start.error is None
    page = window.signals_page
    row = next(r for r in range(page.sig_table.rowCount()) if page.sig_table.item(r, 0).text() == "motType")
    page.sig_table.item(row, 0).setText("MotTypSrvdLong")  # form reloads before the profile re-validates
    assert kp.event_form.start.text() == "rising(MotTypSrvdLong == 3)"
    assert kp.event_form.start.error is None and kp.event_form.end.error is None
    state.validate_now()
    assert kp.event_form.start.error is None


def test_drag_signal_onto_a_subplot(qtbot, window, mf4_dir):
    from PySide6.QtCore import QPointF

    _load(qtbot, window, mf4_dir)
    viewer = window.viewer
    window.resize(1400, 900)
    window.show()
    window.tabs.setCurrentIndex(1)
    viewer.plot_signals(["egoSpeed", "longActAccel", "aebTargetDecel"])
    qtbot.waitUntil(lambda: bool(viewer.data is not None and viewer.worker is None and viewer._axes), timeout=20000)
    assert len(viewer._axes) == 3
    # find the widget position of the first subplot and drop a signal there
    ax = viewer._axes[0]
    ratio = viewer.canvas.devicePixelRatioF()
    x0, y0, x1, y1 = ax.bbox.extents
    pos = QPointF((x0 + x1) / 2 / ratio, viewer.canvas.height() - (y0 + y1) / 2 / ratio)
    assert viewer.axes_at(pos) is ax
    viewer.drop_signals(["aebTargetDecel"], viewer.axes_at(pos))
    assert len(viewer._axes) == 2
    assert viewer._ax_groups[viewer._axes[0]] == ["egoSpeed", "aebTargetDecel"]
    assert viewer.separate_btn.isVisible()
    viewer.drop_signals(["longGap"], viewer._axes[1])  # unticked signal: ticked and added
    assert "longGap" in viewer.checked() and viewer._ax_groups[viewer._axes[1]] == ["longActAccel", "longGap"]
    viewer.separate_all()
    assert len(viewer._axes) == 4


def test_renaming_a_moment_or_kpi_updates_its_users(qtbot, window, mf4_dir):
    _load(qtbot, window, mf4_dir)
    state = window.state
    state.data["events"]["served"] = {"start": "egoSpeed > 1", "moments": {"m1": {"first": "egoSpeed > 2"},
                                                                       "m2": {"offset": "m1", "by": 0.5}}}
    state.data["kpis"] += [{"name": "spd", "event": "served", "value": "egoSpeed", "at": "m1"},
                           {"name": "spd2", "event": "served", "formula": "spd * 2"}]
    state.edited(structure=True)
    page = window.kpi_page
    page.event_form.load("served")
    table = page.event_form.moments
    row = next(r for r in range(table.rowCount()) if table.item(r, 0).text() == "m1")
    table.item(row, 0).setText("onset")
    assert list(state.data["events"]["served"]["moments"]) == ["onset", "m2"]
    assert state.data["events"]["served"]["moments"]["m2"]["offset"] == "onset"
    assert state.data["kpis"][-2]["at"] == "onset"
    page.kpi_form.load(len(state.data["kpis"]) - 2)
    page.kpi_form.name_edit.setText("speedAtOnset")
    page.kpi_form.rename()
    assert state.data["kpis"][-1]["formula"] == "speedAtOnset * 2"
    state.validate_now()
    assert state.compiled is not None, [str(i) for i in state.issues]
