"""Stereo camera page for pair health, capture, and configuration."""

from __future__ import annotations

import json
import time
from dataclasses import replace
from pathlib import Path
from typing import Callable

import numpy as np
from PyQt6.QtCore import QThread, QTimer, Qt, pyqtSignal
from PyQt6.QtGui import QImage, QPixmap
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from recording.save_location import DEFAULT_RECORDINGS_DIR
from stereo.calibration import resolve_stereo_calibration_path
from stereo.capture import StereoCaptureInterrupted, StereoCaptureSession
from stereo.disparity import StereoDisparityProcessor
from stereo.pairs import StereoPairConfig, load_stereo_pairs
from video.frame_rotation import rotate_frame


class _SectionCard(QFrame):
    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        self.setObjectName("stereoSectionCard")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        title_label = QLabel(title)
        title_label.setObjectName("stereoSectionTitle")
        layout.addWidget(title_label)

        self.body = QVBoxLayout()
        self.body.setContentsMargins(0, 0, 0, 0)
        self.body.setSpacing(8)
        layout.addLayout(self.body)


class _CaptureWorker(QThread):
    progress = pyqtSignal(dict)
    completed = pyqtSignal(str, int)
    failed = pyqtSignal(str)

    def __init__(
        self,
        manager,
        pair: StereoPairConfig,
        output_root: Path,
        session_name: str | None,
        count: int | None,
        interval_s: float,
        wait_s: float,
        continuous: bool = False,
        parent=None,
    ):
        super().__init__(parent)
        self.manager = manager
        self.pair = pair
        self.output_root = Path(output_root)
        self.session_name = session_name
        self.count = None if count is None else max(1, int(count))
        self.interval_s = max(0.0, float(interval_s))
        self.wait_s = max(0.0, float(wait_s))
        self.continuous = bool(continuous)

    def run(self) -> None:
        session = StereoCaptureSession(
            self.manager,
            self.pair,
            output_root=self.output_root,
            session_name=self.session_name,
            close_on_stop=False,
        )
        captured = 0
        try:
            session.start()
            if self.continuous:
                while not self.isInterruptionRequested():
                    capture_started = time.monotonic()
                    record = session.capture_once(
                        wait_s=self.wait_s,
                        require_fresh=True,
                        stop_requested=self.isInterruptionRequested,
                    )
                    captured += 1
                    self.progress.emit(record)
                    if not self._sleep_until(capture_started + self.interval_s):
                        break
            else:
                for idx in range(int(self.count or 0)):
                    if self.isInterruptionRequested():
                        break
                    record = session.capture_once(
                        wait_s=self.wait_s,
                        require_fresh=True,
                        stop_requested=self.isInterruptionRequested,
                    )
                    captured += 1
                    self.progress.emit(record)
                    if idx < int(self.count or 0) - 1 and not self._sleep_interruptibly(self.interval_s):
                        break
            self.completed.emit(str(session.manifest_path), captured)
        except StereoCaptureInterrupted:
            self.completed.emit(str(session.manifest_path), captured)
        except Exception as exc:
            self.failed.emit(str(exc))
        finally:
            try:
                session.stop()
            except Exception:
                pass

    def _sleep_until(self, target_ts: float) -> bool:
        return self._sleep_interruptibly(max(0.0, float(target_ts) - time.monotonic()))

    def _sleep_interruptibly(self, duration_s: float) -> bool:
        deadline = time.monotonic() + max(0.0, float(duration_s))
        while time.monotonic() < deadline:
            if self.isInterruptionRequested():
                return False
            time.sleep(min(0.05, deadline - time.monotonic()))
        return not self.isInterruptionRequested()


class _DisparityWorker(QThread):
    frameReady = pyqtSignal(object)
    status = pyqtSignal(str, str)
    failed = pyqtSignal(str)

    def __init__(
        self,
        *,
        manager,
        pair: StereoPairConfig,
        packet_provider: Callable[[str], object | None],
        calibration_path: Path,
        max_width: int,
        fps: float,
        parent=None,
    ):
        super().__init__(parent)
        self.manager = manager
        self.pair = pair
        self.packet_provider = packet_provider
        self.calibration_path = Path(calibration_path)
        self.max_width = max(160, int(max_width))
        self.period_s = 1.0 / max(0.5, float(fps))
        self._last_status_text = ""
        self._last_status_ts = 0.0

    def run(self) -> None:
        try:
            from stereo.calibration import load_stereo_calibration

            calibration = load_stereo_calibration(self.calibration_path)
        except Exception as exc:
            self.failed.emit(f"Could not load stereo calibration: {exc}")
            return

        processor: StereoDisparityProcessor | None = None
        processor_shape: tuple[int, int] | None = None
        last_pair_key: tuple[int, int] | None = None
        last_frame_ts: float | None = None
        self._emit_status(f"Using calibration {calibration.path.name}", "warn", force=True)

        while not self.isInterruptionRequested():
            loop_started = time.monotonic()
            left = self.packet_provider(self.pair.left)
            right = self.packet_provider(self.pair.right)
            if left is None or right is None:
                self._emit_status("Waiting for stereo frames", "warn")
                if not self._sleep_interruptibly(0.03):
                    break
                continue

            try:
                pair_key = (int(left.seq), int(right.seq))
                delta_ms = abs(float(left.monotonic_ts) - float(right.monotonic_ts)) * 1000.0
            except Exception:
                self._emit_status("Frame timing unavailable", "error")
                if not self._sleep_interruptibly(0.05):
                    break
                continue

            if pair_key == last_pair_key:
                if not self._sleep_interruptibly(0.01):
                    break
                continue

            if delta_ms > float(self.pair.max_pair_delta_ms):
                self._emit_status(f"Pair delta high: {delta_ms:.1f} ms", "warn")
                if not self._sleep_interruptibly(0.02):
                    break
                continue

            try:
                left_frame = self._prepared_frame(left, self.pair.left)
                right_frame = self._prepared_frame(right, self.pair.right)
                height, width = left_frame.shape[:2]
                shape = (int(width), int(height))
                if processor is None or processor_shape != shape:
                    processor = StereoDisparityProcessor(
                        calibration,
                        source_size=shape,
                        max_width=self.max_width,
                    )
                    processor_shape = shape
                preview = processor.compute(left_frame, right_frame)
            except Exception as exc:
                self.failed.emit(f"Live disparity failed: {exc}")
                return

            now = time.monotonic()
            fps = 0.0 if last_frame_ts is None else 1.0 / max(0.001, now - last_frame_ts)
            last_frame_ts = now
            last_pair_key = pair_key
            self.frameReady.emit(
                {
                    "frame_bgr": preview.preview_bgr,
                    "delta_ms": delta_ms,
                    "fps": fps,
                    "valid_fraction": preview.valid_fraction,
                    "process_size": preview.process_size,
                    "disparity_min": preview.disparity_min,
                    "disparity_max": preview.disparity_max,
                    "left_seq": pair_key[0],
                    "right_seq": pair_key[1],
                }
            )
            self._emit_status(
                f"Live | {delta_ms:.1f} ms | {preview.valid_fraction * 100.0:.0f}% valid",
                "ok",
            )
            if not self._sleep_interruptibly((loop_started + self.period_s) - time.monotonic()):
                break

    def _prepared_frame(self, packet: object, stream_name: str) -> np.ndarray:
        frame = np.ascontiguousarray(packet.frame_bgr)
        if self.pair.apply_stream_rotation:
            rotation_deg = self._stream_rotation(stream_name)
            if rotation_deg:
                frame = rotate_frame(frame, rotation_deg)
        return np.ascontiguousarray(frame)

    def _stream_rotation(self, stream_name: str) -> int:
        try:
            stream = dict(getattr(self.manager, "stream_defs", {}).get(stream_name, {}) or {})
            return int(stream.get("rotation_deg", 0) or 0)
        except Exception:
            return 0

    def _emit_status(self, text: str, tone: str, *, force: bool = False) -> None:
        now = time.monotonic()
        if not force and text == self._last_status_text and (now - self._last_status_ts) < 1.0:
            return
        self._last_status_text = text
        self._last_status_ts = now
        self.status.emit(text, tone)

    def _sleep_interruptibly(self, duration_s: float) -> bool:
        deadline = time.monotonic() + max(0.0, float(duration_s))
        while time.monotonic() < deadline:
            if self.isInterruptionRequested():
                return False
            time.sleep(min(0.02, deadline - time.monotonic()))
        return not self.isInterruptionRequested()


class StereoPage(QWidget):
    """Operator page for one configured stereo pair."""

    pairSelectionChanged = pyqtSignal(object)
    statusMessage = pyqtSignal(str, int)

    def __init__(
        self,
        *,
        streams_path: str,
        manager=None,
        output_root_provider: Callable[[], Path] | None = None,
        packet_provider: Callable[[str], object | None] | None = None,
        parent=None,
    ):
        super().__init__(parent)
        self.streams_path = str(streams_path)
        self.manager = manager
        self.output_root_provider = output_root_provider or (lambda: Path(DEFAULT_RECORDINGS_DIR))
        self.packet_provider = packet_provider or (lambda _name: None)
        self._pairs: list[StereoPairConfig] = []
        self._capture_worker: _CaptureWorker | None = None
        self._capture_mode = ""
        self._last_manifest_path: str = ""
        self._active_session_name: str = ""
        self._active_output_root: Path | None = None
        self._disparity_worker: _DisparityWorker | None = None
        self._disparity_calibration_override: Path | None = None
        self._last_disparity_frame: np.ndarray | None = None

        self._build_ui()
        self.reload_pairs(emit=False)

        self._health_timer = QTimer(self)
        self._health_timer.setInterval(250)
        self._health_timer.timeout.connect(self.refresh_health)
        self._health_timer.start()

    def shutdown(self) -> None:
        self._stop_disparity_preview()
        if self._capture_worker is not None and self._capture_worker.isRunning():
            self._capture_worker.requestInterruption()
            self._capture_worker.wait(1000)

    def _build_ui(self) -> None:
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(8)

        self.video_host = QWidget()
        video_area = QVBoxLayout(self.video_host)
        video_area.setContentsMargins(0, 0, 0, 0)
        video_area.setSpacing(4)

        self.video_splitter = QSplitter(Qt.Orientation.Vertical)
        self.video_splitter.setChildrenCollapsible(False)

        self.camera_host = QWidget()
        self.video_host_layout = QVBoxLayout(self.camera_host)
        self.video_host_layout.setContentsMargins(0, 0, 0, 0)
        self.video_host_layout.setSpacing(0)
        self.video_splitter.addWidget(self.camera_host)

        self.disparity_frame = QFrame()
        self.disparity_frame.setObjectName("stereoDisparityFrame")
        disparity_frame_layout = QVBoxLayout(self.disparity_frame)
        disparity_frame_layout.setContentsMargins(0, 0, 0, 0)
        disparity_frame_layout.setSpacing(0)
        self.disparity_preview_lbl = QLabel("Live disparity stopped")
        self.disparity_preview_lbl.setObjectName("stereoDisparityPreview")
        self.disparity_preview_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.disparity_preview_lbl.setWordWrap(True)
        self.disparity_preview_lbl.setMinimumSize(320, 180)
        self.disparity_preview_lbl.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        disparity_frame_layout.addWidget(self.disparity_preview_lbl, 1)
        self.video_splitter.addWidget(self.disparity_frame)
        self.video_splitter.setStretchFactor(0, 3)
        self.video_splitter.setStretchFactor(1, 2)
        self.disparity_frame.hide()

        video_area.addWidget(self.video_splitter, 1)
        root.addWidget(self.video_host, 3)

        self.side_panel = QWidget()
        self.side_panel.setMinimumWidth(360)
        self.side_panel.setMaximumWidth(470)
        side = QVBoxLayout(self.side_panel)
        side.setContentsMargins(0, 0, 0, 0)
        side.setSpacing(8)
        root.addWidget(self.side_panel, 1)

        pair_card = _SectionCard("Stereo Pair")
        pair_row = QHBoxLayout()
        self.pair_combo = QComboBox()
        self.pair_combo.currentIndexChanged.connect(self._on_pair_changed)
        self.reload_btn = QPushButton("Reload")
        self.reload_btn.clicked.connect(lambda: self.reload_pairs(emit=True))
        pair_row.addWidget(self.pair_combo, 1)
        pair_row.addWidget(self.reload_btn, 0)
        pair_card.body.addLayout(pair_row)

        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(6)
        self.rig_lbl = self._value_label()
        self.left_lbl = self._value_label()
        self.right_lbl = self._value_label()
        self.calibration_lbl = self._value_label()
        self.baseline_lbl = self._value_label()
        self.sync_lbl = self._value_label()
        for row, (label_text, value) in enumerate(
            [
                ("Rig", self.rig_lbl),
                ("Left", self.left_lbl),
                ("Right", self.right_lbl),
                ("Calibration", self.calibration_lbl),
                ("Baseline", self.baseline_lbl),
                ("Sync", self.sync_lbl),
            ]
        ):
            grid.addWidget(QLabel(label_text), row, 0)
            grid.addWidget(value, row, 1)
        pair_card.body.addLayout(grid)

        self.max_delta_spin = QDoubleSpinBox()
        self.max_delta_spin.setRange(1.0, 1000.0)
        self.max_delta_spin.setDecimals(1)
        self.max_delta_spin.setSingleStep(5.0)
        self.max_delta_spin.setSuffix(" ms")
        self.max_delta_spin.valueChanged.connect(lambda _v: self.refresh_health())
        pair_card.body.addWidget(QLabel("Max Pair Delta"))
        pair_card.body.addWidget(self.max_delta_spin)
        side.addWidget(pair_card)

        health_card = _SectionCard("Health")
        self.pair_state_lbl = QLabel("No pair selected")
        self.pair_state_lbl.setObjectName("stereoPill")
        self.pair_state_lbl.setProperty("tone", "error")
        health_card.body.addWidget(self.pair_state_lbl)
        health_grid = QGridLayout()
        health_grid.setHorizontalSpacing(10)
        health_grid.setVerticalSpacing(6)
        self.delta_lbl = self._value_label()
        self.left_packet_lbl = self._value_label()
        self.right_packet_lbl = self._value_label()
        self.output_lbl = self._value_label()
        for row, (label_text, value) in enumerate(
            [
                ("Delta", self.delta_lbl),
                ("Left Frame", self.left_packet_lbl),
                ("Right Frame", self.right_packet_lbl),
                ("Last Manifest", self.output_lbl),
            ]
        ):
            health_grid.addWidget(QLabel(label_text), row, 0)
            health_grid.addWidget(value, row, 1)
        health_card.body.addLayout(health_grid)
        side.addWidget(health_card)

        disparity_card = _SectionCard("Disparity")
        self.disparity_state_lbl = QLabel("Stopped")
        self.disparity_state_lbl.setObjectName("stereoPill")
        self.disparity_state_lbl.setProperty("tone", "warn")
        self.disparity_state_lbl.setWordWrap(True)
        disparity_card.body.addWidget(self.disparity_state_lbl)

        self.disparity_toggle = QCheckBox("Live Preview")
        self.disparity_toggle.toggled.connect(self._on_disparity_toggled)
        disparity_card.body.addWidget(self.disparity_toggle)

        disparity_path_row = QHBoxLayout()
        self.disparity_calibration_lbl = self._value_label()
        self.disparity_choose_btn = QPushButton("Choose Calibration")
        self.disparity_choose_btn.clicked.connect(self._choose_disparity_calibration)
        disparity_path_row.addWidget(self.disparity_calibration_lbl, 1)
        disparity_path_row.addWidget(self.disparity_choose_btn, 0)
        disparity_card.body.addWidget(QLabel("Calibration"))
        disparity_card.body.addLayout(disparity_path_row)

        disparity_grid = QGridLayout()
        disparity_grid.setHorizontalSpacing(8)
        disparity_grid.setVerticalSpacing(6)
        self.disparity_fps_spin = QDoubleSpinBox()
        self.disparity_fps_spin.setRange(1.0, 20.0)
        self.disparity_fps_spin.setDecimals(1)
        self.disparity_fps_spin.setSingleStep(1.0)
        self.disparity_fps_spin.setSuffix(" fps")
        self.disparity_fps_spin.setValue(8.0)
        self.disparity_width_spin = QSpinBox()
        self.disparity_width_spin.setRange(320, 1280)
        self.disparity_width_spin.setSingleStep(80)
        self.disparity_width_spin.setSuffix(" px")
        self.disparity_width_spin.setValue(960)
        self.disparity_width_spin.valueChanged.connect(lambda _v: self._restart_disparity_preview_if_running())
        self.disparity_fps_spin.valueChanged.connect(lambda _v: self._restart_disparity_preview_if_running())
        disparity_grid.addWidget(QLabel("Preview FPS"), 0, 0)
        disparity_grid.addWidget(QLabel("Max Width"), 0, 1)
        disparity_grid.addWidget(self.disparity_fps_spin, 1, 0)
        disparity_grid.addWidget(self.disparity_width_spin, 1, 1)
        disparity_card.body.addLayout(disparity_grid)

        disparity_metrics = QGridLayout()
        disparity_metrics.setHorizontalSpacing(10)
        disparity_metrics.setVerticalSpacing(6)
        self.disparity_delta_lbl = self._value_label()
        self.disparity_valid_lbl = self._value_label()
        self.disparity_size_lbl = self._value_label()
        for row, (label_text, value) in enumerate(
            [
                ("Delta", self.disparity_delta_lbl),
                ("Valid", self.disparity_valid_lbl),
                ("Size", self.disparity_size_lbl),
            ]
        ):
            disparity_metrics.addWidget(QLabel(label_text), row, 0)
            disparity_metrics.addWidget(value, row, 1)
        disparity_card.body.addLayout(disparity_metrics)
        side.addWidget(disparity_card)

        capture_card = _SectionCard("Capture")
        self.session_edit = QLineEdit()
        self.session_edit.setPlaceholderText("auto timestamp")
        self.session_edit.setClearButtonEnabled(True)
        self.new_session_btn = QPushButton("New Session")
        self.new_session_btn.clicked.connect(self._new_capture_session)
        self.resume_session_btn = QPushButton("Resume Session")
        self.resume_session_btn.clicked.connect(self._choose_resume_session)
        session_row = QHBoxLayout()
        session_row.addWidget(self.session_edit, 1)
        session_row.addWidget(self.resume_session_btn, 0)
        session_row.addWidget(self.new_session_btn, 0)
        capture_card.body.addWidget(QLabel("Session"))
        capture_card.body.addLayout(session_row)

        capture_grid = QGridLayout()
        capture_grid.setHorizontalSpacing(8)
        capture_grid.setVerticalSpacing(6)
        self.count_spin = QSpinBox()
        self.count_spin.setRange(1, 500)
        self.count_spin.setValue(20)
        self.interval_spin = QDoubleSpinBox()
        self.interval_spin.setRange(0.0, 30.0)
        self.interval_spin.setDecimals(2)
        self.interval_spin.setSingleStep(0.05)
        self.interval_spin.setSuffix(" s")
        self.interval_spin.setValue(0.5)
        self.wait_spin = QDoubleSpinBox()
        self.wait_spin.setRange(0.1, 30.0)
        self.wait_spin.setDecimals(2)
        self.wait_spin.setSingleStep(0.1)
        self.wait_spin.setSuffix(" s")
        self.wait_spin.setValue(2.0)
        for col, (label_text, widget) in enumerate(
            [
                ("Count", self.count_spin),
                ("Interval", self.interval_spin),
                ("Wait", self.wait_spin),
            ]
        ):
            capture_grid.addWidget(QLabel(label_text), 0, col)
            capture_grid.addWidget(widget, 1, col)
        capture_card.body.addLayout(capture_grid)

        button_row = QHBoxLayout()
        self.capture_one_btn = QPushButton("Capture Pair")
        self.capture_one_btn.clicked.connect(lambda: self._start_capture(count=1, mode="single"))
        self.capture_burst_btn = QPushButton("Capture Burst")
        self.capture_burst_btn.clicked.connect(lambda: self._start_capture(count=self.count_spin.value(), mode="burst"))
        button_row.addWidget(self.capture_one_btn)
        button_row.addWidget(self.capture_burst_btn)
        capture_card.body.addLayout(button_row)

        record_grid = QGridLayout()
        record_grid.setHorizontalSpacing(8)
        record_grid.setVerticalSpacing(6)
        self.record_fps_spin = QDoubleSpinBox()
        self.record_fps_spin.setRange(0.1, 10.0)
        self.record_fps_spin.setDecimals(1)
        self.record_fps_spin.setSingleStep(0.5)
        self.record_fps_spin.setSuffix(" fps")
        self.record_fps_spin.setValue(2.0)
        record_grid.addWidget(QLabel("Record FPS"), 0, 0)
        record_grid.addWidget(self.record_fps_spin, 1, 0)
        capture_card.body.addLayout(record_grid)

        record_button_row = QHBoxLayout()
        self.record_start_btn = QPushButton("Start Recording")
        self.record_start_btn.clicked.connect(self._start_recording)
        self.record_stop_btn = QPushButton("Stop Recording")
        self.record_stop_btn.clicked.connect(self._stop_recording)
        self.record_stop_btn.setEnabled(False)
        record_button_row.addWidget(self.record_start_btn)
        record_button_row.addWidget(self.record_stop_btn)
        capture_card.body.addLayout(record_button_row)
        side.addWidget(capture_card)

        frames_card = _SectionCard("Captured Pairs")
        self.frames_table = QTableWidget(0, 4)
        self.frames_table.setHorizontalHeaderLabels(["#", "Delta", "Left", "Right"])
        self.frames_table.verticalHeader().hide()
        self.frames_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.frames_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.frames_table.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        frames_card.body.addWidget(self.frames_table)
        side.addWidget(frames_card, 1)

    def _value_label(self) -> QLabel:
        label = QLabel("-")
        label.setObjectName("stereoMetaValue")
        label.setWordWrap(True)
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        return label

    def reload_pairs(self, *, emit: bool) -> None:
        try:
            pairs = load_stereo_pairs(self.streams_path, include_disabled=False)
        except Exception:
            pairs = []
        self._pairs = list(pairs)
        previous = self.current_pair().name if self.current_pair() is not None else ""
        self.pair_combo.blockSignals(True)
        try:
            self.pair_combo.clear()
            for pair in self._pairs:
                self.pair_combo.addItem(pair.name, pair.name)
            if previous:
                idx = self.pair_combo.findText(previous)
                if idx >= 0:
                    self.pair_combo.setCurrentIndex(idx)
        finally:
            self.pair_combo.blockSignals(False)
        self._update_pair_fields()
        self._update_disparity_calibration_label()
        self._set_capture_enabled(self.current_pair() is not None and self.manager is not None)
        if emit and self.current_pair() is not None:
            self.pairSelectionChanged.emit(self.current_pair())

    def current_pair(self) -> StereoPairConfig | None:
        idx = self.pair_combo.currentIndex()
        if idx < 0 or idx >= len(self._pairs):
            return None
        pair = self._pairs[idx]
        return replace(pair, max_pair_delta_ms=float(self.max_delta_spin.value() or pair.max_pair_delta_ms))

    def _on_pair_changed(self, _index: int) -> None:
        self._update_pair_fields()
        self._update_disparity_calibration_label()
        pair = self.current_pair()
        if pair is not None:
            self.pairSelectionChanged.emit(pair)
        self._restart_disparity_preview_if_running()
        self.refresh_health()

    def _update_pair_fields(self) -> None:
        pair = self._pairs[self.pair_combo.currentIndex()] if 0 <= self.pair_combo.currentIndex() < len(self._pairs) else None
        if pair is None:
            self.rig_lbl.setText("-")
            self.left_lbl.setText("-")
            self.right_lbl.setText("-")
            self.calibration_lbl.setText("-")
            self.baseline_lbl.setText("-")
            self.sync_lbl.setText("-")
            self.max_delta_spin.setValue(50.0)
            return
        self.rig_lbl.setText(pair.rig_id)
        self.left_lbl.setText(self._stream_summary(pair.left))
        self.right_lbl.setText(self._stream_summary(pair.right))
        self.calibration_lbl.setText(pair.calibration_id or "pending")
        baseline = pair.metadata.get("baseline_mm")
        self.baseline_lbl.setText("pending" if baseline in (None, "") else f"{baseline} mm")
        self.sync_lbl.setText(str(pair.metadata.get("sync_notes") or "software paired"))
        self.max_delta_spin.blockSignals(True)
        try:
            self.max_delta_spin.setValue(float(pair.max_pair_delta_ms))
        finally:
            self.max_delta_spin.blockSignals(False)

    def _stream_summary(self, name: str) -> str:
        stream = {}
        try:
            stream = dict(getattr(self.manager, "stream_defs", {}).get(name, {}) or {})
        except Exception:
            stream = {}
        if not stream:
            return name
        size = f"{stream.get('width', '-') }x{stream.get('height', '-')}"
        fps = stream.get("fps", "-")
        fmt = stream.get("video_format", "-")
        port = stream.get("port", "-")
        return f"{name} | {size} @ {fps} | {fmt} | UDP {port}"

    def attach_video_placeholder(self, message: str) -> None:
        if self.video_host_layout.count() > 0:
            return
        label = QLabel(message)
        label.setObjectName("videoPanePlaceholder")
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setWordWrap(True)
        self.video_host_layout.addWidget(label, 1)

    def _resolve_disparity_calibration_path(self, pair: StereoPairConfig | None = None) -> Path | None:
        pair = pair or self.current_pair()
        if pair is None:
            return None
        if self._disparity_calibration_override is not None and self._disparity_calibration_override.is_file():
            return self._disparity_calibration_override.resolve()

        identifiers: list[object] = [
            pair.metadata.get("calibration_path"),
            pair.calibration_id,
        ]
        if self._active_session_name:
            try:
                output_root = self._active_output_root or Path(self.output_root_provider())
                identifiers.append(output_root / "stereo_sessions" / self._active_session_name / "stereo_calibration.json")
            except Exception:
                pass

        base_dir = Path(self.streams_path).expanduser().resolve().parent
        for identifier in identifiers:
            resolved = resolve_stereo_calibration_path(identifier, base_dir=base_dir)
            if resolved is not None:
                return resolved
        return None

    def _update_disparity_calibration_label(self) -> None:
        pair = self.current_pair()
        path = self._resolve_disparity_calibration_path(pair)
        if path is not None:
            self.disparity_calibration_lbl.setText(str(path))
            return
        if pair is None:
            self.disparity_calibration_lbl.setText("-")
        elif pair.calibration_id:
            self.disparity_calibration_lbl.setText(f"{pair.calibration_id} (not found)")
        else:
            self.disparity_calibration_lbl.setText("pending")

    def _choose_disparity_calibration(self) -> None:
        try:
            output_root = Path(self.output_root_provider())
        except Exception:
            output_root = Path(DEFAULT_RECORDINGS_DIR)
        start_dir = output_root / "stereo_sessions"
        path, _filter = QFileDialog.getOpenFileName(
            self,
            "Choose stereo calibration",
            str(start_dir if start_dir.exists() else output_root),
            "Stereo calibration JSON (*.json);;JSON files (*.json);;All files (*)",
        )
        if not path:
            return
        selected = Path(path)
        self._disparity_calibration_override = selected
        self._update_disparity_calibration_label()
        self.statusMessage.emit(f"Stereo calibration selected: {selected.name}", 3000)
        self._restart_disparity_preview_if_running()

    def _on_disparity_toggled(self, enabled: bool) -> None:
        if enabled:
            if not self._start_disparity_preview():
                previous = self.disparity_toggle.blockSignals(True)
                try:
                    self.disparity_toggle.setChecked(False)
                finally:
                    self.disparity_toggle.blockSignals(previous)
        else:
            self._stop_disparity_preview()
            self.disparity_frame.hide()
            self._last_disparity_frame = None
            self.disparity_preview_lbl.setPixmap(QPixmap())
            self.disparity_preview_lbl.setText("Live disparity stopped")
            self._set_disparity_state("Stopped", "warn")

    def _start_disparity_preview(self) -> bool:
        pair = self.current_pair()
        if pair is None:
            self._set_disparity_state("No pair selected", "error")
            self.statusMessage.emit("No stereo pair available for disparity", 3000)
            return False
        if self.packet_provider is None:
            self._set_disparity_state("No frame source", "error")
            return False

        calibration_path = self._resolve_disparity_calibration_path(pair)
        if calibration_path is None:
            self._set_disparity_state("Calibration required", "error")
            self.statusMessage.emit("Choose a stereo calibration JSON before starting live disparity", 5000)
            return False

        self._stop_disparity_preview()
        self.disparity_frame.show()
        self.disparity_preview_lbl.setPixmap(QPixmap())
        self.disparity_preview_lbl.setText("Starting live disparity...")
        self._last_disparity_frame = None
        self._set_disparity_state("Starting", "warn")
        self._disparity_worker = _DisparityWorker(
            manager=self.manager,
            pair=pair,
            packet_provider=self.packet_provider,
            calibration_path=calibration_path,
            max_width=int(self.disparity_width_spin.value()),
            fps=float(self.disparity_fps_spin.value()),
            parent=self,
        )
        self._disparity_worker.frameReady.connect(self._on_disparity_frame)
        self._disparity_worker.status.connect(self._set_disparity_state)
        self._disparity_worker.failed.connect(self._on_disparity_failed)
        self._disparity_worker.finished.connect(self._on_disparity_finished)
        self._disparity_worker.start()
        return True

    def _stop_disparity_preview(self) -> None:
        worker = self._disparity_worker
        if worker is not None and worker.isRunning():
            worker.requestInterruption()
            worker.wait(1000)
        self._disparity_worker = None

    def _restart_disparity_preview_if_running(self) -> None:
        if self._disparity_worker is None or not self.disparity_toggle.isChecked():
            return
        if not self._start_disparity_preview():
            previous = self.disparity_toggle.blockSignals(True)
            try:
                self.disparity_toggle.setChecked(False)
            finally:
                self.disparity_toggle.blockSignals(previous)

    def _on_disparity_frame(self, payload: object) -> None:
        if not isinstance(payload, dict):
            return
        frame = payload.get("frame_bgr")
        if frame is None:
            return
        self._last_disparity_frame = np.ascontiguousarray(frame)
        self._render_disparity_frame()
        try:
            self.disparity_delta_lbl.setText(f"{float(payload.get('delta_ms', 0.0)):.1f} ms")
            self.disparity_valid_lbl.setText(f"{float(payload.get('valid_fraction', 0.0)) * 100.0:.0f}%")
            size = payload.get("process_size") or ("-", "-")
            self.disparity_size_lbl.setText(f"{int(size[0])}x{int(size[1])}")
        except Exception:
            pass

    def _render_disparity_frame(self) -> None:
        frame = self._last_disparity_frame
        if frame is None:
            return
        try:
            h, w, ch = frame.shape
            image = QImage(frame.data, w, h, ch * w, QImage.Format.Format_BGR888)
            dpr = max(1.0, float(self.devicePixelRatioF()))
            target_w = max(1, int(self.disparity_preview_lbl.width() * dpr))
            target_h = max(1, int(self.disparity_preview_lbl.height() * dpr))
            pix = QPixmap.fromImage(image).scaled(
                target_w,
                target_h,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.FastTransformation,
            )
            pix.setDevicePixelRatio(dpr)
            self.disparity_preview_lbl.setText("")
            self.disparity_preview_lbl.setPixmap(pix)
        except Exception:
            pass

    def _on_disparity_failed(self, error: str) -> None:
        self._set_disparity_state(error, "error")
        self.disparity_preview_lbl.setPixmap(QPixmap())
        self.disparity_preview_lbl.setText(error)
        previous = self.disparity_toggle.blockSignals(True)
        try:
            self.disparity_toggle.setChecked(False)
        finally:
            self.disparity_toggle.blockSignals(previous)
        self.statusMessage.emit(error, 7000)

    def _on_disparity_finished(self) -> None:
        if self.sender() is self._disparity_worker:
            self._disparity_worker = None

    def _set_disparity_state(self, text: str, tone: str) -> None:
        self.disparity_state_lbl.setText(text)
        self.disparity_state_lbl.setProperty("tone", tone)
        self.disparity_state_lbl.style().unpolish(self.disparity_state_lbl)
        self.disparity_state_lbl.style().polish(self.disparity_state_lbl)
        self.disparity_state_lbl.update()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._render_disparity_frame()

    def refresh_health(self) -> None:
        pair = self.current_pair()
        if pair is None:
            self._set_pair_state("No pair selected", "error")
            self.delta_lbl.setText("-")
            self.left_packet_lbl.setText("-")
            self.right_packet_lbl.setText("-")
            return
        left = self.packet_provider(pair.left)
        right = self.packet_provider(pair.right)
        now = time.monotonic()
        self.left_packet_lbl.setText(self._packet_summary(left, now))
        self.right_packet_lbl.setText(self._packet_summary(right, now))
        if left is None or right is None:
            self._set_pair_state("Waiting for frames", "warn")
            self.delta_lbl.setText("-")
            return
        try:
            delta_ms = abs(float(left.monotonic_ts) - float(right.monotonic_ts)) * 1000.0
        except Exception:
            self._set_pair_state("Frame timing unavailable", "error")
            self.delta_lbl.setText("-")
            return
        self.delta_lbl.setText(f"{delta_ms:.1f} ms")
        if delta_ms <= float(self.max_delta_spin.value()):
            self._set_pair_state("Pair ready", "ok")
        else:
            self._set_pair_state("Pair delta high", "warn")

    def _packet_summary(self, packet: object | None, now: float) -> str:
        if packet is None:
            return "-"
        try:
            age_ms = max(0.0, (now - float(packet.monotonic_ts)) * 1000.0)
            return f"seq {int(packet.seq)} | age {age_ms:.0f} ms"
        except Exception:
            return "present"

    def _set_pair_state(self, text: str, tone: str) -> None:
        self.pair_state_lbl.setText(text)
        self.pair_state_lbl.setProperty("tone", tone)
        self.pair_state_lbl.style().unpolish(self.pair_state_lbl)
        self.pair_state_lbl.style().polish(self.pair_state_lbl)
        self.pair_state_lbl.update()

    def _set_capture_enabled(self, enabled: bool) -> None:
        busy = self._capture_worker is not None and self._capture_worker.isRunning()
        ready = bool(enabled)
        recording = busy and self._capture_mode == "recording"
        for widget in (self.capture_one_btn, self.capture_burst_btn, self.record_start_btn):
            widget.setEnabled(bool(enabled) and not busy)
        self.record_stop_btn.setEnabled(recording)
        for widget in (self.count_spin, self.interval_spin, self.wait_spin, self.record_fps_spin):
            widget.setEnabled(ready and not busy)
        session_enabled = self.current_pair() is not None and not busy
        for widget in (self.resume_session_btn, self.new_session_btn):
            widget.setEnabled(session_enabled)

    def _resolve_session_name(self) -> str:
        typed = self.session_edit.text().strip()
        if typed:
            if self._active_session_name and typed != self._active_session_name:
                self._active_output_root = None
            self._active_session_name = typed
            return typed
        if not self._active_session_name:
            self._active_session_name = time.strftime("%Y%m%d-%H%M%S")
            self.session_edit.setText(self._active_session_name)
        return self._active_session_name

    def _new_capture_session(self) -> None:
        if self._capture_worker is not None and self._capture_worker.isRunning():
            self.statusMessage.emit("Stereo capture already running", 3000)
            return
        self._active_session_name = ""
        self._active_output_root = None
        self.session_edit.clear()
        self.frames_table.setRowCount(0)
        self.output_lbl.setText("-")
        self.statusMessage.emit("Ready for a new stereo capture session", 3000)

    def _choose_resume_session(self) -> None:
        try:
            output_root = Path(self.output_root_provider())
        except Exception:
            output_root = Path(DEFAULT_RECORDINGS_DIR)
        start_dir = output_root / "stereo_sessions"
        path, _filter = QFileDialog.getOpenFileName(
            self,
            "Resume stereo capture session",
            str(start_dir if start_dir.exists() else output_root),
            "Stereo manifest (manifest.json);;JSON files (*.json);;All files (*)",
        )
        if path:
            self._load_session_manifest(Path(path))

    def _load_session_manifest(self, manifest_path: Path) -> bool:
        path = Path(manifest_path)
        if path.is_dir():
            path = path / "manifest.json"
        if not path.exists():
            self.statusMessage.emit(f"Stereo manifest not found: {path}", 5000)
            return False
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            self.statusMessage.emit(f"Could not read stereo manifest: {exc}", 7000)
            return False

        pair = self.current_pair()
        if pair is not None:
            manifest_pair = manifest.get("pair") or {}
            mismatches = [
                key
                for key, expected in (
                    ("name", pair.name),
                    ("left", pair.left),
                    ("right", pair.right),
                    ("rig_id", pair.rig_id),
                )
                if str(manifest_pair.get(key, "")) != str(expected)
            ]
            if mismatches:
                self.statusMessage.emit(
                    "Manifest belongs to a different stereo pair; select the matching pair or start a new session.",
                    7000,
                )
                return False

        session_dir = path.parent
        if session_dir.parent.name != "stereo_sessions":
            self.statusMessage.emit("Stereo manifest must be inside a stereo_sessions/<session> folder.", 7000)
            return False

        self._active_session_name = session_dir.name
        self._active_output_root = session_dir.parent.parent
        self.session_edit.setText(self._active_session_name)
        self.output_lbl.setText(str(path))
        self._update_disparity_calibration_label()
        self.frames_table.setRowCount(0)
        for frame in manifest.get("frames") or []:
            self._on_capture_progress(frame)
        self.statusMessage.emit(
            f"Resumed stereo session '{self._active_session_name}' with {self.frames_table.rowCount()} pair(s)",
            5000,
        )
        return True

    def _start_recording(self) -> None:
        fps = max(0.1, float(self.record_fps_spin.value()))
        self._start_capture(count=None, mode="recording", interval_s=1.0 / fps)

    def _stop_recording(self) -> None:
        if self._capture_worker is None or not self._capture_worker.isRunning() or self._capture_mode != "recording":
            return
        self._capture_worker.requestInterruption()
        self.record_stop_btn.setEnabled(False)
        self.statusMessage.emit("Stopping stereo recording...", 3000)

    def _start_capture(self, *, count: int | None, mode: str = "burst", interval_s: float | None = None) -> None:
        pair = self.current_pair()
        if pair is None or self.manager is None:
            self.statusMessage.emit("No stereo pair available", 3000)
            return
        if self._capture_worker is not None and self._capture_worker.isRunning():
            self.statusMessage.emit("Stereo capture already running", 3000)
            return
        session_name = self._resolve_session_name()
        if self._active_output_root is not None:
            output_root = self._active_output_root
        else:
            try:
                output_root = Path(self.output_root_provider())
            except Exception as exc:
                self.statusMessage.emit(f"Could not prepare stereo output: {exc}", 5000)
                return
        self._capture_worker = _CaptureWorker(
            self.manager,
            pair,
            output_root,
            session_name,
            None if count is None else int(count),
            float(self.interval_spin.value() if interval_s is None else interval_s),
            float(self.wait_spin.value()),
            continuous=(mode == "recording"),
            parent=self,
        )
        self._capture_mode = mode
        self._capture_worker.progress.connect(self._on_capture_progress)
        self._capture_worker.completed.connect(self._on_capture_completed)
        self._capture_worker.failed.connect(self._on_capture_failed)
        self._capture_worker.finished.connect(self._on_capture_worker_finished)
        self._set_capture_enabled(False)
        if mode == "recording":
            self.statusMessage.emit(f"Stereo recording started: {pair.name}", 3000)
        else:
            self.statusMessage.emit(f"Stereo capture started: {pair.name}", 3000)
        self._capture_worker.start()
        if mode == "recording":
            self.record_stop_btn.setEnabled(True)

    def _on_capture_worker_finished(self) -> None:
        self._capture_mode = ""
        self._set_capture_enabled(self.current_pair() is not None and self.manager is not None)

    def _on_capture_progress(self, record: dict) -> None:
        row = self.frames_table.rowCount()
        self.frames_table.insertRow(row)
        values = [
            str(record.get("index", row + 1)),
            f"{float(record.get('pair_delta_ms', 0.0)):.1f} ms",
            str((record.get("left") or {}).get("seq", "-")),
            str((record.get("right") or {}).get("seq", "-")),
        ]
        for col, value in enumerate(values):
            item = QTableWidgetItem(value)
            item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.frames_table.setItem(row, col, item)
        self.frames_table.scrollToBottom()

    def _on_capture_completed(self, manifest_path: str, count: int) -> None:
        self._last_manifest_path = manifest_path
        self.output_lbl.setText(manifest_path)
        if self._capture_mode == "recording":
            self.statusMessage.emit(f"Stereo recording saved {count} pair(s)", 5000)
        else:
            self.statusMessage.emit(f"Stereo capture saved {count} pair(s)", 5000)

    def _on_capture_failed(self, error: str) -> None:
        if self._capture_mode == "recording":
            self.statusMessage.emit(f"Stereo recording failed: {error}", 7000)
        else:
            self.statusMessage.emit(f"Stereo capture failed: {error}", 7000)
