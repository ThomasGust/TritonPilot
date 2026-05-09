from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QGuiApplication
from PyQt6.QtWidgets import (
    QAbstractScrollArea,
    QFrame,
    QLayout,
    QScrollArea,
    QSizePolicy,
    QWidget,
)


def resize_to_available_screen(
    window: QWidget,
    preferred_width: int,
    preferred_height: int,
    *,
    min_width: int = 760,
    min_height: int = 520,
    width_ratio: float = 0.94,
    height_ratio: float = 0.90,
) -> None:
    """Resize a top-level widget without exceeding the current screen."""
    screen = window.screen() or QGuiApplication.primaryScreen()
    if screen is None:
        window.resize(preferred_width, preferred_height)
        return

    available = screen.availableGeometry()
    if available.width() <= 0 or available.height() <= 0:
        window.resize(preferred_width, preferred_height)
        return

    max_width = max(1, int(available.width() * width_ratio))
    max_height = max(1, int(available.height() * height_ratio))
    floor_width = min(int(min_width), max_width)
    floor_height = min(int(min_height), max_height)

    width = max(floor_width, min(int(preferred_width), max_width))
    height = max(floor_height, min(int(preferred_height), max_height))
    window.resize(width, height)


def horizontal_scroll_area(layout: QLayout, *, object_name: str = "responsiveControlStrip") -> QScrollArea:
    """Wrap a wide control row so smaller screens can still reach every control."""
    layout.setContentsMargins(0, 0, 0, 0)
    content = QWidget()
    content.setLayout(layout)
    content.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)

    scroll = QScrollArea()
    scroll.setObjectName(object_name)
    scroll.setWidget(content)
    scroll.setWidgetResizable(False)
    scroll.setFrameShape(QFrame.Shape.NoFrame)
    scroll.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    scroll.setSizeAdjustPolicy(QAbstractScrollArea.SizeAdjustPolicy.AdjustIgnored)
    scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
    scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    scroll.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
    scroll.setMinimumWidth(0)
    scroll.setFixedHeight(
        max(
            28,
            content.sizeHint().height() + scroll.horizontalScrollBar().sizeHint().height() + 6,
        )
    )
    return scroll


def vertical_scroll_area(widget: QWidget, *, object_name: str = "responsiveControlStrip") -> QScrollArea:
    """Wrap a panel whose content may be taller than a compact laptop screen."""
    scroll = QScrollArea()
    scroll.setObjectName(object_name)
    scroll.setWidget(widget)
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QFrame.Shape.NoFrame)
    scroll.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    scroll.setSizeAdjustPolicy(QAbstractScrollArea.SizeAdjustPolicy.AdjustIgnored)
    scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
    scroll.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
    scroll.setMinimumWidth(0)
    return scroll
