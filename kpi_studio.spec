# -*- mode: python ; coding: utf-8 -*-
# Build KPI Studio:  pyinstaller kpi_studio.spec
import sys
from pathlib import Path

import matplotlib
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = Path.cwd()
SRC = ROOT / "src"

# Files read at runtime through src.utils.path_manager.get_resource (relative to src/)
datas = [
    (str(SRC / "kpi_studio" / "library"), "kpi_studio/library"),
    (str(SRC / "kpi_studio" / "profiles"), "kpi_studio/profiles"),
    (str(SRC / "config" / "enum_definitions.yaml"), "config"),
    (str(ROOT / "docs" / "kpi_studio_usage.md"), "docs"),
]
datas += [(matplotlib.get_data_path(), "matplotlib/mpl-data")]
datas += collect_data_files("plotly")    # plotly.min.js, written next to the HTML plots
datas += collect_data_files("asammdf")

hiddenimports = (
    collect_submodules("src.kpi_studio")   # toolbox modules are imported by name
    + [
        "matplotlib.backends.backend_qtagg",
        "PySide6.QtCore",
        "PySide6.QtGui",
        "PySide6.QtWidgets",
        "openpyxl",
        "yaml",
        "pydantic",
        "scipy.signal",
    ]
)

a = Analysis(
    ["run_kpi_studio.py"],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "pyecharts"],
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data)

exe = EXE(
    pyz,
    a.scripts,
    exclude_binaries=True,
    name="KPI_Studio",
    console=False,
    icon=None,
)

coll = COLLECT(exe, a.binaries, a.zipfiles, a.datas, strip=False, upx=True, name="KPI_Studio")

if sys.platform == "darwin":
    app = BUNDLE(coll, name="KPI_Studio.app", icon=None, bundle_identifier="com.jackwang401.kpistudio")
