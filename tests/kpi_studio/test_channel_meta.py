import numpy as np

from src.kpi_studio.io.mf4 import index_file


def _meta_file(path):
    from asammdf import MDF, Signal
    from asammdf.blocks.v4_blocks import ChannelConversion

    t = np.arange(0, 1, 0.01)
    lin = ChannelConversion(conversion_type=1, a=1.0, b=0.0)
    lin.unit = "kph"  # unit stored on the conversion, channel unit empty (as in the EVC logs)
    vt = {"val_0": 0, "text_0": "OFF", "val_1": 3, "text_1": "SERVED", "default": b""}
    # a physical signal with one error marker (4095 = Fault): not an enum
    g_conv = ChannelConversion(conversion_type=1, a=0.01, b=0.0)
    g_conv.unit = "g"
    marker = ChannelConversion(conversion_type=7, val_param_nr=1, ref_param_nr=2, val_0=4095.0, text_0=b"Fault",
                               default_addr=g_conv)
    m = MDF(version="4.10")
    m.append([Signal(t * 10, t, name="VehicleSpeed", unit="", conversion=lin,
                     comment="<CNcomment>\n<TX>Use in conjunction\n with VehicleSpeedQF.</TX></CNcomment>"),
              Signal((t > 0.5).astype(int) * 3, t, name="MotType", conversion=vt, comment="motion type"),
              Signal(np.full(t.shape, 100), t, name="VertAcc", conversion=marker)],
             acq_name="EVC")
    m.save(path, overwrite=True)
    return path


def test_index_reads_conversion_unit_comment_and_text_tables(tmp_path):
    idx = index_file(_meta_file(tmp_path / "m.mf4"))
    ch = {c.name: c for c in idx.channels}
    assert ch["VehicleSpeed"].unit == "kph"
    assert ch["VehicleSpeed"].comment == "Use in conjunction with VehicleSpeedQF."
    assert not ch["VehicleSpeed"].discrete
    assert ch["MotType"].discrete and ch["MotType"].comment == "motion type"
    assert ch["MotType"].enum == (("OFF", 0), ("SERVED", 3))
    assert not ch["VertAcc"].discrete and ch["VertAcc"].enum == () and ch["VertAcc"].unit == "g"


def test_signals_get_unit_kind_description_from_the_file(qtbot, tmp_path):
    from PySide6.QtCore import QSettings
    from src.kpi_studio.gui.main_window import MainWindow

    f = _meta_file(tmp_path / "m.mf4")
    w = MainWindow(QSettings(str(tmp_path / "s.ini"), QSettings.IniFormat))
    w._confirm_discard = lambda: True
    qtbot.addWidget(w)
    w.files_page.add_files([str(f)])
    qtbot.waitUntil(lambda: len(w.state.indexes) == 1, timeout=10000)
    # an existing signal without unit, as in older profiles
    w.state.data["signals"]["VehicleSpeed"] = {"channels": ["EVC::VehicleSpeed"]}
    w.state.data["signals"]["Mot"] = {"channels": ["MotType"], "unit": "-"}
    w.state.edited(structure=True)  # no button: filled as soon as signals or files change
    sigs = w.state.data["signals"]
    qtbot.waitUntil(lambda: sigs["VehicleSpeed"].get("unit") == "kph", timeout=5000)
    assert sigs["VehicleSpeed"]["unit"] == "kph"
    assert sigs["VehicleSpeed"]["description"].startswith("Use in conjunction")
    assert sigs["Mot"]["unit"] == "-" and sigs["Mot"]["kind"] == "discrete"  # filled units are kept
    assert sigs["Mot"]["enum"] == {"OFF": 0, "SERVED": 3}
    row = next(r for r in range(w.signals_page.sig_table.rowCount())
               if w.signals_page.sig_table.item(r, 0).text() == "Mot")
    assert w.signals_page.sig_table.item(row, 4).text() == "2 values: OFF, …"
    from src.kpi_studio.gui.pages.signals_page import EnumDialog
    dlg = EnumDialog("Mot", sigs["Mot"]["enum"])
    qtbot.addWidget(dlg)
    assert [dlg.table.item(r, 1).text() for r in range(dlg.table.rowCount())] == ["OFF", "SERVED"]
    dlg.table.item(1, 1).setText("ON")
    assert dlg.values() == {"OFF": 0, "ON": 3}
    # the file's texts work in expressions
    w.state.data["events"]["served"] = {"start": 'rising(Mot == "SERVED")'}
    w.state.edited(structure=True)
    w.state.validate_now()
    assert w.state.compiled is not None, [str(i) for i in w.state.issues]
    # adding from the channel tree takes the same information
    page = w.signals_page
    page.search.setText("MotType")
    item = page.tree.topLevelItem(0).child(0)
    item.setSelected(True)
    page.add_selected_channels()
    new = sigs["MotType"]
    assert new["kind"] == "discrete" and new["description"] == "motion type"
    # a table typed by hand is kept
    sigs["Mot2"] = {"channels": ["MotType"], "enum": {"A": 0, "B": 3}}
    w.state.edited(structure=True)
    qtbot.waitUntil(lambda: sigs["Mot2"].get("description") == "motion type", timeout=5000)
    assert sigs["Mot2"]["enum"] == {"A": 0, "B": 3}


def test_enum_values_decode_with_the_files_table(tmp_path):
    from src.kpi_studio.core.align import build_frame
    from src.kpi_studio.core.model import SignalDef
    from src.kpi_studio.io.mf4 import read_signals

    f = _meta_file(tmp_path / "m.mf4")
    defs = {"Mot": SignalDef(channels=["MotType"], kind="discrete", enum={"OFF": 0, "SERVED": 3})}
    raw, issues = read_signals(f, defs)
    frame, more, _ = build_frame(raw, defs, 100.0, {})
    v = frame.values["Mot"]
    assert set(np.unique(v[np.isfinite(v)])) == {0.0, 3.0}


def test_enum_column_round_trip():
    from src.kpi_studio.gui.pages.signals_page import format_enum, parse_enum

    assert parse_enum(format_enum({"Off": 0, "Comfort": 1})) == {"Off": 0, "Comfort": 1}
    assert parse_enum("gear_tbl") == "gear_tbl" and parse_enum("") is None
