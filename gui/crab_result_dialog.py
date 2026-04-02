from __future__ import annotations

import cv2
import numpy as np
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QGuiApplication, QImage, QPixmap
from PyQt6.QtWidgets import QDialog, QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout, QWidget


def frame_to_pixmap(frame: np.ndarray | None) -> QPixmap:
    if frame is None:
        return QPixmap()

    if frame.ndim == 2:
        rgb = cv2.cvtColor(frame, cv2.COLOR_GRAY2RGB)
    else:
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)

    height, width, _ = rgb.shape
    image = QImage(rgb.data, width, height, rgb.strides[0], QImage.Format.Format_RGB888).copy()
    return QPixmap.fromImage(image)


class ImagePreviewPanel(QWidget):
    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        self._pixmap = QPixmap()
        self._placeholder_text = "No image"

        self.title_label = QLabel(title)
        self.title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.image_label = QLabel(self._placeholder_text)
        self.image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.image_label.setStyleSheet("background-color: #111; border: 1px solid #444;")
        self.image_label.setMinimumSize(220, 220)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.title_label)
        layout.addWidget(self.image_label, 1)

    def set_frame(self, frame: np.ndarray | None, *, placeholder_text: str = "No image") -> None:
        self._placeholder_text = placeholder_text
        self._pixmap = frame_to_pixmap(frame)
        self._update_pixmap()

    def clear(self, placeholder_text: str = "No image") -> None:
        self.set_frame(None, placeholder_text=placeholder_text)

    def _update_pixmap(self) -> None:
        if self._pixmap.isNull():
            self.image_label.clear()
            self.image_label.setText(self._placeholder_text)
            return

        self.image_label.setText("")
        self.image_label.setPixmap(
            self._pixmap.scaled(
                max(200, self.image_label.width() - 12),
                max(200, self.image_label.height() - 12),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._update_pixmap()


class CrabDetectionResultView(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)

        self.summary_label = QLabel("Crab detection results will appear here.")
        self.summary_label.setObjectName("summaryCard")
        self.summary_label.setWordWrap(True)
        self.summary_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.summary_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

        self.source_label = QLabel("")
        self.source_label.setObjectName("summaryHint")
        self.source_label.setWordWrap(True)
        self.source_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.source_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.source_label.hide()

        self.detail_label = QLabel("")
        self.detail_label.setObjectName("summaryHint")
        self.detail_label.setWordWrap(True)
        self.detail_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.detail_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.detail_label.hide()

        self.original_panel = ImagePreviewPanel("Original View")
        self.unwrapped_panel = ImagePreviewPanel("Unwrapped Board")
        self.mask_panel = ImagePreviewPanel("Crab Mask")

        image_row = QHBoxLayout()
        image_row.addWidget(self.original_panel, 1)
        image_row.addWidget(self.unwrapped_panel, 1)
        image_row.addWidget(self.mask_panel, 1)

        root = QVBoxLayout(self)
        root.addWidget(self.summary_label)
        root.addWidget(self.source_label)
        root.addWidget(self.detail_label)
        root.addLayout(image_row, 1)

    def set_result(
        self,
        summary_text: str,
        annotated_original_bgr: np.ndarray | None,
        annotated_unwrapped_bgr: np.ndarray | None,
        *,
        mask_image: np.ndarray | None = None,
        source_text: str | None = None,
        detail_text: str | None = None,
        tone: str | None = None,
    ) -> None:
        self.summary_label.setText(summary_text)
        self._set_summary_tone(tone)

        self._set_optional_label(self.source_label, source_text)
        self._set_optional_label(self.detail_label, detail_text)

        self.original_panel.set_frame(annotated_original_bgr, placeholder_text="No source image")
        self.unwrapped_panel.set_frame(annotated_unwrapped_bgr, placeholder_text="No board unwrap")
        self.mask_panel.set_frame(mask_image, placeholder_text="No crab mask")

    @staticmethod
    def _set_optional_label(label: QLabel, text: str | None) -> None:
        if text:
            label.setText(text)
            label.show()
        else:
            label.clear()
            label.hide()

    def _set_summary_tone(self, tone: str | None) -> None:
        self.summary_label.setProperty("tone", tone or "")
        self.summary_label.style().unpolish(self.summary_label)
        self.summary_label.style().polish(self.summary_label)
        self.summary_label.update()


class CrabResultDialog(QDialog):
    def __init__(
        self,
        summary_text: str,
        annotated_original_bgr: np.ndarray,
        annotated_unwrapped_bgr: np.ndarray,
        *,
        mask_image: np.ndarray | None = None,
        source_text: str | None = None,
        detail_text: str | None = None,
        parent=None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Crab Detection Results")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)

        self.result_view = CrabDetectionResultView(self)
        self.result_view.set_result(
            summary_text,
            annotated_original_bgr,
            annotated_unwrapped_bgr,
            mask_image=mask_image,
            source_text=source_text,
            detail_text=detail_text,
        )

        root = QVBoxLayout(self)
        root.addWidget(self.result_view)

        self._resize_to_screen()

    def _resize_to_screen(self) -> None:
        screen = self.screen() or QGuiApplication.primaryScreen()
        if screen is None:
            self.resize(1400, 900)
            return

        available = screen.availableGeometry()
        max_width = max(900, int(available.width() * 0.95))
        max_height = max(650, int(available.height() * 0.9))
        self.resize(min(max_width, 1500), min(max_height, 900))
