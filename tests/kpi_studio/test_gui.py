"""GUI smoke tests (offscreen): load files and a profile, preview, run."""

import os

import pytest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("pytestqt")

import numpy as np  # noqa: E402
from src.kpi_studio.gui.pages import kpi_page  # noqa: E402
from PySide6.QtCore import QPoint, QSettings, Qt  # noqa: E402

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


def test_which_kpi_form(qtbot, window, mf4_dir):
    _load(qtbot, window, mf4_dir)
    page = window.kpi_page
    page.new_kpi()
    form = page.kpi_form
    form.event.setCurrentText("aeb")
    form.form.setCurrentIndex([f for f, _ in kpi_page.FORMS].index("range"))
    form.reducer.setCurrentIndex(form.reducer.findData("which"))
    assert form.range.currentText() == "event" and not form.expr.isVisibleTo(form)
    for label, cond in (("PB", "egoSpeed > 5"), ("FB", "longActAccel < -2")):
        form.which.add_row()
        r = form.which.table.rowCount() - 1
        form.which.table.item(r, 0).setText(label)
        form.which.rows[r].set_expression(cond)
        form.which.rows[r].changed.emit()
    k = window.state.data["kpis"][-1]
    assert k["which"] == {"PB": "egoSpeed > 5", "FB": "longActAccel < -2"} and k["over"] == "event"
    window.state.validate_now()
    assert window.state.compiled is not None, [str(i) for i in window.state.issues]
    form.load(len(window.state.data["kpis"]) - 1)  # reloads into the same rows
    assert form.which.items() == k["which"] and form.which.isVisibleTo(form)


def test_kpis_over_named_ranges(qtbot, window, mf4_dir):
    _load(qtbot, window, mf4_dir)
    page = window.kpi_page
    page.select_event("aeb")
    ef = page.event_form
    ef.add_range()
    r = ef.ranges.rowCount() - 1
    ef.ranges.cellWidget(r, 1).setCurrentText("stop")
    ef.ranges.cellWidget(r, 2).setCurrentText("window_end")
    ef.ranges.item(r, 0).setText("after_stop")
    assert window.state.data["events"]["aeb"]["ranges"]["after_stop"] == ["stop", "window_end"]
    page.new_kpi()
    form = page.kpi_form
    form.event.setCurrentText("aeb")
    form.form.setCurrentIndex([f for f, _ in kpi_page.FORMS].index("range"))
    assert "after_stop" in [form.range.itemText(i) for i in range(form.range.count())]
    form.range.setCurrentText("after_stop")
    form.reducer.setCurrentIndex(form.reducer.findData("all"))
    form.expr.setText("egoSpeed < 1")
    form.expr.editingFinished.emit()
    k = window.state.data["kpis"][-1]
    assert k["reduce"] == "all" and k["of"] == "egoSpeed < 1" and k["over"] == "after_stop"
    form.reducer.setCurrentIndex(form.reducer.findData("duration"))
    k = window.state.data["kpis"][-1]
    assert k == {"name": k["name"], "event": "aeb", "reduce": "duration", "over": "after_stop"}
    assert form.unit.text() == "s" and not form.expr.isVisibleTo(form)
    # renaming the range and its moment follows through to the KPI
    page.select_event("aeb")
    ef.ranges.item(r, 0).setText("stopped")
    assert window.state.data["kpis"][-1]["over"] == "stopped"
    window.state.validate_now()
    assert window.state.compiled is not None, [str(i) for i in window.state.issues]


def test_moments_and_ranges_heights_are_adjustable(qtbot, window, mf4_dir):
    _load(qtbot, window, mf4_dir)
    window.resize(1500, 1000)
    window.show()
    window.open_panel("Events & KPIs")
    window.kpi_page.select_event("aeb")
    ef = window.kpi_page.event_form
    sp = ef.tables_split
    assert sp.widget(0).isAncestorOf(ef.moments) and sp.widget(1).isAncestorOf(ef.ranges)
    qtbot.wait(50)
    a, b = sp.sizes()
    assert abs(a - b) <= 2  # they start at the same height
    sp.setSizes([a - 100, b + 100])
    assert sp.sizes()[1] > b


def test_resize_places_show_the_resize_cursor_and_tip(qtbot, window, mf4_dir):
    from PySide6.QtWidgets import QSplitter

    from src.kpi_studio.gui.widgets import resize_cursor
    from src.kpi_studio.gui.widgets.side_bar import ResizeGrip

    _load(qtbot, window, mf4_dir)
    window.resize(1400, 900)
    window.show()
    window.open_panel("Events & KPIs")
    qtbot.wait(50)
    splitters = window.findChildren(QSplitter)
    assert len(splitters) >= 3
    for sp in splitters:
        for i in range(1, sp.count()):
            h = sp.handle(i)
            if h.isVisible():
                assert h.cursor().shape() == Qt.BitmapCursor and h.toolTip() == "Drag to resize"
                horizontal = sp.orientation() == Qt.Horizontal
                assert h.cursor().pixmap().toImage() == resize_cursor.cursor(horizontal).pixmap().toImage()
    grips = window.findChildren(ResizeGrip)
    assert grips and all(g.cursor().shape() == Qt.BitmapCursor and g.toolTip() == "Drag to resize" for g in grips)
    # table column borders keep Qt's own cursor
    assert window.kpi_page.event_form.moments.horizontalHeader().cursor().shape() != Qt.BitmapCursor


def test_unit_can_be_typed_when_the_signal_has_none(qtbot, window, mf4_dir):
    _load(qtbot, window, mf4_dir)
    window.state.validate_now()
    page = window.kpi_page
    page.new_kpi()
    form = page.kpi_form
    form.event.setCurrentText("aeb")
    form.expr.setText("egoSpeedKph")  # no unit in the test profile
    form.expr.editingFinished.emit()
    assert not form.unit.isReadOnly() and form.unit.text() == ""
    form.unit.setText("km/h")
    form.unit.editingFinished.emit()
    assert window.state.data["kpis"][-1]["unit"] == "km/h"
    window.state.validate_now()
    name = window.state.data["kpis"][-1]["name"]
    assert next(k.unit for k in window.state.compiled.kpis if k.defn.name == name) == "km/h"
    form.load(len(window.state.data["kpis"]) - 1)
    assert form.unit.text() == "km/h" and not form.unit.isReadOnly()
    form.expr.setText("longActAccel")  # has a unit: shown, not typed
    form.expr.editingFinished.emit()
    assert form.unit.isReadOnly() and form.unit.text() != "km/h"


def test_no_event_presets_in_the_tool(qtbot, window, mf4_dir):
    _load(qtbot, window, mf4_dir)
    page = window.kpi_page
    pal = page.palette
    tops = [pal.tree.topLevelItem(i).text(0) for i in range(pal.tree.topLevelItemCount())]
    assert not any("preset" in t.lower() for t in tops), tops
    page.new_event()
    ef = page.event_form
    assert not ef.mode.isVisibleTo(ef) and ef.start.isVisibleTo(ef)  # a new event is a condition
    page.select_event("aeb")  # the example still runs on its preset, which stays editable
    assert ef.mode.isVisibleTo(ef) and ef.mode.currentIndex() == 1


def test_unit_column_is_a_drop_down(qtbot, window, mf4_dir):
    from src.kpi_studio.gui.pages.signals_page import NO_UNIT, UNIT_CHOICES, UnitDelegate

    _load(qtbot, window, mf4_dir)
    page = window.signals_page
    table = page.sig_table
    assert isinstance(table.itemDelegateForColumn(2), UnitDelegate)
    assert isinstance(page.der_table.itemDelegateForColumn(2), UnitDelegate)
    name = table.item(0, 0).text()
    ix = table.model().index(0, 2)
    delegate = table.itemDelegateForColumn(2)
    box = delegate.createEditor(table.viewport(), None, ix)
    box.showPopup = lambda: None
    items = [box.itemText(i) for i in range(box.count())]
    assert items[0] == NO_UNIT and len(items) == len(UNIT_CHOICES) + 1
    assert "m/s²" in items and "m/s³" in items and "m/s^2" not in items  # powers shown as superscripts
    delegate.setEditorData(box, ix)
    box.setCurrentText("m/s²")
    delegate.setModelData(box, table.model(), ix)
    assert window.state.data["signals"][name]["unit"] == "m/s^2"  # stored plain
    assert delegate.displayText("m/s^2", None) == "m/s²"
    delegate.setEditorData(box, ix)
    box.setCurrentText("km/h")
    delegate.setModelData(box, table.model(), ix)
    assert window.state.data["signals"][name]["unit"] == "km/h"
    box.setCurrentText(NO_UNIT)
    delegate.setModelData(box, table.model(), ix)
    assert window.state.data["signals"][name].get("unit") is None
    box.setCurrentText("bar")  # a unit not in the list can be typed
    delegate.setModelData(box, table.model(), ix)
    assert window.state.data["signals"][name]["unit"] == "bar"
    box.deleteLater()


def test_moment_pairs_become_named_ranges(tmp_path, qtbot, window):
    import yaml

    from src.kpi_studio.core.rename import use_named_ranges

    data = {"events": {"e": {"start": "x > 0", "moments": {"a": {"first": "x > 1"}}}},
            "kpis": [{"name": "d", "event": "e", "duration": ["a", "end"]},
                     {"name": "m", "event": "e", "reduce": "max", "of": "x", "between": ["a", "end"]},
                     {"name": "w", "event": "e", "reduce": "max", "of": "x"},
                     {"name": "t", "event": "e", "which": {"A": "x > 1"}, "between": ["start", "end"]},
                     {"name": "v", "event": "e", "value": "x", "at": "a"}]}
    assert use_named_ranges(data) == 4
    assert data["events"]["e"]["ranges"] == {"a_to_end": ["a", "end"]}
    assert [k.get("over") for k in data["kpis"]] == ["a_to_end", "a_to_end", "window", "event", None]
    assert data["kpis"][0] == {"name": "d", "event": "e", "reduce": "duration", "over": "a_to_end"}
    assert "between" not in data["kpis"][1]
    path = tmp_path / "old.kpi.yaml"
    path.write_text(yaml.safe_dump({"name": "p", "signals": {"x": {"channels": ["x"]}},
                                    "events": {"e": {"start": "x > 0"}},
                                    "kpis": [{"name": "d", "event": "e", "duration": ["start", "end"]}]}))
    window.state.load(path)
    assert window.state.data["kpis"][0]["over"] == "event" and window.state.dirty


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
        assert viewer.kpi_others[0] in viewer.pv_run.toolTip()
    assert viewer.pv_status.text() == "" and viewer.pv_status.isHidden()  # no grey status text
    assert viewer.kpi_signals("aeb", "vehSpd") == ["egoSpeedKph"]
    run = window.run_page
    run.out_dir.setText(str(tmp_path / "out"))
    run.workers.setValue(1)
    assert run.check()
    run.start_run()
    qtbot.waitUntil(lambda: run.result is not None, timeout=60000)
    assert run.result.n_ok == 2 and run.result.excel.exists()


def test_output_defaults_to_the_input_data_folder(qtbot, window, mf4_dir, tmp_path):
    from src.kpi_studio.app.run_service import input_results_dir
    _load(qtbot, window, mf4_dir)
    run = window.run_page
    run.set_output(None)
    target = input_results_dir(window.state.checked_files)
    assert run.output_root() is None and run.out_dir.text() == str(target)
    assert run.default_btn.isHidden() and run.browse.isEnabled()
    run.set_output(str(tmp_path / "mine"))  # what Browse does
    assert run.output_root() == str(tmp_path / "mine") and run.out_dir.text() == str(tmp_path / "mine")
    assert not run.default_btn.isHidden()
    run.default_btn.click()
    assert run.output_root() is None and run.out_dir.text() == str(target)
    run.out_dir.setText(str(tmp_path / "typed"))  # a typed folder counts as picked
    run.out_dir.editingFinished.emit()
    assert run.output_root() == str(tmp_path / "typed")


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


def test_occurrences_of_every_file(qtbot, window, mf4_dir):
    _load(qtbot, window, mf4_dir)
    viewer = window.viewer
    viewer.set_event("aeb")
    viewer.run_preview()
    qtbot.waitUntil(lambda: bool(len(viewer.previews) == 2 and viewer.preview_worker is None
                                 and viewer.worker is None), timeout=30000)
    occs = viewer.all_occurrences()
    files = [Path(name).name for name, _ in occs]
    assert files == ["a.mf4", "a.mf4", "b.mf4"]  # a.mf4 has two, b.mf4 one
    assert viewer.pv_occ.maximum() == 3 and viewer.pv_occ.suffix() == " / 3"
    assert viewer.pv_run.text() == "Run preview" and viewer.pv_run.isEnabled()
    viewer.pv_occ.setValue(3)  # steps into the next file
    qtbot.waitUntil(lambda: viewer.worker is None and bool(viewer._axes), timeout=20000)
    assert Path(viewer.preview["rec"]).name == "b.mf4" and [Path(n).name for n in viewer.selected_names()] == ["b.mf4"]
    _, occ = occs[2]
    assert viewer._axes[0].get_xlim() == (occ.t_window_start, occ.t_window_end)
    assert "b.mf4" in viewer._axes[0].get_title() and "#1" in viewer._axes[0].get_title()
    viewer.pv_occ.setValue(2)  # and back
    qtbot.waitUntil(lambda: viewer.worker is None, timeout=20000)
    assert Path(viewer.preview["rec"]).name == "a.mf4"
    assert viewer._axes[0].get_xlim() == (occs[1][1].t_window_start, occs[1][1].t_window_end)


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
    click = SimpleNamespace(inaxes=viewer.fig.axes[0], xdata=t[5], ydata=0.0, x=100.0, y=100.0, button=3, key=None)
    viewer._on_press(click)
    viewer._on_release(click)
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
    assert first.text() == "➜ Edit condition"
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


def test_derived_kind_column_follows_the_expression_until_set(qtbot, window, mf4_dir):
    from src.kpi_studio.gui.pages.signals_page import DerivedKindDelegate

    _load(qtbot, window, mf4_dir)
    page = window.signals_page
    window.state.data.setdefault("derived", {}).update({"vKph": "egoSpeed * 3.6", "fast": "egoSpeed > 20"})
    window.state.data["signals"]["egoSpeed"]["kind"] = "continuous"
    window.state.edited(structure=True)
    window.state.validate_now()
    assert window.state.derived_kind("vKph") == "continuous"
    window.state.data["signals"]["egoSpeed"]["kind"] = "discrete"  # the input's Kind is passed on
    assert window.state.derived_kind("vKph") == "discrete" and window.state.kind_source("vKph") == "egoSpeed"
    window.state.data["signals"]["egoSpeed"]["kind"] = "continuous"
    assert page.DER_COLS[3] == "Kind" and isinstance(page.der_table.itemDelegateForColumn(3), DerivedKindDelegate)
    rows = {page.der_table.item(r, 0).text(): r for r in range(page.der_table.rowCount())}
    assert page.der_table.item(rows["vKph"], 3).text() == "continuous"
    assert page.der_table.item(rows["fast"], 3).text() == "boolean"  # a condition
    assert window.viewer.kind("fast") == "boolean"
    asked = []
    page.confirm_kind = lambda name, kind, auto: asked.append((name, kind, auto)) or False  # "No"
    page.der_table.item(rows["vKph"], 3).setText("discrete")
    assert asked == [("vKph", "discrete", "continuous")] and window.state.data["derived"]["vKph"] == "egoSpeed * 3.6"
    qtbot.wait(10)
    rows = {page.der_table.item(r, 0).text(): r for r in range(page.der_table.rowCount())}
    assert page.der_table.item(rows["vKph"], 3).text() == "continuous"  # put back
    page.confirm_kind = lambda name, kind, auto: True  # "Yes"
    page.der_table.item(rows["vKph"], 3).setText("discrete")
    assert window.state.data["derived"]["vKph"] == {"expr": "egoSpeed * 3.6", "kind": "discrete"}
    window.state.validate_now()
    assert "vKph" in window.state.compiled.discrete and window.viewer.kind("vKph") == "discrete"
    qtbot.wait(10)
    rows = {page.der_table.item(r, 0).text(): r for r in range(page.der_table.rowCount())}
    page.der_table.item(rows["vKph"], 3).setText("continuous")  # what the expression gives: not stored
    assert window.state.data["derived"]["vKph"] == "egoSpeed * 3.6"


def test_derived_unit_and_description_columns(qtbot, window, mf4_dir):
    _load(qtbot, window, mf4_dir)
    page = window.signals_page
    window.state.data.setdefault("derived", {})["vKph"] = "egoSpeed * 3.6"
    window.state.edited(structure=True)
    window.state.validate_now()
    row = next(r for r in range(page.der_table.rowCount()) if page.der_table.item(r, 0).text() == "vKph")
    page.der_table.item(row, 2).setText("km/h")
    page.der_table.item(row, 4).setText("ego speed")
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
    # find the widget position of egoSpeed's subplot and drop a signal there
    ax = next(a for a in viewer._axes if viewer._ax_groups[a] == ["egoSpeed"])
    ratio = viewer.canvas.devicePixelRatioF()
    x0, y0, x1, y1 = ax.bbox.extents
    pos = QPointF((x0 + x1) / 2 / ratio, viewer.canvas.height() - (y0 + y1) / 2 / ratio)
    assert viewer.axes_at(pos) is ax
    viewer.drop_signals(["aebTargetDecel"], viewer.axes_at(pos))
    assert len(viewer._axes) == 2
    assert ["egoSpeed", "aebTargetDecel"] in [viewer._ax_groups[a] for a in viewer._axes]
    assert viewer.separate_btn.isVisible()
    other = next(a for a in viewer._axes if viewer._ax_groups[a] == ["longActAccel"])
    viewer.drop_signals(["longGap"], other)  # unticked signal: ticked and added
    assert "longGap" in viewer.checked()
    assert ["longActAccel", "longGap"] in [viewer._ax_groups[a] for a in viewer._axes]
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
    state.validate_now()
    assert window.issues_dock.isHidden()  # no errors: Profile check stays out of the way
    state.data["events"]["served"] = {"start": "egoSpeed > 1", "moments": {"braking": {"first": "egoSpeed > 2"}}}
    state.data["kpis"].append({"name": "spd", "event": "served", "value": "egoSpeed", "at": "moment1"})
    state.edited(structure=True)
    state.validate_now()
    item = next(window.issues.item(i) for i in range(window.issues.count())
                if "moment1" in window.issues.item(i).text())
    window.issues.itemClicked.emit(item)
    assert window.side_bar.current == window.side_bar.titles.index("Events & KPIs")
    assert not window.issues_dock.isHidden()  # pops up with the error
    assert all(window.issues.item(i).text().startswith("✖") for i in range(window.issues.count()))
    assert window.kpi_page.kpis.currentRow() == len(state.data["kpis"]) - 1
    at = window.kpi_page.kpi_form.at
    assert at.currentText() == "moment1" and "e34948" in at.styleSheet()  # missing moment shown in red
    at.setCurrentText("braking")
    assert state.data["kpis"][-1]["at"] == "braking" and at.styleSheet() == ""
    state.validate_now()
    assert state.compiled is not None, [str(i) for i in state.issues]
    assert window.issues_dock.isHidden()  # fixed: Profile check goes away


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
    from PySide6.QtWidgets import QPushButton

    from src.kpi_studio.gui.pages.signals_page import EnumDialog
    from src.kpi_studio.gui.widgets.common import ConditionBuilder

    dlg = EnumDialog("gear", {"P": 0, "D": 1})
    qtbot.addWidget(dlg)
    assert dlg.table.contextMenuPolicy() == Qt.NoContextMenu  # the enum values are only shown: no add / delete
    assert not [b for b in dlg.findChildren(QPushButton) if b.text() in ("+", "−", "-")]
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
    assert texts(row_menu) == ["Edit condition", "Delete moment", "", "Add moment"]
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


def test_dragging_a_column_edge_trades_width_with_its_right_neighbour(qtbot, window, mf4_dir):
    _load(qtbot, window, mf4_dir)
    window.resize(1500, 900)
    window.show()
    bar = window.side_bar
    bar.open(bar.titles.index("Signals"), sticky=True)
    tree = window.signals_page.tree
    h = tree.header()
    assert [tree.headerItem().text(c) for c in range(3)] == ["Signal", "Unit", "Files"]
    qtbot.waitUntil(lambda: bool(h.property("columnSizer")), timeout=5000)
    sizer = h._column_sizer
    sizer._pressed = True
    unit = h.sectionSize(1)
    h.resizeSection(0, h.sectionSize(0) - 150)  # the Signal | Unit edge dragged left: Unit gets wider
    assert h.sectionSize(1) == unit + 150
    signal, unit, files = (h.sectionSize(c) for c in range(3))
    d = min(80, unit - sizer.header_width(1))
    assert d > 20
    h.resizeSection(1, unit - d)  # the Unit | Files edge dragged left: Files gets wider
    assert (h.sectionSize(0), h.sectionSize(2)) == (signal, files + d)
    signal, unit, files = (h.sectionSize(c) for c in range(3))
    grow = 60
    h.resizeSection(1, unit + grow)  # the Unit | Files edge dragged right: Files gets narrower
    assert h.sectionSize(0) == signal  # the Signal column keeps its width
    assert h.sectionSize(2) == files - grow
    h.resizeSection(1, unit + 2000)  # dragged on past Files' minimum: the border stops there
    sizer._pressed = False
    assert h.sectionSize(0) == signal
    assert h.sectionSize(2) == sizer.header_width(2)


def test_a_column_border_squeezes_the_next_columns_once_its_neighbour_is_at_its_minimum(qtbot, window, mf4_dir):
    _load(qtbot, window, mf4_dir)
    window.resize(1500, 900)
    window.show()
    bar = window.side_bar
    bar.open(bar.titles.index("Signals"), sticky=True)
    h = window.signals_page.sig_table.horizontalHeader()
    qtbot.waitUntil(lambda: bool(h.property("columnSizer")), timeout=5000)
    sizer = h._column_sizer
    shown = [c for c in range(h.count()) if not h.isSectionHidden(c)]
    first, neighbour, rest = shown[0], shown[1], shown[2:]
    sizer._pressed = True
    h.resizeSection(first, max(sizer.header_width(first), h.sectionSize(first) - 150))  # widths moved on
    h.resizeSection(neighbour, sizer.header_width(neighbour))  # to the rest; the neighbour at its minimum
    before = {c: h.sectionSize(c) for c in shown}
    room = sum(before[c] - sizer.header_width(c) for c in rest)
    assert room > 30
    h.resizeSection(first, before[first] + 30)  # dragged on: the columns further right give
    assert h.sectionSize(first) == before[first] + 30
    assert sum(h.sectionSize(c) for c in rest) == sum(before[c] for c in rest) - 30
    h.resizeSection(first, before[first] + room + 500)  # all at their minimum: the border stops
    sizer._pressed = False
    assert all(h.sectionSize(c) == sizer.header_width(c) for c in shown[1:])
    assert sum(h.sectionSize(c) for c in shown) == h.viewport().width()


def test_channels_already_added_as_signals_get_a_dot(qtbot, window, mf4_dir):
    _load(qtbot, window, mf4_dir)
    page = window.signals_page
    tree = page.tree
    sigs = window.state.data["signals"]
    for name in [n for n, s in sigs.items() if "throttle_value" in str(s)]:
        del sigs[name]
    window.state.edited(structure=True)
    qtbot.wait(50)

    def channel():
        return next(it for it in tree.findItems("*", Qt.MatchWildcard | Qt.MatchRecursive) if it.childCount() == 0
                    and it.text(0) == "throttle_value")

    leaves = [it for it in tree.findItems("*", Qt.MatchWildcard | Qt.MatchRecursive) if it.childCount() == 0]
    assert any(it.data(0, Qt.UserRole + 1) for it in leaves)  # the profile's signals are marked
    assert not channel().data(0, Qt.UserRole + 1)
    tree.clearSelection()
    channel().setSelected(True)
    page.add_selected_channels()
    qtbot.wait(50)
    assert channel().data(0, Qt.UserRole + 1)  # added: now it has the dot
    assert not channel().icon(0).isNull()


def test_a_border_dragged_with_the_mouse_pushes_columns_both_ways(qtbot, window, mf4_dir):
    from PySide6.QtCore import QPoint
    from PySide6.QtTest import QTest

    _load(qtbot, window, mf4_dir)
    window.resize(1500, 900)
    window.show()
    bar = window.side_bar
    bar.open(bar.titles.index("Signals"), sticky=True)
    h = window.signals_page.sig_table.horizontalHeader()
    qtbot.waitUntil(lambda: bool(h.property("columnSizer")), timeout=5000)
    sizer = h._column_sizer
    shown = sizer._visible()
    assert len(shown) >= 4
    vp = h.viewport()

    def sizes():
        return [h.sectionSize(c) for c in shown]

    def drag(c, dx):
        x = h.sectionViewportPosition(c) + h.sectionSize(c)
        QTest.mousePress(vp, Qt.LeftButton, Qt.NoModifier, QPoint(x, 5))
        for step in range(1, 11):
            QTest.mouseMove(vp, QPoint(x + dx * step // 10, 5))
        QTest.mouseRelease(vp, Qt.LeftButton, Qt.NoModifier, QPoint(x + dx, 5))

    width = sum(sizes())
    mins = [sizer.header_width(c) for c in shown]
    b = shown[2]  # the border right of the third column
    before = sizes()
    drag(b, -3000)  # far left: the third, second and first columns all go to their minimum
    after = sizes()
    assert after[:3] == mins[:3]
    assert after[4:] == before[4:]  # columns right of the neighbour untouched
    assert sum(after) == width
    drag(b, 3000)  # far right: every column right of the border goes to its minimum, none left of it moves
    again = sizes()
    assert again[:2] == mins[:2] and again[3:] == mins[3:]
    assert sum(again) == width


def test_enum_values_window_fits_its_content(qtbot):
    from src.kpi_studio.gui.pages.signals_page import EnumDialog

    long = {f"AUTO_EMERGENCY_BRAKING_PLANNER_STATE_{s}": i for i, s in
            enumerate(["UNSPECIFIED", "READY", "ACTIVE", "HOLD", "UNAVAILABLE", "DEGRADED"])}
    dlg = EnumDialog("state_fb", long)
    qtbot.addWidget(dlg)
    dlg.show()
    qtbot.wait(50)
    t = dlg.table
    fm = t.fontMetrics()
    widest = max(fm.horizontalAdvance(k) for k in long)
    assert t.columnWidth(1) >= widest + 12  # no text is cut off
    assert t.horizontalScrollBar().maximum() == 0 and t.verticalScrollBar().maximum() == 0
    blank = t.viewport().height() - t.rowCount() * t.verticalHeader().defaultSectionSize()
    assert blank < 3 * t.verticalHeader().defaultSectionSize()  # no big empty space below the values


def test_drag_signals_to_reorder_them(qtbot, window, mf4_dir):
    from PySide6.QtCore import QEvent, QMimeData, QPointF
    from PySide6.QtGui import QDropEvent

    _load(qtbot, window, mf4_dir)
    page = window.signals_page
    t = page.sig_table
    names = list(window.state.data["signals"])
    assert len(names) >= 3

    def shown():
        return [t.item(r, 0).text() for r in range(t.rowCount())][:len(names)]

    assert shown() == names
    t.selectRow(0)  # drag the first signal below the third one
    y = t.rowViewportPosition(2) + t.rowHeight(2) - 2

    class Drop(QDropEvent):
        def source(self):
            return t

    drop = Drop(QPointF(10, y), Qt.MoveAction, QMimeData(), Qt.LeftButton, Qt.NoModifier, QEvent.Type.Drop)
    assert page.eventFilter(t.viewport(), drop)
    want = names[1:3] + names[:1] + names[3:]
    assert list(window.state.data["signals"]) == want
    assert shown() == want
    assert {i.row() for i in t.selectedIndexes()} == {2}  # the moved signal stays selected
    page.move_signals([2], 0)  # and back to the top
    assert list(window.state.data["signals"]) == names
    assert page.drop_row(t.viewport().height() - 1) <= page.own_count()  # never among library signals


def test_condition_builder_lists_an_enum_signals_values(qtbot):
    from src.kpi_studio.core.expr import compile_expr
    from src.kpi_studio.gui.widgets.common import ConditionBuilder

    enums = {"fb_state": {"STATE_READY": 1, "STATE_UNSPECIFIED": 0, "on hold": 3}}
    dlg = ConditionBuilder(["speed", "fb_state"], enums=enums)
    qtbot.addWidget(dlg)
    sig, op, val = dlg.rows[0]
    sig.setCurrentText("speed")
    assert not val.pick_action.isVisible()  # not an enum: nothing to pick
    sig.setCurrentText("fb_state")
    assert val.pick_action.isVisible()
    assert dlg.values_of("fb_state") == ["STATE_UNSPECIFIED", "STATE_READY", '"on hold"']  # in value order
    op.setCurrentText("changes to")
    dlg.pick_value(val, op.currentText(), "STATE_READY")
    dlg.pick_value(val, op.currentText(), "STATE_UNSPECIFIED")  # one value: replaced
    assert val.text() == "STATE_UNSPECIFIED"
    op.setCurrentText("in")
    dlg.pick_value(val, op.currentText(), '"on hold"')  # a list: added
    assert val.text() == 'STATE_UNSPECIFIED, "on hold"'
    expr = dlg.expression()
    assert expr == 'fb_state in (STATE_UNSPECIFIED, "on hold")'
    compile_expr(expr, names={"fb_state", "speed"}, enums=enums)  # the condition compiles


def test_expression_picker_offers_the_values_of_enum_signals_in_the_text(qtbot):
    from src.kpi_studio.gui.widgets.common import ExprEdit

    enums = {"fb_state": {"READY": 1, "OFF": 0}, "gear": {"P": 0, "D": 2}}
    e = ExprEdit(lambda: ["fb_state", "gear", "speed"], enums_fn=lambda: enums)
    qtbot.addWidget(e)
    assert list(e.picker_sections()) == ["Signals"]  # no enum signal written yet
    e.setText("gear == D and fb_state == ")
    e.setCursorPosition(len(e.text()))
    sections = e.picker_sections()
    assert list(sections) == ["Values of fb_state", "Values of gear", "Signals"]  # nearest first
    assert sections["Values of fb_state"] == ["OFF", "READY"]
    e.insert_name("READY")
    assert e.text() == "gear == D and fb_state == READY"
    e.insert_name("OFF")  # picking again swaps the value next to the cursor
    assert e.text() == "gear == D and fb_state == OFF"
    e.setText("gap_dist")
    e.setCursorPosition(len(e.text()))
    e.insert_name("speed")  # a picked signal replaces the one in the field
    assert e.text() == "speed"
    e.setText("speed > 0 and ")
    e.setCursorPosition(len(e.text()))
    e.insert_name("gear")  # after a keyword and a space: added
    assert e.text() == "speed > 0 and gear"
    e.setText("abs(speed) > 1")
    e.setCursorPosition(len("abs"))
    e.insert_name("gear")  # a function name is not swapped
    assert e.text().startswith("abs ") and "gear" in e.text()


def test_condition_builder_groups_rows_with_brackets(qtbot):
    from src.kpi_studio.core.expr import compile_expr
    from src.kpi_studio.gui.widgets.common import ConditionBuilder

    dlg = ConditionBuilder(["a", "b", "c"])
    qtbot.addWidget(dlg)
    for _ in range(2):
        dlg.add_row()
    for (sig, op, val), (s, o, v) in zip(dlg.rows, [("a", ">", "1"), ("b", "==", "2"), ("c", "<", "3")]):
        sig.setCurrentText(s)
        op.setCurrentText(o)
        val.setText(v)
    (j0, o0, c0), (j1, o1, c1), (j2, o2, c2) = dlg.links
    assert not j0.isVisible() or not dlg.isVisible()  # the first row joins nothing
    assert o0.width() == c0.width() == o0.minimumWidth() == c0.maximumWidth()  # bracket boxes: one width
    o0.setCurrentText("(")
    assert dlg.bracket_problem() == "1 bracket not closed"
    assert not dlg.ok_button.isEnabled()
    c1.setCurrentText(")")
    j2.setCurrentText("or")
    o2.setCurrentText("not (")
    c2.setCurrentText(")")
    assert dlg.bracket_problem() is None and dlg.ok_button.isEnabled()
    expr = dlg.expression()
    assert expr == "(a > 1 and b == 2) or not (c < 3)"
    compile_expr(expr, names={"a", "b", "c"})
    o2.setCurrentText("")
    assert dlg.bracket_problem() == "a bracket is closed before it is opened"


def test_condition_builder_grows_to_fit_its_text(qtbot):
    from src.kpi_studio.gui.widgets.common import ConditionBuilder

    long = {"fb_state": {f"AUTO_EMERGENCY_BRAKING_PLANNER_STATE_{s}": i for i, s in enumerate(["A", "ACTIVE"])}}
    dlg = ConditionBuilder(["fb_state", "pb_state"], enums=long)
    qtbot.addWidget(dlg)
    dlg.show()
    qtbot.wait(50)
    dlg.add_row()
    for sig, op, val in dlg.rows:
        sig.setCurrentText("fb_state")
        val.setText("AUTO_EMERGENCY_BRAKING_PLANNER_STATE_ACTIVE")
    qtbot.wait(50)
    lay = dlg.layout()
    assert dlg.height() >= lay.totalHeightForWidth(dlg.width())  # nothing squeezed


def test_search_matches_are_highlighted_in_the_signal_tree(qtbot, window, mf4_dir):
    from src.kpi_studio.gui.widgets.common import HighlightDelegate, match_spans

    assert match_spans("speed", "ref[0]/Speed_x/speed") == [(7, 12), (15, 20)]  # every match, any case
    assert match_spans("lmc*it", "LmcAxTrgtLimit") == [(0, 14)]
    assert match_spans("*", "abc") == [] and match_spans("", "abc") == []
    _load(qtbot, window, mf4_dir)
    window.resize(1400, 850)
    window.show()
    bar = window.side_bar
    bar.open(bar.titles.index("Signals"), sticky=True)
    page = window.signals_page
    assert isinstance(page.tree.itemDelegateForColumn(0), HighlightDelegate)
    page.search.setText("speed")
    qtbot.wait(100)
    item = next(it for it in page.tree.findItems("*", Qt.MatchWildcard | Qt.MatchRecursive) if it.childCount() == 0)
    page.tree.setCurrentItem(item)
    img = page.tree.viewport().grab().toImage()  # paints without errors, selected row included
    rect = page.tree.visualItemRect(item)
    colors = {img.pixelColor(x, rect.center().y()).name() for x in range(rect.left(), rect.right(), 2)}
    assert HighlightDelegate.COLOR.name() in colors


def test_condition_builder_lists_signals_alphabetically(qtbot):
    from src.kpi_studio.gui.widgets.common import ConditionBuilder

    dlg = ConditionBuilder(["ttc", "accel_act", "Brake", "throttle"])
    qtbot.addWidget(dlg)
    sig = dlg.rows[0][0]
    assert [sig.itemText(i) for i in range(sig.count())] == ["accel_act", "Brake", "throttle", "ttc"]


def test_search_matches_are_highlighted_in_every_filtered_list(qtbot):
    from src.kpi_studio.gui.widgets.common import HighlightDelegate, NamePicker, ToolPalette

    picker = NamePicker({"Signals": ["ego_speed_kph", "brake"]})
    qtbot.addWidget(picker)
    assert isinstance(picker.list.itemDelegate(), HighlightDelegate)
    picker.filter.setText("eg")
    assert picker.list.itemDelegate().needle_fn() == "eg"
    picker.list.resize(300, 200)
    picker.list.viewport().grab()  # paints with the highlight
    palette = ToolPalette(names_fn=lambda: ["ego_speed_kph"])
    qtbot.addWidget(palette)
    assert isinstance(palette.tree.itemDelegate(), HighlightDelegate)
    palette.search.setText("speed")
    assert palette.tree.itemDelegate().needle_fn() == "speed"
    palette.tree.viewport().grab()


def test_signal_lists_are_alphabetical_everywhere(qtbot, window, mf4_dir):
    _load(qtbot, window, mf4_dir)
    names = window.state.signal_names()
    assert names == sorted(names, key=str.lower)
    viewer_list = [window.viewer.list.item(i).text() for i in range(window.viewer.list.count())]
    assert viewer_list == sorted(viewer_list, key=str.lower)
    picker = window.kpi_page.kpi_form.expr.picker_sections()["Signals"]  # the ▾ list of expression fields
    assert picker == sorted(picker, key=str.lower)


def test_every_ticked_signal_is_plotted_and_the_plots_scroll(qtbot, window, mf4_dir):
    _load(qtbot, window, mf4_dir)
    viewer = window.viewer
    window.resize(1400, 700)
    window.show()
    names = window.state.signal_names()
    viewer.host.clear()
    viewer.plot_signals(names)
    qtbot.waitUntil(lambda: bool(viewer.data is not None and viewer.worker is None and viewer._axes), timeout=20000)
    plotted = [n for ax in viewer._axes for n in viewer._ax_groups[ax]]
    assert len(viewer._axes) == len(plotted) >= 8 and not any(ax.get_title() for ax in viewer._axes)  # no cap
    import src.kpi_studio.gui.widgets.signal_viewer as sv
    assert viewer.canvas.minimumHeight() >= len(viewer._axes) * sv.AXIS_MIN_PX  # each keeps a readable height
    assert viewer.plot_scroll.widget() is viewer.canvas


def test_drag_signals_in_the_plot_list_to_reorder_the_subplots(qtbot, window, mf4_dir):
    _load(qtbot, window, mf4_dir)
    viewer = window.viewer
    window.resize(1400, 800)
    window.show()
    viewer.host.clear()
    viewer.plot_signals(["egoSpeed", "longActAccel", "aebTargetDecel"])
    qtbot.waitUntil(lambda: bool(viewer.data is not None and viewer.worker is None and viewer._axes), timeout=20000)
    lst = viewer.list

    def subplots():
        return [viewer._ax_groups[ax][0] for ax in viewer._axes]

    assert subplots() == ["aebTargetDecel", "egoSpeed", "longActAccel"]  # alphabetical at first
    rows = [i for i in range(lst.count()) if lst.item(i).text() == "longActAccel"]
    lst.move_rows(rows, 0)  # dragged to the top
    assert lst.item(0).text() == "longActAccel"
    assert subplots() == ["longActAccel", "aebTargetDecel", "egoSpeed"]
    assert lst.item(0).checkState() == Qt.Checked
    window.state.edited(structure=True)  # the list is rebuilt: the user's order stays
    qtbot.wait(50)
    viewer.refresh_names()
    assert lst.item(0).text() == "longActAccel"
    assert lst.supportedDropActions() == Qt.CopyAction  # a drag never removes signals from the list


def test_right_click_a_signal_to_draw_it_as_a_stepped_line(qtbot, window, mf4_dir):
    _load(qtbot, window, mf4_dir)
    viewer = window.viewer
    window.resize(1400, 800)
    window.show()
    viewer.host.clear()
    viewer.plot_signals(["egoSpeed", "longActAccel"])
    qtbot.waitUntil(lambda: bool(viewer.data is not None and viewer.worker is None and viewer._axes), timeout=20000)

    def style(name):
        ax = next(a for a in viewer._axes if name in viewer._names.get(a, []))
        return ax.get_lines()[viewer._names[ax].index(name)].get_drawstyle()

    assert style("egoSpeed") == "default"
    menu = viewer.signal_menu(["egoSpeed"])
    act = menu.actions()[0]
    assert act.text() == "Stepped line" and act.isCheckable() and not act.isChecked()
    act.trigger()
    assert style("egoSpeed") == "steps-post" and style("longActAccel") == "default"
    # a stepped line shows the samples held, never values interpolated between two samples
    import numpy as np
    ax = next(a for a in viewer._axes if "egoSpeed" in viewer._names[a])
    held = viewer.data["held"]["egoSpeed"]
    drawn = ax.get_lines()[viewer._names[ax].index("egoSpeed")].get_ydata()
    assert np.array_equal(np.asarray(drawn, dtype=float), np.asarray(held, dtype=float), equal_nan=True)
    assert viewer.signal_menu(["egoSpeed"]).actions()[0].isChecked()
    ax = next(a for a in viewer._axes if "egoSpeed" in viewer._names[a])
    sub = viewer.subplot_menu(ax).actions()[0]  # the same from the signal's name right of its subplot
    assert sub.isChecked()
    sub.trigger()  # unchecked: a plain line again
    assert style("egoSpeed") == "default"


def test_discrete_signals_are_stepped_unless_switched_off_in_the_plot(qtbot, window, mf4_dir):
    _load(qtbot, window, mf4_dir)
    viewer = window.viewer
    window.resize(1400, 800)
    window.show()
    viewer.host.clear()
    window.state.data["signals"]["egoSpeed"]["kind"] = "discrete"
    viewer.plot_signals(["egoSpeed", "longActAccel"])
    qtbot.waitUntil(lambda: bool(viewer.data is not None and viewer.worker is None and viewer._axes), timeout=20000)

    def style(name):
        ax = next(a for a in viewer._axes if name in viewer._names.get(a, []))
        return ax.get_lines()[viewer._names[ax].index(name)].get_drawstyle()

    assert style("egoSpeed") == "steps-post" and style("longActAccel") == "default"  # from Kind
    act = viewer.signal_menu(["egoSpeed"]).actions()[0]
    assert act.isChecked()
    act.trigger()  # the plot's choice wins over Kind
    assert style("egoSpeed") == "default"


def test_boolean_signals_are_shaded_strips_at_the_bottom(qtbot, window, mf4_dir):
    _load(qtbot, window, mf4_dir)
    viewer = window.viewer
    window.resize(1400, 800)
    window.show()
    viewer.host.clear()
    window.state.data["signals"]["longActAccel"]["kind"] = "boolean"
    viewer.plot_signals(["longActAccel", "egoSpeed"])
    qtbot.waitUntil(lambda: bool(viewer.data is not None and viewer.worker is None and viewer._axes), timeout=20000)
    top, bottom = viewer._axes
    assert viewer._names[top] == ["egoSpeed"] and viewer._names[bottom] == ["longActAccel"]
    line = bottom.get_lines()[0]
    assert line.get_drawstyle() == "steps-post"
    assert bottom.collections and bottom.collections[0].get_facecolor()[0][3] < 0.5  # light shade
    assert len(bottom.get_yticks()) == 0  # no value axis for a flag
    assert bottom.get_position().height < top.get_position().height  # a low strip


def test_right_click_a_plotted_signal_to_set_its_kind(qtbot, window, mf4_dir):
    _load(qtbot, window, mf4_dir)
    viewer = window.viewer
    window.resize(1400, 800)
    window.show()
    viewer.host.clear()
    viewer.plot_signals(["egoSpeed", "longActAccel"])
    qtbot.waitUntil(lambda: bool(viewer.data is not None and viewer.worker is None and viewer._axes), timeout=20000)
    assert viewer._names[viewer._axes[0]] == ["egoSpeed"]
    acts = {a.text(): a for a in viewer.signal_menu(["egoSpeed"]).actions()}
    assert acts["continuous"].isChecked() and not acts["boolean"].isChecked()
    acts["boolean"].trigger()
    assert window.state.data["signals"]["egoSpeed"]["kind"] == "boolean"
    assert viewer._names[viewer._axes[-1]] == ["egoSpeed"]  # straight to the bottom
    sub = {a.text(): a for a in viewer.subplot_menu(viewer._axes[-1]).actions()}
    assert sub["boolean"].isChecked() and sub["Stepped line"].isChecked()


def test_kind_changed_in_the_table_shows_in_the_plot_at_once(qtbot, window, mf4_dir):
    from src.kpi_studio.gui.pages.signals_page import KINDS
    _load(qtbot, window, mf4_dir)
    viewer = window.viewer
    window.resize(1400, 800)
    window.show()
    viewer.host.clear()
    viewer.plot_signals(["egoSpeed", "longActAccel"])
    qtbot.waitUntil(lambda: bool(viewer.data is not None and viewer.worker is None and viewer._axes), timeout=20000)
    table = window.signals_page.sig_table
    row = next(r for r in range(table.rowCount()) if table.item(r, 0).text() == "egoSpeed")
    ix = table.model().index(row, 3)
    delegate = table.itemDelegateForColumn(3)
    box = delegate.createEditor(table.viewport(), None, ix)
    box.hidePopup()
    box.setCurrentIndex(KINDS.index("boolean"))
    delegate.setModelData(box, table.model(), ix)
    assert viewer._names[viewer._axes[-1]] == ["egoSpeed"]  # at once, before the profile is checked again
    window.state.validate_now()  # the profile check re-reads the plotted recording with the new Kind
    qtbot.waitUntil(lambda: viewer.worker is None and bool(viewer.cache) and not viewer.queue, timeout=20000)
    qtbot.wait(50)
    assert viewer._names[viewer._axes[-1]] == ["egoSpeed"] and len(viewer._axes) == 2


def test_drag_the_border_to_widen_the_value_list(qtbot, window, mf4_dir):
    from types import SimpleNamespace
    _load(qtbot, window, mf4_dir)
    viewer = window.viewer
    window.resize(1400, 800)
    window.show()
    viewer.plot_signals(["egoSpeed", "longActAccel"])
    qtbot.waitUntil(lambda: bool(viewer.data is not None and viewer.worker is None and viewer._axes), timeout=20000)
    ax = viewer._axes[0]
    x1, y = ax.bbox.x1, (ax.bbox.y0 + ax.bbox.y1) / 2
    ev = SimpleNamespace(x=x1 + 2, y=y, button=1, inaxes=None, xdata=None, key=None)
    assert viewer.on_side_border(ev)
    assert not viewer.on_side_border(SimpleNamespace(x=x1 - 80, y=y))
    viewer._on_motion(ev)
    assert viewer.canvas.cursor().shape() == Qt.BitmapCursor and viewer.canvas.toolTip() == "Drag to resize"
    before = viewer.side_px
    viewer._on_press(ev)
    viewer._on_motion(SimpleNamespace(x=x1 - 150 * viewer.canvas.devicePixelRatioF(), y=y, button=1,
                                      inaxes=ax, xdata=None, key=None))
    viewer._on_release(ev)
    assert viewer.side_px > before + 100 and ax.bbox.x1 < x1 - 100  # the plots give the room
    assert viewer.cursors == [None, None]  # dragging the border places no cursor


def test_right_drag_zooms_and_ctrl_z_goes_back(qtbot, window, mf4_dir):
    from types import SimpleNamespace
    _load(qtbot, window, mf4_dir)
    viewer = window.viewer
    window.resize(1400, 800)
    window.show()
    viewer.plot_signals(["egoSpeed", "longActAccel"])
    qtbot.waitUntil(lambda: bool(viewer.data is not None and viewer.worker is None and viewer._axes), timeout=20000)
    top, bottom = viewer._axes
    full_x, full_y = top.get_xlim(), top.get_ylim()
    (px0, py0), (px1, py1) = top.transData.transform([(4.0, full_y[0] + 1), (8.0, full_y[1] - 1)])

    def ev(px, py):
        x, y = top.transData.inverted().transform((px, py))
        return SimpleNamespace(inaxes=top, xdata=x, ydata=y, x=px, y=py, button=3, key=None)

    viewer._on_press(ev(px0, py0))
    viewer._on_motion(ev((px0 + px1) / 2, (py0 + py1) / 2))
    viewer._on_release(ev(px1, py1))
    assert np.allclose(top.get_xlim(), (4.0, 8.0)) and np.allclose(bottom.get_xlim(), (4.0, 8.0))
    assert np.allclose(top.get_ylim(), (full_y[0] + 1, full_y[1] - 1))
    assert not top.patches  # the zoom box is gone
    viewer._on_press(ev(px0, py0))  # a second zoom, time only
    viewer._on_release(ev(px0 + 60, py0 + 2))
    assert top.get_xlim()[1] - top.get_xlim()[0] < 4.0
    viewer.undo_zoom()
    assert np.allclose(top.get_xlim(), (4.0, 8.0))
    viewer.undo_zoom()
    assert np.allclose(top.get_xlim(), full_x) and np.allclose(top.get_ylim(), full_y)
    viewer.undo_zoom()  # nothing left to undo
    assert np.allclose(top.get_xlim(), full_x)
    assert viewer.cursors == [None, None]


def test_condition_builder_opens_with_the_current_condition(qtbot):
    from src.kpi_studio.gui.widgets.common import ConditionBuilder
    for text, same in [
        ("rising(fb_state == AUTO_EMERGENCY_BRAKING_PLANNER_STATE_ACTIVE) or rising(pb_state == ACTIVE)", None),
        ("falling(fb_state == ACTIVE) and egoSpeed > 2", None),
        ("not ((a > 1 or b in (1, 2)) and c != -1)", None),
        ("(falling(m == 0) and rising(m == 3))", None),
        ("(a > 1 and b == 2) or not (c < 3)", "(a > 1 and b == 2) or not c < 3"),
        ("aeb_abort and x > 1", "aeb_abort != 0 and x > 1"),
    ]:
        dlg = ConditionBuilder(["a", "b", "c"], None, initial=text)
        qtbot.addWidget(dlg)
        assert dlg.expression() == (same or text)
    dlg = ConditionBuilder(["a"], None, initial="abs(a) > 3")  # can't be rows: said so, rows left empty
    qtbot.addWidget(dlg)
    assert dlg.expression() == "" and "can't be split into rows" in dlg.preview.text()
    rows = dlg.rows
    assert len(rows) == 1


def test_event_start_and_end_rules(qtbot, window, mf4_dir):
    _load(qtbot, window, mf4_dir)
    window.open_panel("Events & KPIs")
    page = window.kpi_page
    form = page.event_form
    events = window.state.data["events"]
    name = "served"
    events[name] = {"start": "egoSpeed > 1"}
    window.state.edited(structure=True)
    page.select_event(name)
    assert form.build_buttons[0].text() == "Edit condition"
    assert form.start_rule.currentText() == "first" and form.start_rule.isVisibleTo(form)
    form.start_rule.setCurrentText("last")
    assert events[name]["start_rule"] == "last"
    form.start_rule.setCurrentText("first")
    assert "start_rule" not in events[name]  # the default stays out of the profile
    form.end.setText("")
    assert not form.end_rule.isEnabled()  # 'last' needs an end condition
    form.end.setText("egoSpeed < 1")
    form.end.editingFinished.emit()
    form.end_rule.setCurrentText("last")
    assert events[name]["end_rule"] == "last"
    window.state.validate_now()
    assert window.state.compiled is not None, [str(i) for i in window.state.issues]


def test_kind_is_a_drop_down_list(qtbot, window, mf4_dir):
    from src.kpi_studio.gui.pages.signals_page import KINDS
    _load(qtbot, window, mf4_dir)
    page = window.signals_page
    table = page.sig_table
    row = next(r for r in range(table.rowCount()) if table.item(r, 0).text() == "egoSpeed")
    ix = table.model().index(row, 3)
    delegate = table.itemDelegateForColumn(3)
    box = delegate.createEditor(table.viewport(), None, ix)
    box.hidePopup()
    assert [box.itemText(i) for i in range(box.count())] == KINDS == ["continuous", "discrete", "boolean", "enum"]
    box.setCurrentIndex(KINDS.index("boolean"))
    delegate.setModelData(box, table.model(), ix)
    assert window.state.data["signals"]["egoSpeed"]["kind"] == "boolean"


def test_condition_row_fields(qtbot, window, mf4_dir):
    from src.kpi_studio.gui.widgets.common import ConditionRow
    _load(qtbot, window, mf4_dir)
    row = ConditionRow(window.state.signal_names, lambda: {"gear": {"P": 0, "D": 1}})
    qtbot.addWidget(row)
    row.set_expression("egoSpeed > 5")
    assert (row.sig.currentText(), row.op.currentText(), row.val.text()) == ("egoSpeed", ">", "5")
    assert not row.pick_action.isVisible()
    row.sig.setCurrentText("gear")
    assert row.pick_action.isVisible()
    row.op.setCurrentText("==")
    row.pick_value("D")
    assert row.expression() == "gear == D"
    row.set_expression("a > 1 and b < 2")  # more than one row: kept as typed
    assert row.op.currentText() == "expression" and row.expression() == "a > 1 and b < 2"


def test_which_conditions_have_draggable_columns(qtbot, window, mf4_dir):
    _load(qtbot, window, mf4_dir)
    window.resize(1500, 1000)
    window.show()
    window.open_panel("Events & KPIs")
    page = window.kpi_page
    page.new_kpi()
    form = page.kpi_form
    form.form.setCurrentIndex([f for f, _ in kpi_page.FORMS].index("range"))
    form.reducer.setCurrentIndex(form.reducer.findData("which"))
    form.which.set_items({"PB": "egoSpeed > 5"})
    table = form.which.table
    assert [table.horizontalHeaderItem(c).text() for c in range(4)] == ["Label", "Signal", "Operator", "Value"]
    row = form.which.rows[0]
    assert [table.cellWidget(0, c) for c in (1, 2, 3)] == [row.sig, row.op, row.val]
    h = table.horizontalHeader()
    qtbot.waitUntil(lambda: bool(h.property("columnSizer")))
    sizer = h._column_sizer
    x0 = h.sectionViewportPosition(2)  # the border between Signal and Operator
    before = h.sectionSize(1)
    sizer._drag = (1, x0, {k: h.sectionSize(k) for k in sizer._visible()})
    sizer.drag_to(x0 + 40)
    assert h.sectionSize(1) == before + 40
    x1 = h.sectionViewportPosition(3)  # widen Operator past what its box used to stop at
    sizer._drag = (2, x1, {k: h.sectionSize(k) for k in sizer._visible()})
    sizer.drag_to(x1 + 80)
    qtbot.wait(50)
    for c in (1, 2, 3):  # every field fills its whole column, whatever the width
        assert table.cellWidget(0, c).width() >= table.columnWidth(c) - 1, c  # less the grid line
    assert form.which.items() == {"PB": "egoSpeed > 5"}


def _wide_for_plots():
    import pandas as pd

    from src.kpi_studio.report.plots import TEXT

    return pd.DataFrame({"file": ["f"] * 4, "event": ["e"] * 4, "occurrence": [1, 2, 3, 4],
                         "v": [10.0, 20.0, 30.0, 40.0], "gap": [1.0, 2.0, 3.0, 4.0], "dist": [5.0, 6.0, 7.0, 8.0],
                         "decel": [-3.0, -5.0, None, -7.0], "type": [1.0, 2.0, 3.0, 0.0],
                         TEXT + "type": ["PB", "FB", "PB + FB", "none"]})


def test_scatter_colour_by_a_kpi():
    from src.kpi_studio.core.model import PlotDef
    from src.kpi_studio.report.plots import kpi_figure

    wide = _wide_for_plots()
    fig = kpi_figure(PlotDef(type="scatter", x="v", y="gap", color_by="decel"), wide, {"decel": "m/s^2"}, ["f"])
    scaled, missing = fig.data
    assert list(scaled.marker.color) == [-3.0, -5.0, -7.0] and scaled.marker.coloraxis == "coloraxis"
    assert fig.layout.coloraxis.colorbar.title.text == "decel [m/s^2]"
    assert missing.name == "no decel" and list(missing.x) == [30.0]
    fig = kpi_figure(PlotDef(type="scatter", x="v", y="gap", color_by="type"), wide, {}, ["f"])
    assert [t.name for t in fig.data] == ["FB", "PB", "PB + FB", "none"]  # a text KPI: one colour per text
    assert len({t.marker.color for t in fig.data}) == 4


def test_merged_scatter_plots_share_one_chart():
    from src.kpi_studio.core.model import PlotDef
    from src.kpi_studio.report.plots import scatter_figure

    wide = _wide_for_plots()
    a = PlotDef(type="scatter", x="v", y="gap", color_by="none", merge="braking")
    b = PlotDef(type="scatter", x="v", y="dist", color_by="none", merge="braking")
    fig = scatter_figure([a, b], wide, {"gap": "m", "dist": "m"}, ["f"], "braking")
    assert [t.name for t in fig.data] == ["gap", "dist"]
    assert [t.marker.symbol for t in fig.data] == ["circle", "diamond"]
    assert fig.layout.yaxis.title.text == "gap, dist [m]"
    assert "yaxis2" not in fig.layout.to_plotly_json()  # same unit: one axis
    fig = scatter_figure([a, b], wide, {"gap": "m", "dist": "s"}, ["f"], "braking")
    assert fig.layout.yaxis.title.text == "gap [m], dist [s]"  # still one x-y plot
    assert "yaxis2" not in fig.layout.to_plotly_json()


def test_merge_and_colour_options_in_the_plot_table(qtbot, window, mf4_dir, tmp_path):
    _load(qtbot, window, mf4_dir)
    page = window.run_page
    kpis = window.state.kpi_names()
    window.state.data["plots"] = [{"type": "scatter", "x": kpis[0], "y": kpis[1]},
                                  {"type": "scatter", "x": kpis[0], "y": kpis[2]},
                                  {"type": "box", "y": kpis[1]}]
    window.state.edited(structure=True)
    page.refresh_plots()
    colour = page.plots.cellWidget(0, 4)
    assert [colour.itemText(i) for i in range(3)] == ["file", "event", "none"] and colour.findText(kpis[2]) >= 0
    colour.setCurrentText(kpis[2])
    merge = page.plots.cellWidget(1, 8)
    assert [merge.itemText(i) for i in range(merge.count())] == ["own chart", "row 1"]  # box row 3 not offered
    merge.setCurrentIndex(1)  # row 2 joins row 1's chart
    assert page.plots.cellWidget(2, 8) is None  # box plots are not merged
    plots = window.state.data["plots"]
    assert plots[0]["color_by"] == kpis[2] and plots[0]["merge"] == plots[1]["merge"] == "chart1"
    qtbot.waitUntil(lambda: page.plots.cellWidget(0, 8).currentText() == "row 2")  # shown on both rows
    window.state.validate_now()
    assert window.state.compiled is not None, [str(i) for i in window.state.issues]
    page.plots.cellWidget(0, 8).setCurrentIndex(0)  # row 1 back to its own chart: row 2 is alone again
    assert "merge" not in plots[0] and "merge" not in plots[1]
    plots[2]["merge"] = "together"
    window.state.validate_now()
    assert any("only scatter plots can be merged" in str(i) for i in window.state.issues)


def test_common_axis_from_the_right_click_menu(qtbot, window, mf4_dir, monkeypatch):
    from src.kpi_studio.gui.widgets import signal_viewer as sv

    _load(qtbot, window, mf4_dir)
    viewer = window.viewer
    window.resize(1400, 900)
    window.show()
    viewer.plot_signals(["egoSpeed", "longActAccel", "aebTargetDecel"])
    qtbot.waitUntil(lambda: bool(viewer.data is not None and viewer.worker is None and viewer._axes), timeout=20000)
    assert len(viewer._axes) == 3

    def action(menu, text):
        for a in menu.actions():
            if a.text() == text:
                return a
            if a.menu() is not None and (found := action(a.menu(), text)) is not None:
                return found
        return None

    # no axis yet: one signal cannot share, two or more make a new axis at once
    menu = viewer.signal_menu(["egoSpeed"])
    assert not action(menu, "Common axis").isEnabled()
    menu = viewer.signal_menu(["longActAccel", "aebTargetDecel"])
    action(menu, "Common axis").trigger()
    assert viewer.common == {"longActAccel": "axis 1", "aebTargetDecel": "axis 1"}
    assert len(viewer._axes) == 2
    assert ["longActAccel", "aebTargetDecel"] in [viewer._ax_groups[a] for a in viewer._axes]
    ax = next(a for a in viewer._axes if len(viewer._ax_groups[a]) == 2)
    assert "axis 1" in [t.get_text() for t in ax.texts]  # the axis is named in its subplot
    items = {viewer.list.item(i).text(): viewer.list.item(i) for i in range(viewer.list.count())}
    assert items["longActAccel"].data(sv.AXIS_ROLE)["name"] == "axis 1" == items["aebTargetDecel"].data(sv.AXIS_ROLE)["name"]
    assert items["egoSpeed"].data(sv.AXIS_ROLE) is None
    # listed together under the axis' name: the first carries the name row, the last ends the line
    order = [viewer.list.item(i).text() for i in range(viewer.list.count())]
    i = order.index("aebTargetDecel")
    assert order[i + 1] == "longActAccel"
    first, last = items["aebTargetDecel"].data(sv.AXIS_ROLE), items["longActAccel"].data(sv.AXIS_ROLE)
    assert first["first"] and not first["last"] and last["last"] and not last["first"]
    rect = viewer.list.visualItemRect(items["aebTargetDecel"])
    assert rect.height() >= viewer.list.visualItemRect(items["egoSpeed"]).height() + sv.HEADER_PX
    assert viewer.list.header_at(rect.topLeft() + QPoint(30, 5)) == "axis 1"
    assert viewer.list.header_at(rect.bottomLeft() + QPoint(30, -3)) is None
    menu = viewer.axis_menu("axis 1")
    assert [a.text() for a in menu.actions() if a.text()] == ["Range…", "Rename…", "Select its signals",
                                                               "Remove common axis"]

    # an axis exists: a signal joins it, or starts a new one
    menu = viewer.signal_menu(["egoSpeed"])
    assert action(menu, "Join axis 1") is not None and action(menu, "New common axis") is not None
    action(menu, "New common axis").trigger()
    assert viewer.common["egoSpeed"] == "axis 2" and len(viewer._axes) == 2
    menu = viewer.signal_menu(["egoSpeed"])
    action(menu, "Join axis 1").trigger()
    assert len(viewer._axes) == 1 and len(viewer._ax_groups[viewer._axes[0]]) == 3
    assert set(viewer.common.values()) == {"axis 1"}

    # the range of a common axis
    monkeypatch.setattr(viewer, "ask_axis_range", lambda label: (-12.0, 30.0))
    menu = viewer.signal_menu(["egoSpeed"])
    action(menu, "Range of common axis…").trigger()
    assert viewer._axes[0].get_ylim() == (-12.0, 30.0)
    viewer.draw()  # kept when drawn again
    assert viewer._axes[0].get_ylim() == (-12.0, 30.0)
    monkeypatch.setattr(viewer, "ask_axis_range", lambda label: None)  # automatic again
    menu = viewer.signal_menu(["egoSpeed"])
    action(menu, "Range of common axis…").trigger()
    assert "axis 1" not in viewer.axis_range and viewer._axes[0].get_ylim() != (-12.0, 30.0)

    # rename
    monkeypatch.setattr(sv.QInputDialog, "getText", staticmethod(lambda *a, **k: ("accel", True)))
    menu = viewer.signal_menu(["egoSpeed"])
    action(menu, "Rename common axis…").trigger()
    assert set(viewer.common.values()) == {"accel"}

    rng_tag = next(viewer.list.item(i).data(sv.AXIS_ROLE) for i in range(viewer.list.count())
                   if viewer.list.item(i).data(sv.AXIS_ROLE))
    assert rng_tag["name"] == "accel"
    menu = viewer.signal_menu(["egoSpeed"])
    action(menu, "Remove from common axis").trigger()
    assert len(viewer._axes) == 2 and "egoSpeed" not in viewer.common
    viewer.uncommon(["longActAccel"])  # one left: the axis is gone
    assert viewer.common == {} and len(viewer._axes) == 3
    assert all(viewer.list.item(i).data(sv.AXIS_ROLE) is None for i in range(viewer.list.count()))


def test_common_axes_kept_in_the_profile(qtbot, window, mf4_dir, tmp_path):
    import yaml

    _load(qtbot, window, mf4_dir)
    viewer, state = window.viewer, window.state
    viewer.new_common_axis(["longActAccel", "aebTargetDecel"])
    viewer.set_axis_range("axis 1", (-12, 4))
    viewer.new_common_axis(["egoSpeed", "yawRate"])
    viewer.rename_axis("axis 2", "motion")
    assert state.dirty
    path = tmp_path / "axes.kpi.yaml"
    state.save(path)
    saved = yaml.safe_load(path.read_text())["common_axes"]
    assert {(d["name"], frozenset(d["signals"]), tuple(d.get("range") or ())) for d in saved} == {
        ("axis 1", frozenset({"longActAccel", "aebTargetDecel"}), (-12.0, 4.0)),
        ("motion", frozenset({"egoSpeed", "yawRate"}), ())}

    state.new()  # a new profile has none
    assert viewer.common == {} and viewer.axis_range == {}
    state.load(path)
    qtbot.waitUntil(lambda: viewer.list.count() > 0)
    assert viewer.common == {"longActAccel": "axis 1", "aebTargetDecel": "axis 1",
                             "egoSpeed": "motion", "yawRate": "motion"}
    assert viewer.axis_range == {"axis 1": (-12.0, 4.0)}
    state.validate_now()
    assert not state.dirty  # opening it does not count as a change
    assert not [i for i in state.issues if "common_axes" in str(i)]

    viewer.uncommon(["egoSpeed", "yawRate"])
    assert [d["name"] for d in state.data["common_axes"]] == ["axis 1"] and state.dirty
    viewer.separate_all()
    assert "common_axes" not in state.data


def test_ticked_signals_kept_in_the_profile(qtbot, window, mf4_dir, tmp_path):
    import yaml

    _load(qtbot, window, mf4_dir)
    viewer, state = window.viewer, window.state
    viewer.plot_signals(["egoSpeed", "egoSpeedKph"])  # a measured and a derived signal
    qtbot.waitUntil(lambda: bool(viewer.worker is None and viewer._axes), timeout=20000)
    assert set(state.data["shown_signals"]) == {"egoSpeed", "egoSpeedKph"} and state.dirty
    path = tmp_path / "shown.kpi.yaml"
    state.save(path)
    assert set(yaml.safe_load(path.read_text())["shown_signals"]) == {"egoSpeed", "egoSpeedKph"}

    state.new()
    assert viewer.checked() == []
    state.load(path)
    qtbot.waitUntil(lambda: set(viewer.checked()) == {"egoSpeed", "egoSpeedKph"}, timeout=10000)
    qtbot.waitUntil(lambda: bool(viewer.worker is None and viewer._axes), timeout=20000)
    state.validate_now()
    assert not state.dirty and len(viewer._axes) == 2  # plotted again, and not counted as a change
    viewer.uncheck_all()
    assert "shown_signals" not in state.data and state.dirty


def test_common_axis_signals_always_share_a_subplot(qtbot, window, mf4_dir):
    _load(qtbot, window, mf4_dir)
    viewer = window.viewer
    viewer.plot_signals(["longActAccel", "aebTargetDecel", "egoSpeed"])
    qtbot.waitUntil(lambda: bool(viewer.worker is None and viewer._axes), timeout=20000)
    viewer.new_common_axis(["longActAccel", "aebTargetDecel"])
    viewer.host = {"longActAccel": "aebTargetDecel", "aebTargetDecel": "longActAccel"}  # stale links
    viewer.draw()
    assert sorted(map(sorted, viewer._ax_groups.values())) == [["aebTargetDecel", "longActAccel"], ["egoSpeed"]]
    viewer.set_common_axis(["longActAccel"], "axis 1")  # joining its own axis changes nothing
    assert viewer.common == {"longActAccel": "axis 1", "aebTargetDecel": "axis 1"}


def test_common_axes_unknown_signal_is_a_warning():
    from src.kpi_studio.core.model import Profile
    from src.kpi_studio.core.validate import compile_profile

    p = Profile.model_validate({"name": "x", "signals": {"a": {"channels": ["a"]}},
                                "common_axes": [{"name": "g", "signals": ["a", "nope"], "range": [0, 1]}]})
    cp = compile_profile(p)
    assert any("unknown signal 'nope'" in i.message for i in cp.warnings)


def test_ctrl_click_selects_on_macos(qtbot, window, monkeypatch):
    from PySide6.QtTest import QTest

    from src.kpi_studio.gui.widgets import signal_viewer as sv

    monkeypatch.setattr(sv, "CTRL_CLICK_SELECTS", True)
    lst = window.viewer.list
    window.show()
    for n in ("a", "b", "c", "d"):
        lst.addItem(n)
    shown = []
    class _Menu:
        def exec(self, *_):
            pass

    monkeypatch.setattr(window.viewer, "signal_menu", lambda names: shown.append(names) or _Menu())
    rect = lambda r: lst.visualItemRect(lst.item(r)).center()  # noqa: E731
    QTest.mouseClick(lst.viewport(), Qt.LeftButton, Qt.NoModifier, rect(0))
    QTest.mouseClick(lst.viewport(), Qt.RightButton, Qt.MetaModifier, rect(2))  # macOS Ctrl+click
    window.viewer._list_menu(rect(2))
    assert [i.text() for i in lst.selectedItems()] == ["a", "c"] and shown == []
    QTest.mouseClick(lst.viewport(), Qt.RightButton, Qt.NoModifier, rect(2))  # a real right click
    window.viewer._list_menu(rect(2))
    assert shown == [["a", "c"]]


def test_common_axis_range_dialog(qtbot, window):
    from PySide6.QtCore import QTimer

    viewer = window.viewer
    seen = {}

    def fill():
        dlg = viewer._range_dialog
        from PySide6.QtWidgets import QCheckBox, QDialogButtonBox, QLineEdit
        dlg.findChild(QCheckBox).setChecked(False)
        lo, hi = dlg.findChildren(QLineEdit)
        lo.setText("5")
        hi.setText("1")
        dlg.findChild(QDialogButtonBox).button(QDialogButtonBox.Ok).click()
        seen["error"] = dlg.isVisible()
        hi.setText("9")
        dlg.findChild(QDialogButtonBox).button(QDialogButtonBox.Ok).click()

    QTimer.singleShot(0, fill)
    assert viewer.ask_axis_range("axis 1") == (5.0, 9.0)
    assert seen["error"]  # min above max is refused


def test_software_version_column_in_files(qtbot, window, mf4_dir):
    _load(qtbot, window, mf4_dir)
    page = window.files_page
    col = page.COLS.index("SW version")
    f = window.state.files[0]
    page.table.item(0, col).setText("SW 1.2.3")
    assert window.state.versions[f] == "SW 1.2.3"
    assert "SW 1.2.3" in str(page.settings.value("files/sw_versions"))  # remembered for next time
    assert not page.table.item(0, 3).flags() & Qt.ItemIsEditable  # only the version is typed in
    page.refresh()
    assert page.table.item(0, col).text() == "SW 1.2.3"
    page.table.item(0, col).setText("")
    assert f not in window.state.versions


def test_formula_is_built_from_rows_of_earlier_kpis(qtbot, window, mf4_dir):
    _load(qtbot, window, mf4_dir)
    state = window.state
    state.data["events"]["served"] = {"start": "egoSpeed > 1", "moments": {"m1": {"first": "egoSpeed > 2"}}}
    state.data["kpis"] += [{"name": "spd", "event": "served", "value": "egoSpeed", "at": "m1"},
                           {"name": "gap", "event": "served", "value": "egoSpeed", "at": "start"},
                           {"name": "fail", "event": "served", "formula": "spd > 0.5 or gap == 1"},
                           {"name": "ratio", "event": "served", "formula": "spd / gap"}]
    state.edited(structure=True)
    form = window.kpi_page.kpi_form
    n = len(state.data["kpis"])
    form.load(n - 2)
    f = form.formula
    assert not f.typed.isChecked() and len(f.rows.rows) == 2
    assert [f.rows.rows[0][0].itemText(i) for i in range(f.rows.rows[0][0].count())] == ["gap", "spd"]  # earlier KPIs
    assert f.rows.links[1][0].currentText() == "or"
    f.rows.links[1][0].setCurrentText("xor")
    assert state.data["kpis"][n - 2]["formula"] == "(spd > 0.5) ^ (gap == 1)"
    form.load(n - 1)  # arithmetic doesn't fit rows: typed
    assert f.typed.isChecked() and f.line.text() == "spd / gap" and f.text() == "spd / gap"
    state.validate_now()
    assert state.compiled is not None, [str(i) for i in state.issues]


def test_a_formula_kpi_does_not_widen_the_events_form(qtbot, window, mf4_dir):
    from PySide6.QtCore import QPoint

    _load(qtbot, window, mf4_dir)
    window.resize(1100, 800)
    window.show()
    bar = window.side_bar
    i = bar.titles.index("Events & KPIs")
    bar.set_width(i, 900)
    bar.open(i, sticky=True)
    state = window.state
    state.data["events"]["served"] = {"start": "egoSpeed > 1", "end": "egoSpeed < 0.5"}
    state.data["kpis"] += [{"name": "a", "event": "served", "value": "egoSpeed", "at": "start"},
                           {"name": "f", "event": "served", "formula": "a > 1 or a < 0"}]
    state.edited(structure=True)
    kp = window.kpi_page
    kp.kpis.setCurrentRow(len(state.data["kpis"]) - 1)  # the formula's rows are shown
    qtbot.wait(50)
    kp.events.setCurrentRow(list(state.data["events"]).index("served"))
    qtbot.wait(100)
    view = kp.form_scroll.viewport()
    for b in kp.event_form.build_buttons:
        assert b.mapTo(view, QPoint(b.width(), 0)).x() <= view.width()
    assert kp.form_scroll.horizontalScrollBar().maximum() == 0
