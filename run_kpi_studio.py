"""KPI Studio launcher (also the PyInstaller entry point).

    python run_kpi_studio.py              -> GUI
    python run_kpi_studio.py run ...      -> command line (same as python -m src.kpi_studio.cli)
"""

import multiprocessing
import sys


def main() -> int:
    if len(sys.argv) > 1:
        from src.kpi_studio.cli import main as cli_main
        return cli_main(sys.argv[1:])
    from src.kpi_studio.gui.app import main as gui_main
    return gui_main()


if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(main())
