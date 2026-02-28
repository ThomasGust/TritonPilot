from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QImage, QPixmap
from PyQt6.QtWidgets import QDialog, QLabel, QVBoxLayout

import numpy as np


class CrabResultDialog(QDialog):
    """Simple viewer for a crab-recognition result image."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Crab Recognition")

        self._count_lbl = QLabel("GREEN CRABS: -")
        try:
            self._count_lbl.setAlignment(Qt.AlignmentFlag.AlignHCenter)
            self._count_lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        except Exception:
            pass

        self._img_lbl = QLabel("(no result)")
        self._img_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._img_lbl.setMinimumSize(640, 360)

        lay = QVBoxLayout(self)
        lay.addWidget(self._count_lbl)
        lay.addWidget(self._img_lbl, 1)

        self.resize(900, 650)

    def set_result(self, annotated_bgr: np.ndarray, green_count: int) -> None:
        try:
            self._count_lbl.setText(f"GREEN CRABS: {int(green_count)}")
        except Exception:
            pass

        try:
            h, w, ch = annotated_bgr.shape
            bytes_per_line = ch * w
            qimg = QImage(annotated_bgr.data, w, h, bytes_per_line, QImage.Format.Format_BGR888)
            pix = QPixmap.fromImage(qimg).scaled(
                self._img_lbl.width(),
                self._img_lbl.height(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            self._img_lbl.setPixmap(pix)
            self._img_lbl.setText("")
        except Exception:
            # Fallback to text-only
            self._img_lbl.setPixmap(QPixmap())
            self._img_lbl.setText("(failed to render result image)")

    def resizeEvent(self, event):
        # Keep pixmap scaled as the window is resized.
        try:
            pix = self._img_lbl.pixmap()
            if pix is not None and not pix.isNull():
                self._img_lbl.setPixmap(
                    pix.scaled(
                        self._img_lbl.width(),
                        self._img_lbl.height(),
                        Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation,
                    )
                )
        except Exception:
            pass
        super().resizeEvent(event)
