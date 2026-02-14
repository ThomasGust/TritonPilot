# gui/video_widget.py
from __future__ import annotations

import time
from pathlib import Path

from PyQt6.QtCore import Qt, QThread, pyqtSignal, QTimer
from PyQt6.QtGui import QImage, QPixmap
from PyQt6.QtWidgets import QWidget, QLabel, QVBoxLayout, QSizePolicy

import numpy as np

from recording.video_recorder import VideoRecorder, save_snapshot
from video.cam import RemoteCameraManager, RemoteCv2Camera


class _VideoWorker(QThread):
    """Reads frames from RemoteCv2Camera at a target FPS."""
    frame_ready = pyqtSignal(object)   # emits numpy array

    def __init__(self, camera: RemoteCv2Camera, parent=None, fps: float = 30.0):
        super().__init__(parent)
        self.camera = camera
        self.period = 1.0 / float(fps)
        self._running = True

    def run(self):
        while self._running:
            t0 = time.time()
            ok, frame = self.camera.read()
            if ok and frame is not None:
                self.frame_ready.emit(frame)
            dt = time.time() - t0
            sleep_for = self.period - dt
            if sleep_for > 0:
                QThread.msleep(int(sleep_for * 1000))

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


class VideoWidget(QWidget):
    """Video display for a single ROV stream, with failsafe reconnection.

    Goals:
      - GUI starts even if ROV is off / video service isn't running yet
      - No modal popups
      - Only selected stream decodes (handled by VideoTabs)
      - Auto-retry connect and auto-recover from stalls
    """

    def __init__(self, manager: RemoteCameraManager, stream_name: str, parent=None):
        super().__init__(parent)
        self.manager = manager
        self.stream_name = stream_name

        self.camera: RemoteCv2Camera | None = None
        self.worker: _VideoWorker | None = None
        self._connect_worker: _ConnectWorker | None = None

        self.last_frame: np.ndarray | None = None
        self.last_frame_ts: float = 0.0
        self._rec: VideoRecorder | None = None

        # state
        self._state: str = "waiting"  # waiting|connecting|playing|stalled
        self._last_error: str | None = None
        self._retry_backoff_s: float = 0.5
        self._next_retry_ts: float = 0.0
        self._stall_timeout_s: float = 2.0

        self.label = QLabel(f"{self.stream_name}\nWaiting for stream…")
        self.label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.label.setWordWrap(True)
        self.label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.label.setMinimumSize(320, 240)

        lay = QVBoxLayout(self)
        lay.addWidget(self.label)

        self._tick_timer = QTimer(self)
        self._tick_timer.timeout.connect(self._tick)
        self._tick_timer.start(250)

        # kick off first attempt immediately
        self._schedule_retry(0.0)

    # --- public helpers for MainWindow / status bar ---
    def status(self) -> dict:
        age = None
        if self.last_frame_ts > 0:
            age = max(0.0, time.time() - self.last_frame_ts)
        return {
            "state": self._state,
            "age_s": age,
            "last_error": self._last_error,
        }

    # --- connection / recovery ---
    def _schedule_retry(self, delay_s: float):
        self._next_retry_ts = time.time() + max(0.0, float(delay_s))

    def _start_connect(self):
        if self._connect_worker is not None and self._connect_worker.isRunning():
            return

        self._state = "connecting"
        self.label.setText(f"{self.stream_name}\nConnecting…")
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

        # If the ROV reported recovery actions (e.g., USB rebind), surface them briefly.
        notices = []
        try:
            notices = list(getattr(self.camera, "start_messages", []) or [])
        except Exception:
            notices = []

        if notices:
            # Keep it short so it fits in the widget before the first frame arrives.
            tail = notices[-3:]
            self.label.setText(
                f"{self.stream_name}\nConnected (ROV recovery):\n" + "\n".join(tail) + "\n\nWaiting for frames…"
            )
        else:
            self.label.setText(f"{self.stream_name}\nWaiting for frames…")

        self.worker = _VideoWorker(self.camera, fps=30.0)
        self.worker.frame_ready.connect(self._on_frame)
        self.worker.start()

    def _on_connect_failed(self, err: str):
        self._last_error = err
        self._state = "waiting"

        # Exponential backoff, capped
        self._retry_backoff_s = min(self._retry_backoff_s * 1.5, 5.0)
        self._schedule_retry(self._retry_backoff_s)

        # Friendly message
        msg = f"{self.stream_name}\nStream not available. Retrying…\n\n{err}"
        self.label.setText(msg)

    def _stop_worker_only(self):
        if self.worker:
            try:
                self.worker.stop()
            except Exception:
                pass
            self.worker = None

    def _restart_stream(self):
        self._state = "stalled"
        self.label.setText(f"{self.stream_name}\nReconnecting…")
        self.shutdown(release_only=False)  # keep widget alive
        self._retry_backoff_s = 0.5
        self._schedule_retry(0.2)

    def _tick(self):
        now = time.time()

        if self._state == "playing":
            # Detect stall (no frames recently)
            if self.last_frame_ts > 0 and (now - self.last_frame_ts) > self._stall_timeout_s:
                self._restart_stream()
        else:
            # If we haven't received frames yet, we still want to retry connect.
            if now >= self._next_retry_ts:
                self._start_connect()

    # --- frames ---
    def _on_frame(self, frame: np.ndarray):
        self.last_frame = frame
        self.last_frame_ts = time.time()
        if self._rec is not None:
            self._rec.add_frame(frame)

        h, w, ch = frame.shape
        bytes_per_line = ch * w
        image = QImage(frame.data, w, h, bytes_per_line, QImage.Format.Format_BGR888)
        pix = QPixmap.fromImage(image).scaled(
            self.label.width(),
            self.label.height(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self.label.setPixmap(pix)

    # --- recording / snapshot ---
    def start_recording(self, out_dir: str | None = None, basename: str | None = None, fps: float = 30.0) -> str:
        """Start recording the currently displayed stream."""
        if out_dir is None:
            out_dir = str(Path("recordings"))
        Path(out_dir).mkdir(parents=True, exist_ok=True)
        if basename is None:
            basename = f"{self.stream_name}_{time.strftime('%Y%m%d-%H%M%S')}.mp4"
        out_path = str(Path(out_dir) / basename)

        self._rec = VideoRecorder(out_path, fps=fps)
        self._rec.start()
        return out_path

    def stop_recording(self) -> None:
        if self._rec is not None:
            try:
                self._rec.stop()
            finally:
                self._rec = None

    def save_snapshot(self, out_dir: str | None = None, basename: str | None = None) -> str | None:
        """Save the most recent frame as a PNG. Returns path or None if no frame yet."""
        if self.last_frame is None:
            return None
        if out_dir is None:
            out_dir = str(Path("recordings"))
        Path(out_dir).mkdir(parents=True, exist_ok=True)
        if basename is None:
            basename = f"{self.stream_name}_{time.strftime('%Y%m%d-%H%M%S')}.png"
        out_path = str(Path(out_dir) / basename)
        save_snapshot(self.last_frame, out_path)
        return out_path

    # --- lifecycle ---
    def shutdown(self, release_only: bool = True):
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

        # Stop any in-flight connect attempt
        if self._connect_worker is not None:
            try:
                self._connect_worker.quit()
                self._connect_worker.wait(200)
            except Exception:
                pass
            self._connect_worker = None

        if self.camera is not None:
            try:
                # Prefer manager.close to keep its bookkeeping consistent
                try:
                    self.manager.close(self.stream_name)
                except Exception:
                    self.camera.release()
            except Exception:
                pass
            self.camera = None

        # Reset label if we have no pixmap
        if release_only:
            self.label.setPixmap(QPixmap())
            if self._state != "playing":
                # keep whatever error text we already set
                pass

    def closeEvent(self, event):
        try:
            self._tick_timer.stop()
        except Exception:
            pass
        self.shutdown(release_only=True)
        super().closeEvent(event)
