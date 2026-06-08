"""Single camera widget with connection, display, snapshot, and recording logic."""

from __future__ import annotations

import time
import threading
import logging
from collections import deque
from pathlib import Path

from PyQt6.QtCore import Qt, QThread, pyqtSignal, QTimer
from PyQt6.QtGui import QImage, QPixmap
from PyQt6.QtWidgets import QWidget, QLabel, QVBoxLayout, QSizePolicy

import numpy as np

from recording.video_recorder import VideoRecorder, save_snapshot
from recording.save_location import DEFAULT_RECORDINGS_DIR
from recording.capture_paths import timestamped_camera_stem, unique_capture_path
from video.cam import RemoteCameraManager, RemoteCv2Camera
from video.frame_correction import WaterCorrection
from video.frame_rotation import rotate_frame
from config import (
    WATER_CORRECTION_ZOOM,
    WATER_CORRECTION_K1,
    WATER_CORRECTION_K2,
    WATER_CORRECTION_K3,
    WATER_CORRECTION_AIR_HFOV_DEG,
    WATER_CORRECTION_TARGET_HFOV_DEG,
    VIDEO_DISPLAY_FPS_SINGLE,
    VIDEO_FIRST_FRAME_TIMEOUT_S,
    VIDEO_STALL_TIMEOUT_S,
)

logger = logging.getLogger(__name__)
_ORPHANED_CONNECT_WORKERS: set[QThread] = set()


class _VideoWorker(QThread):
    """Reads frames from RemoteCv2Camera at a target FPS."""

    def __init__(self, camera: RemoteCv2Camera, parent=None, fps: float = 30.0):
        super().__init__(parent)
        self.camera = camera
        self.period = 1.0 / float(fps)
        self._running = True
        # Set to a WaterCorrection instance to enable; None to disable.
        # Replacing this reference from the UI thread is safe in CPython
        # because object-reference assignment is atomic under the GIL.
        self.correction: WaterCorrection | None = None
        self._frame_lock = threading.Lock()
        self._latest_frame: np.ndarray | None = None
        self._latest_seq: int = 0
        self._last_taken_seq: int = 0
        self.rotation_deg: int = int(getattr(camera, "rotation_deg", 0))

    def run(self):
        while self._running:
            t0 = time.time()
            ok, frame = self.camera.read()
            if ok and frame is not None:
                c = self.correction  # read once; atomic under GIL
                if c is not None:
                    try:
                        frame = c.apply(frame)
                    except Exception:
                        pass
                if self.rotation_deg:
                    try:
                        frame = rotate_frame(frame, self.rotation_deg)
                    except Exception:
                        pass
                with self._frame_lock:
                    self._latest_frame = frame
                    self._latest_seq += 1
            dt = time.time() - t0
            sleep_for = self.period - dt
            if sleep_for > 0:
                QThread.msleep(int(sleep_for * 1000))

    def take_latest_frame(self) -> np.ndarray | None:
        with self._frame_lock:
            if self._latest_frame is None or self._latest_seq == self._last_taken_seq:
                return None
            self._last_taken_seq = self._latest_seq
            return self._latest_frame

    def stop(self):
        self._running = False
        self.wait(500)


class _ConnectWorker(QThread):
    """Attempts to open a stream without blocking the UI."""
    connected = pyqtSignal(object)  # RemoteCv2Camera
    failed = pyqtSignal(str)

    def __init__(self, manager: RemoteCameraManager, stream_name: str, parent=None):
        super().__init__(parent)
        self.manager = manager
        self.stream_name = stream_name

    def run(self):
        try:
            cam = self.manager.open(self.stream_name)
            self.connected.emit(cam)
        except Exception as e:
            self.failed.emit(str(e))


def _disconnect_connect_worker(worker) -> None:
    for signal_name in ("connected", "failed"):
        signal = getattr(worker, signal_name, None)
        disconnect = getattr(signal, "disconnect", None)
        if callable(disconnect):
            try:
                disconnect()
            except Exception:
                pass


def _abandon_connect_worker(worker) -> None:
    _disconnect_connect_worker(worker)
    try:
        worker.setParent(None)
    except Exception:
        pass
    try:
        _ORPHANED_CONNECT_WORKERS.add(worker)
    except Exception:
        pass

    def _finished() -> None:
        try:
            _ORPHANED_CONNECT_WORKERS.discard(worker)
        except Exception:
            pass

    try:
        worker.finished.connect(_finished)
    except Exception:
        pass
    try:
        worker.quit()
    except Exception:
        pass


class VideoWidget(QWidget):
    """Video display for a single ROV stream, with failsafe reconnection.

    Goals:
      - GUI starts even if ROV is off / video service isn't running yet
      - No modal popups
      - Only selected stream decodes (handled by VideoTabs)
      - Auto-retry connect and auto-recover from stalls
    """

    def __init__(self, manager: RemoteCameraManager, stream_name: str, parent=None, *, autostart: bool = True):
        super().__init__(parent)
        self.manager = manager
        self.stream_name = stream_name
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

        self.camera: RemoteCv2Camera | None = None
        self.worker: _VideoWorker | None = None
        self._connect_worker: _ConnectWorker | None = None

        self.last_frame: np.ndarray | None = None
        self.last_frame_ts: float = 0.0
        self.frame_buffer: deque[np.ndarray] = deque(maxlen=1)
        self._connected_ts: float = 0.0
        self._rec: VideoRecorder | None = None
        self._record_started_ts: float | None = None
        self._snapshot_indicator_until_ts: float = 0.0
        self._snapshot_indicator_text: str = "SNAP"
        self._snapshot_indicator_duration_s: float = 1.2
        self._display_fps: float = float(VIDEO_DISPLAY_FPS_SINGLE)

        # state
        self._state: str = "waiting"  # waiting|connecting|playing|stalled
        self._last_error: str | None = None
        self._retry_backoff_s: float = 0.5
        self._next_retry_ts: float = 0.0
        self._stall_timeout_s: float = max(2.0, float(VIDEO_STALL_TIMEOUT_S))
        # If we connect successfully but never receive a first frame, treat it as a stall
        # after a slightly longer grace period.
        self._first_frame_timeout_s: float = max(self._stall_timeout_s, float(VIDEO_FIRST_FRAME_TIMEOUT_S))

        # When disconnected/stalled we don't want to leave a stale frame visible.
        # If no new frames arrive, we clear the pixmap and show a status message.
        self._clear_stale_frame: bool = True
        self._rov_link_lost: bool = False
        self._rov_link_wait_message = f"{self.stream_name}\nROV link lost.\nWaiting for heartbeat..."

        self.label = QLabel(f"{self.stream_name}\nWaiting for stream...")
        self.label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.label.setWordWrap(True)
        self.label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.label.setMinimumSize(160, 90)
        self.label.setMargin(0)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(self.label)

        self._record_badge = QLabel("REC 00:00", self)
        self._record_badge.setObjectName("videoRecordBadge")
        self._record_badge.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._record_badge.hide()

        self._snapshot_badge = QLabel(self._snapshot_indicator_text, self)
        self._snapshot_badge.setObjectName("videoSnapshotBadge")
        self._snapshot_badge.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._snapshot_badge.hide()

        # Out-of-water lens correction (toggleable)
        self._correction: WaterCorrection | None = None
        self._correction_enabled: bool = False

        self._tick_timer = QTimer(self)
        self._tick_timer.timeout.connect(self._tick)
        self.set_display_fps(self._display_fps)
        self._tick_timer.start()

        # Kick off first attempt immediately unless the app is waiting for the
        # first heartbeat so offline boot stays responsive.
        if autostart:
            self._start_connect()
        else:
            self._rov_link_lost = True
            self._rov_link_wait_message = f"{self.stream_name}\nWaiting for ROV heartbeat..."
            self._schedule_retry(1.0)
            self._show_message(self._rov_link_wait_message)
        self._refresh_capture_indicators()

    def _show_message(self, msg: str, *, clear_pixmap: bool | None = None) -> None:
        """Show status text reliably.

        QLabel will keep showing the pixmap even if setText() is called, so
        we clear the pixmap whenever we want text to be visible.
        """
        if clear_pixmap is None:
            clear_pixmap = self._clear_stale_frame
        if clear_pixmap:
            try:
                self.label.setPixmap(QPixmap())
            except Exception:
                pass
        if self.label.text() != msg:
            self.label.setText(msg)

    # --- out-of-water correction ---
    def set_water_correction(self, enabled: bool) -> None:
        """Enable or disable the out-of-water lens correction."""
        self._correction_enabled = bool(enabled)
        if enabled and self._correction is None:
            self._correction = WaterCorrection(
                zoom=WATER_CORRECTION_ZOOM,
                k1=WATER_CORRECTION_K1,
                k2=WATER_CORRECTION_K2,
                k3=WATER_CORRECTION_K3,
                air_hfov_deg=WATER_CORRECTION_AIR_HFOV_DEG,
                target_hfov_deg=WATER_CORRECTION_TARGET_HFOV_DEG,
            )
        # Push to the worker thread (atomic assignment, safe under GIL).
        if self.worker is not None:
            self.worker.correction = self._correction if enabled else None

    # --- public helpers for MainWindow / status bar ---
    def status(self) -> dict:
        age = None
        if self.last_frame_ts > 0:
            age = max(0.0, time.time() - self.last_frame_ts)
        return {
            "state": self._state,
            "age_s": age,
            "last_error": self._last_error,
            "rov_link_lost": bool(self._rov_link_lost),
        }

    def water_correction_enabled(self) -> bool:
        return bool(self._correction_enabled)

    def is_recording(self) -> bool:
        return self._rec is not None

    def display_fps(self) -> float:
        return float(self._display_fps)

    def set_display_fps(self, fps: float) -> None:
        try:
            value = float(fps)
        except Exception:
            value = float(VIDEO_DISPLAY_FPS_SINGLE)
        value = max(1.0, min(60.0, value))
        self._display_fps = value
        interval_ms = max(1, int(round(1000.0 / value)))
        try:
            self._tick_timer.setInterval(interval_ms)
        except Exception:
            pass

    def _format_elapsed(self, elapsed_s: float) -> str:
        elapsed_s = max(0, int(elapsed_s))
        minutes, seconds = divmod(elapsed_s, 60)
        hours, minutes = divmod(minutes, 60)
        if hours > 0:
            return f"{hours:d}:{minutes:02d}:{seconds:02d}"
        return f"{minutes:02d}:{seconds:02d}"

    def _layout_capture_badges(self) -> None:
        margin = 10
        for badge, x_mode in (
            (self._record_badge, "left"),
            (self._snapshot_badge, "right"),
        ):
            if not badge.isVisible():
                continue
            badge.adjustSize()
            y = margin
            if x_mode == "left":
                x = margin
            else:
                x = max(margin, self.width() - badge.width() - margin)
            badge.move(x, y)
            badge.raise_()

    def _refresh_capture_indicators(self) -> None:
        now = time.time()

        if self._record_started_ts is not None and self._rec is not None:
            text = f"REC {self._format_elapsed(now - self._record_started_ts)}"
            if self._record_badge.text() != text:
                self._record_badge.setText(text)
            self._record_badge.show()
        else:
            self._record_badge.hide()

        if self._snapshot_indicator_until_ts > now:
            if self._snapshot_badge.text() != self._snapshot_indicator_text:
                self._snapshot_badge.setText(self._snapshot_indicator_text)
            self._snapshot_badge.show()
        else:
            self._snapshot_badge.hide()

        self._layout_capture_badges()

    def _flash_snapshot_indicator(self, text: str = "SNAP") -> None:
        self._snapshot_indicator_text = str(text or "SNAP")
        self._snapshot_indicator_until_ts = time.time() + self._snapshot_indicator_duration_s
        self._refresh_capture_indicators()

    # --- connection / recovery ---
    def _schedule_retry(self, delay_s: float):
        self._next_retry_ts = time.time() + max(0.0, float(delay_s))

    def _start_connect(self):
        if self._rov_link_lost:
            self._show_message(self._rov_link_wait_message)
            self._schedule_retry(1.0)
            return
        if self._connect_worker is not None and self._connect_worker.isRunning():
            return

        self._state = "connecting"
        self._show_message(f"{self.stream_name}\nConnecting...")
        self._connect_worker = _ConnectWorker(self.manager, self.stream_name, parent=self)
        self._connect_worker.connected.connect(self._on_connected)
        self._connect_worker.failed.connect(self._on_connect_failed)
        self._connect_worker.start()

    def _on_connected(self, cam_obj):
        # stop old resources if any
        self._stop_worker_only()

        self.camera = cam_obj
        self._last_error = None
        self._retry_backoff_s = 0.5
        self._state = "playing"
        self._connected_ts = time.time()
        self._rov_link_lost = False

        # New connection: treat frames as "not yet received" until the first one arrives.
        self.last_frame = None
        self.last_frame_ts = 0.0
        self.frame_buffer.clear()

        # If the ROV reported recovery actions (e.g., USB rebind), surface them briefly.
        notices = []
        try:
            notices = list(getattr(self.camera, "start_messages", []) or [])
        except Exception:
            notices = []

        if notices:
            # Keep it short so it fits in the widget before the first frame arrives.
            tail = notices[-3:]
            self._show_message(
                f"{self.stream_name}\nConnected (ROV recovery):\n" + "\n".join(tail) + "\n\nWaiting for frames..."
            )
        else:
            self._show_message(f"{self.stream_name}\nWaiting for frames...")

        self.worker = _VideoWorker(self.camera, fps=30.0)
        if self._correction_enabled and self._correction is not None:
            self.worker.correction = self._correction
        self.worker.start()

    def _on_connect_failed(self, err: str):
        self._last_error = err
        self._state = "waiting"

        # Exponential backoff, capped
        self._retry_backoff_s = min(self._retry_backoff_s * 1.5, 5.0)
        self._schedule_retry(self._retry_backoff_s)

        # Friendly message
        msg = f"{self.stream_name}\nStream not available. Retrying...\n\n{err}"
        self._show_message(msg)

    def _stop_worker_only(self):
        if self.worker:
            try:
                self.worker.stop()
            except Exception:
                pass
            self.worker = None

    def _restart_stream(self, message: str | None = None, *, retry_delay_s: float = 0.2):
        self._state = "stalled"
        # Clear timestamps so we don't immediately re-trigger stall before the
        # new pipeline produces its first frame.
        self.last_frame = None
        self.last_frame_ts = 0.0
        self.frame_buffer.clear()
        self._connected_ts = 0.0
        self.shutdown(release_only=True)  # keep widget alive + clear pixmap
        # Clear any stale frame immediately so the user doesn't think the
        # stream is still live.
        self._show_message(message or f"{self.stream_name}\nDisconnected - attempting to reconnect...")
        self._retry_backoff_s = 0.5
        if not self._rov_link_lost:
            self._schedule_retry(retry_delay_s)

    def set_rov_link_status(self, status: str) -> None:
        status_key = str(status or "").strip().upper()
        if status_key in {"LOST", "NO DATA"}:
            if status_key == "NO DATA":
                self._rov_link_wait_message = f"{self.stream_name}\nWaiting for ROV heartbeat..."
            else:
                self._rov_link_wait_message = f"{self.stream_name}\nROV link lost.\nWaiting for heartbeat..."
            if self._rov_link_lost:
                self._show_message(self._rov_link_wait_message)
                return
            self._rov_link_lost = True
            self._restart_stream(
                self._rov_link_wait_message,
                retry_delay_s=0.0,
            )
            return
        if status_key == "OK" and self._rov_link_lost:
            self._rov_link_lost = False
            self._rov_link_wait_message = f"{self.stream_name}\nROV link lost.\nWaiting for heartbeat..."
            self._restart_stream(
                f"{self.stream_name}\nROV heartbeat recovered.\nReconnecting video...",
                retry_delay_s=0.1,
            )

    def _tick(self):
        now = time.time()

        if self.worker is not None:
            try:
                frame = self.worker.take_latest_frame()
            except Exception:
                frame = None
            if frame is not None:
                self._on_frame(frame)

        if self._state == "playing":
            # Detect stall (no frames recently)
            if self.last_frame_ts > 0:
                if (now - self.last_frame_ts) > self._stall_timeout_s:
                    self._restart_stream()
            else:
                # Connected but never got a first frame
                if self._connected_ts > 0 and (now - self._connected_ts) > self._first_frame_timeout_s:
                    self._restart_stream()
        else:
            # If we haven't received frames yet, we still want to retry connect.
            if self._rov_link_lost:
                self._show_message(f"{self.stream_name}\nROV link lost.\nWaiting for heartbeat...")
            elif now >= self._next_retry_ts:
                self._start_connect()

        self._refresh_capture_indicators()

    # --- frames ---
    def _on_frame(self, frame: np.ndarray):
        self.last_frame = frame
        self.last_frame_ts = time.time()

        try:
            if self.frame_buffer.maxlen != 1:
                self.frame_buffer = deque(maxlen=1)
            self.frame_buffer.append(frame)
        except Exception:
            pass
        if self._rec is not None:
            self._rec.add_frame(frame)

        # Clear any status text (pixmap will be shown instead).
        try:
            if self.label.text():
                self.label.setText("")
        except Exception:
            pass

        self._render_frame(frame)

    def _render_frame(self, frame: np.ndarray) -> None:
        h, w, ch = frame.shape
        bytes_per_line = ch * w
        image = QImage(frame.data, w, h, bytes_per_line, QImage.Format.Format_BGR888)
        dpr = max(1.0, float(self.devicePixelRatioF()))
        target_w = max(1, int(self.label.width() * dpr))
        target_h = max(1, int(self.label.height() * dpr))
        pix = QPixmap.fromImage(image).scaled(
            target_w,
            target_h,
            Qt.AspectRatioMode.KeepAspectRatioByExpanding,
            Qt.TransformationMode.FastTransformation,
        )
        pix.setDevicePixelRatio(dpr)
        self.label.setPixmap(pix)

    def refresh_layout_geometry(self) -> None:
        self._layout_capture_badges()
        if self.last_frame is None:
            return
        try:
            self._render_frame(self.last_frame)
        except Exception:
            pass

    def mouseDoubleClickEvent(self, event):
        """Allow the operator to force a reconnect on the active stream."""
        try:
            self._rov_link_lost = False
            self._restart_stream(
                f"{self.stream_name}\nManual reconnect requested...",
                retry_delay_s=0.1,
            )
        except Exception:
            pass
        super().mouseDoubleClickEvent(event)

    # --- recording / snapshot ---
    def start_recording(self, out_dir: str | None = None, basename: str | None = None, fps: float = 30.0) -> str:
        """Start recording the currently displayed stream.

        Returns the output path (mp4 file when available; otherwise a frames directory).
        """
        if self._rec is not None:
            target = self._rec.target
            return str(target) if target is not None else str(Path(out_dir or DEFAULT_RECORDINGS_DIR) / f"{self.stream_name}.mp4")

        if out_dir is None:
            out_dir = str(DEFAULT_RECORDINGS_DIR)
        Path(out_dir).mkdir(parents=True, exist_ok=True)

        if basename is None:
            base = timestamped_camera_stem(self.stream_name, "video")
        else:
            base = Path(basename).stem or self.stream_name

        out_file = unique_capture_path(out_dir, base, ".mp4")

        self._rec = VideoRecorder(out_file, fps=fps)
        target = self._rec.start()
        self._record_started_ts = time.time()
        self._refresh_capture_indicators()
        return str(target)

    def stop_recording(self) -> None:
        rec = self._rec
        if rec is not None:
            self._rec = None
            self._record_started_ts = None
            self._refresh_capture_indicators()

            def _finish_recording() -> None:
                try:
                    rec.stop()
                except Exception as exc:
                    logger.warning("Video recording finalization failed for '%s': %s", self.stream_name, exc)

            try:
                threading.Thread(
                    target=_finish_recording,
                    name=f"video-rec-stop-{self.stream_name}",
                ).start()
            except Exception:
                _finish_recording()

    def save_snapshot(self, out_dir: str | None = None, basename: str | None = None) -> str | None:
        """Save the most recent frame as a PNG. Returns path or None if no frame yet."""
        if self.last_frame is None:
            return None
        try:
            frame = np.array(self.last_frame, copy=True)
        except Exception:
            return None
        if out_dir is None:
            out_dir = str(DEFAULT_RECORDINGS_DIR)
        Path(out_dir).mkdir(parents=True, exist_ok=True)

        if basename is None:
            base = timestamped_camera_stem(self.stream_name, "snapshot")
        else:
            base = Path(basename).stem or self.stream_name

        out_path = unique_capture_path(out_dir, base, ".png")

        def _write_snapshot() -> None:
            try:
                save_snapshot(frame, out_path)
            except Exception as exc:
                logger.warning("Snapshot write failed for '%s' -> %s: %s", self.stream_name, out_path, exc)

        try:
            threading.Thread(
                target=_write_snapshot,
                name=f"video-snapshot-{self.stream_name}",
            ).start()
        except Exception:
            try:
                _write_snapshot()
            except Exception:
                return None

        self._flash_snapshot_indicator("SNAP")
        return str(out_path)

    # --- lifecycle ---
    def shutdown(self, release_only: bool = True, *, async_release: bool = True):
        """Stop decode/recording and release underlying stream resources.

        release_only:
          - True (default): stop threads/camera but keep timers running so we can reconnect
          - False: used internally to reset state (same effect here)
        """
        self._stop_worker_only()
        try:
            self.stop_recording()
        except Exception:
            pass

        # Stop any in-flight connect attempt. The worker can be inside the
        # video RPC timeout path; deleting a running QThread can terminate the
        # process on Windows/PyQt, so wait for the bounded RPC attempt to exit.
        if self._connect_worker is not None:
            worker = self._connect_worker
            self._connect_worker = None
            if async_release:
                _abandon_connect_worker(worker)
            else:
                try:
                    worker.quit()
                    worker.wait(5000)
                except Exception:
                    pass

        if self.camera is not None:
            camera = self.camera
            self.camera = None
            released = False
            try:
                close_async = getattr(self.manager, "close_async", None)
                if async_release and callable(close_async):
                    released = bool(close_async(self.stream_name))
            except Exception:
                released = False
            if not released:
                try:
                    # Prefer manager.close to keep its bookkeeping consistent.
                    self.manager.close(self.stream_name)
                    released = True
                except Exception:
                    released = False
            if not released:
                try:
                    camera.release()
                except Exception:
                    pass

        # Reset label if we have no pixmap
        if release_only:
            self.label.setPixmap(QPixmap())
            if self._state != "playing":
                # keep whatever error text we already set
                pass

        self._refresh_capture_indicators()

    def closeEvent(self, event):
        try:
            self._tick_timer.stop()
        except Exception:
            pass
        self.shutdown(release_only=True)
        super().closeEvent(event)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._layout_capture_badges()
