"""GUI smoke tests (offscreen): load files and a profile, preview, run."""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("pytestqt")

from PySide6.QtCore import QSettings, Qt  # noqa: E402

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
    w.open_profile(profile_store.builtin_profile_dir() / "aeb_example.kpi.yaml")


def test_load_profile_and_files(qtbot, window, mf4_dir):
    _load(qtbot, window, mf4_dir)
    assert window.state.compiled is not None, [str(i) for i in window.state.issues]
    assert window.signals_page.tree.topLevelItemCount() > 0
    assert window.kpi_page.kpis.count() == 26
    assert window.kpi_page.kpis.item(25).text() == "aeb.peakDecel"  # event.kpi
    assert window.signals_page.sig_table.rowCount() >= 9


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
    viewer.set_event("aeb")
    viewer.run_preview()
    qtbot.waitUntil(lambda: bool(viewer.preview is not None and viewer.preview_worker is None
                                 and viewer.worker is None), timeout=20000)
    assert not hasattr(viewer, "kpi_table")  # no separate KPI table: values sit in the plot's signal list
    listed = [text for by_sig in viewer.kpi_lines.values() for lines in by_sig.values() for text, _ in lines]
    assert listed and all(" = " in t for t in listed)
    texts = [a.get_text() for a in viewer._artists if hasattr(a, "get_text")]
    cut = [t for t in texts if t.startswith("… ") and t.endswith("more KPI(s)")]
    shown = [t for t in listed if t in texts]  # drawn in the lists right of the subplots
    assert shown and (len(shown) == len(listed) or cut)  # those that don't fit are counted
    for ax, by_sig in viewer.kpi_lines.items():  # under a signal that subplot shows
        assert all(sig in viewer._names[ax] for sig in by_sig)
    n_res = sum(1 for r in viewer.preview["outcome"].results if r.event == "aeb" and r.occurrence == 1)
    assert len(listed) + len(viewer.kpi_others) == n_res  # the rest are named in the status tip
    if viewer.kpi_others:
        assert "not plotted" in viewer.pv_status.text() and viewer.kpi_others[0] in viewer.pv_status.toolTip()
    assert viewer.kpi_signals("aeb", "vehSpd") == ["egoSpeedKph"]
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
    window.open_profile(profile_store.builtin_profile_dir() / "aeb_example.kpi.yaml")
    assert len(window.state.recordings()) == 1
    assert window.files_page.table.item(0, 1).text() == "AADT-1_002"
    viewer = window.signals_page.viewer
    viewer.plot_signals(["egoSpeed", "aebTargetDecel"])  # one from each file
    qtbot.waitUntil(lambda: viewer.data is not None and viewer.worker is None, timeout=20000)
    assert {"egoSpeed", "aebTargetDecel"} <= set(viewer.data["values"])
    assert not hasattr(window.files_page, "combine")  # always combined, no option


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
    assert not hasattr(viewer, "rec_combo")  # no recording picker: the Files panel selection decides
    window.files_page.table.setCurrentCell(1, 0)
    assert viewer.selected_names() == [recs[1].name]
    viewer.select_recordings([r.name for r in recs])
    viewer.plot_signals(["egoSpeed"])
    qtbot.waitUntil(lambda: len(viewer.cache) == 2 and viewer.worker is None, timeout=30000)
    second = recs[1].name
    t0 = np.asarray(viewer.cache[second]["time"], dtype=float)
    offsets = window.offset_panel.table
    offsets.item(1, 1).setText("1.5")  # Offset [s] in the Time offset panel
    assert window.state.offsets == {recs[1].files[0]: 1.5}
    assert window.state.recordings()[1].offsets == {recs[1].files[0]: 1.5}  # what the KPI run reads
    assert offsets.item(1, 1).text() == "+1.500"
    # the shifted recording is read again with the offset applied
    qtbot.waitUntil(lambda: second in viewer.cache and viewer.worker is None, timeout=30000)
    assert np.allclose(np.asarray(viewer.cache[second]["time"], dtype=float)[:5], t0[:5] + 1.5)
    offsets.item(1, 1).setText("0")  # zero removes the shift
    assert window.state.offsets == {}
    offsets.item(0, 1).setText("abc")  # invalid input is ignored
    assert window.state.offsets == {} and offsets.item(0, 1).text() == "0"


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


def test_main_window_shows_only_the_signal_viewer(window):
    window.show()
    assert window.viewer_box.isVisible()
    assert not window.kpi_page.isVisible() and not window.run_page.isVisible()  # in side panels


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


def test_ticking_a_signal_loads_and_plots_it(qtbot, window, mf4_dir):
    _load(qtbot, window, mf4_dir)
    viewer = window.signals_page.viewer
    assert not viewer.cache
    item = next(viewer.list.item(i) for i in range(viewer.list.count()) if viewer.list.item(i).text() == "egoSpeed")
    item.setCheckState(Qt.Checked)  # no Load signals press needed
    qtbot.waitUntil(lambda: bool(viewer.cache) and viewer.worker is None, timeout=30000)
    assert viewer.fig.axes and viewer.fig.axes[0].get_lines()


def test_profile_check_click_goes_to_kpi_with_missing_moment(qtbot, window, mf4_dir):
    _load(qtbot, window, mf4_dir)
    state = window.state
    state.data["events"]["served"] = {"start": "egoSpeed > 1", "moments": {"braking": {"first": "egoSpeed > 2"}}}
    state.data["kpis"].append({"name": "spd", "event": "served", "value": "egoSpeed", "at": "moment1"})
    state.edited(structure=True)
    state.validate_now()
    item = next(window.issues.item(i) for i in range(window.issues.count())
                if "moment1" in window.issues.item(i).text())
    window.issues.itemClicked.emit(item)
    assert window.side_bar.current == window.side_bar.titles.index("Events & KPIs")
    assert window.kpi_page.kpis.currentRow() == len(state.data["kpis"]) - 1
    at = window.kpi_page.kpi_form.at
    assert at.currentText() == "moment1" and "e34948" in at.styleSheet()  # missing moment shown in red
    at.setCurrentText("braking")
    assert state.data["kpis"][-1]["at"] == "braking" and at.styleSheet() == ""
    state.validate_now()
    assert state.compiled is not None, [str(i) for i in state.issues]


def test_library_profile_opens_self_contained(qtbot, window, mf4_dir):
    _load(qtbot, window, mf4_dir)
    window.open_profile(profile_store.builtin_profile_dir() / "aeb_basic.kpi.yaml")  # imports vehicle + aeb_core
    data = window.state.data
    assert data["imports"] == [] and not any("use" in k for k in data["kpis"])
    assert "aeb" in data["events"] and len(data["kpis"]) == 26 and "egoSpeed" in data["signals"]
    assert window.state.dirty  # saving writes the copied-in definitions
    assert window.state.compiled is not None, [str(i) for i in window.state.issues]


def _texts(menu):
    return [a.text() for a in menu.actions()]


def test_right_click_deletes_signal_and_derived(qtbot, window, mf4_dir):
    from src.kpi_studio.gui.widgets.context_menu import menu_for

    _load(qtbot, window, mf4_dir)
    page = window.signals_page
    for table, section, labels in ((page.sig_table, "signals", ["Delete"]),
                                   (page.der_table, "derived", ["Edit…", "Delete", "", "Add…"])):
        name = table.item(0, 0).data(Qt.UserRole)
        name = name[0] if isinstance(name, tuple) else table.item(0, 0).text()
        table.clearSelection()
        menu = menu_for(table, table.visualRect(table.model().index(0, 0)).center())
        assert _texts(menu) == labels
        menu.actions()[labels.index("Delete")].trigger()
        assert name not in window.state.data[section]


def test_right_click_edits_and_adds_derived(qtbot, window, mf4_dir, monkeypatch):
    from src.kpi_studio.gui.widgets.context_menu import menu_for

    _load(qtbot, window, mf4_dir)
    page = window.signals_page
    table = page.der_table
    edited = []
    monkeypatch.setattr(page, "edit_derived", edited.append)
    monkeypatch.setattr(page, "open_builder", lambda *a: edited.append("added"))  # no dialog in tests
    menu = menu_for(table, table.visualRect(table.model().index(1, 0)).center())
    menu.actions()[0].trigger()
    assert edited == [table.item(1, 0).data(Qt.UserRole)]
    table.selectAll()
    menu = menu_for(table, table.visualRect(table.model().index(1, 0)).center())
    assert _texts(menu) == [f"Delete {table.rowCount()} rows", "", "Add…"]  # Edit is for one row
    menu_for(table, row=-1).actions()[-1].trigger()  # from the column header: Add only
    assert edited[-1] == "added" and _texts(menu_for(table, row=-1)) == ["Add…"]


def test_side_panel_width_can_be_dragged_and_is_remembered(qtbot, window, mf4_dir):
    from PySide6.QtCore import QPointF, QSettings
    from PySide6.QtGui import QMouseEvent

    _load(qtbot, window, mf4_dir)
    window.resize(1400, 900)
    window.show()
    bar = window.side_bar
    i = bar.titles.index("Time offset")
    bar.open(i)
    grip = bar.grips[i]
    w0 = bar.panels[i].width()
    assert grip.geometry().right() == w0 - 1
    def mouse(kind, x):
        return QMouseEvent(kind, QPointF(2, 50), QPointF(x, 50), Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)
    grip.mousePressEvent(mouse(QMouseEvent.Type.MouseButtonPress, 100))
    bar._over = lambda: False
    bar._check_mouse(); bar._check_mouse()
    assert bar.current == i  # dragging keeps it open
    grip.mouseMoveEvent(mouse(QMouseEvent.Type.MouseMove, 250))
    grip.mouseReleaseEvent(mouse(QMouseEvent.Type.MouseButtonRelease, 250))
    assert bar.panels[i].width() == w0 + 150
    assert int(window.settings.value("side_panels/width_time_offset")) == w0 + 150
    bar.set_width(i, 10)
    assert bar.widths[i] == bar.MIN_WIDTH
    assert isinstance(window.settings, QSettings)


def test_go_to_a_signal_issue_opens_the_channels_panel(qtbot, window, mf4_dir):
    _load(qtbot, window, mf4_dir)
    window.show()
    window.go_to("signals.egoSpeed")
    assert window.side_bar.current == window.side_bar.titles.index("Signals")
    assert window.signals_page.sig_table.currentRow() == 0


def test_delete_several_selected_signals_and_events(qtbot, window, mf4_dir):
    from PySide6.QtCore import QItemSelectionModel

    from src.kpi_studio.gui.widgets.context_menu import menu_for

    _load(qtbot, window, mf4_dir)
    page = window.signals_page
    table = page.sig_table
    names = [table.item(r, 0).data(Qt.UserRole)[0] for r in (1, 3)]
    sm = table.selectionModel()
    sm.clearSelection()
    for r in (1, 3):  # cells, not whole rows (cmd-click on cells)
        sm.select(table.model().index(r, 1), QItemSelectionModel.Select)
    menu = menu_for(table, table.visualRect(table.model().index(3, 1)).center())
    assert _texts(menu) == ["Delete 2 rows"]
    menu.actions()[0].trigger()
    assert not set(names) & set(window.state.data["signals"])
    kp = window.kpi_page
    for name in ("e1", "e2", "e3"):
        window.state.data["events"][name] = {"start": "egoSpeed > 1"}
    window.state.edited(structure=True)
    ev = kp.events
    rows = [r for r in range(ev.count()) if ev.item(r).text() in ("e1", "e3")]
    ev.clearSelection()
    for r in rows:
        ev.item(r).setSelected(True)
    menu = menu_for(ev, ev.visualItemRect(ev.item(rows[0])).center())
    assert _texts(menu) == ["Delete 2 events", "", "Add event"]
    menu.actions()[0].trigger()
    assert "e1" not in window.state.data["events"] and "e3" not in window.state.data["events"]
    assert "e2" in window.state.data["events"]
    k = kp.kpis
    n = k.count()
    k.clearSelection()
    k.item(0).setSelected(True)
    k.item(2).setSelected(True)
    menu = menu_for(k, k.visualItemRect(k.item(2)).center())
    assert _texts(menu)[0] == "Delete 2 KPIs"
    menu.actions()[0].trigger()
    assert k.count() == n - 2


def test_channel_tree_right_click_adds_signal(qtbot, window, mf4_dir):
    from src.kpi_studio.gui.widgets.context_menu import menu_for

    _load(qtbot, window, mf4_dir)
    page = window.signals_page
    window.state.data["signals"].pop("throttleValue", None)
    window.state.edited(structure=True)
    page.refresh_channels()
    leaf = next(page.tree.topLevelItem(i).child(j) for i in range(page.tree.topLevelItemCount())
                for j in range(page.tree.topLevelItem(i).childCount())
                if page.tree.topLevelItem(i).child(j).text(0) == "throttle_value")
    page.tree.scrollToItem(leaf)
    menu = menu_for(page.tree, page.tree.visualItemRect(leaf).center())
    assert _texts(menu) == ["Add as signal"]
    n = len(window.state.data["signals"])
    menu.actions()[0].trigger()
    assert len(window.state.data["signals"]) == n + 1


def test_files_and_plots_right_click_menus(qtbot, window, mf4_dir):
    from src.kpi_studio.gui.widgets.context_menu import menu_for

    _load(qtbot, window, mf4_dir)
    files = window.files_page.table
    menu = menu_for(files, files.visualRect(files.model().index(0, 0)).center())
    assert _texts(menu) == ["Check selected", "Uncheck selected", "Remove file", "", "Add files…", "Add folder…",
                            "Check all files", "Uncheck all files", "Remove all files"]
    menu.actions()[2].trigger()
    assert len(window.state.files) == 1
    plots = window.run_page.plots
    n = len(window.state.data["plots"])
    menu_for(plots, row=-1).actions()[0].trigger()
    assert len(window.state.data["plots"]) == n + 1
    menu = menu_for(plots, row=0)
    assert _texts(menu) == ["Delete plot", "", "Add plot"]
    menu.actions()[0].trigger()
    assert len(window.state.data["plots"]) == n


def test_enum_and_condition_dialogs_right_click(qtbot):
    from PySide6.QtCore import QPoint

    from src.kpi_studio.gui.pages.signals_page import EnumDialog
    from src.kpi_studio.gui.widgets.common import ConditionBuilder
    from src.kpi_studio.gui.widgets.context_menu import menu_for

    dlg = EnumDialog("gear", {"P": 0, "D": 1})
    qtbot.addWidget(dlg)
    assert _texts(menu_for(dlg.table, row=-1)) == ["Add value"]
    menu_for(dlg.table, row=-1).actions()[0].trigger()
    assert dlg.table.rowCount() == 3
    menu_for(dlg.table, row=0).actions()[0].trigger()
    assert dlg.table.rowCount() == 2
    cb = ConditionBuilder(["a", "b"])
    qtbot.addWidget(cb)
    cb.show()
    cb.menu_at(QPoint(1, 1)).actions()[-1].trigger()
    assert len(cb.rows) == 2
    op = cb.rows[1][1]
    menu = cb.menu_at(op.geometry().center())
    assert _texts(menu) == ["Delete row", "", "Add row"]
    menu.actions()[0].trigger()
    assert len(cb.rows) == 1


def test_moment_right_click_menus(qtbot, window, mf4_dir, monkeypatch):
    from src.kpi_studio.gui.widgets import common

    _load(qtbot, window, mf4_dir)
    window.state.data.setdefault("events", {})["vc"] = {
        "start": "egoSpeed > 1", "moments": {"m1": {"first": "egoSpeed > 2", "after": "start"}}}
    form = window.kpi_page.event_form
    form.load("vc")
    table = form.moments
    texts = lambda m: [a.text() for a in m.actions()]  # noqa: E731
    from PySide6.QtCore import QPoint
    from src.kpi_studio.gui.widgets.context_menu import menu_for
    blank = menu_for(table, table.viewport().rect().bottomLeft() + QPoint(5, -5))
    assert texts(blank) == ["Add moment"]
    blank.actions()[0].trigger()
    assert len(form.ev["moments"]) == 2
    row_menu = menu_for(table, row=0)
    assert texts(row_menu) == ["Build condition…", "Delete moment", "", "Add moment"]
    monkeypatch.setattr(common.ConditionBuilder, "exec", lambda self: common.QDialog.Accepted)
    monkeypatch.setattr(common.ConditionBuilder, "expression", lambda self: "rising(egoSpeed == 3)")
    row_menu.actions()[0].trigger()
    assert form.ev["moments"]["m1"]["first"] == "rising(egoSpeed == 3)"
    menu_for(table, row=0).actions()[1].trigger()
    assert "m1" not in form.ev["moments"] and len(form.ev["moments"]) == 1


def test_check_all_and_uncheck_all(qtbot, window, mf4_dir):
    _load(qtbot, window, mf4_dir)
    viewer = window.signals_page.viewer
    viewer.check_all()
    assert set(viewer.checked()) == {viewer.list.item(i).text() for i in range(viewer.list.count())}
    qtbot.waitUntil(lambda: bool(viewer.cache) and viewer.worker is None, timeout=30000)
    viewer.uncheck_all()
    assert not viewer.checked()


def test_side_panels_open_on_hover_and_hide(qtbot, window, mf4_dir, monkeypatch):
    from src.kpi_studio.gui.widgets import side_bar

    _load(qtbot, window, mf4_dir)
    window.resize(1400, 900)
    window.show()
    bar = window.side_bar
    assert [t.text for t in bar.tabs] == ["Files", "Time offset", "Signals", "Events & KPIs", "Plots & Run"]
    assert all(not p.isVisible() for p in bar.panels)  # hidden until hovered
    bar.tabs[2].hovered.emit()
    assert bar.panels[2].isVisible() and window.signals_page.tree.isVisible()
    assert window.signals_page.sig_table.isVisible() and window.signals_page.der_table.isVisible()
    assert bar.panels[2].height() == bar.host.height()
    bar.tabs[1].hovered.emit()  # another tab swaps the panel
    assert bar.panels[1].isVisible() and not bar.panels[2].isVisible()
    assert window.offset_panel.table.rowCount() == len(window.state.files)
    monkeypatch.setattr(bar, "_over", lambda: True)
    bar._check_mouse(); bar._check_mouse()
    assert bar.panels[1].isVisible()  # the mouse is still over it
    monkeypatch.setattr(bar, "_over", lambda: False)
    bar._check_mouse(); bar._check_mouse()
    assert bar.current is None and not bar.panels[1].isVisible()
    assert side_bar.STRIP_WIDTH == bar.width()


def test_events_and_kpis_right_click_menus(qtbot, window, mf4_dir):
    from PySide6.QtCore import QPoint

    from src.kpi_studio.gui.widgets.context_menu import menu_for

    _load(qtbot, window, mf4_dir)
    page = window.kpi_page
    assert not hasattr(page, "add_ev") and not hasattr(page, "add_kpi")  # no + / − buttons
    lst = page.kpis
    n = lst.count()
    on_item = menu_for(lst, lst.visualItemRect(lst.item(1)).center())
    assert _texts(on_item) == ["Delete KPI", "", "Add KPI"] and lst.currentRow() == 1
    name = lst.item(1).text()
    on_item.actions()[0].trigger()
    assert lst.count() == n - 1 and name not in [lst.item(i).text() for i in range(lst.count())]
    ev = page.events
    blank = menu_for(ev, ev.viewport().rect().bottomLeft() + QPoint(5, -3))
    assert _texts(blank) == ["Add event"]
    before = len(window.state.data["events"])
    blank.actions()[0].trigger()
    assert len(window.state.data["events"]) == before + 1


def test_plus_minus_buttons_in_every_section(qtbot, window, mf4_dir):
    from PySide6.QtWidgets import QPushButton

    _load(qtbot, window, mf4_dir)

    def pm(widget):
        return [b for b in widget.findChildren(QPushButton) if b.text() in ("+", "−")]

    assert [b.text() for b in window.files_page.header_buttons] == ["+", "−"]
    assert [a.text() for a in window.files_page.header_buttons[0].menu().actions()] == ["Add files…", "Add folder…"]
    assert len(pm(window.signals_page)) == 4  # channels +, signals −, derived + −
    assert len(pm(window.kpi_page)) >= 6  # events, KPIs, moments
    assert len(pm(window.run_page)) == 2  # plots
    page = window.kpi_page
    n = len(window.state.data["kpis"])
    plus = next(b for b in pm(page) if b.text() == "+" and b.toolTip() == "Add a KPI")
    plus.click()
    assert len(window.state.data["kpis"]) == n + 1
    minus = next(b for b in pm(page) if b.toolTip() == "Delete the selected KPIs")
    minus.click()
    assert len(window.state.data["kpis"]) == n
    files_minus = window.files_page.header_buttons[1]
    window.files_page.table.selectRow(0)
    files_minus.click()
    assert len(window.state.files) == 1


def test_every_column_can_be_dragged_and_cut_text_shows_on_hover(qtbot, window, mf4_dir):
    from PySide6.QtWidgets import QHeaderView

    from src.kpi_studio.gui.widgets import columns

    _load(qtbot, window, mf4_dir)
    window.resize(1400, 900)
    window.show()
    bar = window.side_bar
    bar.open(bar.titles.index("Signals"))
    table = window.signals_page.sig_table
    h = table.horizontalHeader()
    qtbot.waitUntil(lambda: bool(h.property("columnSizer")), timeout=5000)
    assert all(h.sectionResizeMode(c) == QHeaderView.Interactive for c in range(h.count()))
    sizer = h._column_sizer
    sizer.fit()
    assert h.sectionSize(1) >= columns.MIN_STRETCH  # the stretching column still takes the free width
    # the user drags a column edge: that width stays, even when the table refills
    sizer._pressed = True
    h.resizeSection(0, 5)  # never narrower than the header text
    assert h.sectionSize(0) == sizer.header_width(0) > 5
    h.resizeSection(0, 70)
    sizer._pressed = False
    assert sizer.user_sized
    window.state.edited(structure=True)
    qtbot.wait(50)
    assert h.sectionSize(0) == 70
    # a cut-off name shows in full on hover; one that fits keeps its own tooltip
    name = table.item(0, 0).text()
    pos = table.visualRect(table.model().index(0, 0)).center()
    assert columns.tip_at(table, pos).startswith(name)
    h.resizeSection(0, 400)
    assert columns.tip_at(table, table.visualRect(table.model().index(0, 0)).center()) is None
    sizer._fitting = True  # (squeeze it below the header width, as only a too-narrow panel can)
    h.resizeSection(1, sizer.header_width(1) - 30)
    sizer._fitting = False
    hpos = h.sectionViewportPosition(1) + 10
    from PySide6.QtCore import QPoint
    assert columns.tip_at(h, QPoint(hpos, 5)) == table.horizontalHeaderItem(1).text()


def test_clicked_side_panel_stays_open_until_clicked_again(qtbot, window, mf4_dir, monkeypatch):
    from PySide6.QtWidgets import QToolTip

    _load(qtbot, window, mf4_dir)
    window.resize(1400, 900)
    window.show()
    bar = window.side_bar
    monkeypatch.setattr(bar, "_over", lambda: False)  # the mouse is away from the strip and the panel
    bar.tabs[0].clicked.emit()
    bar._check_mouse(); bar._check_mouse(); bar._check_mouse()
    assert bar.current == 0 and bar.panels[0].isVisible()  # pinned
    bar.tabs[2].hovered.emit()
    assert bar.current == 0  # hovering another tab leaves the pinned panel
    bar.tabs[2].clicked.emit()  # clicking another tab pins that one
    assert bar.current == 2 and bar.pinned and not bar.panels[0].isVisible()
    bar.tabs[2].clicked.emit()  # clicking the pinned tab closes it
    assert bar.current is None and not bar.panels[2].isVisible()
    bar.tabs[1].hovered.emit()  # hovering still hides once the mouse leaves
    bar._check_mouse(); bar._check_mouse()
    assert bar.current is None
    assert QToolTip.font().pixelSize() == 13  # hover text as large as the table text


def test_pinned_side_panel_closes_on_a_click_in_the_main_view(qtbot, window, mf4_dir):
    from PySide6.QtCore import QPoint

    _load(qtbot, window, mf4_dir)
    window.resize(1400, 900)
    window.show()
    bar = window.side_bar
    bar.tabs[2].clicked.emit()
    qtbot.mouseClick(window.signals_page.sig_table.viewport(), Qt.LeftButton)  # inside the panel
    assert bar.current == 2 and bar.pinned
    # blank space in the panel: the press is passed on to the window behind it, still not "elsewhere"
    blank = bar.panels[2].rect().bottomRight() - QPoint(40, 40)
    qtbot.mouseClick(bar.panels[2], Qt.LeftButton, pos=blank)
    qtbot.mouseClick(window, Qt.LeftButton, pos=bar.panels[2].mapTo(window, blank))
    assert bar.current == 2 and bar.pinned
    outside = bar.host.mapTo(window, QPoint(bar.panels[2].width() + 50, 200))  # the main view, beside it
    qtbot.mouseClick(window, Qt.LeftButton, pos=outside)
    assert bar.current is None and not bar.panels[2].isVisible()


def test_files_can_be_unchecked_to_leave_them_out_of_the_analysis(qtbot, window, mf4_dir):
    from src.kpi_studio.gui.widgets.context_menu import menu_for

    _load(qtbot, window, mf4_dir)
    state, page = window.state, window.files_page
    table = page.table
    assert all(table.item(r, 0).checkState() == Qt.Checked for r in range(table.rowCount()))
    b = state.files[1]
    table.item(1, 0).setCheckState(Qt.Unchecked)  # the user unticks b.mf4
    assert state.checked_files == [state.files[0]] and b in state.unchecked
    assert all(b not in r.files for r in state.recordings())  # not analysed, not in the viewer
    assert window.viewer.files.count() == 1
    assert page.summary.text().startswith("1 of 2 file(s) checked")
    assert table.item(1, 0).checkState() == Qt.Unchecked  # still unchecked after the refresh
    page.check_all(True)
    assert state.checked_files == state.files
    table.selectRow(0)
    menu = menu_for(table, table.visualRect(table.model().index(0, 0)).center())
    next(a for a in menu.actions() if a.text() == "Uncheck selected").trigger()
    assert state.checked_files == [b]
    page.check_all(False)
    assert not state.checked_files and not state.recordings()
    window.run_page.log.clear()
    window.run_page.start_run()
    assert "No files checked" in window.run_page.log.toPlainText() and window.run_page.worker is None
    state.set_files([state.files[0]])  # removing a file forgets its check state
    assert state.unchecked == {state.files[0]}


def test_build_buttons_stay_in_view_in_a_narrow_events_panel(qtbot, window, mf4_dir):
    from PySide6.QtCore import QPoint

    _load(qtbot, window, mf4_dir)
    window.resize(1100, 800)
    window.show()
    bar = window.side_bar
    i = bar.titles.index("Events & KPIs")
    bar.set_width(i, 900)
    bar.open(i, sticky=True)
    state = window.state
    state.data["events"]["served"] = {"start": "egoSpeed > 1", "end": "egoSpeed < 0.5"}  # a condition event
    state.edited(structure=True)
    kp = window.kpi_page
    kp.events.setCurrentRow(list(state.data["events"]).index("served"))
    qtbot.wait(100)
    view = kp.form_scroll.viewport()
    for b in kp.event_form.build_buttons:
        right = b.mapTo(view, QPoint(b.width(), 0)).x()
        assert b.isVisible() and right <= view.width(), (right, view.width())
    assert kp.form_scroll.horizontalScrollBar().maximum() == 0  # the fields got narrower instead


def test_cut_off_text_tip_sits_under_the_cell(qtbot, window, mf4_dir):
    from PySide6.QtCore import QPoint
    from PySide6.QtGui import QHelpEvent
    from PySide6.QtWidgets import QApplication, QToolTip

    from src.kpi_studio.gui.widgets import columns

    _load(qtbot, window, mf4_dir)
    window.resize(1400, 900)
    window.show()
    bar = window.side_bar
    bar.open(bar.titles.index("Signals"), sticky=True)
    table = window.signals_page.sig_table
    h = table.horizontalHeader()
    qtbot.waitUntil(lambda: bool(h.property("columnSizer")), timeout=5000)
    h.resizeSection(0, h._column_sizer.header_width(0))
    rect = table.visualRect(table.model().index(0, 0))
    pos = rect.center() + QPoint(5, 0)
    QApplication.sendEvent(table.viewport(), QHelpEvent(QHelpEvent.Type.ToolTip, pos,
                                                        table.viewport().mapToGlobal(pos)))
    label = columns._tip_label()
    assert label is not None and QToolTip.text().startswith(table.item(0, 0).text())
    assert label.pos() == table.viewport().mapToGlobal(rect.bottomLeft()) + QPoint(0, 2)
    QToolTip.hideText()
    assert not window.viewer.toolbar.coordinates  # no (x, y) readout in the plot toolbar


def test_right_click_a_header_to_pick_the_columns_shown(qtbot, window, mf4_dir):
    from src.kpi_studio.gui.widgets import columns
    from src.kpi_studio.gui.widgets.context_menu import menu_for

    _load(qtbot, window, mf4_dir)
    for view, key in ((window.files_page.table, "columns/files"), (window.signals_page.sig_table, "columns/signals")):
        h = view.horizontalHeader()
        columns.make_resizable(h)  # as when the panel is first shown
        menu = columns.column_menu(view)
        names = [view.horizontalHeaderItem(c).text() for c in range(view.columnCount())]
        assert [a.text() for a in menu.actions()] == names and all(a.isChecked() for a in menu.actions())
        menu.actions()[1].trigger()  # untick the second column: hidden, and remembered
        assert h.isSectionHidden(1) and window.settings.value(key) == [names[1]]
        assert h.sectionViewportPosition(2) == h.sectionViewportPosition(0) + h.sectionSize(0)  # no gap left
        assert not menu.actions()[1].isChecked()
        menu.actions()[1].trigger()  # tick it again
        assert not h.isSectionHidden(1) and h.sectionSize(1) > 0
        for a in menu.actions()[1:]:
            a.trigger()
        assert not menu.actions()[0].isEnabled()  # the last shown column can't be hidden
        for a in menu.actions()[1:]:
            a.trigger()
        assert not any(h.isSectionHidden(c) for c in range(h.count()))
    # the rows still have their own menus
    table = window.files_page.table
    assert "Remove file" in [a.text() for a in menu_for(table, table.visualRect(table.model().index(0, 0)).center()).actions()]
    # hidden columns come back hidden in a new session
    window.settings.setValue("columns/signals", ["Enum"])
    columns.install_picker(window.signals_page.sig_table, window.settings, "columns/signals")
    sig = window.signals_page.sig_table
    enum = [sig.horizontalHeaderItem(c).text() for c in range(sig.columnCount())].index("Enum")
    assert sig.horizontalHeader().isSectionHidden(enum)


def test_moment_expression_column_is_the_widest(qtbot, window, mf4_dir):
    _load(qtbot, window, mf4_dir)
    window.resize(1500, 900)
    window.show()
    bar = window.side_bar
    bar.open(bar.titles.index("Events & KPIs"), sticky=True)
    window.kpi_page.events.setCurrentRow(0)
    qtbot.wait(100)
    h = window.kpi_page.event_form.moments.horizontalHeader()
    assert h.sectionSize(2) > h.sectionSize(3) and h.sectionSize(2) > h.sectionSize(1)


def test_dragged_columns_still_span_the_whole_width(qtbot, window, mf4_dir):
    _load(qtbot, window, mf4_dir)
    window.resize(1500, 900)
    window.show()
    bar = window.side_bar
    bar.open(bar.titles.index("Events & KPIs"), sticky=True)
    window.kpi_page.events.setCurrentRow(0)
    qtbot.wait(100)
    h = window.kpi_page.event_form.moments.horizontalHeader()
    qtbot.waitUntil(lambda: bool(h.property("columnSizer")), timeout=5000)
    sizer = h._column_sizer

    def total():
        return sum(h.sectionSize(c) for c in range(h.count()) if not h.isSectionHidden(c))

    for col, width in ((1, 100), (3, 220), (2, 150), (3, 2000)):  # Rule narrower, Options wider, …
        before = h.sectionSize(col)
        sizer._pressed = True
        h.resizeSection(col, width)
        sizer._pressed = False
        assert total() == h.viewport().width()  # the columns still fill the table exactly
        after = h.sectionSize(col)
        assert (after >= before) if width > before else (after <= before)  # and the drag took effect
        assert after == max(width, sizer.header_width(col)) or width == 2000  # (2000: capped to the view)
    bar.set_width(bar.current, bar.widths[bar.current] - 150)  # the panel narrows: still filled
    qtbot.wait(50)
    assert total() == h.viewport().width()


def test_long_text_in_a_box_shows_its_start(qtbot, window, mf4_dir):
    from src.kpi_studio.gui.widgets.common import ExprEdit

    _load(qtbot, window, mf4_dir)
    window.resize(1500, 900)
    window.show()
    bar = window.side_bar
    bar.open(bar.titles.index("Events & KPIs"), sticky=True)
    form = window.kpi_page.event_form
    window.kpi_page.events.setCurrentRow(0)
    qtbot.wait(50)
    edits = [w for w in form.moments.findChildren(ExprEdit) if w.isVisible()]
    assert edits and all(e.cursorPosition() == 0 for e in edits)
    form.desc.setText("a very long description " * 10)  # set while shown (another item loaded)
    assert form.desc.cursorPosition() == 0
    form.desc.setFocus()
    qtbot.waitUntil(form.desc.hasFocus, timeout=2000)
    form.desc.end(False)  # while typing, the cursor stays where the user puts it
    form.desc.insert("x")
    assert form.desc.cursorPosition() == len(form.desc.text())


def test_drag_channels_onto_the_signals_table(qtbot, window, mf4_dir):
    from PySide6.QtCore import QEvent, QMimeData, QPointF
    from PySide6.QtGui import QDropEvent

    _load(qtbot, window, mf4_dir)
    page = window.signals_page
    assert window.side_bar.titles[2] == "Signals"  # the panel was called Channels
    tree = page.tree
    channel = next(it for it in tree.findItems("*", Qt.MatchWildcard | Qt.MatchRecursive) if it.childCount() == 0
                   and it.text(0) == "throttle_value")
    sigs = window.state.data["signals"]
    before = len(sigs)
    for name in [n for n, s in sigs.items() if "throttle_value" in str(s)]:
        del sigs[name]
    window.state.edited(structure=True)
    tree.clearSelection()
    channel.setSelected(True)

    class Drop(QDropEvent):
        def __init__(self, kind, source):
            super().__init__(QPointF(10, 10), Qt.CopyAction, QMimeData(), Qt.LeftButton, Qt.NoModifier, kind)
            self._source = source

        def source(self):
            return self._source

    viewport = page.sig_table.viewport()
    assert not page.eventFilter(viewport, Drop(QEvent.Type.Drop, None))  # a drop from elsewhere is ignored
    assert len(sigs) < before
    assert page.eventFilter(viewport, Drop(QEvent.Type.Drop, tree))
    assert any("throttle_value" in str(s) for s in sigs.values())  # dropped channel added as a signal
