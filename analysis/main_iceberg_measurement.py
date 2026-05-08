from __future__ import annotations

import argparse
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PyQt6.QtWidgets import QApplication

from analysis.gui.iceberg_measurement_window import IcebergMeasurementWindow
from gui.style import apply_modern_style


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Standalone GUI for measuring the iceberg hull variable PVC segment.",
    )
    parser.add_argument(
        "paths",
        nargs="*",
        help="Optional image or video file to load when the app starts.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    app = QApplication(sys.argv)
    apply_modern_style(app)

    window = IcebergMeasurementWindow(media_paths=args.paths)
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
