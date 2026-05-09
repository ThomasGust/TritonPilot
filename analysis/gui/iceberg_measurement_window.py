from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
from PyQt6.QtCore import QRectF, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QCursor, QPainter, QPen
from PyQt6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from analysis.gui.crab_result_dialog import frame_to_pixmap
from analysis.iceberg_measurement import (
    MeasurementError,
    measure_affine_variable_length,
    measure_line_endpoint_iceberg_variable_length,
)
from gui.responsive import horizontal_scroll_area, resize_to_available_screen


IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp"}
VIDEO_SUFFIXES = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".wmv"}

AFFINE_MODE = "affine"
SPATIAL_MODE = "spatial"


@dataclass(frozen=True)
class ClickSpec:
    key: str
    label: str
    marker: str | None = None


def is_supported_image_path(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES


def is_supported_video_path(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in VIDEO_SUFFIXES


class MeasurementCanvas(QWidget):
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
        self._mode = AFFINE_MODE
        self._measurement_badge = ""
        self._selected_key: str | None = None
        self._hover_key: str | None = None
        self._dragging_key: str | None = None
        self._panning = False
        self._last_pan_pos: tuple[float, float] | None = None
        self._zoom = 1.0
        self._pan = np.array([0.0, 0.0], dtype=np.float64)
        self.setMinimumSize(520, 340)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def set_click_specs(self, mode: str, specs: list[ClickSpec], *, clear: bool = False) -> None:
        self._mode = mode
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

    def set_frame(self, frame_bgr: np.ndarray | None) -> None:
        self._pixmap = frame_to_pixmap(frame_bgr)
        if frame_bgr is None:
            self._image_width = 0
            self._image_height = 0
        else:
            self._image_height, self._image_width = frame_bgr.shape[:2]
        self.reset_zoom()
        self.clear_points()

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

    def undo_point(self) -> None:
        key = self._last_present_key()
        if key is None:
            return
        self._remove_point(key)

    def remove_selected_point(self) -> None:
        if self._selected_key is None:
            return
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
        if spec is None:
            return None
        return spec.label

    def required_point_count(self) -> int:
        return len(self._click_specs)

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
        return QRectF(
            target.x() + float(self._pan[0]),
            target.y() + float(self._pan[1]),
            target.width(),
            target.height(),
        )

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
        return {
            key: self._image_to_widget(point)
            for key, point in self._points_by_key.items()
        }

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
        factor = 1.2 ** wheel_steps
        self._set_zoom(
            self._zoom * factor,
            (event.position().x(), event.position().y()),
        )
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
        if self._mode == SPATIAL_MODE:
            self._draw_polyline(painter, widget_points, ["top_a_start", "top_a_end"], QColor("#55d6ff"), 3)
            self._draw_polyline(painter, widget_points, ["top_b_start", "top_b_end"], QColor("#55d6ff"), 3)
            self._draw_polyline(painter, widget_points, ["top_c_start", "top_c_end"], QColor("#55d6ff"), 3)
            self._draw_polyline(painter, widget_points, ["top_d_start", "top_d_end"], QColor("#55d6ff"), 3)
            self._draw_polyline(painter, widget_points, ["post_e_start", "post_e_end"], QColor("#8de46b"), 3)
            self._draw_polyline(painter, widget_points, ["post_f_start", "post_f_end"], QColor("#8de46b"), 3)
            self._draw_polyline(painter, widget_points, ["post_g_start", "post_g_end"], QColor("#8de46b"), 3)
            self._draw_polyline(painter, widget_points, ["variable_start", "variable_end"], QColor("#ffe66a"), 4)
            return

        self._draw_polyline(painter, widget_points, ["parallel_start", "parallel_end"], QColor("#55d6ff"), 3)
        self._draw_polyline(painter, widget_points, ["perpendicular_start", "perpendicular_end"], QColor("#8de46b"), 3)
        self._draw_polyline(painter, widget_points, ["variable_start", "variable_end"], QColor("#ffe66a"), 4)

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
        for index, spec in enumerate(self._click_specs):
            if spec.key not in widget_points:
                continue
            x, y = widget_points[spec.key]
            is_selected = spec.key == self._selected_key
            is_hovered = spec.key == self._hover_key
            radius = 6 if is_selected else 4
            marker_fill = QColor("#ffcf5a") if is_selected else QColor("#fff4a3")
            if is_hovered and not is_selected:
                marker_fill = QColor("#ffffff")

            marker_rect = QRectF(x - radius, y - radius, radius * 2, radius * 2)
            painter.setPen(QPen(marker_outline, 1))
            painter.setBrush(marker_fill)
            painter.drawEllipse(marker_rect)
            if is_selected:
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.setPen(QPen(QColor("#ffcf5a"), 2))
                painter.drawEllipse(QRectF(x - 10, y - 10, 20, 20))

    def _draw_measurement_badge(self, painter: QPainter, widget_points: dict[str, tuple[float, float]]) -> None:
        if not self._measurement_badge:
            return
        if self._mode == SPATIAL_MODE:
            start = widget_points.get("variable_start")
            end = widget_points.get("variable_end")
        else:
            start = widget_points.get("variable_start")
            end = widget_points.get("variable_end")
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


class IcebergMeasurementWindow(QMainWindow):
    def __init__(self, media_paths: list[str | Path] | None = None, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.setWindowTitle("Iceberg Hull Measurement")
        resize_to_available_screen(self, 1400, 860, min_width=860, min_height=600)

        self._video_path: Path | None = None
        self._video_capture: cv2.VideoCapture | None = None
        self._video_frame_count = 0
        self._video_fps = 0.0
        self._video_current_frame_index = 0
        self._updating_video_controls = False
        self._last_dir = str(Path.cwd())
        self._last_frame: np.ndarray | None = None
        self._last_source_text = ""

        self._build_ui()
        self._show_empty_state()
        resize_to_available_screen(self, 1400, 860, min_width=860, min_height=600)

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

        self.mode_combo = QComboBox()
        self.mode_combo.addItem("Line endpoints (recommended)", SPATIAL_MODE)
        self.mode_combo.addItem("Quick affine (straight-on only)", AFFINE_MODE)
        self.mode_combo.currentIndexChanged.connect(self._mode_changed)

        self.parallel_length_spin = self._make_length_spinbox(15.0)
        self.parallel_length_spin.valueChanged.connect(self._calibration_values_changed)
        self.perpendicular_length_spin = self._make_length_spinbox(55.0)
        self.perpendicular_length_spin.valueChanged.connect(self._calibration_values_changed)
        self.parallel_length_label = QLabel("Parallel ref")
        self.perpendicular_length_label = QLabel("Perpendicular ref")

        self.undo_btn = QPushButton("Undo")
        self.undo_btn.clicked.connect(self.canvas_undo)
        self.delete_btn = QPushButton("Delete Point")
        self.delete_btn.clicked.connect(self.canvas_delete_selected)
        self.clear_btn = QPushButton("Clear Points")
        self.clear_btn.clicked.connect(self.canvas_clear)

        self.zoom_out_btn = QPushButton("Zoom -")
        self.zoom_reset_btn = QPushButton("Fit")
        self.zoom_in_btn = QPushButton("Zoom +")
        self.zoom_label = QLabel("100%")
        self.zoom_label.setObjectName("summaryHint")
        self.zoom_label.setMinimumWidth(48)
        self.zoom_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        measurement_row = QHBoxLayout()
        measurement_row.addWidget(QLabel("Calibration"))
        measurement_row.addWidget(self.mode_combo)
        measurement_row.addSpacing(10)
        measurement_row.addWidget(self.parallel_length_label)
        measurement_row.addWidget(self.parallel_length_spin)
        measurement_row.addWidget(self.perpendicular_length_label)
        measurement_row.addWidget(self.perpendicular_length_spin)
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

        self.canvas = MeasurementCanvas(self)
        self.canvas.set_click_specs(self.current_mode(), self._current_click_specs())
        self.canvas.points_changed.connect(self._points_changed)
        self.canvas.selection_changed.connect(self._selection_changed)
        self.canvas.zoom_changed.connect(self._zoom_changed)
        self.zoom_out_btn.clicked.connect(self.canvas.zoom_out)
        self.zoom_reset_btn.clicked.connect(self.canvas.reset_zoom)
        self.zoom_in_btn.clicked.connect(self.canvas.zoom_in)

        container = QWidget(self)
        layout = QVBoxLayout(container)
        layout.addWidget(horizontal_scroll_area(video_row))
        layout.addWidget(horizontal_scroll_area(measurement_row))
        layout.addWidget(self.source_label)
        layout.addWidget(self.summary_label)
        layout.addWidget(self.detail_label)
        layout.addWidget(self.next_click_label)
        layout.addWidget(self.canvas, 1)
        self.setCentralWidget(container)

        self.statusBar().showMessage("Open a video or image to start measuring.")
        self._refresh_mode_labels()
        self._refresh_controls()

    @staticmethod
    def _make_length_spinbox(value: float) -> QDoubleSpinBox:
        spinbox = QDoubleSpinBox()
        spinbox.setDecimals(2)
        spinbox.setRange(0.01, 1000.0)
        spinbox.setSingleStep(1.0)
        spinbox.setValue(float(value))
        spinbox.setSuffix(" cm")
        spinbox.setMinimumWidth(105)
        return spinbox

    def current_mode(self) -> str:
        value = self.mode_combo.currentData()
        return str(value or AFFINE_MODE)

    def _current_click_specs(self) -> list[ClickSpec]:
        if self.current_mode() == SPATIAL_MODE:
            square_side = self.perpendicular_length_spin.value()
            known_post = self.parallel_length_spin.value()
            return [
                ClickSpec("top_a_start", f"A side start near E post ({square_side:g} cm pipe)", "A1"),
                ClickSpec("top_a_end", "A side end near F post", "A2"),
                ClickSpec("top_b_start", f"B side start near F post ({square_side:g} cm pipe)", "B1"),
                ClickSpec("top_b_end", "B side end near G post", "B2"),
                ClickSpec("top_c_start", f"C side start near G post ({square_side:g} cm pipe)", "C1"),
                ClickSpec("top_c_end", "C side end near variable post", "C2"),
                ClickSpec("top_d_start", f"D side start near variable post ({square_side:g} cm pipe)", "D1"),
                ClickSpec("top_d_end", "D side end near E post", "D2"),
                ClickSpec("post_e_start", f"E post upper endpoint ({known_post:g} cm pipe)", "E1"),
                ClickSpec("post_e_end", "E post lower endpoint", "E2"),
                ClickSpec("post_f_start", f"F post upper endpoint ({known_post:g} cm pipe)", "F1"),
                ClickSpec("post_f_end", "F post lower endpoint", "F2"),
                ClickSpec("post_g_start", f"G post upper endpoint ({known_post:g} cm pipe)", "G1"),
                ClickSpec("post_g_end", "G post lower endpoint", "G2"),
                ClickSpec("variable_start", "H variable upper endpoint", "H1"),
                ClickSpec("variable_end", "H variable lower endpoint", "H2"),
            ]

        parallel = self.parallel_length_spin.value()
        perpendicular = self.perpendicular_length_spin.value()
        return [
            ClickSpec("parallel_start", f"{parallel:g} cm start"),
            ClickSpec("parallel_end", f"{parallel:g} cm end"),
            ClickSpec("perpendicular_start", f"{perpendicular:g} cm start"),
            ClickSpec("perpendicular_end", f"{perpendicular:g} cm end"),
            ClickSpec("variable_start", "Variable start"),
            ClickSpec("variable_end", "Variable end"),
        ]

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
            self._show_error_state(
                "That file type is not a supported image.",
                source_text=str(path),
            )
            return

        image = cv2.imread(str(path))
        if image is None:
            self._show_error_state(
                "Could not read the selected image.",
                source_text=str(path),
            )
            return

        self._close_video()
        self._last_dir = str(path.parent)
        self._show_frame(image, source_text=str(path), video_frame_index=None)

    def set_video_path(self, video_path: str | Path) -> None:
        path = Path(video_path).expanduser()
        if not is_supported_video_path(path):
            self._show_error_state(
                "That file type is not a supported video.",
                source_text=str(path),
            )
            return

        self._close_video()
        capture = cv2.VideoCapture(str(path))
        if not capture.isOpened():
            self._show_error_state(
                "Could not open the selected video.",
                source_text=str(path),
            )
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
        if not ok:
            return None
        return frame

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
        source_text = self._format_video_source_text(self._video_current_frame_index)
        self._show_frame(frame, source_text=source_text, video_frame_index=self._video_current_frame_index)

    def _show_frame(
        self,
        frame: np.ndarray,
        *,
        source_text: str,
        video_frame_index: int | None,
    ) -> None:
        self._last_frame = frame.copy()
        self._last_source_text = source_text
        self.source_label.setText(source_text)
        self.canvas.set_frame(frame)
        self._set_summary(
            "Click the calibration points, then the variable segment.",
            detail_text=self._mode_detail_text(),
        )
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
        time_seconds = self._video_time_for_frame(frame_index)
        total_frames = max(1, self._video_frame_count)
        return (
            f"{self._video_path} | frame {frame_index + 1}/{total_frames} "
            f"@ {time_seconds:.2f}s"
        )

    def _open_image(self) -> None:
        selected_path, _ = QFileDialog.getOpenFileName(
            self,
            "Open iceberg frame",
            self._last_dir,
            "Images (*.jpg *.jpeg *.png *.bmp *.tif *.tiff *.webp)",
        )
        if selected_path:
            self.set_image_path(Path(selected_path))

    def _open_video(self) -> None:
        selected_path, _ = QFileDialog.getOpenFileName(
            self,
            "Open iceberg video",
            self._last_dir,
            "Videos (*.mp4 *.mov *.m4v *.avi *.mkv *.wmv)",
        )
        if selected_path:
            self.set_video_path(Path(selected_path))

    def _video_slider_changed(self, frame_index: int) -> None:
        if self._updating_video_controls:
            return
        self._show_video_frame(frame_index)

    def _show_previous_video_frame(self) -> None:
        self._show_video_frame(max(0, self._video_current_frame_index - 1))

    def _show_next_video_frame(self) -> None:
        if self._video_frame_count > 0:
            next_frame = min(self._video_frame_count - 1, self._video_current_frame_index + 1)
        else:
            next_frame = self._video_current_frame_index + 1
        self._show_video_frame(next_frame)

    def _mode_changed(self, *_args) -> None:
        self._refresh_mode_labels()
        self.canvas.set_click_specs(
            self.current_mode(),
            self._current_click_specs(),
            clear=True,
        )
        if self._last_frame is not None:
            self._set_summary(
                "Click the calibration points, then the variable segment.",
                detail_text=self._mode_detail_text(),
            )
        self._update_next_click_label()
        self._refresh_controls()

    def _refresh_mode_labels(self) -> None:
        if self.current_mode() == SPATIAL_MODE:
            self.parallel_length_label.setText("Known posts")
            self.perpendicular_length_label.setText("Top pipes")
            return
        self.parallel_length_label.setText("Parallel ref")
        self.perpendicular_length_label.setText("Perpendicular ref")

    def _calibration_values_changed(self, *_args) -> None:
        self.canvas.set_click_specs(self.current_mode(), self._current_click_specs())
        self._points_changed(self.canvas.point_count())

    def canvas_undo(self) -> None:
        self.canvas.undo_point()

    def canvas_delete_selected(self) -> None:
        self.canvas.remove_selected_point()

    def canvas_clear(self) -> None:
        self.canvas.clear_points()

    def _points_changed(self, count: int) -> None:
        self._update_next_click_label()
        self._refresh_controls()
        self._recalculate_measurement()

    def _selection_changed(self, _key: str) -> None:
        self._update_next_click_label()
        self._refresh_controls()

    def _zoom_changed(self, zoom: float) -> None:
        self.zoom_label.setText(f"{int(round(zoom * 100.0))}%")
        self._refresh_controls()

    def _recalculate_measurement(self) -> None:
        if self._last_frame is None:
            self.canvas.set_measurement_badge(None)
            return

        count = self.canvas.point_count()
        required = self.canvas.required_point_count()
        if count < required:
            self.canvas.set_measurement_badge(None)
            self._set_summary(
                f"{count}/{required} points selected.",
                detail_text=self._mode_detail_text(),
            )
            return

        points = self.canvas.points_by_key()
        try:
            if self.current_mode() == SPATIAL_MODE:
                result = measure_line_endpoint_iceberg_variable_length(
                    top_line_image_segments=[
                        [points["top_a_start"], points["top_a_end"]],
                        [points["top_b_start"], points["top_b_end"]],
                        [points["top_c_start"], points["top_c_end"]],
                        [points["top_d_start"], points["top_d_end"]],
                    ],
                    known_post_image_segments=[
                        [points["post_e_start"], points["post_e_end"]],
                        [points["post_f_start"], points["post_f_end"]],
                        [points["post_g_start"], points["post_g_end"]],
                    ],
                    variable_image_segment=[points["variable_start"], points["variable_end"]],
                    top_pipe_cm=self.perpendicular_length_spin.value(),
                    known_post_cm=self.parallel_length_spin.value(),
                )
                self.canvas.set_measurement_badge(f"{result.length_cm:.1f} cm")
                inset_text = (
                    f" | inferred square inset={result.top_joint_inset_cm:.2f} cm"
                    if result.top_joint_inset_cm is not None
                    else ""
                )
                self._set_summary(
                    f"Variable length: {result.length_cm:.2f} cm",
                    detail_text=(
                        "endpoint geometry | "
                        f"known-point fit={result.reprojection_rmse_px:.2f} px | "
                        f"variable-line fit={result.variable_reprojection_error_px:.2f} px"
                        f"{inset_text}"
                    ),
                )
                self.statusBar().showMessage("Iceberg measurement updated.", 5000)
                return

            result = measure_affine_variable_length(
                parallel_reference_start=points["parallel_start"],
                parallel_reference_end=points["parallel_end"],
                perpendicular_reference_start=points["perpendicular_start"],
                perpendicular_reference_end=points["perpendicular_end"],
                variable_start=points["variable_start"],
                variable_end=points["variable_end"],
                parallel_reference_cm=self.parallel_length_spin.value(),
                perpendicular_reference_cm=self.perpendicular_length_spin.value(),
            )
        except MeasurementError as exc:
            self.canvas.set_measurement_badge(None)
            self._set_summary(str(exc), detail_text=self._mode_detail_text(), tone="warn")
            self.statusBar().showMessage(str(exc), 5000)
            return

        ratio_delta = result.length_cm - result.parallel_only_length_cm
        self.canvas.set_measurement_badge(f"{result.length_cm:.1f} cm")
        self._set_summary(
            f"Quick affine estimate: {result.length_cm:.2f} cm",
            detail_text=(
                "not perspective corrected | "
                f"parallel-only check={result.parallel_only_length_cm:.2f} cm | "
                f"check delta={ratio_delta:+.2f} cm | "
                f"parallel component={result.parallel_component_cm:.2f} cm | "
                f"perpendicular drift={result.perpendicular_component_cm:.2f} cm | "
                f"alignment error={result.alignment_error_degrees:.1f} deg"
            ),
            tone="warn",
        )
        self.statusBar().showMessage(
            "Quick affine estimate updated; use line endpoint geometry when the camera is angled.",
            6000,
        )

    def _mode_detail_text(self) -> str:
        if self.current_mode() == SPATIAL_MODE:
            return (
                "Click both endpoints of each 55 cm top pipe in order A, B, C, D around the square. "
                "Then click the upper and lower endpoints of known posts E, F, G, and the two endpoints of variable line H. "
                "Drag markers to refine; right-click a marker or use Delete Point to remove it."
            )
        return (
            "Quick estimate only: use the 15 cm segment parallel to the variable piece, the 55 cm perpendicular "
            "segment, then the variable endpoints. Drag markers to refine. This can be very wrong when the camera is angled."
        )

    def _update_next_click_label(self) -> None:
        next_label = self.canvas.next_click_label()
        selected_label = self.canvas.selected_label()
        if next_label is None:
            text = "All points selected."
        else:
            text = f"Next: {next_label}"
        if selected_label is not None:
            text = f"{text}  Selected: {selected_label}"
        self.next_click_label.setText(text)

    def _set_summary(
        self,
        summary_text: str,
        *,
        detail_text: str | None = None,
        tone: str | None = None,
    ) -> None:
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
        self.source_label.clear()
        self.canvas.set_frame(None)
        self._set_summary(
            "Open a video or image to start iceberg measurement.",
            detail_text=self._mode_detail_text(),
        )
        self._update_next_click_label()
        self._refresh_controls()

    def _show_error_state(
        self,
        summary_text: str,
        *,
        source_text: str | None = None,
    ) -> None:
        self._last_frame = None
        self._last_source_text = source_text or ""
        self.source_label.setText(source_text or "")
        self.canvas.set_frame(None)
        self._set_summary(summary_text, tone="warn")
        self.statusBar().showMessage(summary_text, 5000)
        self._update_next_click_label()
        self._refresh_controls()
        if source_text:
            QMessageBox.warning(self, "Iceberg Measurement", f"{summary_text}\n\n{source_text}")

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
        has_points = self.canvas.point_count() > 0
        self.undo_btn.setEnabled(has_points)
        self.delete_btn.setEnabled(self.canvas.selected_label() is not None)
        self.clear_btn.setEnabled(has_points)
        has_frame = self._last_frame is not None
        self.zoom_out_btn.setEnabled(has_frame and self.canvas.zoom_factor() > 1.0)
        self.zoom_reset_btn.setEnabled(has_frame and self.canvas.zoom_factor() > 1.0)
        self.zoom_in_btn.setEnabled(has_frame and self.canvas.zoom_factor() < 10.0)

    def _update_window_title(self, source_text: str, video_frame_index: int | None) -> None:
        title_path = Path(source_text.split(" | ", 1)[0])
        suffix = title_path.name or "frame"
        if video_frame_index is not None:
            suffix = f"{suffix} frame {video_frame_index + 1}"
        self.setWindowTitle(f"Iceberg Hull Measurement - {suffix}")

    def closeEvent(self, event) -> None:
        self._close_video()
        super().closeEvent(event)
