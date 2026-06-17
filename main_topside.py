"""Topside GUI entry point for TritonPilot.

This module intentionally does only startup wiring: create the Qt application,
apply shared styling, and hand control to ``MainWindow``. Keeping the entry
point small makes tests and field debugging easier because the live services
are created in one predictable place.
"""

import os
import sys

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QFont, QIcon, QLinearGradient, QPainter, QPen, QPixmap
from PyQt6.QtWidgets import QApplication, QSplashScreen

from app_paths import APP_DISPLAY_NAME, APP_ORGANIZATION, app_icon_path, streams_file_path


SPLASH_W = 520
SPLASH_H = 292
CUSTOM_ARG_FLAGS = {"--no-splash", "--windowed", "--maximized", "--fullscreen"}


def _smoke_test() -> int:
    """Verify packaged resources without opening the operator window."""
    missing = [path for path in (streams_file_path(), app_icon_path()) if not path.exists()]
    return 1 if missing else 0


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return bool(default)
    return raw not in {"0", "false", "no", "off"}


def _startup_window_mode(argv: list[str]) -> str:
    if "--fullscreen" in argv:
        return "fullscreen"
    if "--windowed" in argv:
        return "windowed"
    if "--maximized" in argv:
        return "maximized"
    if _env_bool("TRITON_START_FULLSCREEN", False):
        return "fullscreen"
    if not _env_bool("TRITON_START_MAXIMIZED", True):
        return "windowed"
    return "maximized"


def _qt_argv(argv: list[str]) -> list[str]:
    return [arg for arg in argv if arg not in CUSTOM_ARG_FLAGS]


def _make_splash_pixmap() -> QPixmap:
    pixmap = QPixmap(SPLASH_W, SPLASH_H)
    pixmap.fill(QColor("#111827"))

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

    gradient = QLinearGradient(0, 0, SPLASH_W, SPLASH_H)
    gradient.setColorAt(0.0, QColor("#111827"))
    gradient.setColorAt(1.0, QColor("#0b1020"))
    painter.fillRect(0, 0, SPLASH_W, SPLASH_H, gradient)

    painter.setPen(QPen(QColor("#26324d"), 1))
    painter.drawRoundedRect(0, 0, SPLASH_W - 1, SPLASH_H - 1, 22, 22)

    logo = QPixmap(str(app_icon_path()))
    if not logo.isNull():
        logo = logo.scaled(88, 88, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
        painter.drawPixmap((SPLASH_W - logo.width()) // 2, 42, logo)

    painter.setPen(QColor("#f7fbff"))
    title_font = QFont()
    title_font.setPointSize(24)
    title_font.setBold(True)
    painter.setFont(title_font)
    painter.drawText(0, 150, SPLASH_W, 42, Qt.AlignmentFlag.AlignHCenter, APP_DISPLAY_NAME)

    painter.setPen(QColor("#b8c3d8"))
    sub_font = QFont()
    sub_font.setPointSize(10)
    painter.setFont(sub_font)
    painter.drawText(0, 188, SPLASH_W, 24, Qt.AlignmentFlag.AlignHCenter, "Starting pilot console")

    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor("#f2b83a"))
    painter.drawRoundedRect(170, 232, 180, 4, 2, 2)
    painter.setBrush(QColor("#31405f"))
    painter.drawRoundedRect(170, 244, 180, 4, 2, 2)

    painter.end()
    return pixmap


def _show_startup_message(app: QApplication, splash: QSplashScreen | None, message: str) -> None:
    if splash is None:
        return
    splash.showMessage(
        message,
        Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignBottom,
        QColor("#dbe6ff"),
    )
    app.processEvents()


def main(argv: list[str] | None = None) -> int:
    """Start the topside operator application."""
    argv = list(sys.argv if argv is None else argv)
    if "--smoke-test" in argv:
        return _smoke_test()

    use_splash = "--no-splash" not in argv
    startup_window_mode = _startup_window_mode(argv)
    qt_argv = _qt_argv(argv)

    app = QApplication(qt_argv)
    app.setApplicationName(APP_DISPLAY_NAME)
    app.setOrganizationName(APP_ORGANIZATION)

    icon = QIcon(str(app_icon_path()))
    if not icon.isNull():
        app.setWindowIcon(icon)

    splash: QSplashScreen | None = None
    if use_splash:
        splash = QSplashScreen(_make_splash_pixmap())
        splash.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        splash.show()
        _show_startup_message(app, splash, "Loading runtime...")

    from config import STREAMS_FILE
    from gui.main_window import MainWindow
    from gui.style import apply_modern_style

    apply_modern_style(app)
    _show_startup_message(app, splash, "Preparing controls and video...")

    win = MainWindow(streams_path=str(STREAMS_FILE))
    if not icon.isNull():
        win.setWindowIcon(icon)
    win.setAutoFillBackground(True)
    _show_startup_message(app, splash, "Opening pilot view...")
    if startup_window_mode == "fullscreen":
        win.set_fullscreen_mode(True)
    elif startup_window_mode == "maximized":
        win.showMaximized()
    else:
        win.show()
    app.processEvents()
    if splash is not None:
        splash.finish(win)

    return int(app.exec())


if __name__ == "__main__":
    sys.exit(main())

