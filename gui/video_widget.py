# gui/video_widget.py
from __future__ import annotations
import time
from pathlib import Path

from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QImage, QPixmap
from PyQt6.QtWidgets import QWidget, QLabel, QVBoxLayout, QSizePolicy

import numpy as np

from recording.video_recorder import VideoRecorder, save_snapshot

from video.cam import RemoteCameraManager, RemoteCv2Camera


class _VideoWorker(QThread):
    frame_ready = pyqtSignal(object)   # emits numpy array

    def __init__(self, camera: RemoteCv2Camera, parent=None, fps: float = 30.0):
        super().__init__(parent)
        self.camera = camera
        self.period = 1.0 / fps
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


class VideoWidget(QWidget):
    """
    Simple video display for a single ROV stream.
    """
    def __init__(self, manager: RemoteCameraManager, stream_name: str, parent=None):
        super().__init__(parent)
        self.manager = manager
        self.stream_name = stream_name
        self.camera: RemoteCv2Camera | None = None
        self.worker: _VideoWorker | None = None

        self.last_frame: np.ndarray | None = None
        self.last_frame_ts: float = 0.0
        self._rec: VideoRecorder | None = None

        self.label = QLabel("Connecting…")
        self.label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.label.setMinimumSize(320, 240)

        lay = QVBoxLayout(self)
        lay.addWidget(self.label)

        self._open_stream()

    def _open_stream(self):
        self.camera = self.manager.open(self.stream_name)

        self.worker = _VideoWorker(self.camera, fps=30.0)
        self.worker.frame_ready.connect(self._on_frame)
        self.worker.start()

    def _on_frame(self, frame: np.ndarray):
        self.last_frame = frame
        self.last_frame_ts = time.time()
        if self._rec is not None:
            self._rec.add_frame(frame)

        h, w, ch = frame.shape
        bytes_per_line = ch * w
        # PyQt6: QImage.Format.Format_BGR888
        image = QImage(frame.data, w, h, bytes_per_line, QImage.Format.Format_BGR888)
        pix = QPixmap.fromImage(image).scaled(
            self.label.width(),
            self.label.height(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self.label.setPixmap(pix)


    def start_recording(self, out_dir: str | None = None, basename: str | None = None, fps: float = 30.0) -> str:
        """Start recording the currently displayed stream."""
        if out_dir is None:
            out_dir = str(Path("recordings") / time.strftime("%Y%m%d-%H%M%S"))
        if basename is None:
            basename = f"{self.stream_name}"
        self._rec = VideoRecorder(out_dir=out_dir, basename=basename, fps=fps)
        self._rec.start()
        return out_dir

    def stop_recording(self) -> None:
        if self._rec is not None:
            self._rec.stop()
            self._rec = None

    def save_snapshot(self, out_dir: str | None = None, basename: str | None = None) -> str | None:
        """Save a PNG snapshot of the most recent frame."""
        if self.last_frame is None:
            return None
        if out_dir is None:
            out_dir = str(Path("recordings") / time.strftime("%Y%m%d-%H%M%S"))
        Path(out_dir).mkdir(parents=True, exist_ok=True)
        if basename is None:
            basename = f"{self.stream_name}_{time.strftime('%Y%m%d-%H%M%S')}.png"
        out_path = str(Path(out_dir) / basename)
        save_snapshot(self.last_frame, out_path)
        return out_path

    def closeEvent(self, event):
        if self.worker:
            self.worker.stop()
        self.stop_recording()
        if self.camera:
            self.camera.release()
        super().closeEvent(event)
