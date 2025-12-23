# gui/main_window.py
from __future__ import annotations

import os
from pathlib import Path

from PyQt6.QtCore import pyqtSignal, QObject, Qt
from PyQt6.QtGui import QAction
from PyQt6.QtWidgets import (
    QMainWindow,
    QWidget,
    QHBoxLayout,
    QMessageBox,
)
from config import (
    PILOT_PUB_ENDPOINT,
    SENSOR_SUB_ENDPOINT,
)
from input.pilot_service import PilotPublisherService
from telemetry.sensor_service import SensorSubscriberService
from video.cam import RemoteCameraManager
from gui.video_widget import VideoWidget
from gui.sensor_panel import SensorPanel

class MainWindow(QMainWindow):
    # we'll receive sensor messages from a background thread → emit to UI thread
    sensor_msg_sig = pyqtSignal(dict)

    def __init__(self, streams_path: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("ROV Topside (PyQt6)")

        # connect signal to slot
        self.sensor_msg_sig.connect(self._handle_sensor_msg_on_ui)

        # 1) pilot publisher (xbox -> ROV)
        self.pilot_svc = PilotPublisherService(
            endpoint=PILOT_PUB_ENDPOINT,
            rate_hz=30.0,
            deadzone=0.1,
            debug=False,
        )
        self.pilot_svc.start()

        # 2) sensor subscriber (ROV -> topside)
        self.sensor_panel = SensorPanel()
        self.sensor_svc = SensorSubscriberService(
            endpoint=SENSOR_SUB_ENDPOINT,
            on_message=self._on_sensor_msg_from_thread,
            debug=False,
        )
        self.sensor_svc.start()

        # 3) video
        if not os.path.exists(streams_path):
            QMessageBox.critical(self, "Error", f"Streams config not found:\n{streams_path}")
            streams_path = str(Path("data") / "streams.json")
        self.cam_mgr = RemoteCameraManager(streams_path)
        stream_names = self.cam_mgr.list_available()
        if stream_names:
            self.video_widget = VideoWidget(self.cam_mgr, stream_name=stream_names[0])
        else:
            self.video_widget = None

        # layout
        central = QWidget()
        outer = QHBoxLayout(central)
        if self.video_widget is not None:
            outer.addWidget(self.video_widget, 2)
        outer.addWidget(self.sensor_panel, 1)
        self.setCentralWidget(central)

        self._make_menu()

        self.resize(1200, 700)

    # background → UI
    def _on_sensor_msg_from_thread(self, msg: dict):
        self.sensor_msg_sig.emit(msg)

    def _handle_sensor_msg_on_ui(self, msg: dict):
        self.sensor_panel.upsert_sensor(msg)

    def _make_menu(self):
        bar = self.menuBar()
        file_menu = bar.addMenu("&File")

        quit_act = QAction("Quit", self)
        quit_act.triggered.connect(self.close)
        file_menu.addAction(quit_act)

    def closeEvent(self, event):
        # stop services
        try:
            self.sensor_svc.stop()
        except Exception:
            pass
        try:
            self.pilot_svc.stop()
        except Exception:
            pass
        if self.video_widget is not None:
            self.video_widget.close()
        super().closeEvent(event)
