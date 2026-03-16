from __future__ import annotations

import cv2
import numpy as np
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QGuiApplication, QImage, QPixmap
from PyQt6.QtWidgets import QDialog, QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout, QWidget


def _bgr_to_pixmap(frame_bgr: np.ndarray) -> QPixmap:
    rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
    height, width, _ = rgb.shape
    image = QImage(rgb.data, width, height, rgb.strides[0], QImage.Format.Format_RGB888).copy()
    return QPixmap.fromImage(image)


class CrabResultDialog(QDialog):
    def __init__(
        self,
        summary_text: str,
        annotated_original_bgr: np.ndarray,
        annotated_unwrapped_bgr: np.ndarray,
        parent=None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Crab Detection Results")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)

        self._original_pixmap = _bgr_to_pixmap(annotated_original_bgr)
        self._unwrapped_pixmap = _bgr_to_pixmap(annotated_unwrapped_bgr)

        self.summary_label = QLabel(summary_text)
        self.summary_label.setWordWrap(True)
        self.summary_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.original_label = QLabel("Original View")
        self.original_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.unwrapped_label = QLabel("Unwrapped Board")
        self.unwrapped_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.original_image = QLabel()
        self.original_image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.original_image.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.original_image.setStyleSheet("background-color: #111; border: 1px solid #444;")

        self.unwrapped_image = QLabel()
        self.unwrapped_image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.unwrapped_image.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.unwrapped_image.setStyleSheet("background-color: #111; border: 1px solid #444;")

        original_panel = QWidget()
        original_layout = QVBoxLayout(original_panel)
        original_layout.setContentsMargins(0, 0, 0, 0)
        original_layout.addWidget(self.original_label)
        original_layout.addWidget(self.original_image, 1)

        unwrapped_panel = QWidget()
        unwrapped_layout = QVBoxLayout(unwrapped_panel)
        unwrapped_layout.setContentsMargins(0, 0, 0, 0)
        unwrapped_layout.addWidget(self.unwrapped_label)
        unwrapped_layout.addWidget(self.unwrapped_image, 1)

        image_row = QHBoxLayout()
        image_row.addWidget(original_panel, 1)
        image_row.addWidget(unwrapped_panel, 1)

        root = QVBoxLayout(self)
        root.addWidget(self.summary_label)
        root.addLayout(image_row, 1)

        self._resize_to_screen()
        self._update_pixmaps()

    def _resize_to_screen(self) -> None:
        screen = self.screen() or QGuiApplication.primaryScreen()
        if screen is None:
            self.resize(1200, 800)
            return
        available = screen.availableGeometry()
        max_width = max(700, int(available.width() * 0.92))
        max_height = max(500, int(available.height() * 0.88))
        desired_width = self._original_pixmap.width() + self._unwrapped_pixmap.width() + 120
        desired_height = max(self._original_pixmap.height(), self._unwrapped_pixmap.height()) + 140
        self.resize(min(max_width, desired_width), min(max_height, desired_height))

    def _update_pixmaps(self) -> None:
        target_width = max(200, int((self.width() - 80) / 2))
        target_height = max(200, self.height() - 140)
        self.original_image.setPixmap(
            self._original_pixmap.scaled(
                target_width,
                target_height,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )
        self.unwrapped_image.setPixmap(
            self._unwrapped_pixmap.scaled(
                target_width,
                target_height,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._update_pixmaps()
