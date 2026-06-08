import os
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication

from gui.direct_gst_video_widget import (
    DirectGstVideoWidget,
    DirectReceiverConfig,
    _looks_like_green_startup_artifact,
    build_direct_receiver_cmd,
)
from gui.video_tabs import VideoTabs


def test_direct_h264_receiver_renders_with_direct3d_without_raw_pipe():
    cmd = build_direct_receiver_cmd(
        "gst-launch-1.0",
        DirectReceiverConfig(
            name="Primary Camera",
            codec="h264",
            port=5000,
            bind_address="192.168.1.1",
            latency_ms=5,
            h264_decoder="decodebin",
        ),
    )

    assert "rtph264depay" in cmd
    assert "decodebin" in cmd
    assert "d3d11videosink" in cmd
    assert "fdsink" not in cmd
    assert "video/x-raw,format=BGR" not in cmd
    assert "sync=false" in cmd
    assert "async=false" in cmd
    assert "leaky=downstream" in cmd


def test_direct_jpeg_receiver_uses_direct3d_sink():
    cmd = build_direct_receiver_cmd(
        "gst-launch-1.0",
        DirectReceiverConfig(
            name="Primary Camera",
            codec="jpeg",
            port=5000,
            bind_address="192.168.1.1",
        ),
    )

    assert "rtpjpegdepay" in cmd
    assert "jpegdec" in cmd
    assert "d3d11videosink" in cmd
    assert "fdsink" not in cmd


def test_video_tabs_selects_direct_widget_for_direct3d_stream():
    class _Manager:
        stream_defs = {
            "Primary Camera": {"render_mode": "direct3d"},
            "Aux Camera": {},
        }

    tabs = VideoTabs.__new__(VideoTabs)
    tabs.manager = _Manager()

    assert tabs._widget_class_for_stream("Primary Camera").__name__ == "DirectGstVideoWidget"
    assert tabs._widget_class_for_stream("Aux Camera").__name__ == "VideoWidget"


def _app() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


class _FakeCaptureCamera:
    def __init__(self):
        self.packet = SimpleNamespace(
            frame_bgr=np.zeros((16, 24, 3), dtype=np.uint8),
            seq=1,
            monotonic_ts=time.monotonic(),
            wall_ts=time.time(),
        )

    def latest_frame_packet(self):
        return self.packet

    def read_frame_packet(self):
        return self.packet


class _FakeManager:
    def __init__(self):
        self.capture = _FakeCaptureCamera()
        self.opened = 0
        self.closed = 0

    def open_capture(self, name):
        self.opened += 1
        return self.capture

    def close_capture(self, name):
        self.closed += 1


class _FakeRecorder:
    def __init__(self, out_path, fps=30.0):
        self.out_path = Path(out_path)
        self.fps = float(fps)
        self.target = self.out_path
        self.frames = []
        self.stopped = False
        self.stop_timeout_s = None
        self.stop_drain_pending = None

    def start(self):
        self.out_path.parent.mkdir(parents=True, exist_ok=True)
        self.out_path.touch()
        return self.target

    def add_frame(self, frame):
        self.frames.append(np.array(frame, copy=True))
        return True

    def stop(self, timeout_s=5.0, *, drain_pending=True):
        self.stop_timeout_s = timeout_s
        self.stop_drain_pending = drain_pending
        self.stopped = True


def test_direct_widget_snapshot_and_recording_use_capture_receiver(monkeypatch, tmp_path):
    app = _app()
    monkeypatch.setattr("gui.direct_gst_video_widget.DirectGstVideoWidget._start_connect", lambda self: None)
    monkeypatch.setattr("gui.direct_gst_video_widget.VideoRecorder", _FakeRecorder)

    def _fake_save_snapshot(frame, out_path):
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        Path(out_path).touch()

    monkeypatch.setattr("gui.direct_gst_video_widget.save_snapshot", _fake_save_snapshot)

    manager = _FakeManager()
    widget = DirectGstVideoWidget(manager, "Primary Camera")
    widget.resize(320, 180)
    widget.show()
    try:
        app.processEvents()
        assert widget._record_label_timer.timerType() == Qt.TimerType.PreciseTimer
        assert widget._record_label_timer.isSingleShot() is True

        target = widget.start_recording(out_dir=str(tmp_path), basename="primary", fps=10.0)
        assert target is not None
        assert widget.is_recording() is True
        assert manager.opened == 1
        assert widget._record_badge.text() == "REC 00:00"

        widget._record_started_monotonic_s = time.monotonic() - 34.2
        widget._on_record_label_tick()
        assert widget._record_badge.text() == "REC 00:34"

        widget._record_started_monotonic_s = time.monotonic() - 29.0
        widget._refresh_capture_indicators()
        assert widget._record_badge.text() == "REC 00:34"

        snap_path = widget.save_snapshot(out_dir=str(tmp_path), basename="snap")
        deadline = time.monotonic() + 1.0
        while snap_path and not Path(snap_path).exists() and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)

        assert snap_path is not None
        assert Path(snap_path).exists()
        assert widget._snapshot_badge.parent() is widget._capture_overlay
        assert widget._record_badge.parent() is widget._capture_overlay
        assert widget._capture_overlay.isWindow() is True
        assert widget._capture_overlay.isVisible() is True
        assert widget._snapshot_badge.isVisible() is True

        rec = widget._rec
        widget.stop_recording()
        deadline = time.monotonic() + 1.0
        while rec is not None and not rec.stopped and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)
        assert widget.is_recording() is False
        assert rec is not None
        assert rec.stopped is True
        assert rec.stop_timeout_s == 10.0
        assert rec.stop_drain_pending is True
    finally:
        widget.close()
        widget.deleteLater()
        app.processEvents()


def test_direct_recording_skips_green_startup_artifact_frames(monkeypatch, tmp_path):
    app = _app()
    monkeypatch.setattr("gui.direct_gst_video_widget.DirectGstVideoWidget._start_connect", lambda self: None)
    monkeypatch.setattr("gui.direct_gst_video_widget.VideoRecorder", _FakeRecorder)

    green = np.zeros((24, 32, 3), dtype=np.uint8)
    green[:, :, 1] = 72
    normal = np.zeros((24, 32, 3), dtype=np.uint8)
    normal[:, :, 0] = 80
    normal[:, :, 1] = 78
    normal[:, :, 2] = 84
    normal[::2, ::2, :] = 150

    assert _looks_like_green_startup_artifact(green) is True
    assert _looks_like_green_startup_artifact(normal) is False

    class _PacketCamera:
        def __init__(self):
            self._idx = 0
            self._frames = [green, green, green, normal, normal]

        def latest_frame_packet(self):
            frame = self._frames[min(self._idx, len(self._frames) - 1)]
            self._idx += 1
            return SimpleNamespace(
                frame_bgr=frame,
                seq=self._idx,
                monotonic_ts=time.monotonic(),
                wall_ts=time.time(),
            )

        def read_frame_packet(self):
            return self.latest_frame_packet()

    manager = _FakeManager()
    manager.capture = _PacketCamera()
    widget = DirectGstVideoWidget(manager, "Primary Camera")
    try:
        app.processEvents()
        target = widget.start_recording(out_dir=str(tmp_path), basename="primary", fps=30.0)
        assert target is not None
        rec = widget._rec
        deadline = time.monotonic() + 1.0
        while rec is not None and not rec.frames and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)

        assert rec is not None
        assert rec.frames
        assert _looks_like_green_startup_artifact(rec.frames[0]) is False
    finally:
        widget.close()
        widget.deleteLater()
        app.processEvents()


def test_direct_widget_async_shutdown_does_not_wait_for_connect_worker(monkeypatch):
    app = _app()
    monkeypatch.setattr("gui.direct_gst_video_widget.DirectGstVideoWidget._start_connect", lambda self: None)
    stopped = []
    monkeypatch.setattr("gui.direct_gst_video_widget._stop_process_async", lambda proc, grace_s=0.05: stopped.append(proc))

    class _FakeProc:
        pid = 12345

        def poll(self):
            return None

    class _FakeWorker:
        def __init__(self):
            self.quit_called = False
            self.wait_called = False
            self.proc = None

        def quit(self):
            self.quit_called = True

        def wait(self, _timeout_ms):
            self.wait_called = True
            return True

        def setParent(self, _parent):
            return None

    manager = _FakeManager()
    widget = DirectGstVideoWidget(manager, "Primary Camera")
    worker = _FakeWorker()
    proc = _FakeProc()
    widget._connect_worker = worker
    widget._connect_attempt_active = True
    widget._proc = proc
    try:
        widget.shutdown(async_release=True)

        assert worker.quit_called is True
        assert worker.wait_called is False
        assert stopped == [proc]
    finally:
        widget.close()
        widget.deleteLater()
        app.processEvents()


def test_direct_widget_can_defer_first_connect_until_heartbeat(monkeypatch):
    app = _app()
    starts = []
    monkeypatch.setattr(
        "gui.direct_gst_video_widget.DirectGstVideoWidget._start_connect",
        lambda self: starts.append(self.stream_name),
    )

    manager = _FakeManager()
    widget = DirectGstVideoWidget(manager, "Primary Camera", autostart=False)
    try:
        app.processEvents()
        assert starts == []
        assert widget._rov_link_lost is True
        assert "Waiting for ROV heartbeat" in widget._message.text()

        widget.set_rov_link_status("OK")

        assert widget._rov_link_lost is False
        assert "heartbeat recovered" in widget._message.text()
        assert widget._next_retry_ts > 0
    finally:
        widget.close()
        widget.deleteLater()
        app.processEvents()


def test_direct_widget_link_loss_clears_renderer_and_recovers(monkeypatch):
    app = _app()
    monkeypatch.setattr("gui.direct_gst_video_widget.DirectGstVideoWidget._start_connect", lambda self: None)
    stopped = []
    monkeypatch.setattr("gui.direct_gst_video_widget._stop_process_async", lambda proc, grace_s=0.05: stopped.append(proc))

    class _FakeProc:
        pid = 23456

        def poll(self):
            return None

    manager = _FakeManager()
    widget = DirectGstVideoWidget(manager, "Primary Camera")
    proc = _FakeProc()
    widget._proc = proc
    widget._state = "playing"
    try:
        widget.set_rov_link_status("LOST")

        assert widget._rov_link_lost is True
        assert widget._proc is None
        assert stopped == [proc]
        assert "ROV link lost" in widget._message.text()
        next_retry_while_lost = widget._next_retry_ts

        widget.set_rov_link_status("OK")

        assert widget._rov_link_lost is False
        assert "heartbeat recovered" in widget._message.text()
        assert widget._next_retry_ts > next_retry_while_lost
    finally:
        widget.close()
        widget.deleteLater()
        app.processEvents()
