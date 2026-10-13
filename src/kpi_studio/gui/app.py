"""Start the KPI Studio GUI:  python -m src.kpi_studio.gui.app"""

from __future__ import annotations

import logging
import multiprocessing
import sys

from PySide6.QtWidgets import QApplication

from src.kpi_studio.gui.main_window import MainWindow


def main(argv=None) -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    app = QApplication.instance() or QApplication(argv or sys.argv)
    app.setApplicationName("KPI Studio")
    app.setStyle("Fusion")  # same look on macOS, Windows and Linux; the native macOS style squeezes forms
    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(main())
