"""Stereo camera page for pair health, capture, and configuration."""

from __future__ import annotations

import time
from dataclasses import replace
from pathlib import Path
from typing import Callable

from PyQt6.QtCore import QThread, QTimer, Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from recording.save_location import DEFAULT_RECORDINGS_DIR
from stereo.capture import StereoCaptureSession
from stereo.pairs import StereoPairConfig, load_stereo_pairs


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
        count: int,
        interval_s: float,
        wait_s: float,
        parent=None,
    ):
        super().__init__(parent)
        self.manager = manager
        self.pair = pair
        self.output_root = Path(output_root)
        self.session_name = session_name
        self.count = max(1, int(count))
        self.interval_s = max(0.0, float(interval_s))
        self.wait_s = max(0.0, float(wait_s))

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
            for idx in range(self.count):
                record = session.capture_once(wait_s=self.wait_s, require_fresh=True)
                captured += 1
                self.progress.emit(record)
                if idx < self.count - 1:
                    time.sleep(self.interval_s)
            self.completed.emit(str(session.manifest_path), captured)
        except Exception as exc:
            self.failed.emit(str(exc))
        finally:
            try:
                session.stop()
            except Exception:
                pass


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
        self._last_manifest_path: str = ""
        self._active_session_name: str = ""

        self._build_ui()
        self.reload_pairs(emit=False)

        self._health_timer = QTimer(self)
        self._health_timer.setInterval(250)
        self._health_timer.timeout.connect(self.refresh_health)
        self._health_timer.start()

    def shutdown(self) -> None:
        if self._capture_worker is not None and self._capture_worker.isRunning():
            self._capture_worker.wait(1000)

    def _build_ui(self) -> None:
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(8)

        self.video_host = QWidget()
        self.video_host_layout = QVBoxLayout(self.video_host)
        self.video_host_layout.setContentsMargins(0, 0, 0, 0)
        self.video_host_layout.setSpacing(0)
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

        capture_card = _SectionCard("Capture")
        self.session_edit = QLineEdit()
        self.session_edit.setPlaceholderText("auto timestamp")
        self.session_edit.setClearButtonEnabled(True)
        self.new_session_btn = QPushButton("New Session")
        self.new_session_btn.clicked.connect(self._new_capture_session)
        session_row = QHBoxLayout()
        session_row.addWidget(self.session_edit, 1)
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
        self.capture_one_btn.clicked.connect(lambda: self._start_capture(count=1))
        self.capture_burst_btn = QPushButton("Capture Burst")
        self.capture_burst_btn.clicked.connect(lambda: self._start_capture(count=self.count_spin.value()))
        button_row.addWidget(self.capture_one_btn)
        button_row.addWidget(self.capture_burst_btn)
        capture_card.body.addLayout(button_row)
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
        pair = self.current_pair()
        if pair is not None:
            self.pairSelectionChanged.emit(pair)
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
        for widget in (self.capture_one_btn, self.capture_burst_btn, self.new_session_btn):
            widget.setEnabled(bool(enabled) and not busy)

    def _resolve_session_name(self) -> str:
        typed = self.session_edit.text().strip()
        if typed:
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
        self.session_edit.clear()
        self.frames_table.setRowCount(0)
        self.output_lbl.setText("-")
        self.statusMessage.emit("Ready for a new stereo capture session", 3000)

    def _start_capture(self, *, count: int) -> None:
        pair = self.current_pair()
        if pair is None or self.manager is None:
            self.statusMessage.emit("No stereo pair available", 3000)
            return
        if self._capture_worker is not None and self._capture_worker.isRunning():
            self.statusMessage.emit("Stereo capture already running", 3000)
            return
        try:
            output_root = Path(self.output_root_provider())
        except Exception as exc:
            self.statusMessage.emit(f"Could not prepare stereo output: {exc}", 5000)
            return
        session_name = self._resolve_session_name()
        self._capture_worker = _CaptureWorker(
            self.manager,
            pair,
            output_root,
            session_name,
            int(count),
            float(self.interval_spin.value()),
            float(self.wait_spin.value()),
            parent=self,
        )
        self._capture_worker.progress.connect(self._on_capture_progress)
        self._capture_worker.completed.connect(self._on_capture_completed)
        self._capture_worker.failed.connect(self._on_capture_failed)
        self._capture_worker.finished.connect(lambda: self._set_capture_enabled(self.current_pair() is not None and self.manager is not None))
        self._set_capture_enabled(False)
        self.statusMessage.emit(f"Stereo capture started: {pair.name}", 3000)
        self._capture_worker.start()

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
        self.statusMessage.emit(f"Stereo capture saved {count} pair(s)", 5000)

    def _on_capture_failed(self, error: str) -> None:
        self.statusMessage.emit(f"Stereo capture failed: {error}", 7000)
