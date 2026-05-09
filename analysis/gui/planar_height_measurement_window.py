from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
from PyQt6.QtCore import QRectF, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QCursor, QPainter, QPen
from PyQt6.QtWidgets import (
    QDoubleSpinBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSlider,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from analysis.gui.crab_result_dialog import frame_to_pixmap
from analysis.planar_measurement import MeasurementError, measure_planar_height_from_plane
from gui.responsive import horizontal_scroll_area, resize_to_available_screen


IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp"}
VIDEO_SUFFIXES = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".wmv"}
REFERENCE_COUNT = 5
UNWRAPPED_WIDTH = 1000
UNWRAPPED_HEIGHT = 700


@dataclass(frozen=True)
class ClickSpec:
    key: str
    label: str


def is_supported_image_path(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES


def is_supported_video_path(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in VIDEO_SUFFIXES


class PlanarMeasurementCanvas(QWidget):
    points_changed = pyqtSignal(int)
    selection_changed = pyqtSignal(str)
    zoom_changed = pyqtSignal(float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pixmap = frame_to_pixmap(None)
        self._image_width = 0
        self._image_height = 0
        self._points_by_key: dict[str, tuple[float, float]] = {}
        self._click_specs: list[ClickSpec] = []
        self._measurement_badge = ""
        self._selected_key: str | None = None
        self._hover_key: str | None = None
        self._dragging_key: str | None = None
        self._panning = False
        self._last_pan_pos: tuple[float, float] | None = None
        self._zoom = 1.0
        self._pan = np.array([0.0, 0.0], dtype=np.float64)
        self.setMinimumSize(300, 240)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def set_click_specs(self, specs: list[ClickSpec], *, clear: bool = False) -> None:
        self._click_specs = list(specs)
        if clear:
            self.clear_points()
            return

        valid_keys = {spec.key for spec in self._click_specs}
        old_count = len(self._points_by_key)
        self._points_by_key = {
            key: point for key, point in self._points_by_key.items() if key in valid_keys
        }
        if self._selected_key not in valid_keys:
            self._set_selected_key(None)
        if self._hover_key not in valid_keys:
            self._hover_key = None
        if len(self._points_by_key) != old_count:
            self.points_changed.emit(len(self._points_by_key))
        self._refresh_cursor()
        self.update()

    def set_frame(self, frame_bgr: np.ndarray | None, *, clear: bool = True) -> None:
        previous_size = (self._image_width, self._image_height)
        self._pixmap = frame_to_pixmap(frame_bgr)
        if frame_bgr is None:
            self._image_width = 0
            self._image_height = 0
        else:
            self._image_height, self._image_width = frame_bgr.shape[:2]
        size_changed = previous_size != (self._image_width, self._image_height)
        if clear or frame_bgr is None or size_changed:
            self.reset_zoom()
        if clear or frame_bgr is None:
            self.clear_points()
        else:
            self._refresh_cursor()
            self.update()

    def set_measurement_badge(self, text: str | None) -> None:
        self._measurement_badge = text or ""
        self.update()

    def clear_points(self) -> None:
        self._points_by_key = {}
        self._hover_key = None
        self._dragging_key = None
        self._panning = False
        self._last_pan_pos = None
        self._set_selected_key(None)
        self._measurement_badge = ""
        self.points_changed.emit(0)
        self._refresh_cursor()
        self.update()

    def undo_point(self) -> None:
        key = self._last_present_key()
        if key is not None:
            self._remove_point(key)

    def remove_selected_point(self) -> None:
        if self._selected_key is not None:
            self._remove_point(self._selected_key)

    def point_count(self) -> int:
        return len(self._points_by_key)

    def selected_label(self) -> str | None:
        if self._selected_key is None:
            return None
        for spec in self._click_specs:
            if spec.key == self._selected_key:
                return spec.label
        return None

    def points_by_key(self) -> dict[str, tuple[float, float]]:
        return {
            spec.key: self._points_by_key[spec.key]
            for spec in self._click_specs
            if spec.key in self._points_by_key
        }

    def next_click_label(self) -> str | None:
        spec = self._first_missing_spec()
        return None if spec is None else spec.label

    def required_point_count(self) -> int:
        return len(self._click_specs)

    def zoom_factor(self) -> float:
        return float(self._zoom)

    def zoom_in(self) -> None:
        center = self.rect().center()
        self._set_zoom(self._zoom * 1.25, (float(center.x()), float(center.y())))

    def zoom_out(self) -> None:
        center = self.rect().center()
        self._set_zoom(self._zoom / 1.25, (float(center.x()), float(center.y())))

    def reset_zoom(self) -> None:
        changed = self._zoom != 1.0 or np.any(np.abs(self._pan) > 1.0e-9)
        self._zoom = 1.0
        self._pan[:] = 0.0
        if changed:
            self.zoom_changed.emit(self._zoom)
        self._refresh_cursor()
        self.update()

    def _first_missing_spec(self) -> ClickSpec | None:
        for spec in self._click_specs:
            if spec.key not in self._points_by_key:
                return spec
        return None

    def _last_present_key(self) -> str | None:
        for spec in reversed(self._click_specs):
            if spec.key in self._points_by_key:
                return spec.key
        return None

    def _set_selected_key(self, key: str | None) -> None:
        if key == self._selected_key:
            return
        self._selected_key = key
        self.selection_changed.emit(key or "")

    def _remove_point(self, key: str) -> None:
        if key not in self._points_by_key:
            return
        del self._points_by_key[key]
        if self._selected_key == key:
            self._set_selected_key(None)
        if self._hover_key == key:
            self._hover_key = None
        if self._dragging_key == key:
            self._dragging_key = None
        self._measurement_badge = ""
        self.points_changed.emit(len(self._points_by_key))
        self._refresh_cursor()
        self.update()

    def _centered_target_rect(self, zoom: float | None = None) -> QRectF:
        contents = self.contentsRect()
        if self._pixmap.isNull() or self._image_width <= 0 or self._image_height <= 0:
            return QRectF(contents)

        zoom = self._zoom if zoom is None else float(zoom)
        scale = min(
            contents.width() / float(self._image_width),
            contents.height() / float(self._image_height),
        ) * zoom
        draw_width = self._image_width * scale
        draw_height = self._image_height * scale
        return QRectF(
            contents.x() + (contents.width() - draw_width) / 2.0,
            contents.y() + (contents.height() - draw_height) / 2.0,
            draw_width,
            draw_height,
        )

    def _target_rect(self) -> QRectF:
        target = self._centered_target_rect()
        if self._pixmap.isNull() or self._image_width <= 0 or self._image_height <= 0:
            return target
        return QRectF(target.x() + float(self._pan[0]), target.y() + float(self._pan[1]), target.width(), target.height())

    def _clamp_pan(self) -> None:
        if self._pixmap.isNull() or self._image_width <= 0 or self._image_height <= 0:
            self._pan[:] = 0.0
            return

        contents = self.contentsRect()
        target = self._centered_target_rect()
        if target.width() <= contents.width():
            self._pan[0] = 0.0
        else:
            min_pan_x = contents.right() - target.right()
            max_pan_x = contents.left() - target.left()
            self._pan[0] = float(np.clip(self._pan[0], min_pan_x, max_pan_x))

        if target.height() <= contents.height():
            self._pan[1] = 0.0
        else:
            min_pan_y = contents.bottom() - target.bottom()
            max_pan_y = contents.top() - target.top()
            self._pan[1] = float(np.clip(self._pan[1], min_pan_y, max_pan_y))

    def _set_zoom(self, zoom: float, anchor: tuple[float, float] | None = None) -> None:
        if self._pixmap.isNull() or self._image_width <= 0 or self._image_height <= 0:
            return

        zoom = float(np.clip(zoom, 1.0, 10.0))
        if abs(zoom - self._zoom) < 1.0e-6:
            return

        if anchor is None:
            center = self.rect().center()
            anchor = (float(center.x()), float(center.y()))
        anchor_x, anchor_y = anchor
        image_anchor = self._widget_to_image(anchor_x, anchor_y)
        if image_anchor is None:
            image_anchor = (self._image_width * 0.5, self._image_height * 0.5)

        self._zoom = zoom
        centered = self._centered_target_rect()
        self._pan[0] = anchor_x - image_anchor[0] * centered.width() / max(1.0, float(self._image_width)) - centered.x()
        self._pan[1] = anchor_y - image_anchor[1] * centered.height() / max(1.0, float(self._image_height)) - centered.y()
        self._clamp_pan()
        self.zoom_changed.emit(self._zoom)
        self._refresh_cursor()
        self.update()

    def _image_to_widget(self, point: tuple[float, float]) -> tuple[float, float]:
        target = self._target_rect()
        return (
            target.x() + point[0] * target.width() / max(1.0, float(self._image_width)),
            target.y() + point[1] * target.height() / max(1.0, float(self._image_height)),
        )

    def _widget_to_image(self, x: float, y: float) -> tuple[float, float] | None:
        target = self._target_rect()
        if not target.contains(x, y):
            return None
        image_x = (x - target.x()) * self._image_width / max(1.0, target.width())
        image_y = (y - target.y()) * self._image_height / max(1.0, target.height())
        return (
            float(np.clip(image_x, 0, max(0, self._image_width - 1))),
            float(np.clip(image_y, 0, max(0, self._image_height - 1))),
        )

    def _widget_points_by_key(self) -> dict[str, tuple[float, float]]:
        return {key: self._image_to_widget(point) for key, point in self._points_by_key.items()}

    def _nearest_point_key(self, x: float, y: float, *, max_distance: float = 10.0) -> str | None:
        nearest_key = None
        nearest_distance = max_distance
        for key, (point_x, point_y) in self._widget_points_by_key().items():
            distance = float(np.hypot(point_x - x, point_y - y))
            if distance <= nearest_distance:
                nearest_key = key
                nearest_distance = distance
        return nearest_key

    def _refresh_cursor(self) -> None:
        if self._panning:
            self.setCursor(QCursor(Qt.CursorShape.ClosedHandCursor))
            return
        if self._dragging_key is not None or self._hover_key is not None:
            self.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
            return
        if not self._pixmap.isNull() and self._first_missing_spec() is not None:
            self.setCursor(QCursor(Qt.CursorShape.CrossCursor))
            return
        self.setCursor(QCursor(Qt.CursorShape.ArrowCursor))

    def mousePressEvent(self, event) -> None:
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        x = event.position().x()
        y = event.position().y()

        if event.button() == Qt.MouseButton.MiddleButton:
            if not self._pixmap.isNull() and self._zoom > 1.0:
                self._panning = True
                self._last_pan_pos = (x, y)
                self._refresh_cursor()
            return
        if event.button() == Qt.MouseButton.RightButton:
            key = self._nearest_point_key(x, y)
            if key is not None:
                self._remove_point(key)
            else:
                self.undo_point()
            return
        if event.button() != Qt.MouseButton.LeftButton or self._pixmap.isNull():
            return

        existing_key = self._nearest_point_key(x, y)
        if existing_key is not None:
            self._set_selected_key(existing_key)
            self._dragging_key = existing_key
            self._refresh_cursor()
            self.update()
            return

        next_spec = self._first_missing_spec()
        if next_spec is None:
            return
        point = self._widget_to_image(x, y)
        if point is None:
            return

        self._points_by_key[next_spec.key] = point
        self._measurement_badge = ""
        self._set_selected_key(next_spec.key)
        self._dragging_key = next_spec.key
        self.points_changed.emit(len(self._points_by_key))
        self._refresh_cursor()
        self.update()

    def mouseMoveEvent(self, event) -> None:
        x = event.position().x()
        y = event.position().y()
        if self._panning and self._last_pan_pos is not None:
            last_x, last_y = self._last_pan_pos
            self._pan += np.array([x - last_x, y - last_y], dtype=np.float64)
            self._last_pan_pos = (x, y)
            self._clamp_pan()
            self.update()
            return

        if self._dragging_key is not None:
            point = self._widget_to_image(x, y)
            if point is None:
                return
            self._points_by_key[self._dragging_key] = point
            self._measurement_badge = ""
            self.points_changed.emit(len(self._points_by_key))
            self.update()
            return

        hover_key = None if self._pixmap.isNull() else self._nearest_point_key(x, y)
        if hover_key != self._hover_key:
            self._hover_key = hover_key
            self._refresh_cursor()
            self.update()

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.MiddleButton and self._panning:
            self._panning = False
            self._last_pan_pos = None
            self._refresh_cursor()
            return
        if event.button() == Qt.MouseButton.LeftButton and self._dragging_key is not None:
            self._dragging_key = None
            self.points_changed.emit(len(self._points_by_key))
            self._refresh_cursor()
            self.update()

    def leaveEvent(self, event) -> None:
        if self._dragging_key is None and self._hover_key is not None:
            self._hover_key = None
            self._refresh_cursor()
            self.update()
        super().leaveEvent(event)

    def wheelEvent(self, event) -> None:
        if self._pixmap.isNull():
            return
        wheel_steps = event.angleDelta().y() / 120.0
        if abs(wheel_steps) < 1.0e-6:
            return
        self._set_zoom(self._zoom * (1.2 ** wheel_steps), (event.position().x(), event.position().y()))
        event.accept()

    def resizeEvent(self, event) -> None:
        self._clamp_pan()
        super().resizeEvent(event)

    def keyPressEvent(self, event) -> None:
        if event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            self.remove_selected_point()
            return
        if event.key() == Qt.Key.Key_Escape:
            self._set_selected_key(None)
            self.update()
            return
        super().keyPressEvent(event)

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#101116"))

        target = self._target_rect()
        if self._pixmap.isNull():
            painter.setPen(QColor("#aab0c0"))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "Open an image or video frame")
            return

        painter.drawPixmap(target.toRect(), self._pixmap)
        if not self._points_by_key:
            return

        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        widget_points = self._widget_points_by_key()
        self._draw_segments(painter, widget_points)
        self._draw_points(painter, widget_points)
        self._draw_measurement_badge(painter, widget_points)

    def _draw_segments(self, painter: QPainter, widget_points: dict[str, tuple[float, float]]) -> None:
        self._draw_polyline(painter, widget_points, ["rect_a", "rect_b", "rect_c", "rect_d", "rect_a"], QColor("#55d6ff"), 3)
        for index in range(REFERENCE_COUNT):
            self._draw_polyline(
                painter,
                widget_points,
                [f"ref_{index + 1}_start", f"ref_{index + 1}_end"],
                QColor("#8de46b"),
                2,
            )
        self._draw_polyline(painter, widget_points, ["height_start", "height_end"], QColor("#ffe66a"), 4)

    @staticmethod
    def _draw_polyline(
        painter: QPainter,
        widget_points: dict[str, tuple[float, float]],
        keys: list[str],
        color: QColor,
        width: int,
    ) -> None:
        painter.setPen(QPen(color, width))
        for start_key, end_key in zip(keys, keys[1:]):
            if start_key not in widget_points or end_key not in widget_points:
                continue
            start = widget_points[start_key]
            end = widget_points[end_key]
            painter.drawLine(int(start[0]), int(start[1]), int(end[0]), int(end[1]))

    def _draw_points(self, painter: QPainter, widget_points: dict[str, tuple[float, float]]) -> None:
        marker_outline = QColor("#15161d")
        for spec in self._click_specs:
            if spec.key not in widget_points:
                continue
            x, y = widget_points[spec.key]
            is_selected = spec.key == self._selected_key
            is_hovered = spec.key == self._hover_key
            radius = 6 if is_selected else 4
            marker_fill = QColor("#ffcf5a") if is_selected else QColor("#fff4a3")
            if is_hovered and not is_selected:
                marker_fill = QColor("#ffffff")

            painter.setPen(QPen(marker_outline, 1))
            painter.setBrush(marker_fill)
            painter.drawEllipse(QRectF(x - radius, y - radius, radius * 2, radius * 2))
            if is_selected:
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.setPen(QPen(QColor("#ffcf5a"), 2))
                painter.drawEllipse(QRectF(x - 10, y - 10, 20, 20))

    def _draw_measurement_badge(self, painter: QPainter, widget_points: dict[str, tuple[float, float]]) -> None:
        if not self._measurement_badge:
            return
        start = widget_points.get("height_start")
        end = widget_points.get("height_end")
        if start is None or end is None:
            return

        x = int((start[0] + end[0]) * 0.5)
        y = int((start[1] + end[1]) * 0.5)
        text_rect = QRectF(x - 70, y - 34, 140, 28)
        painter.setPen(QPen(QColor("#111318"), 1))
        painter.setBrush(QColor(20, 24, 32, 230))
        painter.drawRoundedRect(text_rect, 6, 6)
        painter.setPen(QColor("#fff4a3"))
        painter.drawText(text_rect, Qt.AlignmentFlag.AlignCenter, self._measurement_badge)


class PlanarHeightMeasurementWindow(QMainWindow):
    def __init__(self, media_paths: list[str | Path] | None = None, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.setWindowTitle("Planar Prop Height Measurement")
        resize_to_available_screen(self, 1480, 900, min_width=900, min_height=620)

        self._video_path: Path | None = None
        self._video_capture: cv2.VideoCapture | None = None
        self._video_frame_count = 0
        self._video_fps = 0.0
        self._video_current_frame_index = 0
        self._updating_video_controls = False
        self._last_dir = str(Path.cwd())
        self._last_frame: np.ndarray | None = None
        self._last_source_text = ""
        self._unwrapped_frame: np.ndarray | None = None
        self._homography_image_to_unwrapped: np.ndarray | None = None
        self._active_canvas: PlanarMeasurementCanvas | None = None

        self._build_ui()
        self._show_empty_state()
        resize_to_available_screen(self, 1480, 900, min_width=900, min_height=620)

        if media_paths:
            self.set_media_paths(media_paths)

    def _build_ui(self) -> None:
        self.open_image_btn = QPushButton("Open Image")
        self.open_image_btn.clicked.connect(self._open_image)
        self.open_video_btn = QPushButton("Open Video")
        self.open_video_btn.clicked.connect(self._open_video)
        self.previous_frame_btn = QPushButton("Prev Frame")
        self.previous_frame_btn.clicked.connect(self._show_previous_video_frame)
        self.next_frame_btn = QPushButton("Next Frame")
        self.next_frame_btn.clicked.connect(self._show_next_video_frame)

        self.frame_slider = QSlider(Qt.Orientation.Horizontal)
        self.frame_slider.setMinimum(0)
        self.frame_slider.setMaximum(0)
        self.frame_slider.setTracking(False)
        self.frame_slider.valueChanged.connect(self._video_slider_changed)

        self.video_position_label = QLabel("No video loaded")
        self.video_position_label.setObjectName("summaryHint")
        self.video_position_label.setMinimumWidth(210)

        video_row = QHBoxLayout()
        video_row.addWidget(self.open_image_btn)
        video_row.addWidget(self.open_video_btn)
        video_row.addSpacing(12)
        video_row.addWidget(self.previous_frame_btn)
        video_row.addWidget(self.next_frame_btn)
        video_row.addWidget(self.frame_slider, 1)
        video_row.addWidget(self.video_position_label)

        self.reference_length_spins = [self._make_length_spinbox(15.0) for _ in range(REFERENCE_COUNT)]
        for spinbox in self.reference_length_spins:
            spinbox.valueChanged.connect(self._calibration_values_changed)

        self.zoom_out_btn = QPushButton("Zoom -")
        self.zoom_reset_btn = QPushButton("Fit")
        self.zoom_in_btn = QPushButton("Zoom +")
        self.zoom_label = QLabel("100%")
        self.zoom_label.setObjectName("summaryHint")
        self.zoom_label.setMinimumWidth(48)
        self.zoom_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.undo_btn = QPushButton("Undo")
        self.undo_btn.clicked.connect(self.canvas_undo)
        self.delete_btn = QPushButton("Delete Point")
        self.delete_btn.clicked.connect(self.canvas_delete_selected)
        self.clear_btn = QPushButton("Clear Points")
        self.clear_btn.clicked.connect(self.canvas_clear)

        measurement_row = QHBoxLayout()
        measurement_row.addWidget(QLabel("Reference lengths"))
        for index, spinbox in enumerate(self.reference_length_spins, start=1):
            measurement_row.addWidget(QLabel(f"R{index}"))
            measurement_row.addWidget(spinbox)
        measurement_row.addSpacing(10)
        measurement_row.addWidget(self.zoom_out_btn)
        measurement_row.addWidget(self.zoom_reset_btn)
        measurement_row.addWidget(self.zoom_in_btn)
        measurement_row.addWidget(self.zoom_label)
        measurement_row.addStretch(1)
        measurement_row.addWidget(self.undo_btn)
        measurement_row.addWidget(self.delete_btn)
        measurement_row.addWidget(self.clear_btn)

        self.source_label = QLabel("")
        self.source_label.setObjectName("summaryHint")
        self.source_label.setWordWrap(True)
        self.source_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

        self.summary_label = QLabel("")
        self.summary_label.setObjectName("summaryCard")
        self.summary_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.summary_label.setWordWrap(True)
        self.summary_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

        self.detail_label = QLabel("")
        self.detail_label.setObjectName("summaryHint")
        self.detail_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.detail_label.setWordWrap(True)
        self.detail_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

        self.next_click_label = QLabel("")
        self.next_click_label.setObjectName("summaryHint")
        self.next_click_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.source_canvas = PlanarMeasurementCanvas(self)
        self.source_canvas.set_click_specs(self._source_click_specs())
        self.source_canvas.points_changed.connect(self._points_changed)
        self.source_canvas.selection_changed.connect(self._selection_changed)
        self.source_canvas.zoom_changed.connect(self._zoom_changed)

        self.plane_canvas = PlanarMeasurementCanvas(self)
        self.plane_canvas.set_click_specs(self._plane_click_specs())
        self.plane_canvas.points_changed.connect(self._points_changed)
        self.plane_canvas.selection_changed.connect(self._selection_changed)
        self.plane_canvas.zoom_changed.connect(self._zoom_changed)
        self._active_canvas = self.plane_canvas

        self.zoom_out_btn.clicked.connect(self._zoom_active_out)
        self.zoom_reset_btn.clicked.connect(self._zoom_active_reset)
        self.zoom_in_btn.clicked.connect(self._zoom_active_in)

        source_panel = QWidget(self)
        source_layout = QVBoxLayout(source_panel)
        source_layout.setContentsMargins(0, 0, 0, 0)
        source_label = QLabel("Original frame: click the plane rectangle")
        source_label.setObjectName("summaryHint")
        source_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        source_label.setWordWrap(True)
        source_layout.addWidget(source_label)
        source_layout.addWidget(self.source_canvas, 1)

        plane_panel = QWidget(self)
        plane_layout = QVBoxLayout(plane_panel)
        plane_layout.setContentsMargins(0, 0, 0, 0)
        plane_label = QLabel("Unwrapped plane: draw references and height")
        plane_label.setObjectName("summaryHint")
        plane_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        plane_label.setWordWrap(True)
        plane_layout.addWidget(plane_label)
        plane_layout.addWidget(self.plane_canvas, 1)

        self.canvas_splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self.canvas_splitter.addWidget(source_panel)
        self.canvas_splitter.addWidget(plane_panel)
        self.canvas_splitter.setStretchFactor(0, 1)
        self.canvas_splitter.setStretchFactor(1, 1)

        container = QWidget(self)
        layout = QVBoxLayout(container)
        layout.addWidget(horizontal_scroll_area(video_row))
        layout.addWidget(horizontal_scroll_area(measurement_row))
        layout.addWidget(self.source_label)
        layout.addWidget(self.summary_label)
        layout.addWidget(self.detail_label)
        layout.addWidget(self.next_click_label)
        layout.addWidget(self.canvas_splitter, 1)
        self.setCentralWidget(container)

        self.statusBar().showMessage("Open a video or image to start measuring.")
        self._refresh_controls()

    @staticmethod
    def _make_length_spinbox(value: float) -> QDoubleSpinBox:
        spinbox = QDoubleSpinBox()
        spinbox.setDecimals(2)
        spinbox.setRange(0.01, 1000.0)
        spinbox.setSingleStep(1.0)
        spinbox.setValue(float(value))
        spinbox.setSuffix(" cm")
        spinbox.setMinimumWidth(94)
        return spinbox

    def _source_click_specs(self) -> list[ClickSpec]:
        return [
            ClickSpec("rect_a", "Rectangle corner A"),
            ClickSpec("rect_b", "Rectangle corner B"),
            ClickSpec("rect_c", "Rectangle corner C"),
            ClickSpec("rect_d", "Rectangle corner D"),
        ]

    def _plane_click_specs(self) -> list[ClickSpec]:
        specs: list[ClickSpec] = []
        for index in range(REFERENCE_COUNT):
            number = index + 1
            specs.append(ClickSpec(f"ref_{number}_start", f"Reference {number} start"))
            specs.append(ClickSpec(f"ref_{number}_end", f"Reference {number} end"))
        specs.extend(
            [
                ClickSpec("height_start", "Height start"),
                ClickSpec("height_end", "Height end"),
            ]
        )
        return specs

    def set_media_paths(self, paths: list[str | Path]) -> None:
        existing_paths = [Path(path).expanduser() for path in paths if Path(path).expanduser().exists()]
        if not existing_paths:
            self._show_error_state("No supported media path was found.")
            return

        first_path = existing_paths[0]
        if is_supported_video_path(first_path):
            self.set_video_path(first_path)
            return
        if is_supported_image_path(first_path):
            self.set_image_path(first_path)
            return
        self._show_error_state("That file type is not supported.", source_text=str(first_path))

    def set_image_path(self, image_path: str | Path) -> None:
        path = Path(image_path).expanduser()
        if not is_supported_image_path(path):
            self._show_error_state("That file type is not a supported image.", source_text=str(path))
            return

        image = cv2.imread(str(path))
        if image is None:
            self._show_error_state("Could not read the selected image.", source_text=str(path))
            return

        self._close_video()
        self._last_dir = str(path.parent)
        self._show_frame(image, source_text=str(path), video_frame_index=None)

    def set_video_path(self, video_path: str | Path) -> None:
        path = Path(video_path).expanduser()
        if not is_supported_video_path(path):
            self._show_error_state("That file type is not a supported video.", source_text=str(path))
            return

        self._close_video()
        capture = cv2.VideoCapture(str(path))
        if not capture.isOpened():
            self._show_error_state("Could not open the selected video.", source_text=str(path))
            return

        self._video_path = path
        self._video_capture = capture
        self._video_frame_count = max(0, int(capture.get(cv2.CAP_PROP_FRAME_COUNT)))
        self._video_fps = float(capture.get(cv2.CAP_PROP_FPS) or 0.0)
        self._last_dir = str(path.parent)

        self._updating_video_controls = True
        try:
            self.frame_slider.setMinimum(0)
            self.frame_slider.setMaximum(max(0, self._video_frame_count - 1))
            self.frame_slider.setValue(0)
        finally:
            self._updating_video_controls = False

        self._show_video_frame(0)

    def _close_video(self) -> None:
        if self._video_capture is not None:
            self._video_capture.release()
        self._video_path = None
        self._video_capture = None
        self._video_frame_count = 0
        self._video_fps = 0.0
        self._video_current_frame_index = 0
        self._updating_video_controls = True
        try:
            self.frame_slider.setRange(0, 0)
            self.frame_slider.setValue(0)
        finally:
            self._updating_video_controls = False
        self.video_position_label.setText("No video loaded")

    def _read_video_frame(self, frame_index: int) -> np.ndarray | None:
        if self._video_capture is None:
            return None
        if self._video_frame_count > 0:
            frame_index = max(0, min(int(frame_index), self._video_frame_count - 1))
        else:
            frame_index = max(0, int(frame_index))
        self._video_capture.set(cv2.CAP_PROP_POS_FRAMES, frame_index)
        ok, frame = self._video_capture.read()
        return frame if ok else None

    def _show_video_frame(self, frame_index: int) -> None:
        frame = self._read_video_frame(frame_index)
        if frame is None:
            self.statusBar().showMessage("Could not read that video frame.", 4000)
            return

        if self._video_frame_count > 0:
            frame_index = max(0, min(int(frame_index), self._video_frame_count - 1))
        self._video_current_frame_index = int(frame_index)
        self._updating_video_controls = True
        try:
            self.frame_slider.setValue(self._video_current_frame_index)
        finally:
            self._updating_video_controls = False

        time_seconds = self._video_time_for_frame(self._video_current_frame_index)
        self.video_position_label.setText(
            f"Frame {self._video_current_frame_index + 1}/{max(1, self._video_frame_count)}  "
            f"{time_seconds:.2f}s"
        )
        self._show_frame(
            frame,
            source_text=self._format_video_source_text(self._video_current_frame_index),
            video_frame_index=self._video_current_frame_index,
        )

    def _show_frame(self, frame: np.ndarray, *, source_text: str, video_frame_index: int | None) -> None:
        self._last_frame = frame.copy()
        self._last_source_text = source_text
        self._unwrapped_frame = None
        self._homography_image_to_unwrapped = None
        self.source_label.setText(source_text)
        self.source_canvas.set_frame(frame)
        self.plane_canvas.set_frame(None)
        self._active_canvas = self.source_canvas
        self._set_summary("Click the plane rectangle on the original frame.", detail_text=self._mode_detail_text())
        self._update_next_click_label()
        self._refresh_controls()
        self._update_window_title(source_text, video_frame_index)

    def _video_time_for_frame(self, frame_index: int) -> float:
        if self._video_fps <= 0.0:
            return 0.0
        return float(frame_index) / self._video_fps

    def _format_video_source_text(self, frame_index: int) -> str:
        if self._video_path is None:
            return "Video frame"
        total_frames = max(1, self._video_frame_count)
        return (
            f"{self._video_path} | frame {frame_index + 1}/{total_frames} "
            f"@ {self._video_time_for_frame(frame_index):.2f}s"
        )

    def _open_image(self) -> None:
        selected_path, _ = QFileDialog.getOpenFileName(
            self,
            "Open planar prop frame",
            self._last_dir,
            "Images (*.jpg *.jpeg *.png *.bmp *.tif *.tiff *.webp)",
        )
        if selected_path:
            self.set_image_path(Path(selected_path))

    def _open_video(self) -> None:
        selected_path, _ = QFileDialog.getOpenFileName(
            self,
            "Open planar prop video",
            self._last_dir,
            "Videos (*.mp4 *.mov *.m4v *.avi *.mkv *.wmv)",
        )
        if selected_path:
            self.set_video_path(Path(selected_path))

    def _video_slider_changed(self, frame_index: int) -> None:
        if not self._updating_video_controls:
            self._show_video_frame(frame_index)

    def _show_previous_video_frame(self) -> None:
        self._show_video_frame(max(0, self._video_current_frame_index - 1))

    def _show_next_video_frame(self) -> None:
        if self._video_frame_count > 0:
            next_frame = min(self._video_frame_count - 1, self._video_current_frame_index + 1)
        else:
            next_frame = self._video_current_frame_index + 1
        self._show_video_frame(next_frame)

    def _active_or_plane_canvas(self) -> PlanarMeasurementCanvas:
        return self._active_canvas or self.plane_canvas

    def _rectangle_points(self) -> dict[str, tuple[float, float]]:
        return self.source_canvas.points_by_key()

    def _update_unwrapped_frame(self) -> None:
        if self._last_frame is None or self.source_canvas.point_count() < self.source_canvas.required_point_count():
            if self._unwrapped_frame is not None:
                self._unwrapped_frame = None
                self._homography_image_to_unwrapped = None
                self.plane_canvas.set_frame(None)
            return

        points = self._rectangle_points()
        source = np.array(
            [
                points["rect_a"],
                points["rect_b"],
                points["rect_c"],
                points["rect_d"],
            ],
            dtype=np.float32,
        )
        destination = np.array(
            [
                [0.0, 0.0],
                [float(UNWRAPPED_WIDTH - 1), 0.0],
                [float(UNWRAPPED_WIDTH - 1), float(UNWRAPPED_HEIGHT - 1)],
                [0.0, float(UNWRAPPED_HEIGHT - 1)],
            ],
            dtype=np.float32,
        )
        homography = cv2.getPerspectiveTransform(source, destination)
        if homography is None or not np.all(np.isfinite(homography)):
            self._unwrapped_frame = None
            self._homography_image_to_unwrapped = None
            self.plane_canvas.set_frame(None)
            return

        self._homography_image_to_unwrapped = homography.astype(np.float64)
        self._unwrapped_frame = cv2.warpPerspective(
            self._last_frame,
            homography,
            (UNWRAPPED_WIDTH, UNWRAPPED_HEIGHT),
            flags=cv2.INTER_LINEAR,
        )
        self.plane_canvas.set_frame(self._unwrapped_frame, clear=False)

    def _calibration_values_changed(self, *_args) -> None:
        self._points_changed(self.plane_canvas.point_count())

    def canvas_undo(self) -> None:
        self._active_or_plane_canvas().undo_point()

    def canvas_delete_selected(self) -> None:
        self._active_or_plane_canvas().remove_selected_point()

    def canvas_clear(self) -> None:
        self.source_canvas.clear_points()
        self.plane_canvas.clear_points()
        self._unwrapped_frame = None
        self._homography_image_to_unwrapped = None
        self.plane_canvas.set_frame(None)
        self._active_canvas = self.source_canvas

    def _zoom_active_in(self) -> None:
        self._active_or_plane_canvas().zoom_in()

    def _zoom_active_out(self) -> None:
        self._active_or_plane_canvas().zoom_out()

    def _zoom_active_reset(self) -> None:
        self._active_or_plane_canvas().reset_zoom()

    def _points_changed(self, _count: int) -> None:
        sender = self.sender()
        if isinstance(sender, PlanarMeasurementCanvas):
            self._active_canvas = sender
        if sender is self.source_canvas:
            self._update_unwrapped_frame()
        self._update_next_click_label()
        self._refresh_controls()
        self._recalculate_measurement()

    def _selection_changed(self, _key: str) -> None:
        sender = self.sender()
        if isinstance(sender, PlanarMeasurementCanvas):
            self._active_canvas = sender
        self._update_next_click_label()
        self._refresh_controls()

    def _zoom_changed(self, zoom: float) -> None:
        sender = self.sender()
        if isinstance(sender, PlanarMeasurementCanvas):
            self._active_canvas = sender
        self.zoom_label.setText(f"{int(round(self._active_or_plane_canvas().zoom_factor() * 100.0))}%")
        self._refresh_controls()

    def _recalculate_measurement(self) -> None:
        if self._last_frame is None:
            self.plane_canvas.set_measurement_badge(None)
            return

        source_count = self.source_canvas.point_count()
        source_required = self.source_canvas.required_point_count()
        if source_count < source_required:
            self.plane_canvas.set_measurement_badge(None)
            self._set_summary(
                f"{source_count}/{source_required} rectangle corners selected.",
                detail_text=self._mode_detail_text(),
            )
            return

        if self._unwrapped_frame is None or self._homography_image_to_unwrapped is None:
            self.plane_canvas.set_measurement_badge(None)
            self._set_summary("Could not unwrap that rectangle.", detail_text=self._mode_detail_text(), tone="warn")
            return

        plane_count = self.plane_canvas.point_count()
        plane_required = self.plane_canvas.required_point_count()
        if plane_count < plane_required:
            self.plane_canvas.set_measurement_badge(None)
            self._set_summary(
                f"{plane_count}/{plane_required} unwrapped-plane points selected.",
                detail_text=self._mode_detail_text(),
            )
            return

        points = self.plane_canvas.points_by_key()
        try:
            result = measure_planar_height_from_plane(
                reference_plane_segments=[
                    [points[f"ref_{index + 1}_start"], points[f"ref_{index + 1}_end"]]
                    for index in range(REFERENCE_COUNT)
                ],
                reference_lengths_cm=[spin.value() for spin in self.reference_length_spins],
                height_start_plane=points["height_start"],
                height_end_plane=points["height_end"],
                plane_size_units=(float(UNWRAPPED_WIDTH - 1), float(UNWRAPPED_HEIGHT - 1)),
                homography_image_to_plane=self._homography_image_to_unwrapped,
            )
        except MeasurementError as exc:
            self.plane_canvas.set_measurement_badge(None)
            self._set_summary(str(exc), detail_text=self._mode_detail_text(), tone="warn")
            self.statusBar().showMessage(str(exc), 5000)
            return

        self.plane_canvas.set_measurement_badge(f"{result.height_cm:.1f} cm")
        self._set_summary(
            f"Height: {result.height_cm:.2f} cm",
            detail_text=(
                "unwrapped planar homography | "
                f"rectangle scale=({result.plane_width_cm:.2f}, {result.plane_height_cm:.2f}) cm | "
                f"reference fit RMSE={result.reference_rmse_cm:.2f} cm"
            ),
        )
        self.statusBar().showMessage("Planar height measurement updated.", 5000)

    def _mode_detail_text(self) -> str:
        return (
            "Click rectangle corners A, B, C, D on the original frame. The right pane unwraps that plane; "
            "draw the five known reference segments and the height segment there, then set the reference lengths above. "
            "Wheel zooms; middle-drag pans; right-click removes a point."
        )

    def _update_next_click_label(self) -> None:
        source_next = self.source_canvas.next_click_label()
        if source_next is not None:
            text = f"Original next: {source_next}"
        elif self._unwrapped_frame is None:
            text = "Adjust the rectangle until the unwrapped view appears."
        else:
            plane_next = self.plane_canvas.next_click_label()
            text = "All points selected." if plane_next is None else f"Unwrapped next: {plane_next}"

        active = self._active_or_plane_canvas()
        selected_label = active.selected_label()
        if selected_label is not None:
            text = f"{text}  Selected: {selected_label}"
        self.next_click_label.setText(text)

    def _set_summary(self, summary_text: str, *, detail_text: str | None = None, tone: str | None = None) -> None:
        self.summary_label.setText(summary_text)
        self.summary_label.setProperty("tone", tone or "")
        self.summary_label.style().unpolish(self.summary_label)
        self.summary_label.style().polish(self.summary_label)
        self.summary_label.update()

        if detail_text:
            self.detail_label.setText(detail_text)
            self.detail_label.show()
        else:
            self.detail_label.clear()
            self.detail_label.hide()

    def _show_empty_state(self) -> None:
        self._last_frame = None
        self._last_source_text = ""
        self._unwrapped_frame = None
        self._homography_image_to_unwrapped = None
        self.source_label.clear()
        self.source_canvas.set_frame(None)
        self.plane_canvas.set_frame(None)
        self._active_canvas = self.source_canvas
        self._set_summary("Open a video or image to start planar height measurement.", detail_text=self._mode_detail_text())
        self._update_next_click_label()
        self._refresh_controls()

    def _show_error_state(self, summary_text: str, *, source_text: str | None = None) -> None:
        self._last_frame = None
        self._last_source_text = source_text or ""
        self._unwrapped_frame = None
        self._homography_image_to_unwrapped = None
        self.source_label.setText(source_text or "")
        self.source_canvas.set_frame(None)
        self.plane_canvas.set_frame(None)
        self._active_canvas = self.source_canvas
        self._set_summary(summary_text, tone="warn")
        self.statusBar().showMessage(summary_text, 5000)
        self._update_next_click_label()
        self._refresh_controls()
        if source_text:
            QMessageBox.warning(self, "Planar Height Measurement", f"{summary_text}\n\n{source_text}")

    def _refresh_controls(self) -> None:
        has_video = self._video_capture is not None and self._video_path is not None
        self.previous_frame_btn.setEnabled(has_video and self._video_current_frame_index > 0)
        self.next_frame_btn.setEnabled(
            has_video
            and (
                self._video_frame_count <= 0
                or self._video_current_frame_index < self._video_frame_count - 1
            )
        )
        self.frame_slider.setEnabled(has_video)
        active = self._active_or_plane_canvas()
        has_points = self.source_canvas.point_count() > 0 or self.plane_canvas.point_count() > 0
        self.undo_btn.setEnabled(has_points)
        self.delete_btn.setEnabled(active.selected_label() is not None)
        self.clear_btn.setEnabled(has_points)
        has_frame = self._last_frame is not None
        self.zoom_label.setText(f"{int(round(active.zoom_factor() * 100.0))}%")
        self.zoom_out_btn.setEnabled(has_frame and active.zoom_factor() > 1.0)
        self.zoom_reset_btn.setEnabled(has_frame and active.zoom_factor() > 1.0)
        self.zoom_in_btn.setEnabled(has_frame and active.zoom_factor() < 10.0)

    def _update_window_title(self, source_text: str, video_frame_index: int | None) -> None:
        title_path = Path(source_text.split(" | ", 1)[0])
        suffix = title_path.name or "frame"
        if video_frame_index is not None:
            suffix = f"{suffix} frame {video_frame_index + 1}"
        self.setWindowTitle(f"Planar Prop Height Measurement - {suffix}")

    def closeEvent(self, event) -> None:
        self._close_video()
        super().closeEvent(event)
