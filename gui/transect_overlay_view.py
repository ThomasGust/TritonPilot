"""Transect-tab view that displays the CV's annotated frames.

A dumb image surface: the CV worker bakes the overlay onto a BGR frame with
``tracking.transect_overlay.draw_transect_overlay`` (off the GUI thread) and
calls :meth:`submit_frame` from that worker thread; the widget marshals onto the
GUI thread via a queued signal, converts to a QImage, and paints it aspect-fit.

This is the live counterpart of the offline ``tools/transect_overlay_demo.py``
output, so what the pilot sees on the Transect tab is exactly what the model
sees -- the target box, the detected blue square, margins, and the lock light --
the indicator they use to decide whether to engage the hold.
"""

from __future__ import annotations

from typing import Optional

import numpy as np
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QColor, QImage, QPainter
from PyQt6.QtWidgets import QSizePolicy, QWidget


class TransectOverlayView(QWidget):
    """Show annotated CV frames; thread-safe via :meth:`submit_frame`."""

    _frame_sig = pyqtSignal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("transectOverlayView")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._qimage: Optional[QImage] = None
        self._placeholder = "Transect autopilot — waiting for video…"
        # Queued (default cross-thread) so worker-thread submits repaint safely.
        self._frame_sig.connect(self._on_frame)

    def submit_frame(self, frame_bgr: np.ndarray) -> None:
        """Hand an annotated BGR frame to the view (callable from any thread)."""
        self._frame_sig.emit(frame_bgr)

    def set_placeholder_text(self, text: str) -> None:
        self._placeholder = str(text or "")
        if self._qimage is None:
            self.update()

    def clear(self) -> None:
        """Drop the current frame and hide the view (revealing the video below)."""
        self._qimage = None
        self.hide()
        self.update()

    def _on_frame(self, frame_bgr) -> None:
        img = self._qimage_from_bgr(frame_bgr)
        if img is None:
            return
        self._qimage = img
        # Reveal the overlay only once a real frame exists, so a slow/absent CV
        # feed never hides the working video underneath with a placeholder.
        if not self.isVisible():
            self.show()
            self.raise_()
        self.update()

    @staticmethod
    def _qimage_from_bgr(frame_bgr) -> Optional[QImage]:
        if frame_bgr is None or not hasattr(frame_bgr, "shape") or frame_bgr.ndim != 3:
            return None
        h, w = frame_bgr.shape[:2]
        if h <= 0 or w <= 0:
            return None
        # BGR -> RGB, contiguous; .copy() detaches the QImage from the numpy buffer.
        rgb = np.ascontiguousarray(frame_bgr[:, :, ::-1])
        return QImage(rgb.data, w, h, 3 * w, QImage.Format.Format_RGB888).copy()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        rect = self.rect()
        painter.fillRect(rect, QColor(12, 12, 12))
        if self._qimage is None or self._qimage.isNull():
            painter.setPen(QColor(200, 200, 200))
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, self._placeholder)
            return
        scaled = self._qimage.scaled(
            rect.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        x = rect.x() + (rect.width() - scaled.width()) // 2
        y = rect.y() + (rect.height() - scaled.height()) // 2
        painter.drawImage(x, y, scaled)
