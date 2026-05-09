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
    QSplitter,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from analysis.gui.crab_result_dialog import frame_to_pixmap
from analysis.planar_measurement import MeasurementError, measure_planar_segment_from_plane
from gui.responsive import horizontal_scroll_area, resize_to_available_screen


IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp"}
VIDEO_SUFFIXES = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".wmv"}
REFERENCE_COUNT = 7
INITIAL_MEASUREMENT_COUNT = 2
DEFAULT_REFERENCE_LENGTHS_CM = [25.0, 25.0, 15.0, 15.0, 21.0, 21.0, 45.0]
MEASUREMENT_COLORS = ["#ffe66a", "#ff8f4f", "#55d6ff", "#d59cff"]
UNWRAPPED_WIDTH = 1480
UNWRAPPED_HEIGHT = 700
SOURCE_MODE_REFINED = "refined_12"
SOURCE_MODE_QUICK = "quick_6"
QUICK_SOURCE_KEYS = [
    "top_start",
    "top_end",
    "left_side_top",
    "left_side_bottom",
    "right_side_top",
    "right_side_bottom",
]
REFINED_SOURCE_KEYS = [
    "left_a",
    "left_b",
    "left_c",
    "left_d",
    "center_a",
    "center_b",
    "center_c",
    "center_d",
    "right_a",
    "right_b",
    "right_c",
    "right_d",
]
ANCHOR_KEYS = [
    "anchor_top_left",
    "anchor_top_right",
    "anchor_bottom_right",
    "anchor_bottom_left",
]


def measurement_segment_key(index: int) -> str:
    return f"measure_{index + 1}"


def measurement_start_key(index: int) -> str:
    return f"{measurement_segment_key(index)}_start"


def measurement_end_key(index: int) -> str:
    return f"{measurement_segment_key(index)}_end"


def measurement_index_from_key(key: str) -> int | None:
    parts = key.split("_")
    if len(parts) != 3 or parts[0] != "measure" or parts[2] not in {"start", "end"}:
        return None
    try:
        number = int(parts[1])
    except ValueError:
        return None
    if number < 1:
        return None
    return number - 1


def measurement_indices_from_specs(specs: list[ClickSpec]) -> list[int]:
    indices = {
        index
        for spec in specs
        for index in [measurement_index_from_key(spec.key)]
        if index is not None
    }
    return sorted(indices)


@dataclass(frozen=True)
class ClickSpec:
    key: str
    label: str


def is_supported_image_path(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES


def is_supported_video_path(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in VIDEO_SUFFIXES


def line_intersection(
    line_a_start: tuple[float, float],
    line_a_end: tuple[float, float],
    line_b_start: tuple[float, float],
    line_b_end: tuple[float, float],
) -> tuple[float, float]:
    a1 = np.array([line_a_start[0], line_a_start[1], 1.0], dtype=np.float64)
    a2 = np.array([line_a_end[0], line_a_end[1], 1.0], dtype=np.float64)
    b1 = np.array([line_b_start[0], line_b_start[1], 1.0], dtype=np.float64)
    b2 = np.array([line_b_end[0], line_b_end[1], 1.0], dtype=np.float64)
    line_a = np.cross(a1, a2)
    line_b = np.cross(b1, b2)
    intersection = np.cross(line_a, line_b)
    if abs(float(intersection[2])) < 1.0e-9:
        raise MeasurementError("Boundary lines are too close to parallel")
    return (
        float(intersection[0] / intersection[2]),
        float(intersection[1] / intersection[2]),
    )


def homogeneous_line(
    point_a: tuple[float, float],
    point_b: tuple[float, float],
) -> np.ndarray:
    a = np.array([point_a[0], point_a[1], 1.0], dtype=np.float64)
    b = np.array([point_b[0], point_b[1], 1.0], dtype=np.float64)
    line = np.cross(a, b)
    norm = float(np.linalg.norm(line[:2]))
    if norm < 1.0e-9:
        raise MeasurementError("Line endpoints are too close together")
    return line / norm


def fit_line_through_points(points: list[tuple[float, float]]) -> np.ndarray:
    if len(points) < 2:
        raise MeasurementError("At least two points are required to fit a line")
    homogeneous_points = np.asarray(
        [[point[0], point[1], 1.0] for point in points],
        dtype=np.float64,
    )
    try:
        _, _, vh = np.linalg.svd(homogeneous_points)
    except np.linalg.LinAlgError as exc:
        raise MeasurementError("Could not fit a boundary line") from exc
    line = vh[-1]
    norm = float(np.linalg.norm(line[:2]))
    if norm < 1.0e-9:
        raise MeasurementError("Could not fit a usable boundary line")
    return line / norm


def fit_vanishing_point(lines: list[np.ndarray]) -> np.ndarray:
    if len(lines) < 2:
        raise MeasurementError("At least two parallel direction lines are required")
    line_matrix = np.asarray(lines, dtype=np.float64)
    try:
        _, _, vh = np.linalg.svd(line_matrix)
    except np.linalg.LinAlgError as exc:
        raise MeasurementError("Could not fit a vanishing point") from exc
    point = vh[-1]
    if not np.all(np.isfinite(point)) or float(np.linalg.norm(point[:2])) < 1.0e-9:
        raise MeasurementError("Could not fit a usable vanishing point")
    return point


def line_through_anchor_and_vanishing(
    anchor: tuple[float, float],
    vanishing: np.ndarray,
) -> np.ndarray:
    anchor_h = np.array([anchor[0], anchor[1], 1.0], dtype=np.float64)
    line = np.cross(anchor_h, vanishing)
    norm = float(np.linalg.norm(line[:2]))
    if norm < 1.0e-9:
        raise MeasurementError("Could not build a boundary line from fitted directions")
    return line / norm


def intersection_from_lines(line_a: np.ndarray, line_b: np.ndarray) -> tuple[float, float]:
    point = np.cross(line_a, line_b)
    if abs(float(point[2])) < 1.0e-9:
        raise MeasurementError("Boundary lines are too close to parallel")
    return (
        float(point[0] / point[2]),
        float(point[1] / point[2]),
    )


def average_point(points: list[tuple[float, float]]) -> tuple[float, float]:
    value = np.asarray(points, dtype=np.float64)
    return (float(np.mean(value[:, 0])), float(np.mean(value[:, 1])))


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
        self._measurement_badges: dict[str, str] = {}
        self._selected_key: str | None = None
        self._hover_key: str | None = None
        self._dragging_key: str | None = None
        self._last_changed_key: str | None = None
        self._pan_mode = False
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
            self._emit_points_changed("__specs__")
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

    def set_points_by_key(
        self,
        points_by_key: dict[str, tuple[float, float]],
        *,
        replace: bool = False,
        emit: bool = True,
    ) -> None:
        valid_keys = {spec.key for spec in self._click_specs}
        updated = {} if replace else dict(self._points_by_key)
        for key, point in points_by_key.items():
            if key not in valid_keys:
                continue
            updated[key] = (float(point[0]), float(point[1]))

        if updated == self._points_by_key:
            self.update()
            return

        self._points_by_key = updated
        if self._selected_key not in self._points_by_key:
            self._set_selected_key(None)
        if self._hover_key not in self._points_by_key:
            self._hover_key = None
        if self._dragging_key not in self._points_by_key:
            self._dragging_key = None
        if emit:
            self._emit_points_changed("__programmatic__")
        self._refresh_cursor()
        self.update()

    def remove_points_by_key(self, keys: list[str] | tuple[str, ...], *, emit: bool = True) -> None:
        removed = False
        for key in keys:
            if key in self._points_by_key:
                del self._points_by_key[key]
                removed = True
        if not removed:
            return

        if self._selected_key not in self._points_by_key:
            self._set_selected_key(None)
        if self._hover_key not in self._points_by_key:
            self._hover_key = None
        if self._dragging_key not in self._points_by_key:
            self._dragging_key = None
        self._measurement_badges = {}
        if emit:
            self._emit_points_changed("__programmatic__")
        self._refresh_cursor()
        self.update()

    def set_pan_mode(self, enabled: bool) -> None:
        self._pan_mode = bool(enabled)
        if self._pan_mode and self._dragging_key is not None:
            self._dragging_key = None
        self._refresh_cursor()
        self.update()

    def pan_mode(self) -> bool:
        return self._pan_mode

    def set_measurement_badge(self, text: str | None) -> None:
        self._measurement_badges = {}
        if text:
            self._measurement_badges[measurement_segment_key(0)] = text
        self.update()

    def set_measurement_badges(self, badges_by_segment: dict[str, str] | None) -> None:
        self._measurement_badges = dict(badges_by_segment or {})
        self.update()

    def clear_points(self) -> None:
        self._points_by_key = {}
        self._hover_key = None
        self._dragging_key = None
        self._panning = False
        self._last_pan_pos = None
        self._set_selected_key(None)
        self._measurement_badges = {}
        self._emit_points_changed("__clear__")
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
        return self.label_for_key(self._selected_key)

    def label_for_key(self, key: str) -> str | None:
        for spec in self._click_specs:
            if spec.key == key:
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
        self._measurement_badges = {}
        self._emit_points_changed(key)
        self._refresh_cursor()
        self.update()

    def last_changed_key(self) -> str | None:
        return self._last_changed_key

    def has_frame(self) -> bool:
        return not self._pixmap.isNull() and self._image_width > 0 and self._image_height > 0

    def _emit_points_changed(self, key: str | None) -> None:
        self._last_changed_key = key
        self.points_changed.emit(len(self._points_by_key))

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

    def _widget_to_image(
        self,
        x: float,
        y: float,
        *,
        clamp: bool = True,
        require_inside: bool = True,
    ) -> tuple[float, float] | None:
        target = self._target_rect()
        if target.width() <= 1.0e-9 or target.height() <= 1.0e-9:
            return None
        if require_inside and not target.contains(x, y):
            return None
        image_x = (x - target.x()) * self._image_width / max(1.0, target.width())
        image_y = (y - target.y()) * self._image_height / max(1.0, target.height())
        if not clamp:
            margin_x = max(1.0, float(self._image_width))
            margin_y = max(1.0, float(self._image_height))
            return (
                float(np.clip(image_x, -margin_x, margin_x * 2.0)),
                float(np.clip(image_y, -margin_y, margin_y * 2.0)),
            )
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
        if self._pan_mode and not self._pixmap.isNull():
            self.setCursor(QCursor(Qt.CursorShape.OpenHandCursor))
            return
        if self._dragging_key is not None or self._hover_key is not None:
            self.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
            return
        if not self._pixmap.isNull() and self._first_missing_spec() is not None:
            self.setCursor(QCursor(Qt.CursorShape.CrossCursor))
            return
        self.setCursor(QCursor(Qt.CursorShape.ArrowCursor))

    def _start_pan(self, x: float, y: float) -> None:
        if self._pixmap.isNull() or self._zoom <= 1.0:
            return
        self._panning = True
        self._last_pan_pos = (x, y)
        self._refresh_cursor()

    def mousePressEvent(self, event) -> None:
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        x = event.position().x()
        y = event.position().y()

        if event.button() == Qt.MouseButton.MiddleButton:
            self._start_pan(x, y)
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
        if self._pan_mode or event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
            self._start_pan(x, y)
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
        self._measurement_badges = {}
        self._set_selected_key(next_spec.key)
        self._dragging_key = next_spec.key
        self._emit_points_changed(next_spec.key)
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
            point = self._widget_to_image(x, y, clamp=False, require_inside=False)
            if point is None:
                return
            self._points_by_key[self._dragging_key] = point
            self._measurement_badges = {}
            self._emit_points_changed(self._dragging_key)
            self.update()
            return

        hover_key = None if self._pixmap.isNull() else self._nearest_point_key(x, y)
        if hover_key != self._hover_key:
            self._hover_key = hover_key
            self._refresh_cursor()
            self.update()

    def mouseReleaseEvent(self, event) -> None:
        if event.button() in (Qt.MouseButton.MiddleButton, Qt.MouseButton.LeftButton) and self._panning:
            self._panning = False
            self._last_pan_pos = None
            self._refresh_cursor()
            return
        if event.button() == Qt.MouseButton.LeftButton and self._dragging_key is not None:
            released_key = self._dragging_key
            self._dragging_key = None
            self._emit_points_changed(released_key)
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
        self._draw_measurement_badges(painter, widget_points)

    def _draw_segments(self, painter: QPainter, widget_points: dict[str, tuple[float, float]]) -> None:
        if any(key in widget_points for key in ("left_a", "center_a", "right_a")):
            self._draw_polyline(painter, widget_points, ["left_a", "left_b", "left_c", "left_d", "left_a"], QColor("#55d6ff"), 3)
            self._draw_polyline(painter, widget_points, ["center_a", "center_b", "center_c", "center_d", "center_a"], QColor("#7ee6ff"), 3)
            self._draw_polyline(painter, widget_points, ["right_a", "right_b", "right_c", "right_d", "right_a"], QColor("#55d6ff"), 3)
            self._draw_polyline(
                painter,
                widget_points,
                [
                    "anchor_top_left",
                    "anchor_top_right",
                    "anchor_bottom_right",
                    "anchor_bottom_left",
                    "anchor_top_left",
                ],
                QColor("#ff8f4f"),
                3,
            )
            return

        if any(key in widget_points for key in ("top_start", "left_side_top", "right_side_top")):
            self._draw_polyline(painter, widget_points, ["top_start", "top_end"], QColor("#55d6ff"), 3)
            self._draw_polyline(painter, widget_points, ["left_side_top", "left_side_bottom"], QColor("#8de46b"), 3)
            self._draw_polyline(painter, widget_points, ["right_side_top", "right_side_bottom"], QColor("#8de46b"), 3)
            self._draw_polyline(painter, widget_points, ["left_side_bottom", "right_side_bottom"], QColor("#55d6ff"), 2)
            required = {
                "top_start",
                "top_end",
                "left_side_top",
                "left_side_bottom",
                "right_side_top",
                "right_side_bottom",
            }
            if required.issubset(widget_points):
                try:
                    top_left = line_intersection(
                        widget_points["top_start"],
                        widget_points["top_end"],
                        widget_points["left_side_top"],
                        widget_points["left_side_bottom"],
                    )
                    top_right = line_intersection(
                        widget_points["top_start"],
                        widget_points["top_end"],
                        widget_points["right_side_top"],
                        widget_points["right_side_bottom"],
                    )
                except MeasurementError:
                    return
                painter.setPen(QPen(QColor("#ffe66a"), 3))
                polygon = [
                    top_left,
                    top_right,
                    widget_points["right_side_bottom"],
                    widget_points["left_side_bottom"],
                    top_left,
                ]
                for start, end in zip(polygon, polygon[1:]):
                    painter.drawLine(int(start[0]), int(start[1]), int(end[0]), int(end[1]))
            self._draw_polyline(
                painter,
                widget_points,
                [
                    "anchor_top_left",
                    "anchor_top_right",
                    "anchor_bottom_right",
                    "anchor_bottom_left",
                    "anchor_top_left",
                ],
                QColor("#ff8f4f"),
                3,
            )
            return

        if any(key in widget_points for key in ANCHOR_KEYS):
            self._draw_polyline(
                painter,
                widget_points,
                [
                    "anchor_top_left",
                    "anchor_top_right",
                    "anchor_bottom_right",
                    "anchor_bottom_left",
                    "anchor_top_left",
                ],
                QColor("#ff8f4f"),
                3,
            )
            return

        for index in range(REFERENCE_COUNT):
            self._draw_polyline(
                painter,
                widget_points,
                [f"ref_{index + 1}_start", f"ref_{index + 1}_end"],
                QColor("#8de46b"),
                2,
            )
        for index in measurement_indices_from_specs(self._click_specs):
            color = QColor(MEASUREMENT_COLORS[index % len(MEASUREMENT_COLORS)])
            self._draw_polyline(
                painter,
                widget_points,
                [measurement_start_key(index), measurement_end_key(index)],
                color,
                4,
            )

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
            if spec.key in ANCHOR_KEYS:
                marker_fill = QColor("#ff8f4f")
            else:
                marker_fill = QColor("#fff4a3")
            if is_selected:
                marker_fill = QColor("#ffcf5a")
            if is_hovered and not is_selected:
                marker_fill = QColor("#ffffff")

            painter.setPen(QPen(marker_outline, 1))
            painter.setBrush(marker_fill)
            painter.drawEllipse(QRectF(x - radius, y - radius, radius * 2, radius * 2))
            if is_selected:
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.setPen(QPen(QColor("#ffcf5a"), 2))
                painter.drawEllipse(QRectF(x - 10, y - 10, 20, 20))

    def _draw_measurement_badges(self, painter: QPainter, widget_points: dict[str, tuple[float, float]]) -> None:
        if not self._measurement_badges:
            return
        for index in measurement_indices_from_specs(self._click_specs):
            segment_key = measurement_segment_key(index)
            badge = self._measurement_badges.get(segment_key)
            if not badge:
                continue

            start = widget_points.get(measurement_start_key(index))
            end = widget_points.get(measurement_end_key(index))
            if start is None or end is None:
                continue

            x = int((start[0] + end[0]) * 0.5)
            y = int((start[1] + end[1]) * 0.5)
            text_rect = QRectF(x - 70, y - 34, 140, 28)
            painter.setPen(QPen(QColor("#111318"), 1))
            painter.setBrush(QColor(20, 24, 32, 230))
            painter.drawRoundedRect(text_rect, 6, 6)
            painter.setPen(QColor(MEASUREMENT_COLORS[index % len(MEASUREMENT_COLORS)]))
            painter.drawText(text_rect, Qt.AlignmentFlag.AlignCenter, badge)


class MultiRectLengthMeasurementWindow(QMainWindow):
    def __init__(self, media_paths: list[str | Path] | None = None, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.setWindowTitle("Multi-Rectangle Prop Length Measurement")
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
        self._measurement_count = INITIAL_MEASUREMENT_COUNT

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

        self.source_mode_combo = QComboBox()
        self.source_mode_combo.addItem("6 boundary lines + corner anchors", SOURCE_MODE_QUICK)
        self.source_mode_combo.addItem("12 rectangle corners (experimental)", SOURCE_MODE_REFINED)
        self.source_mode_combo.currentIndexChanged.connect(self._source_mode_changed)

        self.reference_length_spins = [
            self._make_length_spinbox(value) for value in DEFAULT_REFERENCE_LENGTHS_CM
        ]
        for spinbox in self.reference_length_spins:
            spinbox.valueChanged.connect(self._calibration_values_changed)

        self.zoom_out_btn = QPushButton("Zoom -")
        self.zoom_reset_btn = QPushButton("Fit")
        self.zoom_in_btn = QPushButton("Zoom +")
        self.pan_btn = QPushButton("Pan")
        self.pan_btn.setCheckable(True)
        self.pan_btn.toggled.connect(self._pan_mode_changed)
        self.zoom_label = QLabel("100%")
        self.zoom_label.setObjectName("summaryHint")
        self.zoom_label.setMinimumWidth(48)
        self.zoom_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.undo_btn = QPushButton("Undo")
        self.undo_btn.clicked.connect(self.canvas_undo)
        self.delete_btn = QPushButton("Delete")
        self.delete_btn.clicked.connect(self.canvas_delete_selected)
        self.clear_btn = QPushButton("Clear")
        self.clear_btn.clicked.connect(self.canvas_clear)
        self.add_measure_btn = QPushButton("Add")
        self.add_measure_btn.clicked.connect(self._add_measurement_line)
        self.remove_measure_btn = QPushButton("Remove")
        self.remove_measure_btn.clicked.connect(self._remove_measurement_line)

        reference_row = QHBoxLayout()
        reference_row.addWidget(QLabel("Boundary"))
        reference_row.addWidget(self.source_mode_combo)
        reference_row.addSpacing(10)
        reference_row.addWidget(QLabel("Reference lengths"))
        for index, spinbox in enumerate(self.reference_length_spins, start=1):
            reference_row.addWidget(QLabel(f"R{index}"))
            reference_row.addWidget(spinbox)
        reference_row.addStretch(1)

        view_row = QHBoxLayout()
        view_row.addWidget(QLabel("View"))
        view_row.addWidget(self.zoom_out_btn)
        view_row.addWidget(self.zoom_reset_btn)
        view_row.addWidget(self.zoom_in_btn)
        view_row.addWidget(self.pan_btn)
        view_row.addWidget(self.zoom_label)
        view_row.addStretch(1)

        action_row = QHBoxLayout()
        action_row.addWidget(QLabel("Measurements"))
        action_row.addWidget(self.add_measure_btn)
        action_row.addWidget(self.remove_measure_btn)
        action_row.addSpacing(10)
        action_row.addWidget(QLabel("Points"))
        action_row.addWidget(self.undo_btn)
        action_row.addWidget(self.delete_btn)
        action_row.addWidget(self.clear_btn)
        action_row.addStretch(1)

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

        self.preview_canvas = PlanarMeasurementCanvas(self)
        self.preview_canvas.set_click_specs([])
        self.preview_canvas.zoom_changed.connect(self._zoom_changed)

        self.plane_canvas = PlanarMeasurementCanvas(self)
        self.plane_canvas.set_click_specs(self._plane_click_specs())
        self.plane_canvas.points_changed.connect(self._points_changed)
        self.plane_canvas.selection_changed.connect(self._selection_changed)
        self.plane_canvas.zoom_changed.connect(self._zoom_changed)
        self._active_canvas = self.source_canvas

        self.zoom_out_btn.clicked.connect(self._zoom_active_out)
        self.zoom_reset_btn.clicked.connect(self._zoom_active_reset)
        self.zoom_in_btn.clicked.connect(self._zoom_active_in)

        source_panel = QWidget(self)
        source_layout = QVBoxLayout(source_panel)
        source_layout.setContentsMargins(0, 0, 0, 0)
        source_label = QLabel("Original frame: click boundary lines, then drag orange corner anchors")
        source_label.setObjectName("summaryHint")
        source_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        source_label.setWordWrap(True)
        source_layout.addWidget(source_label)
        source_layout.addWidget(self.source_canvas, 1)

        preview_panel = QWidget(self)
        preview_layout = QVBoxLayout(preview_panel)
        preview_layout.setContentsMargins(0, 0, 0, 0)
        preview_label = QLabel("Live unwrapped preview")
        preview_label.setObjectName("summaryHint")
        preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        preview_label.setWordWrap(True)
        preview_layout.addWidget(preview_label)
        preview_layout.addWidget(self.preview_canvas, 1)

        plane_panel = QWidget(self)
        plane_layout = QVBoxLayout(plane_panel)
        plane_layout.setContentsMargins(0, 0, 0, 0)
        plane_label = QLabel("Unwrapped plane: draw references and measurements")
        plane_label.setObjectName("summaryHint")
        plane_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        plane_label.setWordWrap(True)
        plane_layout.addWidget(plane_label)
        plane_layout.addWidget(self.plane_canvas, 1)

        self.setup_splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self.setup_splitter.setChildrenCollapsible(False)
        self.setup_splitter.addWidget(source_panel)
        self.setup_splitter.addWidget(preview_panel)
        self.setup_splitter.setStretchFactor(0, 1)
        self.setup_splitter.setStretchFactor(1, 1)
        self.setup_splitter.setSizes([1, 1])

        self.view_tabs = QTabWidget(self)
        self.view_tabs.addTab(self.setup_splitter, "Anchors")
        self.view_tabs.addTab(plane_panel, "Unwrapped")
        self.view_tabs.currentChanged.connect(self._view_tab_changed)

        container = QWidget(self)
        layout = QVBoxLayout(container)
        layout.addWidget(horizontal_scroll_area(video_row))
        layout.addWidget(horizontal_scroll_area(reference_row))
        layout.addLayout(view_row)
        layout.addLayout(action_row)
        layout.addWidget(self.source_label)
        layout.addWidget(self.summary_label)
        layout.addWidget(self.detail_label)
        layout.addWidget(self.next_click_label)
        layout.addWidget(self.view_tabs, 1)
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

    def _current_source_mode(self) -> str:
        if not hasattr(self, "source_mode_combo"):
            return SOURCE_MODE_QUICK
        return str(self.source_mode_combo.currentData() or SOURCE_MODE_QUICK)

    def _source_click_specs(self) -> list[ClickSpec]:
        if self._current_source_mode() == SOURCE_MODE_QUICK:
            source_specs = [
                ClickSpec("top_start", "Top line point 1"),
                ClickSpec("top_end", "Top line point 2"),
                ClickSpec("left_side_top", "Left boundary upper point"),
                ClickSpec("left_side_bottom", "Left boundary lower point"),
                ClickSpec("right_side_top", "Right boundary upper point"),
                ClickSpec("right_side_bottom", "Right boundary lower point"),
            ]
            return source_specs + self._anchor_click_specs()

        source_specs = [
            ClickSpec("left_a", "Left rectangle top-left"),
            ClickSpec("left_b", "Left rectangle top-right"),
            ClickSpec("left_c", "Left rectangle bottom-right"),
            ClickSpec("left_d", "Left rectangle bottom-left"),
            ClickSpec("center_a", "Center rectangle top-left"),
            ClickSpec("center_b", "Center rectangle top-right"),
            ClickSpec("center_c", "Center rectangle bottom-right"),
            ClickSpec("center_d", "Center rectangle bottom-left"),
            ClickSpec("right_a", "Right rectangle top-left"),
            ClickSpec("right_b", "Right rectangle top-right"),
            ClickSpec("right_c", "Right rectangle bottom-right"),
            ClickSpec("right_d", "Right rectangle bottom-left"),
        ]
        return source_specs + self._anchor_click_specs()

    def _anchor_click_specs(self) -> list[ClickSpec]:
        return [
            ClickSpec("anchor_top_left", "Anchor top-left"),
            ClickSpec("anchor_top_right", "Anchor top-right"),
            ClickSpec("anchor_bottom_right", "Anchor bottom-right"),
            ClickSpec("anchor_bottom_left", "Anchor bottom-left"),
        ]

    def _plane_click_specs(self) -> list[ClickSpec]:
        specs: list[ClickSpec] = []
        for index in range(REFERENCE_COUNT):
            number = index + 1
            specs.append(ClickSpec(f"ref_{number}_start", f"Reference {number} start"))
            specs.append(ClickSpec(f"ref_{number}_end", f"Reference {number} end"))
        for index in range(self._measurement_count):
            number = index + 1
            specs.append(ClickSpec(measurement_start_key(index), f"Measurement {number} start"))
            specs.append(ClickSpec(measurement_end_key(index), f"Measurement {number} end"))
        return specs

    def _source_solver_keys(self) -> list[str]:
        if self._current_source_mode() == SOURCE_MODE_QUICK:
            return QUICK_SOURCE_KEYS
        return REFINED_SOURCE_KEYS

    def _count_present_keys(self, keys: list[str]) -> int:
        points = self.source_canvas.points_by_key()
        return sum(1 for key in keys if key in points)

    def _source_solver_ready(self) -> bool:
        points = self.source_canvas.points_by_key()
        return all(key in points for key in self._source_solver_keys())

    def _anchor_points_ready(self) -> bool:
        points = self.source_canvas.points_by_key()
        return all(key in points for key in ANCHOR_KEYS)

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
        self.preview_canvas.set_frame(None)
        self.plane_canvas.set_frame(None)
        self._active_canvas = self.source_canvas
        self._set_summary("Click the boundary lines, then adjust the orange homography corners.", detail_text=self._mode_detail_text())
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

    def _anchor_points_by_key(self) -> dict[str, tuple[float, float]]:
        points = self.source_canvas.points_by_key()
        return {key: points[key] for key in ANCHOR_KEYS if key in points}

    def _sync_anchor_points_from_source(self) -> bool:
        if not self._source_solver_ready():
            self.source_canvas.remove_points_by_key(ANCHOR_KEYS, emit=False)
            return False

        points = self._rectangle_points()
        try:
            source = self._full_quad_from_source_points(points)
        except MeasurementError:
            self.source_canvas.remove_points_by_key(ANCHOR_KEYS, emit=False)
            return False

        anchor_points = {
            "anchor_top_left": (float(source[0][0]), float(source[0][1])),
            "anchor_top_right": (float(source[1][0]), float(source[1][1])),
            "anchor_bottom_right": (float(source[2][0]), float(source[2][1])),
            "anchor_bottom_left": (float(source[3][0]), float(source[3][1])),
        }
        self.source_canvas.set_points_by_key(anchor_points, emit=False)
        return True

    def _full_quad_from_quick_points(
        self,
        points: dict[str, tuple[float, float]],
    ) -> np.ndarray:
        top_left = line_intersection(
            points["top_start"],
            points["top_end"],
            points["left_side_top"],
            points["left_side_bottom"],
        )
        top_right = line_intersection(
            points["top_start"],
            points["top_end"],
            points["right_side_top"],
            points["right_side_bottom"],
        )
        return np.array(
            [
                top_left,
                top_right,
                points["right_side_bottom"],
                points["left_side_bottom"],
            ],
            dtype=np.float32,
        )

    def _full_quad_from_refined_points(
        self,
        points: dict[str, tuple[float, float]],
    ) -> np.ndarray:
        prefixes = ["left", "center", "right"]
        horizontal_lines: list[np.ndarray] = []
        vertical_lines: list[np.ndarray] = []
        top_points: list[tuple[float, float]] = []
        bottom_points: list[tuple[float, float]] = []

        for prefix in prefixes:
            a = points[f"{prefix}_a"]
            b = points[f"{prefix}_b"]
            c = points[f"{prefix}_c"]
            d = points[f"{prefix}_d"]
            horizontal_lines.append(homogeneous_line(a, b))
            horizontal_lines.append(homogeneous_line(d, c))
            vertical_lines.append(homogeneous_line(a, d))
            vertical_lines.append(homogeneous_line(b, c))
            top_points.extend([a, b])
            bottom_points.extend([d, c])

        horizontal_vanishing = fit_vanishing_point(horizontal_lines)
        vertical_vanishing = fit_vanishing_point(vertical_lines)

        top_line = line_through_anchor_and_vanishing(average_point(top_points), horizontal_vanishing)
        bottom_line = line_through_anchor_and_vanishing(average_point(bottom_points), horizontal_vanishing)
        left_line = line_through_anchor_and_vanishing(
            average_point([points["left_a"], points["left_d"]]),
            vertical_vanishing,
        )
        right_line = line_through_anchor_and_vanishing(
            average_point([points["right_b"], points["right_c"]]),
            vertical_vanishing,
        )

        return np.array(
            [
                intersection_from_lines(top_line, left_line),
                intersection_from_lines(top_line, right_line),
                intersection_from_lines(bottom_line, right_line),
                intersection_from_lines(bottom_line, left_line),
            ],
            dtype=np.float32,
        )

    def _full_quad_from_source_points(
        self,
        points: dict[str, tuple[float, float]],
    ) -> np.ndarray:
        if self._current_source_mode() == SOURCE_MODE_QUICK:
            return self._full_quad_from_quick_points(points)
        return self._full_quad_from_refined_points(points)

    def _full_quad_from_anchor_points(
        self,
        points: dict[str, tuple[float, float]],
    ) -> np.ndarray:
        return np.array(
            [
                points["anchor_top_left"],
                points["anchor_top_right"],
                points["anchor_bottom_right"],
                points["anchor_bottom_left"],
            ],
            dtype=np.float32,
        )

    def _update_unwrapped_frame(self) -> None:
        if self._last_frame is None or not self._source_solver_ready() or not self._anchor_points_ready():
            if self._unwrapped_frame is not None:
                self._unwrapped_frame = None
                self._homography_image_to_unwrapped = None
                self.preview_canvas.set_frame(None)
                self.plane_canvas.set_frame(None)
            return

        points = self._anchor_points_by_key()
        try:
            source = self._full_quad_from_anchor_points(points)
        except MeasurementError:
            self._unwrapped_frame = None
            self._homography_image_to_unwrapped = None
            self.preview_canvas.set_frame(None)
            self.plane_canvas.set_frame(None)
            return

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
            self.preview_canvas.set_frame(None)
            self.plane_canvas.set_frame(None)
            return

        self._homography_image_to_unwrapped = homography.astype(np.float64)
        self._unwrapped_frame = cv2.warpPerspective(
            self._last_frame,
            homography,
            (UNWRAPPED_WIDTH, UNWRAPPED_HEIGHT),
            flags=cv2.INTER_LINEAR,
        )
        self.preview_canvas.set_frame(self._unwrapped_frame, clear=False)
        self.plane_canvas.set_frame(self._unwrapped_frame, clear=False)

    def _calibration_values_changed(self, *_args) -> None:
        self._points_changed(self.plane_canvas.point_count())

    def _source_mode_changed(self, *_args) -> None:
        self.source_canvas.set_click_specs(self._source_click_specs(), clear=True)
        self.plane_canvas.clear_points()
        self._unwrapped_frame = None
        self._homography_image_to_unwrapped = None
        self.preview_canvas.set_frame(None)
        self.plane_canvas.set_frame(None)
        self._active_canvas = self.source_canvas
        if hasattr(self, "view_tabs"):
            self.view_tabs.setCurrentIndex(0)
        self._set_summary("Click the source geometry on the original frame.", detail_text=self._mode_detail_text())
        self._update_next_click_label()
        self._refresh_controls()

    def _view_tab_changed(self, index: int) -> None:
        self._active_canvas = self.source_canvas if index == 0 else self.plane_canvas
        self._update_next_click_label()
        self._refresh_controls()

    def canvas_undo(self) -> None:
        self._active_or_plane_canvas().undo_point()

    def canvas_delete_selected(self) -> None:
        self._active_or_plane_canvas().remove_selected_point()

    def canvas_clear(self) -> None:
        self.source_canvas.clear_points()
        self.plane_canvas.clear_points()
        self._unwrapped_frame = None
        self._homography_image_to_unwrapped = None
        self.preview_canvas.set_frame(None)
        self.plane_canvas.set_frame(None)
        self._active_canvas = self.source_canvas

    def _zoom_active_in(self) -> None:
        self._active_or_plane_canvas().zoom_in()

    def _zoom_active_out(self) -> None:
        self._active_or_plane_canvas().zoom_out()

    def _zoom_active_reset(self) -> None:
        self._active_or_plane_canvas().reset_zoom()

    def _pan_mode_changed(self, enabled: bool) -> None:
        for canvas in (self.source_canvas, self.preview_canvas, self.plane_canvas):
            canvas.set_pan_mode(enabled)
        self._refresh_controls()

    def _add_measurement_line(self) -> None:
        self._measurement_count += 1
        self.plane_canvas.set_click_specs(self._plane_click_specs(), clear=False)
        self.view_tabs.setCurrentIndex(1)
        self._active_canvas = self.plane_canvas
        self._set_summary(
            f"Measurement {self._measurement_count} added.",
            detail_text=self._mode_detail_text(),
        )
        self._update_next_click_label()
        self._refresh_controls()
        self._recalculate_measurement()

    def _remove_measurement_line(self) -> None:
        if self._measurement_count <= 1:
            return
        self._measurement_count -= 1
        self.plane_canvas.set_click_specs(self._plane_click_specs(), clear=False)
        self.plane_canvas.set_measurement_badges(None)
        self._active_canvas = self.plane_canvas
        self._update_next_click_label()
        self._refresh_controls()
        self._recalculate_measurement()

    def _points_changed(self, _count: int) -> None:
        sender = self.sender()
        if isinstance(sender, PlanarMeasurementCanvas):
            self._active_canvas = sender
        if sender is self.source_canvas:
            changed_key = self.source_canvas.last_changed_key()
            if changed_key in self._source_solver_keys() or changed_key in {"__clear__", "__specs__"}:
                self._sync_anchor_points_from_source()
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
            self.plane_canvas.set_measurement_badges(None)
            return

        source_keys = self._source_solver_keys()
        source_count = self._count_present_keys(source_keys)
        source_required = len(source_keys)
        if source_count < source_required:
            self.plane_canvas.set_measurement_badges(None)
            self._set_summary(
                f"{source_count}/{source_required} boundary-line points selected.",
                detail_text=self._mode_detail_text(),
            )
            return

        anchor_count = self._count_present_keys(ANCHOR_KEYS)
        if anchor_count < len(ANCHOR_KEYS):
            self.plane_canvas.set_measurement_badges(None)
            self._set_summary(
                f"{anchor_count}/{len(ANCHOR_KEYS)} homography corner anchors selected.",
                detail_text=self._mode_detail_text(),
            )
            return

        if self._unwrapped_frame is None or self._homography_image_to_unwrapped is None:
            self.plane_canvas.set_measurement_badges(None)
            self._set_summary("Could not unwrap from those anchors.", detail_text=self._mode_detail_text(), tone="warn")
            return

        points = self.plane_canvas.points_by_key()
        reference_keys = [
            key
            for index in range(REFERENCE_COUNT)
            for key in (f"ref_{index + 1}_start", f"ref_{index + 1}_end")
        ]
        reference_count = sum(1 for key in reference_keys if key in points)
        reference_required = len(reference_keys)
        if reference_count < reference_required:
            self.plane_canvas.set_measurement_badges(None)
            self._set_summary(
                f"{reference_count}/{reference_required} reference points selected on the unwrapped plane.",
                detail_text=self._mode_detail_text(),
            )
            return

        try:
            reference_segments = [
                [points[f"ref_{index + 1}_start"], points[f"ref_{index + 1}_end"]]
                for index in range(REFERENCE_COUNT)
            ]
            reference_lengths = [spin.value() for spin in self.reference_length_spins]
            calibration_result = measure_planar_segment_from_plane(
                reference_plane_segments=reference_segments,
                reference_lengths_cm=reference_lengths,
                segment_start_plane=(0.0, 0.0),
                segment_end_plane=(1.0, 0.0),
                plane_size_units=(float(UNWRAPPED_WIDTH - 1), float(UNWRAPPED_HEIGHT - 1)),
                homography_image_to_plane=self._homography_image_to_unwrapped,
            )
        except MeasurementError as exc:
            self.plane_canvas.set_measurement_badges(None)
            self._set_summary(str(exc), detail_text=self._mode_detail_text(), tone="warn")
            self.statusBar().showMessage(str(exc), 5000)
            return

        scale_detail = (
            "manual full-structure homography | "
            f"rectangle scale=({calibration_result.plane_width_cm:.2f}, {calibration_result.plane_height_cm:.2f}) cm | "
            f"reference fit RMSE={calibration_result.reference_rmse_cm:.2f} cm"
        )
        completed_measurements = [
            index
            for index in range(self._measurement_count)
            if measurement_start_key(index) in points and measurement_end_key(index) in points
        ]
        if not completed_measurements:
            self.plane_canvas.set_measurement_badges(None)
            self._set_summary(
                "Reference scale ready. Add or draw measurement lines.",
                detail_text=scale_detail,
            )
            return

        try:
            results = [
                measure_planar_segment_from_plane(
                    reference_plane_segments=reference_segments,
                    reference_lengths_cm=reference_lengths,
                    segment_start_plane=points[measurement_start_key(index)],
                    segment_end_plane=points[measurement_end_key(index)],
                    plane_size_units=(float(UNWRAPPED_WIDTH - 1), float(UNWRAPPED_HEIGHT - 1)),
                    homography_image_to_plane=self._homography_image_to_unwrapped,
                )
                for index in completed_measurements
            ]
        except MeasurementError as exc:
            self.plane_canvas.set_measurement_badges(None)
            self._set_summary(str(exc), detail_text=self._mode_detail_text(), tone="warn")
            self.statusBar().showMessage(str(exc), 5000)
            return

        badges = {
            measurement_segment_key(index): f"M{index + 1} {result.length_cm:.1f} cm"
            for index, result in zip(completed_measurements, results)
        }
        self.plane_canvas.set_measurement_badges(badges)
        measurement_text = " | ".join(
            f"M{index + 1}: {result.length_cm:.2f} cm"
            for index, result in zip(completed_measurements, results)
        )
        if len(results) > 1:
            measurement_text = f"{measurement_text} | Total: {sum(result.length_cm for result in results):.2f} cm"
        self._set_summary(
            measurement_text,
            detail_text=scale_detail,
        )
        self.statusBar().showMessage("Multi-rectangle measurements updated.", 5000)

    def _mode_detail_text(self) -> str:
        return (
            "Click two points on the top line plus two points on each outside boundary line. "
            "The app seeds four orange homography corners from those six points; drag those corners while checking the live preview. "
            "Draw the seven known reference segments and any number of measurement lines on the Unwrapped tab, then set the reference lengths above. "
            "Wheel zooms; Pan mode or middle-drag moves the view; Shift-drag also pans while zoomed; right-click removes a point."
        )

    def _update_next_click_label(self) -> None:
        points = self.source_canvas.points_by_key()
        source_next = next((key for key in self._source_solver_keys() if key not in points), None)
        if source_next is not None:
            label = self.source_canvas.label_for_key(source_next) or source_next
            text = f"Original next: {label}"
        else:
            anchor_next = next((key for key in ANCHOR_KEYS if key not in points), None)
            if anchor_next is not None:
                label = self.source_canvas.label_for_key(anchor_next) or anchor_next
                text = f"Original next: {label}"
            elif self._unwrapped_frame is None:
                text = "Drag the orange corner anchors until the preview unwraps correctly."
            else:
                plane_next = self.plane_canvas.next_click_label()
                text = "All points selected. Use Add Measure for another line." if plane_next is None else f"Unwrapped next: {plane_next}"

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
        self.preview_canvas.set_frame(None)
        self.plane_canvas.set_frame(None)
        self._active_canvas = self.source_canvas
        self._set_summary("Open a video or image to start multi-rectangle length measurement.", detail_text=self._mode_detail_text())
        self._update_next_click_label()
        self._refresh_controls()

    def _show_error_state(self, summary_text: str, *, source_text: str | None = None) -> None:
        self._last_frame = None
        self._last_source_text = source_text or ""
        self._unwrapped_frame = None
        self._homography_image_to_unwrapped = None
        self.source_label.setText(source_text or "")
        self.source_canvas.set_frame(None)
        self.preview_canvas.set_frame(None)
        self.plane_canvas.set_frame(None)
        self._active_canvas = self.source_canvas
        self._set_summary(summary_text, tone="warn")
        self.statusBar().showMessage(summary_text, 5000)
        self._update_next_click_label()
        self._refresh_controls()
        if source_text:
            QMessageBox.warning(self, "Multi-Rectangle Length Measurement", f"{summary_text}\n\n{source_text}")

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
        self.zoom_label.setText(f"{int(round(active.zoom_factor() * 100.0))}%")
        has_active_frame = active.has_frame()
        self.zoom_out_btn.setEnabled(has_active_frame and active.zoom_factor() > 1.0)
        self.zoom_reset_btn.setEnabled(has_active_frame and active.zoom_factor() > 1.0)
        self.zoom_in_btn.setEnabled(has_active_frame and active.zoom_factor() < 10.0)
        self.pan_btn.setEnabled(has_active_frame)
        self.add_measure_btn.setEnabled(True)
        self.remove_measure_btn.setEnabled(self._measurement_count > 1)

    def _update_window_title(self, source_text: str, video_frame_index: int | None) -> None:
        title_path = Path(source_text.split(" | ", 1)[0])
        suffix = title_path.name or "frame"
        if video_frame_index is not None:
            suffix = f"{suffix} frame {video_frame_index + 1}"
        self.setWindowTitle(f"Multi-Rectangle Prop Length Measurement - {suffix}")

    def closeEvent(self, event) -> None:
        self._close_video()
        super().closeEvent(event)
