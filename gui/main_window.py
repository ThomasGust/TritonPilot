# gui/main_window.py
from __future__ import annotations

import os
from pathlib import Path

from PyQt6.QtCore import pyqtSignal, QObject, Qt, QTimer
from PyQt6.QtGui import QAction
from PyQt6.QtWidgets import (
    QMainWindow,
    QWidget,
    QHBoxLayout,
    QLabel,
)
from config import (
    PILOT_PUB_ENDPOINT,
    SENSOR_SUB_ENDPOINT,
    CONTROLLER_DEADZONE,
)
from input.pilot_service import PilotPublisherService
from telemetry.sensor_service import SensorSubscriberService
from video.cam import RemoteCameraManager
from recording.stream_recorder import StreamRecorder
from gui.video_tabs import VideoTabs
from gui.sensor_panel import SensorPanel

class MainWindow(QMainWindow):
    # we'll receive sensor messages from a background thread → emit to UI thread
    sensor_msg_sig = pyqtSignal(dict)
    pilot_status_sig = pyqtSignal(dict)

    def __init__(self, streams_path: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("ROV Topside (PyQt6)")

        # link status
        self._last_sensor_ts = 0.0
        self._last_hb_ts = 0.0
        self._last_hb = {}

        self._link_lbl = QLabel("Link: (no data)")
        self.statusBar().addPermanentWidget(self._link_lbl)

        self._ctrl_lbl = QLabel("Controller: (starting)")
        self.statusBar().addPermanentWidget(self._ctrl_lbl)

        self._video_lbl = QLabel("Video: -")
        self.statusBar().addPermanentWidget(self._video_lbl)

        self._link_timer = QTimer(self)
        self._link_timer.timeout.connect(self._update_link_status)
        self._link_timer.start(200)


        # connect signals to slots
        self.sensor_msg_sig.connect(self._handle_sensor_msg_on_ui)
        self.pilot_status_sig.connect(self._handle_pilot_status_on_ui)
        self._last_ctrl_status: dict = {'controller': 'unknown'}

        # 1) pilot publisher (xbox -> ROV)
        self.pilot_svc = PilotPublisherService(
            endpoint=PILOT_PUB_ENDPOINT,
            rate_hz=30.0,
            deadzone=CONTROLLER_DEADZONE,
            debug=False,
            on_status=self._on_pilot_status_from_thread,
        )
        self.pilot_svc.start()

        # optional stream recorder (pilot + sensors + heartbeat)
        self._stream_recorder: StreamRecorder | None = None
        self._record_dir: str | None = None

        # 2) sensor subscriber (ROV -> topside)
        self.sensor_panel = SensorPanel()
        self.sensor_svc = SensorSubscriberService(
            endpoint=SENSOR_SUB_ENDPOINT,
            on_message=self._on_sensor_msg_from_thread,
            debug=False,
        )
        self.sensor_svc.start()

        # 3) video (failsafe: GUI should boot even if ROV/video isn't available yet)
        self.cam_mgr = None
        self.video_panel = None
        try:
            if not os.path.exists(streams_path):
                # Don't block startup; just disable video.
                self.statusBar().showMessage(f"Streams config not found: {streams_path}", 10000)
            else:
                self.cam_mgr = RemoteCameraManager(streams_path)
                stream_names = self.cam_mgr.list_available()
                if stream_names:
                    self.video_panel = VideoTabs(self.cam_mgr, stream_names=stream_names)
                else:
                    self.statusBar().showMessage("No enabled video streams in streams.json", 8000)
        except Exception as e:
            self.cam_mgr = None
            self.video_panel = None
            self.statusBar().showMessage(f"Video init failed (continuing without video): {e}", 12000)

        # layout
        central = QWidget()
        outer = QHBoxLayout(central)
        if self.video_panel is not None:
            outer.addWidget(self.video_panel, 2)
        outer.addWidget(self.sensor_panel, 1)
        self.setCentralWidget(central)

        self._make_menu()

        self.resize(1200, 700)

    # background → UI
    def _on_sensor_msg_from_thread(self, msg: dict):
        # called in sensor thread
        if self._stream_recorder is not None:
            self._stream_recorder.record("sensors", msg)
        self.sensor_msg_sig.emit(msg)


    def _on_pilot_status_from_thread(self, status: dict):
        # Called from the pilot publisher thread; marshal to UI thread.
        self.pilot_status_sig.emit(status)

    def _handle_pilot_status_on_ui(self, status: dict):
        self._last_ctrl_status = status or {'controller': 'unknown'}
        state = (status or {}).get('controller', 'unknown')
        if state == 'connected':
            name = (status or {}).get('name') or 'controller'
            self._ctrl_lbl.setText(f"Controller: OK ({name})")
        elif state == 'disconnected':
            err = (status or {}).get('error') or 'not connected'
            self._ctrl_lbl.setText(f"Controller: - ({err})")
        elif state == 'stopped':
            self._ctrl_lbl.setText("Controller: stopped")
        else:
            self._ctrl_lbl.setText(f"Controller: {state}")

    def _handle_sensor_msg_on_ui(self, msg: dict):
        import time
        if msg.get("sensor") == "heartbeat" or msg.get("type") == "heartbeat":
            self._last_hb_ts = time.time()
            self._last_hb = msg
        else:
            self._last_sensor_ts = time.time()

        self.sensor_panel.upsert_sensor(msg)

    def _update_link_status(self):
        import time
        now = time.time()

        # Prefer heartbeat if present, fall back to any sensor traffic.
        hb_age = None
        if self._last_hb_ts > 0:
            hb_age = now - self._last_hb_ts
        sensor_age = None
        if self._last_sensor_ts > 0:
            sensor_age = now - self._last_sensor_ts

        # Determine link state from heartbeat when available.
        age = hb_age if hb_age is not None else sensor_age
        if age is None:
            status = "NO DATA"
        elif age < 0.5:
            status = "OK"
        elif age < 2.0:
            status = "WARN"
        else:
            status = "LOST"

        parts = [f"Link: {status}"]
        if hb_age is not None:
            armed = bool(self._last_hb.get("armed", False))
            pilot_age = self._last_hb.get("pilot_age", None)
            if pilot_age is not None:
                parts.append(f"pilot_age={pilot_age:.2f}s")
            parts.append("ARMED" if armed else "disarmed")
        elif sensor_age is not None:
            parts.append(f"sensor_age={sensor_age:.2f}s")

        self._link_lbl.setText(" | ".join(parts))

        # Video indicator (show per-stream state; do not throw on missing video)
        try:
            if self.video_panel is None:
                self._video_lbl.setText("Video: -")
            else:
                name = self.video_panel.current_stream_name()
                vw = self.video_panel.current_video_widget()
                if name is None or vw is None:
                    self._video_lbl.setText("Video: -")
                else:
                    st = vw.status()
                    if st.get("state") == "playing":
                        age = st.get("age_s")
                        if age is not None:
                            self._video_lbl.setText(f"Video: {name} (OK, age={age:.1f}s)")
                        else:
                            self._video_lbl.setText(f"Video: {name} (OK)")
                    elif st.get("state") == "waiting":
                        self._video_lbl.setText(f"Video: {name} (waiting)")
                    else:
                        self._video_lbl.setText(f"Video: {name} ({st.get('state')})")
        except Exception:
            self._video_lbl.setText("Video: -")

    def _make_menu(self):
        bar = self.menuBar()
        file_menu = bar.addMenu("&File")
        rec_menu = bar.addMenu("&Record")

        # Stream log (JSONL)
        start_log = QAction("Start Stream Log", self)
        start_log.triggered.connect(self._start_stream_log)
        rec_menu.addAction(start_log)

        stop_log = QAction("Stop Stream Log", self)
        stop_log.triggered.connect(self._stop_stream_log)
        rec_menu.addAction(stop_log)

        rec_menu.addSeparator()

        snap_act = QAction("Save Snapshot", self)
        snap_act.triggered.connect(self._save_snapshot)
        rec_menu.addAction(snap_act)

        start_vid = QAction("Start Video Recording", self)
        start_vid.triggered.connect(self._start_video_recording)
        rec_menu.addAction(start_vid)

        stop_vid = QAction("Stop Video Recording", self)
        stop_vid.triggered.connect(self._stop_video_recording)
        rec_menu.addAction(stop_vid)

        quit_act = QAction("Quit", self)
        quit_act.triggered.connect(self.close)
        file_menu.addAction(quit_act)

    def closeEvent(self, event):
        try:
            if self.video_panel is not None:
                self.video_panel.stop_all()
        except Exception:
            pass
        # stop recorders
        try:
            self._stop_stream_log()
        except Exception:
            pass

        # stop services
        try:
            self.sensor_svc.stop()
        except Exception:
            pass
        try:
            self.pilot_svc.stop()
        except Exception:
            pass
        if self.video_panel is not None:
            self.video_panel.close()
        super().closeEvent(event)
    def _start_stream_log(self):
        if self._stream_recorder is not None:
            return
        session_dir = StreamRecorder.make_session_dir("recordings")
        self._record_dir = str(session_dir)
        self._stream_recorder = StreamRecorder(session_dir / "streams.jsonl")
        self._stream_recorder.start()

        # record pilot frames via callback
        try:
            self.pilot_svc.on_send = lambda msg: self._stream_recorder.record("pilot", msg)  # type: ignore
        except Exception:
            pass

        self.statusBar().showMessage(f"Recording streams → {self._record_dir}", 5000)

    def _stop_stream_log(self):
        if self._stream_recorder is None:
            return
        try:
            self.pilot_svc.on_send = None  # type: ignore
        except Exception:
            pass
        self._stream_recorder.stop()
        self._stream_recorder = None
        self.statusBar().showMessage("Stream recording stopped", 3000)
    def _current_video_widget(self):
        if self.video_panel is None:
            return None
        try:
            return self.video_panel.current_video_widget()
        except Exception:
            return None

    def _save_snapshot(self):
        if self.video_panel is None:
            return
        out_dir = self._record_dir or str(StreamRecorder.make_session_dir("recordings"))
        path = self.video_panel.save_snapshot(out_dir=out_dir)
        if path:
            self.statusBar().showMessage(f"Saved snapshot: {path}", 5000)
        else:
            self.statusBar().showMessage("No frame yet (snapshot not saved)", 3000)

    def _start_video_recording(self):
        vw = self._current_video_widget()
        if vw is None:
            return
        out_dir = self._record_dir or str(StreamRecorder.make_session_dir("recordings"))
        vw.start_recording(out_dir=out_dir, basename=vw.stream_name, fps=30.0)
        self.statusBar().showMessage(f"Video recording started → {out_dir}", 5000)

    def _stop_video_recording(self):
        vw = self._current_video_widget()
        if vw is None:
            return
        vw.stop_recording()
        self.statusBar().showMessage("Video recording stopped", 3000)


