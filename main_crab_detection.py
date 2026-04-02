from __future__ import annotations

import argparse
import sys

from PyQt6.QtWidgets import QApplication

from crab_detector_cv import DEFAULT_UNWRAP_SIZE
from gui.crab_detection_window import CrabDetectionWindow
from gui.style import apply_modern_style


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Standalone GUI for running crab detection against image paths or folders.",
    )
    parser.add_argument(
        "paths",
        nargs="*",
        help="Image files or folders to load when the app starts.",
    )
    parser.add_argument(
        "--no-force-square",
        action="store_true",
        help="Preserve the detected board aspect ratio instead of forcing a square unwrap.",
    )
    parser.add_argument(
        "--unwrap-size",
        nargs=2,
        type=int,
        metavar=("WIDTH", "HEIGHT"),
        default=DEFAULT_UNWRAP_SIZE,
        help=f"Output size for the board unwrap. Default: {DEFAULT_UNWRAP_SIZE[0]} {DEFAULT_UNWRAP_SIZE[1]}.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    app = QApplication(sys.argv)
    apply_modern_style(app)

    window = CrabDetectionWindow(
        image_paths=args.paths,
        force_square=not args.no_force_square,
        unwrap_size=args.unwrap_size,
    )
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
