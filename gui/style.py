# gui/style.py
from __future__ import annotations

from PyQt6.QtGui import QPalette, QColor
from PyQt6.QtWidgets import QApplication

def apply_modern_style(app: QApplication) -> None:
    """Apply a clean, modern dark-ish Fusion theme.

    This is intentionally lightweight (single file, no external deps).
    """
    try:
        app.setStyle("Fusion")
    except Exception:
        pass

    # Dark palette (readable on bright poolsides / field laptops)
    pal = QPalette()
    pal.setColor(QPalette.ColorRole.Window, QColor(24, 24, 28))
    pal.setColor(QPalette.ColorRole.WindowText, QColor(235, 235, 235))
    pal.setColor(QPalette.ColorRole.Base, QColor(18, 18, 22))
    pal.setColor(QPalette.ColorRole.AlternateBase, QColor(28, 28, 34))
    pal.setColor(QPalette.ColorRole.ToolTipBase, QColor(235, 235, 235))
    pal.setColor(QPalette.ColorRole.ToolTipText, QColor(20, 20, 20))
    pal.setColor(QPalette.ColorRole.Text, QColor(235, 235, 235))
    pal.setColor(QPalette.ColorRole.Button, QColor(32, 32, 38))
    pal.setColor(QPalette.ColorRole.ButtonText, QColor(235, 235, 235))
    pal.setColor(QPalette.ColorRole.BrightText, QColor(255, 80, 80))
    pal.setColor(QPalette.ColorRole.Highlight, QColor(70, 120, 255))
    pal.setColor(QPalette.ColorRole.HighlightedText, QColor(255, 255, 255))
    try:
        app.setPalette(pal)
    except Exception:
        pass

    # Small QSS pass: spacing, rounded corners, less "busy" tables
    qss = """
    QMainWindow { background: #18181c; }
    QWidget { font-size: 12px; }
    QTabWidget::pane { border: 1px solid #2a2a32; border-radius: 10px; }
    QTabBar::tab { padding: 8px 12px; margin: 2px; border-radius: 10px; }
    QTabBar::tab:selected { background: #2a2a36; }
    QStatusBar { border-top: 1px solid #2a2a32; }
    QLabel#summaryCard {
        background: #202028;
        border: 1px solid #2f2f3a;
        border-radius: 10px;
        padding: 2px 4px;
        font-size: 13px;
    }
    QLabel#summaryCard[tone="alert"] {
        background: #3b2525;
        border: 1px solid #9c4a4a;
        color: #ffd9d9;
        font-weight: 700;
    }
    QLabel#summaryCard[tone="warn"] {
        background: #332b1d;
        border: 1px solid #a07e34;
        color: #ffe6ae;
    }
    QLabel#summaryHint {
        color: #b6bac8;
        padding: 2px 4px 6px 4px;
    }
    QTableWidget { border: 1px solid #2a2a32; border-radius: 10px; gridline-color: #2a2a32; }
    QHeaderView::section { background: #202028; padding: 6px 8px; border: none; border-bottom: 1px solid #2a2a32; }
    QLabel { color: #ebebeb; }
    """
    try:
        app.setStyleSheet(qss)
    except Exception:
        pass
