from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from crab_detector_cv import DEFAULT_UNWRAP_SIZE, detection_summary_text, natural_case_sort_key, render_detection_views
from gui.crab_result_dialog import CrabDetectionResultView

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp"}


def is_supported_image_path(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES


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
        self.setWindowTitle("Crab Detection Debugger")
        self.resize(1600, 950)

        self._force_square = bool(force_square)
        self._unwrap_size = normalize_unwrap_size(unwrap_size)
        self._image_paths: list[Path] = []
        self._current_index = -1
        self._last_dir = str(Path.cwd())
        self._last_image: np.ndarray | None = None
        self._last_source_text = ""
        self.current_summary_text = ""

        self._build_ui()
        self._show_empty_state()

        if image_paths:
            self.set_image_paths(image_paths)

    def _build_ui(self) -> None:
        self.open_images_btn = QPushButton("Open Image(s)")
        self.open_images_btn.clicked.connect(self._open_images)

        self.open_folder_btn = QPushButton("Open Folder")
        self.open_folder_btn.clicked.connect(self._open_folder)

        self.previous_btn = QPushButton("Previous")
        self.previous_btn.clicked.connect(self._show_previous_image)

        self.next_btn = QPushButton("Next")
        self.next_btn.clicked.connect(self._show_next_image)

        self.reload_btn = QPushButton("Reload")
        self.reload_btn.clicked.connect(self._reload_current_source)

        self.force_square_checkbox = QCheckBox("Force square board")
        self.force_square_checkbox.setChecked(self._force_square)
        self.force_square_checkbox.toggled.connect(self._toggle_force_square)

        controls = QHBoxLayout()
        controls.addWidget(self.open_images_btn)
        controls.addWidget(self.open_folder_btn)
        controls.addSpacing(12)
        controls.addWidget(self.previous_btn)
        controls.addWidget(self.next_btn)
        controls.addWidget(self.reload_btn)
        controls.addStretch(1)
        controls.addWidget(self.force_square_checkbox)

        self.path_label = QLabel("")
        self.path_label.setObjectName("summaryHint")
        self.path_label.setWordWrap(True)
        self.path_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

        self.result_view = CrabDetectionResultView(self)

        container = QWidget(self)
        layout = QVBoxLayout(container)
        layout.addLayout(controls)
        layout.addWidget(self.path_label)
        layout.addWidget(self.result_view, 1)
        self.setCentralWidget(container)

        self.statusBar().showMessage("Open an image or folder to start debugging crab detection.")
        self._refresh_navigation_buttons()

    def set_image_paths(self, image_paths: list[str | Path], start_index: int = 0) -> None:
        resolved_paths = collect_image_paths(image_paths)
        if not resolved_paths:
            self._image_paths = []
            self._current_index = -1
            self._show_error_state(
                "No supported images were found.",
                detail_text="Choose image files directly or point the debugger at a folder with images.",
            )
            return

        self._image_paths = resolved_paths
        self._current_index = max(0, min(int(start_index), len(self._image_paths) - 1))
        self._last_dir = str(self._image_paths[self._current_index].parent)
        self._refresh_navigation_buttons()
        self._load_current_path()

    def load_frame(self, frame_bgr: np.ndarray, *, source_label: str = "Live frame") -> str:
        self._image_paths = []
        self._current_index = -1
        self._refresh_navigation_buttons()
        self._run_detection(frame_bgr.copy(), source_text=source_label)
        return self.current_summary_text

    def _open_images(self) -> None:
        selected_paths, _ = QFileDialog.getOpenFileNames(
            self,
            "Open crab image(s)",
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
            self._load_current_path()
            return
        if self._last_image is not None:
            self._run_detection(self._last_image.copy(), source_text=self._last_source_text or "Live frame")
            return
        self.statusBar().showMessage("Nothing to reload yet.", 3000)

    def _toggle_force_square(self, checked: bool) -> None:
        self._force_square = bool(checked)
        if self._current_index >= 0 and self._image_paths:
            self._load_current_path()
            return
        if self._last_image is not None:
            self._run_detection(self._last_image.copy(), source_text=self._last_source_text or "Live frame")
            return
        self.statusBar().showMessage(
            f"Force square board {'enabled' if self._force_square else 'disabled'}.",
            3000,
        )

    def _load_current_path(self) -> None:
        if self._current_index < 0 or self._current_index >= len(self._image_paths):
            self._show_empty_state()
            return

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
                    f"force_square={self._force_square} | unwrap_size={self._unwrap_size[0]}x{self._unwrap_size[1]}"
                ),
                tone="warn",
            )
            self._refresh_navigation_buttons()
            self.statusBar().showMessage(self.current_summary_text, 5000)
            self._update_window_title(source_text)
            return

        self.current_summary_text = detection_summary_text(detection_result)
        self.result_view.set_result(
            self.current_summary_text,
            annotated_original,
            annotated_unwrapped,
            mask_image=detection_result["unwrapped_mask"],
            source_text=source_text,
            detail_text=self._build_detail_text(detection_result),
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
            f"force_square={self._force_square}",
            f"unwrap_size={self._unwrap_size[0]}x{self._unwrap_size[1]}",
        ]
        if detection_labels:
            details.append(detection_labels)
        return " | ".join(details)

    def _update_window_title(self, source_text: str) -> None:
        title_suffix = Path(source_text.split(" (", 1)[0]).name or "Live frame"
        self.setWindowTitle(f"Crab Detection Debugger - {title_suffix}")

    def _show_empty_state(self) -> None:
        self.current_summary_text = ""
        self.path_label.setText("")
        self._last_image = None
        self._last_source_text = ""
        self.result_view.set_result(
            "Open an image or folder to start debugging crab detection.",
            None,
            None,
            detail_text="This app runs the existing path-based CV detector and shows the rendered outputs.",
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

    def _refresh_navigation_buttons(self) -> None:
        has_images = bool(self._image_paths)
        self.previous_btn.setEnabled(has_images and self._current_index > 0)
        self.next_btn.setEnabled(has_images and self._current_index < len(self._image_paths) - 1)
        self.reload_btn.setEnabled(has_images or self._last_image is not None)
