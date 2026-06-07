import os
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from gui.direct_gst_video_widget import DirectGstVideoWidget, DirectReceiverConfig, build_direct_receiver_cmd
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
        self.frames = 0
        self.stopped = False

    def start(self):
        self.out_path.parent.mkdir(parents=True, exist_ok=True)
        self.out_path.touch()
        return self.target

    def add_frame(self, frame):
        self.frames += 1

    def stop(self):
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
        target = widget.start_recording(out_dir=str(tmp_path), basename="primary", fps=10.0)
        assert target is not None
        assert widget.is_recording() is True
        assert manager.opened == 1

        snap_path = widget.save_snapshot(out_dir=str(tmp_path), basename="snap")
        deadline = time.monotonic() + 1.0
        while snap_path and not Path(snap_path).exists() and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)

        assert snap_path is not None
        assert Path(snap_path).exists()
        assert widget._snapshot_badge.isVisible() is True

        widget.stop_recording()
        app.processEvents()
        assert widget.is_recording() is False
    finally:
        widget.close()
        widget.deleteLater()
        app.processEvents()
