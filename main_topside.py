"""Topside GUI entry point for TritonPilot.

This module intentionally does only startup wiring: create the Qt application,
apply shared styling, and hand control to ``MainWindow``. Keeping the entry
point small makes tests and field debugging easier because the live services
are created in one predictable place.
"""

import sys

from PyQt6.QtWidgets import QApplication

from config import STREAMS_FILE
from gui.main_window import MainWindow
from gui.style import apply_modern_style


def main():
    """Start the topside operator application."""
    app = QApplication(sys.argv)
    apply_modern_style(app)

    win = MainWindow(streams_path=str(STREAMS_FILE))
    win.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()

