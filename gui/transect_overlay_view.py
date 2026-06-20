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
from PyQt6.QtCore import QPointF, QRectF, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QImage, QPainter, QPen, QPolygonF
from PyQt6.QtWidgets import QApplication, QSizePolicy, QWidget


def _coerce_source_shape(shape) -> tuple[int, int] | None:
    try:
        if len(shape) >= 2:
            h, w = int(shape[0]), int(shape[1])
            if h > 0 and w > 0:
                return h, w
    except Exception:
        pass
    return None


def _display_mapping(source_shape: tuple[int, int] | None) -> tuple[float, float, float, float]:
    """Return source crop x/y/w/h used by the square transect video."""
    if source_shape is None:
        return 0.0, 0.0, 1.0, 1.0
    src_h, src_w = source_shape
    if src_w > src_h:
        side = float(src_h)
        return (float(src_w - src_h) * 0.5, 0.0, side, side)
    if src_h > src_w:
        side = float(src_w)
        return (0.0, float(src_h - src_w) * 0.5, side, side)
    return 0.0, 0.0, float(src_w), float(src_h)


def _point(rect, source_shape: tuple[int, int] | None, nx: float, ny: float) -> QPointF:
    rect = QRectF(rect)
    if source_shape is None:
        return QPointF(
            rect.left() + float(nx) * rect.width(),
            rect.top() + float(ny) * rect.height(),
        )
    src_h, src_w = source_shape
    crop_x, crop_y, crop_w, crop_h = _display_mapping(source_shape)
    x = ((float(nx) * src_w) - crop_x) / max(1.0, crop_w)
    y = ((float(ny) * src_h) - crop_y) / max(1.0, crop_h)
    return QPointF(rect.left() + x * rect.width(), rect.top() + y * rect.height())


def _x_fraction_to_px(rect, source_shape: tuple[int, int] | None, frac: float) -> float:
    rect = QRectF(rect)
    if source_shape is None:
        return float(frac) * rect.width()
    _src_h, src_w = source_shape
    _crop_x, _crop_y, crop_w, _crop_h = _display_mapping(source_shape)
    return float(frac) * float(src_w) / max(1.0, crop_w) * rect.width()


def _state_color(lock_state: str) -> QColor:
    return {
        "lock": QColor(44, 210, 105),
        "acquiring": QColor(255, 190, 52),
        "lost": QColor(255, 72, 72),
        "no_target": QColor(170, 176, 184),
    }.get(str(lock_state), QColor(170, 176, 184))


def _draw_rotated_square(painter: QPainter, center: QPointF, side: float, angle_deg: float) -> None:
    import math

    half = max(1.0, float(side) * 0.5)
    theta = math.radians(float(angle_deg))
    ct, st = math.cos(theta), math.sin(theta)
    pts = []
    for x, y in ((-half, -half), (half, -half), (half, half), (-half, half)):
        pts.append(QPointF(center.x() + x * ct - y * st, center.y() + x * st + y * ct))
    painter.drawPolygon(QPolygonF(pts))


def paint_transect_hud_overlay(
    painter: QPainter,
    rect,
    model,
    estimate,
    observation=None,
    source_shape=None,
) -> None:
    """Paint the same transparent geometry HUD used by the live Transect tab.

    ``source_shape`` is the uncropped frame shape. When present, the overlay maps
    source-frame detections into the center square crop shown by the pilot tab.
    """
    if model is None or estimate is None:
        return

    rect = QRectF(rect)
    source_shape = _coerce_source_shape(source_shape)

    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

    lock_state = str(getattr(estimate, "lock_state", "no_target"))
    state_color = _state_color(lock_state)
    cyan = QColor(74, 222, 255)
    white = QColor(245, 248, 252)
    red = QColor(255, 55, 55)
    black_plate = QColor(8, 12, 18, 178)

    target = _point(rect, source_shape, model.target_cx, model.target_cy)
    side = _x_fraction_to_px(rect, source_shape, model.nominal_blue_fraction)
    half = side * 0.5
    target_rect = QRectF(target.x() - half, target.y() - half, side, side)

    pen = QPen(cyan, 2)
    pen.setStyle(Qt.PenStyle.DashLine)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawRect(target_rect)

    tol = _x_fraction_to_px(rect, source_shape, model.image_pos_tol)
    painter.setPen(QPen(cyan, 1))
    painter.drawEllipse(target, tol, tol)
    painter.drawLine(QPointF(target.x() - 12, target.y()), QPointF(target.x() + 12, target.y()))
    painter.drawLine(QPointF(target.x(), target.y() - 12), QPointF(target.x(), target.y() + 12))

    obs = observation
    if obs is not None and getattr(obs, "blue_found", False):
        detected = _point(rect, source_shape, obs.blue_cx, obs.blue_cy)
        det_side = _x_fraction_to_px(rect, source_shape, obs.blue_fraction)
        painter.setPen(QPen(state_color, 3))
        _draw_rotated_square(painter, detected, det_side, obs.blue_rotation_deg)
        painter.drawLine(detected, target)
        painter.drawLine(QPointF(detected.x() - 8, detected.y()), QPointF(detected.x() + 8, detected.y()))
        painter.drawLine(QPointF(detected.x(), detected.y() - 8), QPointF(detected.x(), detected.y() + 8))

    if obs is not None:
        bar = max(6, int(rect.height()) // 45)
        for mag, edge_rect in (
            (obs.red_left, QRectF(rect.left(), rect.top(), bar, rect.height())),
            (obs.red_right, QRectF(rect.right() - bar, rect.top(), bar, rect.height())),
            (obs.red_top, QRectF(rect.left(), rect.top(), rect.width(), bar)),
            (obs.red_bottom, QRectF(rect.left(), rect.bottom() - bar, rect.width(), bar)),
        ):
            if float(mag) > 0.02:
                c = QColor(red)
                c.setAlpha(max(45, min(190, int(220 * float(mag)))))
                painter.fillRect(edge_rect, c)

    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(black_plate)
    plate_w = min(rect.width() - 20, max(310, rect.width() * 0.48))
    plate = QRectF(rect.left() + 10, rect.top() + 10, max(1.0, plate_w), 108)
    painter.drawRoundedRect(plate, 6, 6)

    painter.setBrush(state_color)
    painter.drawEllipse(QPointF(plate.right() - 24, plate.top() + 24), 10, 10)
    painter.setPen(QPen(white, 1))
    metrics = painter.fontMetrics()
    text_w = max(1, int(plate.width() - 24))

    def _fit(text: str) -> str:
        return metrics.elidedText(str(text), Qt.TextElideMode.ElideRight, text_w)

    label = lock_state.upper().replace("_", " ")
    painter.drawText(QRectF(plate.left() + 12, plate.top() + 8, plate.width() - 52, 22), _fit(label))

    e = estimate.error
    line = f"conf {estimate.confidence * 100:.0f}%  ex {e.ex:+.2f}  ey {e.ey:+.2f}"
    painter.drawText(QRectF(plate.left() + 12, plate.top() + 34, plate.width() - 24, 20), _fit(line))
    if estimate.margin_cm is not None:
        detail = f"es {e.es:+.2f}  er {e.er:+.2f}  margin {estimate.margin_cm:+.1f}cm"
    elif estimate.violation > 0:
        detail = f"es {e.es:+.2f}  er {e.er:+.2f}  red {estimate.violation * 100:.0f}%"
    else:
        detail = f"es {e.es:+.2f}  er {e.er:+.2f}  searching"
    painter.drawText(QRectF(plate.left() + 12, plate.top() + 58, plate.width() - 24, 20), _fit(detail))


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
        # Freshness watchdog: if CV frames stall (e.g. H.264 loss during a fast
        # arm move), hide this overlay so the smoother hardware-decoded video
        # underneath shows through instead of a frozen annotated frame.
        self._stale_ms = 400
        self._stale_timer = QTimer(self)
        self._stale_timer.setSingleShot(True)
        self._stale_timer.timeout.connect(self._on_stale)

    def submit_frame(self, frame_bgr: np.ndarray) -> None:
        """Hand an annotated BGR frame to the view (callable from any thread)."""
        self._frame_sig.emit(frame_bgr)

    def set_placeholder_text(self, text: str) -> None:
        self._placeholder = str(text or "")
        if self._qimage is None:
            self.update()

    def clear(self) -> None:
        """Drop the current frame and hide the view (revealing the video below)."""
        self._stale_timer.stop()
        self._qimage = None
        self.hide()
        self.update()

    def _on_stale(self) -> None:
        """No fresh frame within the window -> reveal the live video underneath."""
        self.hide()

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
        # Restart the staleness countdown; a stall will reveal the video below.
        self._stale_timer.start(self._stale_ms)

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


class TransectHudOverlayView(QWidget):
    """Transparent CV geometry HUD layered over the live transect video."""

    _estimate_sig = pyqtSignal(object, object, object, object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("transectHudOverlayView")
        self.setWindowFlags(
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowTransparentForInput
            | Qt.WindowType.NoDropShadowWindowHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAutoFillBackground(False)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._model = None
        self._estimate = None
        self._observation = None
        self._source_shape: tuple[int, int] | None = None  # h, w
        self._anchor: QWidget | None = None
        self._estimate_sig.connect(self._on_estimate)
        self._sync_timer = QTimer(self)
        self._sync_timer.setInterval(250)
        self._sync_timer.timeout.connect(self.sync_to_anchor)
        self._stale_timer = QTimer(self)
        self._stale_timer.setSingleShot(True)
        self._stale_timer.timeout.connect(self._on_stale)
        app = QApplication.instance()
        if app is not None:
            app.applicationStateChanged.connect(self._on_application_state_changed)
        self.hide()

    def set_anchor_widget(self, anchor: QWidget | None) -> None:
        self._anchor = anchor
        if anchor is not None:
            owner = anchor.window()
            if self.parent() is not owner:
                self.setParent(owner, self.windowFlags())
            self.sync_to_anchor()

    def sync_to_anchor(self) -> bool:
        anchor = self._anchor
        if anchor is None:
            return False
        owner = anchor.window()
        if not self._application_is_active() or not anchor.isVisible() or not owner.isVisible() or owner.isMinimized():
            self.hide()
            return False
        rect = anchor.contentsRect()
        side = max(1, min(rect.width(), rect.height()))
        x = rect.x() + max(0, (rect.width() - side) // 2)
        y = rect.y() + max(0, (rect.height() - side) // 2)
        top_left = anchor.mapToGlobal(rect.topLeft())
        self.setGeometry(top_left.x() + x, top_left.y() + y, side, side)
        if self.isVisible():
            self.raise_()
            self._raise_native_window()
        return True

    def show_for_anchor(self) -> None:
        if not self.sync_to_anchor():
            return
        self.show()
        self.raise_()
        self._raise_native_window()
        if not self._sync_timer.isActive():
            self._sync_timer.start()

    def _on_stale(self) -> None:
        self.hide()
        self._sync_timer.stop()

    def _on_application_state_changed(self, state) -> None:
        if state != Qt.ApplicationState.ApplicationActive:
            self.hide()
            self._sync_timer.stop()
        elif self._estimate is not None and self._stale_timer.isActive():
            self.show_for_anchor()
            self.update()

    @staticmethod
    def _application_is_active() -> bool:
        app = QApplication.instance()
        if app is None:
            return True
        return app.applicationState() == Qt.ApplicationState.ApplicationActive

    def _raise_native_window(self) -> None:
        """Raise above the embedded Direct3D window without becoming globally top-most."""
        try:
            import ctypes

            hwnd_top = 0
            swp_nosize = 0x0001
            swp_nomove = 0x0002
            swp_noactivate = 0x0010
            swp_showwindow = 0x0040
            ctypes.windll.user32.SetWindowPos(
                int(self.winId()),
                hwnd_top,
                0,
                0,
                0,
                0,
                swp_nomove | swp_nosize | swp_noactivate | swp_showwindow,
            )
        except Exception:
            pass

    def submit_estimate(self, model, estimate, observation, frame_shape) -> None:
        """Queue one CV estimate for painting; callable from the CV worker."""
        self._estimate_sig.emit(model, estimate, observation, tuple(frame_shape or ()))

    def clear(self) -> None:
        self._stale_timer.stop()
        self._model = None
        self._estimate = None
        self._observation = None
        self._source_shape = None
        self.hide()
        self._sync_timer.stop()
        self.update()

    def _on_estimate(self, model, estimate, observation, frame_shape) -> None:
        self._model = model
        self._estimate = estimate
        self._observation = observation
        self._source_shape = self._coerce_source_shape(frame_shape)
        if not self.isVisible():
            self.show_for_anchor()
        else:
            self.sync_to_anchor()
        self.update()
        self._stale_timer.start(900)

    @staticmethod
    def _coerce_source_shape(shape) -> tuple[int, int] | None:
        return _coerce_source_shape(shape)

    def _display_mapping(self) -> tuple[float, float, float, float]:
        return _display_mapping(self._source_shape)

    def _point(self, nx: float, ny: float) -> QPointF:
        return _point(self.rect(), self._source_shape, nx, ny)

    def _x_fraction_to_px(self, frac: float) -> float:
        return _x_fraction_to_px(self.rect(), self._source_shape, frac)

    @staticmethod
    def _state_color(lock_state: str) -> QColor:
        return _state_color(lock_state)

    def _draw_rotated_square(self, painter: QPainter, center: QPointF, side: float, angle_deg: float) -> None:
        _draw_rotated_square(painter, center, side, angle_deg)

    def paintEvent(self, _event) -> None:  # noqa: N802
        painter = QPainter(self)
        paint_transect_hud_overlay(
            painter,
            self.rect(),
            self._model,
            self._estimate,
            self._observation,
            self._source_shape,
        )
