"""Shared Qt palette and stylesheet for TritonPilot."""

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
    QScrollArea#responsiveControlStrip {
        background: transparent;
        border: none;
    }
    QScrollArea#responsiveControlStrip > QWidget > QWidget {
        background: transparent;
    }
    QTabWidget::pane { border: 1px solid #2a2a32; border-radius: 10px; }
    QTabBar::tab { padding: 8px 12px; margin: 2px; border-radius: 10px; }
    QTabBar::tab:selected { background: #2a2a36; }
    QPushButton#armDisarmButton {
        padding: 6px 12px;
        border-radius: 8px;
        border: 1px solid #4b5265;
        background: #202733;
        color: #f0f4ff;
        font-weight: 700;
    }
    QPushButton#armDisarmButton:hover {
        border: 1px solid #7d8eb3;
        background: #283242;
    }
    QPushButton#armDisarmButton[armed="true"] {
        color: #ffd9d9;
        background: #482525;
        border: 1px solid #b65a5a;
    }
    QLabel#tetherStatusPill {
        padding: 5px 10px;
        border-radius: 8px;
        border: 1px solid #3d465a;
        background: #1d2430;
        color: #dfe8ff;
        font-weight: 800;
    }
    QLabel#tetherStatusPill[tone="ok"] {
        color: #d9ffea;
        background: #1f3b2a;
        border: 1px solid #3f9b62;
    }
    QLabel#tetherStatusPill[tone="warn"] {
        color: #ffe6ae;
        background: #332b1d;
        border: 1px solid #a07e34;
    }
    QLabel#tetherStatusPill[tone="alert"] {
        color: #fff0f0;
        background: #5a2020;
        border: 1px solid #d45f5f;
    }
    QLabel#tetherStatusBanner {
        padding: 8px 12px;
        border-radius: 6px;
        font-size: 15px;
        font-weight: 900;
        background: #5a2020;
        color: #fff0f0;
        border: 1px solid #e36b6b;
    }
    QLabel#tetherStatusBanner[tone="ok"] {
        background: #1f3b2a;
        color: #d9ffea;
        border: 1px solid #3f9b62;
    }
    QLabel#tetherStatusBanner[tone="warn"] {
        background: #332b1d;
        color: #ffe6ae;
        border: 1px solid #a07e34;
    }
    QStatusBar { border-top: 1px solid #2a2a32; }
    QStatusBar QLabel[tone="alert"] {
        color: #ffb3b3;
        font-weight: 700;
    }
    QStatusBar QLabel[tone="warn"] {
        color: #ffe6ae;
    }
    QWidget#videoLayoutBar {
        background: transparent;
        border: none;
        padding: 0;
    }
    QWidget#videoControlGroup {
        background: transparent;
    }
    QLabel#videoControlLabel {
        min-width: 18px;
        padding: 1px 4px;
        border-radius: 6px;
        color: #9aa4bf;
        background: transparent;
        font-weight: 600;
    }
    QLabel#videoControlLabel[active="true"] {
        color: #ffffff;
        background: #335fb6;
    }
    QComboBox#videoLayoutCombo,
    QComboBox#videoPaneSelector,
    QComboBox#transectCameraSelector {
        padding: 2px 6px;
        border: 1px solid #2a2a32;
        border-radius: 6px;
        background: #15161d;
    }
    QComboBox#videoPaneSelector[active="true"] {
        border: 1px solid #4a78d8;
    }
    QFrame#videoPane {
        background: #0f1015;
        border: 2px solid #0f1015;
        border-radius: 2px;
    }
    QFrame#videoPane[active="true"] {
        border: 2px solid #5a86ff;
        background: #0f1015;
    }
    QLabel#videoRecordBadge {
        color: #fff4f4;
        background: rgba(158, 28, 28, 224);
        border: 1px solid rgba(255, 132, 132, 210);
        border-radius: 12px;
        padding: 4px 10px;
        font-weight: 700;
    }
    QLabel#videoSnapshotBadge {
        color: #f7fbff;
        background: rgba(39, 72, 118, 224);
        border: 1px solid rgba(140, 188, 255, 210);
        border-radius: 12px;
        padding: 4px 10px;
        font-weight: 700;
    }
    QLabel#videoPanePlaceholder {
        color: #97a0b8;
        padding: 8px;
    }
    QWidget#transectControls {
        background: transparent;
    }
    QLabel#transectTargetLabel,
    QCheckBox#transectRotationServoToggle {
        color: #c8c8d0;
        font-weight: 600;
    }
    QDoubleSpinBox#transectTargetBlueWidthSpin {
        color: #eef1f7;
        background: #1c1c22;
        border: 1px solid #44444f;
        border-radius: 6px;
        padding: 2px 6px;
    }
    QLabel#transectStopwatch {
        padding: 2px 10px;
        border-radius: 8px;
        color: #eef1f7;
        background: #20232c;
        border: 1px solid #3a4354;
        font-family: Consolas, Menlo, monospace;
        font-weight: 800;
    }
    QLabel#transectStopwatch[tone="running"] {
        color: #f7fbff;
        background: #214d82;
        border: 1px solid #5d9cec;
    }
    QLabel#transectStopwatch[tone="paused"] {
        color: #f2e1ff;
        background: #413246;
        border: 1px solid #725182;
    }
    QLabel#transectStopwatch[tone="complete"] {
        color: #0c140c;
        background: #46c84b;
        border: 1px solid #5fe065;
    }
    QLabel#transectCvStatus {
        padding: 1px 8px;
        border-radius: 8px;
        color: #c8c8d0;
        background: #1c1c22;
    }
    QLabel#transectCvStatus[tone="ok"] {
        color: #0c140c;
        background: #46c84b;
        font-weight: 600;
    }
    QLabel#transectCvStatus[tone="warn"] {
        color: #14110a;
        background: #e0a72e;
    }
    QLabel#transectCvStatus[tone="bad"] {
        color: #160b0b;
        background: #e0563c;
        font-weight: 600;
    }
    QLabel#transectCvStatus[tone="neutral"] {
        color: #d8d8e0;
        background: #2a2a32;
    }
    QPushButton#transectEngageButton {
        padding: 4px 18px;
        border-radius: 6px;
        font-weight: 700;
        color: #e8e8ee;
        background: #33333d;
        border: 1px solid #44444f;
    }
    QPushButton#transectEngageButton:hover {
        border: 1px solid #6a6a7a;
    }
    QPushButton#transectEngageButton[tone="ready"] {
        color: #0c140c;
        background: #46c84b;
        border: 1px solid #5fe065;
    }
    QPushButton#transectEngageButton[tone="engaged"] {
        color: #ffffff;
        background: #c0392b;
        border: 1px solid #e0563c;
    }
    QFrame#transectSquareHost {
        background: #0f1015;
        border: 1px solid #2a2a32;
        border-radius: 2px;
    }
    QFrame#managementSectionCard {
        border: 1px solid #2a2a32;
        border-radius: 12px;
        background: #16161b;
    }
    QLabel#managementSectionTitle {
        font-size: 14px;
        font-weight: 700;
    }
    QLabel#managementSectionSubtitle {
        color: #b6bac8;
    }
    QLabel#managementMetaValue {
        color: #d7dbe8;
    }
    QLabel#managementPill {
        border-radius: 10px;
        padding: 3px 10px;
        font-weight: 700;
        background: #2c3648;
    }
    QLabel#managementPill[tone="ok"] {
        color: #d9ffea;
        background: #204530;
        border: 1px solid #2f7a4f;
    }
    QLabel#managementPill[tone="error"] {
        color: #ffd9d9;
        background: #4a2424;
        border: 1px solid #995252;
    }
    QLabel#managementFeedback {
        border-radius: 10px;
        padding: 8px 10px;
        background: #202028;
        border: 1px solid #2f2f3a;
    }
    QLabel#managementFeedback[tone="ok"] {
        color: #d9ffea;
        background: #1f3526;
        border: 1px solid #2f7a4f;
    }
    QLabel#managementFeedback[tone="error"] {
        color: #ffd9d9;
        background: #402222;
        border: 1px solid #995252;
    }
    QLabel#managementFeedback[tone="info"] {
        color: #dbe6ff;
        background: #1f2c42;
        border: 1px solid #4468aa;
    }
    QLabel#managementRestartBanner {
        color: #ffe6ae;
        background: #332b1d;
        border: 1px solid #a07e34;
        border-radius: 10px;
        padding: 8px 10px;
        font-weight: 700;
    }
    QFrame#sshHeader {
        border: 1px solid #2a2a32;
        border-radius: 8px;
        background: #16161b;
    }
    QLabel#sshStatus {
        color: #d7dbe8;
        padding: 2px 4px;
        font-weight: 700;
    }
    QLabel#sshStatus[tone="ok"] {
        color: #9be7b0;
    }
    QLabel#sshStatus[tone="warn"] {
        color: #f4cf7a;
    }
    QLabel#sshStatus[tone="alert"] {
        color: #ffaaa5;
    }
    QPlainTextEdit#sshOutput {
        font-family: Consolas, "Cascadia Mono", monospace;
        font-size: 12px;
        background: #090a0f;
        color: #d9f2e0;
        border: 1px solid #2a2a32;
        border-radius: 8px;
        padding: 8px;
    }
    QTableWidget { border: 1px solid #2a2a32; border-radius: 10px; gridline-color: #2a2a32; }
    QHeaderView::section { background: #202028; padding: 6px 8px; border: none; border-bottom: 1px solid #2a2a32; }
    QLabel { color: #ebebeb; }
    """
    try:
        app.setStyleSheet(qss)
    except Exception:
        pass
