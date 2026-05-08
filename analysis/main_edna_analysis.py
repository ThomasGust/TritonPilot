from __future__ import annotations

import argparse
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PyQt6.QtWidgets import QApplication

from analysis.gui.edna_analysis_window import EDNAAnalysisWindow
from gui.style import apply_modern_style


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Standalone GUI for MATE ROV 2026 eDNA percent-frequency analysis.",
    )
    parser.add_argument(
        "--sample",
        action="store_true",
        help="Start with the example counts from the task statement.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    app = QApplication(sys.argv)
    apply_modern_style(app)

    window = EDNAAnalysisWindow(use_sample=args.sample)
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
