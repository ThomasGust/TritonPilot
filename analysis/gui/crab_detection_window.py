from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
from PyQt6.QtCore import QRectF, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QPainter, QPen
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
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

from analysis.crab_detector_cv import (
    DEFAULT_UNWRAP_SIZE,
    competition_summary_text,
    detect_crabs_in_video,
    draw_competition_green_crab_detections,
    draw_crab_detections,
    natural_case_sort_key,
    order_corners,
    render_detection_views,
)
from analysis.gui.crab_result_dialog import CrabDetectionResultView, frame_to_pixmap
from gui.responsive import horizontal_scroll_area, resize_to_available_screen

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp"}
VIDEO_SUFFIXES = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".wmv"}


def is_supported_image_path(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES


def is_supported_video_path(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in VIDEO_SUFFIXES


def normalize_unwrap_size(value: int | tuple[int, int] | list[int]) -> tuple[int, int]:
    if isinstance(value, int):
        size = int(value)
        return (size, size)
    if len(value) != 2:
        raise ValueError("unwrap_size must be an int or a (width, height) pair")
    return (int(value[0]), int(value[1]))


def collect_image_paths(inputs: list[str | Path]) -> list[Path]:
    ordered_paths: list[Path] = []
    seen: set[Path] = set()

    for raw_value in inputs:
        path = Path(raw_value).expanduser()
        if not path.exists():
            continue

        if path.is_dir():
            folder_paths = [
                child
                for child in path.rglob("*")
                if is_supported_image_path(child)
            ]
            for child in sorted(folder_paths, key=natural_case_sort_key):
                resolved = child.resolve()
                if resolved not in seen:
                    seen.add(resolved)
                    ordered_paths.append(resolved)
            continue

        if is_supported_image_path(path):
            resolved = path.resolve()
            if resolved not in seen:
                seen.add(resolved)
                ordered_paths.append(resolved)

    return ordered_paths


class CornerPickerCanvas(QWidget):
    points_changed = pyqtSignal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pixmap = frame_to_pixmap(None)
        self._image_width = 0
        self._image_height = 0
        self._points: list[tuple[float, float]] = []
        self.setMinimumSize(520, 320)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMouseTracking(True)

    def set_image(self, frame_bgr: np.ndarray) -> None:
        self._pixmap = frame_to_pixmap(frame_bgr)
        self._image_height, self._image_width = frame_bgr.shape[:2]
        self.clear_points()

    def clear_points(self) -> None:
        self._points = []
        self.points_changed.emit(0)
        self.update()

    def undo_point(self) -> None:
        if not self._points:
            return
        self._points.pop()
        self.points_changed.emit(len(self._points))
        self.update()

    def selected_polygon(self) -> np.ndarray | None:
        if len(self._points) != 4:
            return None
        return order_corners(np.asarray(self._points, dtype=np.float32))

    def _target_rect(self) -> QRectF:
        contents = self.contentsRect()
        if self._pixmap.isNull() or self._image_width <= 0 or self._image_height <= 0:
            return QRectF(contents)

        scale = min(
            contents.width() / float(self._image_width),
            contents.height() / float(self._image_height),
        )
        draw_width = self._image_width * scale
        draw_height = self._image_height * scale
        return QRectF(
            contents.x() + (contents.width() - draw_width) / 2.0,
            contents.y() + (contents.height() - draw_height) / 2.0,
            draw_width,
            draw_height,
        )

    def _image_to_widget(self, point: tuple[float, float]):
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

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.RightButton:
            self.undo_point()
            return
        if event.button() != Qt.MouseButton.LeftButton or len(self._points) >= 4:
            return

        point = self._widget_to_image(event.position().x(), event.position().y())
        if point is None:
            return

        self._points.append(point)
        self.points_changed.emit(len(self._points))
        self.update()

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#111111"))

        target = self._target_rect()
        if not self._pixmap.isNull():
            painter.drawPixmap(target.toRect(), self._pixmap)

        if not self._points:
            return

        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        points = [self._image_to_widget(point) for point in self._points]

        line_pen = QPen(QColor(0, 230, 255), 3)
        painter.setPen(line_pen)
        for index in range(1, len(points)):
            painter.drawLine(
                int(points[index - 1][0]),
                int(points[index - 1][1]),
                int(points[index][0]),
                int(points[index][1]),
            )
        if len(points) == 4:
            painter.drawLine(
                int(points[-1][0]),
                int(points[-1][1]),
                int(points[0][0]),
                int(points[0][1]),
            )

        point_pen = QPen(QColor(5, 30, 35), 2)
        painter.setPen(point_pen)
        painter.setBrush(QColor(255, 245, 120))
        for index, (x, y) in enumerate(points, start=1):
            painter.drawEllipse(int(x - 6), int(y - 6), 12, 12)
            painter.setPen(QPen(QColor(255, 245, 120), 2))
            painter.drawText(int(x + 10), int(y - 10), str(index))
            painter.setPen(point_pen)


class ManualBoardPickerDialog(QDialog):
    def __init__(self, image: np.ndarray, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Manual Plane Corners")
        resize_to_available_screen(self, 1100, 760, min_width=700, min_height=500)
        self._selected_polygon: np.ndarray | None = None

        self.canvas = CornerPickerCanvas(self)
        self.canvas.set_image(image)
        self.canvas.points_changed.connect(self._update_controls)

        self.status_label = QLabel("0/4 corners selected")
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.undo_btn = QPushButton("Undo")
        self.undo_btn.clicked.connect(self.canvas.undo_point)

        self.clear_btn = QPushButton("Clear")
        self.clear_btn.clicked.connect(self.canvas.clear_points)

        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.clicked.connect(self.reject)

        self.use_btn = QPushButton("Use Corners")
        self.use_btn.clicked.connect(self._accept_polygon)
        self.use_btn.setDefault(True)

        controls = QHBoxLayout()
        controls.addWidget(self.status_label)
        controls.addStretch(1)
        controls.addWidget(self.undo_btn)
        controls.addWidget(self.clear_btn)
        controls.addWidget(self.cancel_btn)
        controls.addWidget(self.use_btn)

        layout = QVBoxLayout(self)
        layout.addWidget(self.canvas, 1)
        layout.addLayout(controls)
        resize_to_available_screen(self, 1100, 760, min_width=700, min_height=500)
        self._update_controls(0)

    def selected_polygon(self) -> np.ndarray | None:
        return self._selected_polygon

    def _update_controls(self, count: int) -> None:
        self.status_label.setText(f"{count}/4 corners selected")
        self.undo_btn.setEnabled(count > 0)
        self.clear_btn.setEnabled(count > 0)
        self.use_btn.setEnabled(count == 4)

    def _accept_polygon(self) -> None:
        polygon = self.canvas.selected_polygon()
        if polygon is None:
            return
        self._selected_polygon = polygon
        self.accept()


class CrabDetectionWindow(QMainWindow):
    def __init__(
        self,
        image_paths: list[str | Path] | None = None,
        *,
        force_square: bool = True,
        unwrap_size: int = DEFAULT_UNWRAP_SIZE,
        parent=None,
    ):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.setWindowTitle("Crab Competition Analyzer")
        resize_to_available_screen(self, 1600, 950, min_width=900, min_height=620)

        self._force_square = bool(force_square)
        self._unwrap_size = normalize_unwrap_size(unwrap_size)
        self._image_paths: list[Path] = []
        self._current_index = -1
        self._video_path: Path | None = None
        self._video_capture: cv2.VideoCapture | None = None
        self._video_frame_count = 0
        self._video_fps = 0.0
        self._video_duration_seconds = 0.0
        self._video_current_frame_index = 0
        self._video_current_frame: np.ndarray | None = None
        self._updating_video_controls = False
        self._last_dir = str(Path.cwd())
        self._last_image: np.ndarray | None = None
        self._last_source_text = ""
        self._manual_board_polygon: np.ndarray | None = None
        self.current_summary_text = ""

        self._build_ui()
        self._show_empty_state()
        resize_to_available_screen(self, 1600, 950, min_width=900, min_height=620)

        if image_paths:
            self.set_media_paths(image_paths)

    def _build_ui(self) -> None:
        self.open_images_btn = QPushButton("Open Photo(s)")
        self.open_images_btn.clicked.connect(self._open_images)

        self.open_folder_btn = QPushButton("Open Folder")
        self.open_folder_btn.clicked.connect(self._open_folder)

        self.open_video_btn = QPushButton("Open Video")
        self.open_video_btn.clicked.connect(self._open_video)

        self.previous_btn = QPushButton("Previous")
        self.previous_btn.clicked.connect(self._show_previous_image)

        self.next_btn = QPushButton("Next")
        self.next_btn.clicked.connect(self._show_next_image)

        self.reload_btn = QPushButton("Reload")
        self.reload_btn.clicked.connect(self._reload_current_source)

        self.manual_plane_btn = QPushButton("Manual Board")
        self.manual_plane_btn.clicked.connect(self._pick_manual_board_polygon)

        self.clear_manual_plane_btn = QPushButton("Auto Board")
        self.clear_manual_plane_btn.clicked.connect(self._clear_manual_board_polygon)

        self.force_square_checkbox = QCheckBox("Force square board")
        self.force_square_checkbox.setChecked(self._force_square)
        self.force_square_checkbox.toggled.connect(self._toggle_force_square)

        controls = QHBoxLayout()
        controls.addWidget(self.open_images_btn)
        controls.addWidget(self.open_folder_btn)
        controls.addWidget(self.open_video_btn)
        controls.addSpacing(12)
        controls.addWidget(self.previous_btn)
        controls.addWidget(self.next_btn)
        controls.addWidget(self.reload_btn)
        controls.addSpacing(12)
        controls.addWidget(self.manual_plane_btn)
        controls.addWidget(self.clear_manual_plane_btn)
        controls.addStretch(1)
        controls.addWidget(self.force_square_checkbox)

        self.path_label = QLabel("")
        self.path_label.setObjectName("summaryHint")
        self.path_label.setWordWrap(True)
        self.path_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

        self.video_controls_container = QWidget(self)
        self.video_controls_container.setObjectName("crabVideoControls")
        self.video_previous_frame_btn = QPushButton("Prev Frame")
        self.video_previous_frame_btn.clicked.connect(self._show_previous_video_frame)
        self.video_next_frame_btn = QPushButton("Next Frame")
        self.video_next_frame_btn.clicked.connect(self._show_next_video_frame)
        self.video_run_frame_btn = QPushButton("Run Selected Frame")
        self.video_run_frame_btn.clicked.connect(self._run_selected_video_frame)
        self.video_scan_range_btn = QPushButton("Scan Range")
        self.video_scan_range_btn.clicked.connect(self._run_video_range_scan)

        self.video_frame_slider = QSlider(Qt.Orientation.Horizontal)
        self.video_frame_slider.setMinimum(0)
        self.video_frame_slider.setMaximum(0)
        self.video_frame_slider.setTracking(False)
        self.video_frame_slider.valueChanged.connect(self._video_slider_changed)

        self.video_position_label = QLabel("No video loaded")
        self.video_position_label.setObjectName("summaryHint")
        self.video_position_label.setMinimumWidth(220)

        self.video_start_spin = self._make_seconds_spinbox()
        self.video_start_spin.valueChanged.connect(self._validate_video_range)
        self.video_end_spin = self._make_seconds_spinbox()
        self.video_end_spin.valueChanged.connect(self._validate_video_range)
        self.video_interval_spin = self._make_seconds_spinbox(
            minimum=0.05,
            maximum=10.0,
            value=0.5,
            step=0.05,
        )
        self.video_use_start_btn = QPushButton("Set Start")
        self.video_use_start_btn.clicked.connect(self._set_video_range_start_to_current)
        self.video_use_end_btn = QPushButton("Set End")
        self.video_use_end_btn.clicked.connect(self._set_video_range_end_to_current)

        video_controls = QVBoxLayout(self.video_controls_container)
        video_controls.setContentsMargins(0, 0, 0, 0)
        video_top_row = QHBoxLayout()
        video_top_row.addWidget(self.video_previous_frame_btn)
        video_top_row.addWidget(self.video_next_frame_btn)
        video_top_row.addWidget(self.video_frame_slider, 1)
        video_top_row.addWidget(self.video_position_label)
        video_top_row.addWidget(self.video_run_frame_btn)
        video_controls.addWidget(horizontal_scroll_area(video_top_row))

        video_range_row = QHBoxLayout()
        video_range_row.addWidget(QLabel("Scan start"))
        video_range_row.addWidget(self.video_start_spin)
        video_range_row.addWidget(self.video_use_start_btn)
        video_range_row.addSpacing(8)
        video_range_row.addWidget(QLabel("Scan end"))
        video_range_row.addWidget(self.video_end_spin)
        video_range_row.addWidget(self.video_use_end_btn)
        video_range_row.addSpacing(8)
        video_range_row.addWidget(QLabel("Sample every"))
        video_range_row.addWidget(self.video_interval_spin)
        video_range_row.addWidget(self.video_scan_range_btn)
        video_range_row.addStretch(1)
        video_controls.addWidget(horizontal_scroll_area(video_range_row))

        self.result_view = CrabDetectionResultView(self)

        container = QWidget(self)
        layout = QVBoxLayout(container)
        layout.addWidget(horizontal_scroll_area(controls))
        layout.addWidget(self.path_label)
        layout.addWidget(self.video_controls_container)
        layout.addWidget(self.result_view, 1)
        self.setCentralWidget(container)

        self.video_controls_container.hide()
        self.statusBar().showMessage("Open an image, folder, or video to start crab detection.")
        self._refresh_navigation_buttons()

    @staticmethod
    def _make_seconds_spinbox(
        *,
        minimum: float = 0.0,
        maximum: float = 0.0,
        value: float = 0.0,
        step: float = 0.1,
    ) -> QDoubleSpinBox:
        spinbox = QDoubleSpinBox()
        spinbox.setDecimals(2)
        spinbox.setRange(float(minimum), float(maximum))
        spinbox.setValue(float(value))
        spinbox.setSingleStep(float(step))
        spinbox.setSuffix(" s")
        spinbox.setMinimumWidth(96)
        return spinbox

    def set_media_paths(self, paths: list[str | Path]) -> None:
        existing_paths = [Path(path).expanduser() for path in paths if Path(path).expanduser().exists()]
        if len(existing_paths) == 1 and is_supported_video_path(existing_paths[0]):
            self.set_video_path(existing_paths[0])
            return
        self.set_image_paths(paths)

    def set_image_paths(self, image_paths: list[str | Path], start_index: int = 0) -> None:
        self._close_video()
        resolved_paths = collect_image_paths(image_paths)
        if not resolved_paths:
            self._image_paths = []
            self._current_index = -1
            self.video_controls_container.hide()
            self._show_error_state(
                "No supported images were found.",
                detail_text="Choose image files directly or point the debugger at a folder with images.",
            )
            return

        self._image_paths = resolved_paths
        self._current_index = max(0, min(int(start_index), len(self._image_paths) - 1))
        self._last_dir = str(self._image_paths[self._current_index].parent)
        self.video_controls_container.hide()
        self._refresh_navigation_buttons()
        self._load_current_path()

    def load_frame(self, frame_bgr: np.ndarray, *, source_label: str = "Live frame") -> str:
        self._close_video()
        self._image_paths = []
        self._current_index = -1
        self._manual_board_polygon = None
        self.video_controls_container.hide()
        self._refresh_navigation_buttons()
        self._run_detection(frame_bgr.copy(), source_text=source_label)
        return self.current_summary_text

    def _close_video(self) -> None:
        if self._video_capture is not None:
            self._video_capture.release()
        self._video_capture = None
        self._video_path = None
        self._video_frame_count = 0
        self._video_fps = 0.0
        self._video_duration_seconds = 0.0
        self._video_current_frame_index = 0
        self._video_current_frame = None

    def set_video_path(self, video_path: str | Path) -> None:
        path = Path(video_path).expanduser()
        if not is_supported_video_path(path):
            self._show_error_state(
                "That file type is not a supported video.",
                source_text=str(path),
                detail_text="Choose an MP4, MOV, AVI, MKV, M4V, or WMV file.",
            )
            return

        self._close_video()
        capture = cv2.VideoCapture(str(path))
        if not capture.isOpened():
            self._show_error_state(
                "Could not open the selected video.",
                source_text=str(path),
                detail_text="OpenCV could not read frames from this file.",
            )
            return

        self._image_paths = []
        self._current_index = -1
        self._manual_board_polygon = None
        self._video_path = path.resolve()
        self._video_capture = capture
        self._video_frame_count = max(0, int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0))
        self._video_fps = float(capture.get(cv2.CAP_PROP_FPS) or 0.0)
        if self._video_fps <= 0.0:
            self._video_fps = 30.0
        self._video_duration_seconds = (
            self._video_frame_count / self._video_fps
            if self._video_frame_count > 0
            else 0.0
        )
        self._last_dir = str(self._video_path.parent)
        self.video_controls_container.show()
        self._configure_video_controls()
        self._show_video_frame(0)
        self.statusBar().showMessage("Video loaded. Choose a frame or scan a range.", 5000)

    def _configure_video_controls(self) -> None:
        self._updating_video_controls = True
        try:
            max_frame = max(0, self._video_frame_count - 1)
            self.video_frame_slider.setRange(0, max_frame)
            self.video_frame_slider.setValue(0)
            duration = max(0.0, self._video_duration_seconds)
            for spinbox in (self.video_start_spin, self.video_end_spin):
                spinbox.setRange(0.0, duration)
            self.video_start_spin.setValue(0.0)
            self.video_end_spin.setValue(duration)
            if self.video_interval_spin.value() <= 0.0:
                self.video_interval_spin.setValue(0.5)
        finally:
            self._updating_video_controls = False
        self._validate_video_range()

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

    def _show_video_frame(self, frame_index: int) -> None:
        frame = self._read_video_frame(frame_index)
        if frame is None:
            self.statusBar().showMessage("Could not read that video frame.", 4000)
            return

        if self._video_frame_count > 0:
            frame_index = max(0, min(int(frame_index), self._video_frame_count - 1))
        self._video_current_frame_index = int(frame_index)
        self._video_current_frame = frame.copy()
        self._last_image = frame.copy()
        self._last_source_text = self._format_video_source_text(frame_index)
        self.path_label.setText(str(self._video_path or ""))
        self._updating_video_controls = True
        try:
            self.video_frame_slider.setValue(self._video_current_frame_index)
        finally:
            self._updating_video_controls = False

        time_seconds = self._video_time_for_frame(self._video_current_frame_index)
        self.video_position_label.setText(
            f"Frame {self._video_current_frame_index + 1}/{max(1, self._video_frame_count)}  "
            f"{time_seconds:.2f}s"
        )
        self.current_summary_text = "Selected video frame is ready to analyze."
        self.result_view.set_panel_titles(
            "Selected Frame",
            "Competition Display",
        )
        self.result_view.set_result(
            self.current_summary_text,
            frame,
            None,
            mask_image=None,
            source_text=self._last_source_text,
            detail_text="Run the selected frame or scan the chosen time range.",
        )
        self._refresh_navigation_buttons()
        self._update_window_title(self._last_source_text)

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

    def _run_selected_video_frame(self) -> None:
        if self._video_current_frame is None:
            self.statusBar().showMessage("Open a video and select a frame first.", 3000)
            return
        self._manual_board_polygon = None

        if self._video_path is not None and self._video_fps > 0:
            center_seconds = self._video_time_for_frame(self._video_current_frame_index)
            search_radius = max(0.35, min(0.75, self.video_interval_spin.value()))
            start_seconds = max(0.0, center_seconds - search_radius)
            end_seconds = min(
                self._video_duration_seconds or center_seconds + search_radius,
                center_seconds + search_radius,
            )
            sample_interval = max(0.10, min(0.25, self.video_interval_spin.value()))
            self.statusBar().showMessage("Checking nearby frames for the cleanest detection...")
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
            try:
                result = detect_crabs_in_video(
                    self._video_path,
                    start_seconds=start_seconds,
                    end_seconds=end_seconds,
                    sample_interval_seconds=sample_interval,
                    force_square=self._force_square,
                    unwrap_size=self._unwrap_size,
                )
            finally:
                QApplication.restoreOverrideCursor()

            if result is not None:
                frame_index = int(result["frame_index"])
                frame = result["frame"]
                self._video_current_frame_index = frame_index
                self._video_current_frame = frame.copy()
                self._last_image = frame.copy()
                source_text = (
                    f"{self._video_path} | best nearby frame "
                    f"{frame_index + 1}/{max(1, self._video_frame_count)} "
                    f"@ {result['time_seconds']:.2f}s"
                )
                self._last_source_text = source_text
                self._updating_video_controls = True
                try:
                    self.video_frame_slider.setValue(max(0, min(frame_index, self.video_frame_slider.maximum())))
                finally:
                    self._updating_video_controls = False
                self.video_position_label.setText(
                    f"Best nearby frame {frame_index + 1}/{max(1, self._video_frame_count)}  "
                    f"{result['time_seconds']:.2f}s"
                )
                detail_prefix = (
                    f"nearby search={start_seconds:.2f}s-{end_seconds:.2f}s | "
                    f"interval={sample_interval:.2f}s"
                )
                self._show_detection_result(
                    frame,
                    result["detection_result"],
                    source_text=source_text,
                    detail_prefix=detail_prefix,
                )
                return

        self._run_detection(
            self._video_current_frame.copy(),
            source_text=self._format_video_source_text(self._video_current_frame_index),
        )

    def _set_video_range_start_to_current(self) -> None:
        self.video_start_spin.setValue(self._video_time_for_frame(self._video_current_frame_index))

    def _set_video_range_end_to_current(self) -> None:
        self.video_end_spin.setValue(self._video_time_for_frame(self._video_current_frame_index))

    def _validate_video_range(self) -> None:
        if not hasattr(self, "video_scan_range_btn"):
            return
        has_video = self._video_capture is not None and self._video_path is not None
        valid_range = self.video_start_spin.value() <= self.video_end_spin.value()
        self.video_scan_range_btn.setEnabled(has_video and valid_range)

    def _run_video_range_scan(self) -> None:
        if self._video_path is None:
            self.statusBar().showMessage("Open a video before scanning a range.", 3000)
            return
        start_seconds = self.video_start_spin.value()
        end_seconds = self.video_end_spin.value()
        if start_seconds > end_seconds:
            self.statusBar().showMessage("Scan start must be before scan end.", 4000)
            return

        self.statusBar().showMessage("Scanning selected video range...")
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            result = detect_crabs_in_video(
                self._video_path,
                start_seconds=start_seconds,
                end_seconds=end_seconds,
                sample_interval_seconds=self.video_interval_spin.value(),
                force_square=self._force_square,
                unwrap_size=self._unwrap_size,
            )
        finally:
            QApplication.restoreOverrideCursor()

        if result is None:
            self.current_summary_text = "No reliable crab result was found in that video range."
            self.result_view.set_result(
                self.current_summary_text,
                self._video_current_frame,
                None,
                source_text=str(self._video_path),
                detail_text=(
                    f"scan={start_seconds:.2f}s-{end_seconds:.2f}s | "
                    f"interval={self.video_interval_spin.value():.2f}s | "
                    "try a clearer range with the whole board in view"
                ),
                tone="warn",
            )
            self.statusBar().showMessage(self.current_summary_text, 5000)
            return

        frame_index = int(result["frame_index"])
        frame = result["frame"]
        detection_result = result["detection_result"]
        self._video_current_frame_index = frame_index
        self._video_current_frame = frame.copy()
        self._last_image = frame.copy()
        source_text = (
            f"{self._video_path} | best frame {frame_index + 1}/{max(1, self._video_frame_count)} "
            f"@ {result['time_seconds']:.2f}s"
        )
        self._last_source_text = source_text
        self._updating_video_controls = True
        try:
            self.video_frame_slider.setValue(max(0, min(frame_index, self.video_frame_slider.maximum())))
        finally:
            self._updating_video_controls = False
        self.video_position_label.setText(
            f"Best frame {frame_index + 1}/{max(1, self._video_frame_count)}  "
            f"{result['time_seconds']:.2f}s"
        )
        scan_detail = (
            f"scan={start_seconds:.2f}s-{end_seconds:.2f}s | "
            f"interval={self.video_interval_spin.value():.2f}s"
        )
        temporal_vote = result.get("temporal_vote")
        if temporal_vote:
            signature = temporal_vote.get("signature", (0, 0, 0, 0))
            scan_detail = (
                f"{scan_detail} | temporal vote="
                f"{signature[0]} green/{signature[1]} jonah/{signature[2]} rock "
                f"from {temporal_vote.get('support_count', 0)}/"
                f"{temporal_vote.get('eligible_count', 0)} plausible samples"
            )
        self._show_detection_result(
            frame,
            detection_result,
            source_text=source_text,
            detail_prefix=scan_detail,
        )

    def _open_images(self) -> None:
        selected_paths, _ = QFileDialog.getOpenFileNames(
            self,
            "Open crab photo(s)",
            self._last_dir,
            "Images (*.jpg *.jpeg *.png *.bmp *.tif *.tiff *.webp)",
        )
        if not selected_paths:
            return
        self.set_image_paths([Path(path) for path in selected_paths])

    def _open_folder(self) -> None:
        selected_dir = QFileDialog.getExistingDirectory(self, "Open image folder", self._last_dir)
        if not selected_dir:
            return

        folder_images = collect_image_paths([selected_dir])
        if not folder_images:
            QMessageBox.information(
                self,
                "Crab Detection",
                "No supported images were found in that folder.",
            )
            return

        self.set_image_paths(folder_images)

    def _open_video(self) -> None:
        selected_path, _ = QFileDialog.getOpenFileName(
            self,
            "Open crab video",
            self._last_dir,
            "Videos (*.mp4 *.mov *.m4v *.avi *.mkv *.wmv)",
        )
        if not selected_path:
            return
        self.set_video_path(Path(selected_path))

    def _show_previous_image(self) -> None:
        if self._current_index <= 0:
            return
        self._current_index -= 1
        self._refresh_navigation_buttons()
        self._load_current_path()

    def _show_next_image(self) -> None:
        if self._current_index < 0 or self._current_index >= len(self._image_paths) - 1:
            return
        self._current_index += 1
        self._refresh_navigation_buttons()
        self._load_current_path()

    def _reload_current_source(self) -> None:
        if self._current_index >= 0 and self._image_paths:
            self._load_current_path(preserve_manual=True)
            return
        if self._last_image is not None:
            self._run_detection(self._last_image.copy(), source_text=self._last_source_text or "Live frame")
            return
        self.statusBar().showMessage("Nothing to reload yet.", 3000)

    def _toggle_force_square(self, checked: bool) -> None:
        self._force_square = bool(checked)
        if self._current_index >= 0 and self._image_paths:
            self._load_current_path(preserve_manual=True)
            return
        if self._last_image is not None:
            self._run_detection(self._last_image.copy(), source_text=self._last_source_text or "Live frame")
            return
        self.statusBar().showMessage(
            f"Force square board {'enabled' if self._force_square else 'disabled'}.",
            3000,
        )

    def _load_current_path(self, *, preserve_manual: bool = False) -> None:
        if self._current_index < 0 or self._current_index >= len(self._image_paths):
            self._show_empty_state()
            return

        if not preserve_manual:
            self._manual_board_polygon = None

        image_path = self._image_paths[self._current_index]
        self._last_dir = str(image_path.parent)
        image = cv2.imread(str(image_path))
        if image is None:
            self._show_error_state(
                "Could not read the selected image.",
                source_text=str(image_path),
                detail_text="OpenCV returned no image data for this path.",
            )
            return

        source_text = f"{image_path} ({self._current_index + 1}/{len(self._image_paths)})"
        self._run_detection(image, source_text=source_text)

    def _run_detection(self, image: np.ndarray, *, source_text: str) -> None:
        self._last_image = image.copy()
        self._last_source_text = source_text
        detection_result, annotated_original, annotated_unwrapped = render_detection_views(
            image,
            force_square=self._force_square,
            unwrap_size=self._unwrap_size,
            board_polygon=self._manual_board_polygon,
        )

        self.path_label.setText(source_text)

        if detection_result is None or annotated_original is None or annotated_unwrapped is None:
            self.current_summary_text = "Could not find the board or identify any crabs."
            self.result_view.set_result(
                self.current_summary_text,
                image,
                None,
                source_text=source_text,
                detail_text=(
                    f"board={'manual' if self._manual_board_polygon is not None else 'auto'} | "
                    f"force_square={self._force_square} | "
                    f"unwrap_size={self._unwrap_size[0]}x{self._unwrap_size[1]}"
                ),
                tone="warn",
            )
            self._refresh_navigation_buttons()
            self.statusBar().showMessage(self.current_summary_text, 5000)
            self._update_window_title(source_text)
            return

        competition_view = draw_competition_green_crab_detections(detection_result)
        self.current_summary_text = competition_summary_text(detection_result)
        self.result_view.set_panel_titles(
            "Original With Boxes",
            "Competition Display",
        )
        self.result_view.set_result(
            self.current_summary_text,
            annotated_original,
            competition_view,
            source_text=source_text,
            detail_text=self._build_detail_text(detection_result),
        )
        self._refresh_navigation_buttons()
        self.statusBar().showMessage(self.current_summary_text, 8000)
        self._update_window_title(source_text)

    def _show_detection_result(
        self,
        image: np.ndarray,
        detection_result: dict,
        *,
        source_text: str,
        detail_prefix: str | None = None,
    ) -> None:
        annotated_original = draw_crab_detections(image, detection_result)
        competition_view = draw_competition_green_crab_detections(detection_result)
        self.path_label.setText(source_text)
        self.current_summary_text = competition_summary_text(detection_result)
        detail_text = self._build_detail_text(detection_result)
        if detail_prefix:
            detail_text = f"{detail_prefix} | {detail_text}"
        self.result_view.set_panel_titles(
            "Original With Boxes",
            "Competition Display",
        )
        self.result_view.set_result(
            self.current_summary_text,
            annotated_original,
            competition_view,
            source_text=source_text,
            detail_text=detail_text,
        )
        self._refresh_navigation_buttons()
        self.statusBar().showMessage(self.current_summary_text, 8000)
        self._update_window_title(source_text)

    def _build_detail_text(self, detection_result: dict) -> str:
        detection_labels = ", ".join(
            f"#{detection['index']} {detection['classification']['label']}"
            for detection in detection_result["detections"]
        )
        details = [
            f"board={detection_result.get('board_polygon_source', 'auto')}",
            f"force_square={self._force_square}",
            f"unwrap_size={self._unwrap_size[0]}x{self._unwrap_size[1]}",
        ]
        if detection_labels:
            details.append(detection_labels)
        return " | ".join(details)

    def _update_window_title(self, source_text: str) -> None:
        title_suffix = Path(source_text.split(" (", 1)[0]).name or "Live frame"
        self.setWindowTitle(f"Crab Competition Analyzer - {title_suffix}")

    def _show_empty_state(self) -> None:
        self.current_summary_text = ""
        self.path_label.setText("")
        self._last_image = None
        self._last_source_text = ""
        self._manual_board_polygon = None
        self.result_view.set_panel_titles(
            "Original View",
            "Competition Display",
        )
        self.result_view.set_result(
            "Open an image, folder, or video to start crab detection.",
            None,
            None,
            detail_text="For video, select a frame manually or scan a time range for the best frame.",
        )
        self._refresh_navigation_buttons()

    def _show_error_state(
        self,
        summary_text: str,
        *,
        source_text: str | None = None,
        detail_text: str | None = None,
    ) -> None:
        self.current_summary_text = summary_text
        self.path_label.setText(source_text or "")
        if source_text is None:
            self._last_image = None
            self._last_source_text = ""
            self._manual_board_polygon = None
        self.result_view.set_result(
            summary_text,
            None,
            None,
            source_text=source_text,
            detail_text=detail_text,
            tone="warn",
        )
        self.statusBar().showMessage(summary_text, 5000)
        self._refresh_navigation_buttons()

    def _pick_manual_board_polygon(self) -> None:
        if self._last_image is None:
            self.statusBar().showMessage("Open or capture an image before selecting plane corners.", 3000)
            return

        dialog = ManualBoardPickerDialog(self._last_image, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        polygon = dialog.selected_polygon()
        if polygon is None:
            return

        self._manual_board_polygon = polygon
        self._run_detection(
            self._last_image.copy(),
            source_text=self._last_source_text or "Live frame",
        )

    def _clear_manual_board_polygon(self) -> None:
        if self._manual_board_polygon is None:
            return
        self._manual_board_polygon = None
        if self._last_image is not None:
            self._run_detection(
                self._last_image.copy(),
                source_text=self._last_source_text or "Live frame",
            )
        else:
            self._refresh_navigation_buttons()

    def closeEvent(self, event) -> None:
        self._close_video()
        super().closeEvent(event)

    def _refresh_navigation_buttons(self) -> None:
        has_images = bool(self._image_paths)
        has_video = self._video_capture is not None and self._video_path is not None
        self.previous_btn.setEnabled(has_images and self._current_index > 0)
        self.next_btn.setEnabled(has_images and self._current_index < len(self._image_paths) - 1)
        self.reload_btn.setEnabled(has_images or self._last_image is not None)
        self.manual_plane_btn.setEnabled(self._last_image is not None)
        self.clear_manual_plane_btn.setEnabled(self._manual_board_polygon is not None)
        self.video_previous_frame_btn.setEnabled(has_video and self._video_current_frame_index > 0)
        self.video_next_frame_btn.setEnabled(
            has_video
            and (
                self._video_frame_count <= 0
                or self._video_current_frame_index < self._video_frame_count - 1
            )
        )
        self.video_frame_slider.setEnabled(has_video)
        self.video_run_frame_btn.setEnabled(has_video and self._video_current_frame is not None)
        self.video_start_spin.setEnabled(has_video)
        self.video_end_spin.setEnabled(has_video)
        self.video_interval_spin.setEnabled(has_video)
        self.video_use_start_btn.setEnabled(has_video)
        self.video_use_end_btn.setEnabled(has_video)
        self._validate_video_range()
