import numpy as np

from src.kpi_studio.io.mf4 import index_file


def _meta_file(path):
    from asammdf import MDF, Signal
    from asammdf.blocks.v4_blocks import ChannelConversion

    t = np.arange(0, 1, 0.01)
    lin = ChannelConversion(conversion_type=1, a=1.0, b=0.0)
    lin.unit = "kph"  # unit stored on the conversion, channel unit empty (as in the EVC logs)
    vt = {"val_0": 0, "text_0": "OFF", "val_1": 3, "text_1": "SERVED", "default": b"?"}
    m = MDF(version="4.10")
    m.append([Signal(t * 10, t, name="VehicleSpeed", unit="", conversion=lin,
                     comment="<CNcomment>\n<TX>Use in conjunction\n with VehicleSpeedQF.</TX></CNcomment>"),
              Signal((t > 0.5).astype(int) * 3, t, name="MotType", conversion=vt, comment="motion type")],
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
    w.state.edited(structure=True)
    w.signals_page.fill_from_files()
    sigs = w.state.data["signals"]
    assert sigs["VehicleSpeed"]["unit"] == "kph"
    assert sigs["VehicleSpeed"]["description"].startswith("Use in conjunction")
    assert sigs["Mot"]["unit"] == "-" and sigs["Mot"]["kind"] == "discrete"  # filled units are kept
    # adding from the channel tree takes the same information
    page = w.signals_page
    page.search.setText("MotType")
    item = page.tree.topLevelItem(0).child(0)
    item.setSelected(True)
    page.add_selected_channels()
    new = sigs["MotType"]
    assert new["kind"] == "discrete" and new["description"] == "motion type"
