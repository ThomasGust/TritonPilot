from __future__ import annotations

import argparse
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PyQt6.QtWidgets import QApplication

from analysis.gui.iceberg_tracking_window import IcebergTrackingWindow
from gui.style import apply_modern_style


def build_parser() -> argparse.ArgumentParser:
    return argparse.ArgumentParser(
        description="Standalone GUI for MATE ROV iceberg tracking threat assessment.",
    )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    parser.parse_args(argv)

    app = QApplication(sys.argv)
    apply_modern_style(app)

    window = IcebergTrackingWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
