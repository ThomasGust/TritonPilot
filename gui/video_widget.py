# gui/video_widget.py
from __future__ import annotations
import time

from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QImage, QPixmap
from PyQt6.QtWidgets import QWidget, QLabel, QVBoxLayout, QSizePolicy

import numpy as np

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

    def closeEvent(self, event):
        if self.worker:
            self.worker.stop()
        if self.camera:
            self.camera.release()
        super().closeEvent(event)
