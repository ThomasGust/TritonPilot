import os
import base64
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication

from recording.compressed_stream_recorder import (
    H264RtpMp4RecordConfig,
    build_h264_rtp_mp4_record_cmd,
)
from gui.direct_gst_video_widget import (
    DirectGstVideoWidget,
    DirectReceiverConfig,
    _compressed_recording_requested,
    _looks_like_green_startup_artifact,
    _snapshot_frame_pipe_fps,
    _snapshot_frame_pipe_requested,
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
    assert "tee" not in cmd
    assert "rtp_t." not in cmd
    assert "udpsink" not in cmd
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


def test_direct_receiver_can_center_crop_to_square():
    cmd = build_direct_receiver_cmd(
        "gst-launch-1.0",
        DirectReceiverConfig(
            name="Primary Camera",
            codec="h264",
            port=5000,
            bind_address="192.168.1.1",
            width=1920,
            height=1080,
            square_crop=True,
        ),
    )

    assert "videocrop" in cmd
    assert "left=420" in cmd
    assert "right=420" in cmd
    assert cmd.index("videocrop") > cmd.index("videoconvert")
    assert cmd.index("videocrop") < cmd.index("d3d11videosink")


def test_direct_receiver_can_tee_viewport_frames_to_raw_pipe():
    cmd = build_direct_receiver_cmd(
        "gst-launch-1.0",
        DirectReceiverConfig(
            name="Primary Camera",
            codec="h264",
            port=5000,
            bind_address="192.168.1.1",
            width=1920,
            height=1080,
            square_crop=True,
            frame_pipe=True,
        ),
    )

    assert "tee" in cmd
    assert "name=frame_t" in cmd
    assert "frame_t." in cmd
    assert "fdsink" in cmd
    assert "fd=1" in cmd
    assert "d3d11videosink" in cmd
    assert "video/x-raw,format=BGR,width=1920,height=1080,colorimetry=1:4:0:0,range=full" in cmd
    render_branch = cmd[cmd.index("name=frame_t"):cmd.index("frame_t.")]
    assert "videoconvert" in render_branch
    frame_branch = cmd[cmd.index("frame_t."):]
    assert "videocrop" not in frame_branch


def test_direct_receiver_can_throttle_snapshot_frame_pipe():
    cmd = build_direct_receiver_cmd(
        "gst-launch-1.0",
        DirectReceiverConfig(
            name="Primary Camera",
            codec="h264",
            port=5000,
            bind_address="192.168.1.1",
            width=1920,
            height=1080,
            frame_pipe=True,
            frame_pipe_fps=2.0,
        ),
    )

    assert "videorate" in cmd
    assert "drop-only=true" in cmd
    assert "max-rate=2" in cmd
    assert "video/x-raw,format=BGR,width=1920,height=1080,colorimetry=1:4:0:0,range=full,framerate=2/1" in cmd


def test_direct_media_capture_defaults_can_be_overridden(monkeypatch):
    monkeypatch.delenv("TRITON_DIRECT_COMPRESSED_RECORDING", raising=False)
    monkeypatch.delenv("TRITON_DIRECT_SNAPSHOT_FRAMES", raising=False)
    monkeypatch.delenv("TRITON_DIRECT_SNAPSHOT_FPS", raising=False)

    assert _compressed_recording_requested({}) is True
    assert _compressed_recording_requested({"direct_compressed_recording": False}) is False
    assert _snapshot_frame_pipe_requested({}) is True
    assert _snapshot_frame_pipe_requested({"direct_snapshot_frame_pipe": False}) is False
    assert _snapshot_frame_pipe_fps({}) == 2.0

    monkeypatch.setenv("TRITON_DIRECT_COMPRESSED_RECORDING", "0")
    monkeypatch.setenv("TRITON_DIRECT_SNAPSHOT_FRAMES", "0")
    assert _compressed_recording_requested({}) is False
    assert _snapshot_frame_pipe_requested({}) is False


def test_direct_h264_receiver_can_forward_compressed_rtp_for_recording():
    cmd = build_direct_receiver_cmd(
        "gst-launch-1.0",
        DirectReceiverConfig(
            name="Aux Camera",
            codec="h264",
            port=5002,
            bind_address="192.168.1.1",
            latency_ms=5,
            record_rtp_port=7002,
        ),
    )

    assert "tee" in cmd
    assert "name=rtp_t" in cmd
    assert "rtp_t." in cmd
    assert "udpsink" in cmd
    assert "host=127.0.0.1" in cmd
    assert "port=7002" in cmd
    record_branch_start = len(cmd) - list(reversed(cmd)).index("rtp_t.") - 1
    record_branch = cmd[record_branch_start:]
    assert "max-size-buffers=0" in record_branch
    assert "leaky=downstream" not in record_branch
    assert "rtph264depay" in cmd
    assert "d3d11videosink" in cmd
    assert "fdsink" not in cmd


def test_compressed_h264_record_command_copies_without_decoding(tmp_path):
    target = tmp_path / "compressed.partial.ts"
    sdp = tmp_path / "compressed.sdp"
    cmd = build_h264_rtp_mp4_record_cmd(
        "ffmpeg",
        H264RtpMp4RecordConfig(
            name="Aux Camera",
            port=7002,
            out_path=target,
            sdp_path=sdp,
            latency_ms=250,
        ),
    )

    assert cmd[0] == "ffmpeg"
    assert "-protocol_whitelist" in cmd
    assert "file,udp,rtp" in cmd
    assert str(sdp.resolve()).replace("\\", "/") in cmd
    assert "-c:v" in cmd
    assert "copy" in cmd
    assert "-f" in cmd
    assert "mpegts" in cmd
    assert str(target.resolve()).replace("\\", "/") in cmd
    assert "decodebin" not in cmd
    assert "videoconvert" not in cmd
    assert "video/x-raw" not in cmd


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


def _usable_frame(width: int = 24, height: int = 16, base: int = 80) -> np.ndarray:
    frame = np.zeros((height, width, 3), dtype=np.uint8)
    frame[:, :, 0] = base
    frame[:, :, 1] = min(base + 18, 255)
    frame[:, :, 2] = min(base + 36, 255)
    frame[::2, ::2, :] = min(base + 70, 255)
    return frame


class _FakeCaptureCamera:
    def __init__(self):
        self.seq = 0
        self.packet = self._packet()

    def _packet(self):
        self.seq += 1
        return SimpleNamespace(
            frame_bgr=_usable_frame(base=70 + (self.seq % 3) * 10),
            seq=self.seq,
            monotonic_ts=time.monotonic(),
            wall_ts=time.time(),
        )

    def latest_frame_packet(self):
        return self.packet

    def read_frame_packet(self):
        self.packet = self._packet()
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


class _FakeCompressedRecorder:
    instances = []

    def __init__(self, out_path, *, name, port, bind_address="127.0.0.1", latency_ms=250):
        self.out_path = Path(out_path)
        self.name = name
        self.port = int(port)
        self.bind_address = bind_address
        self.latency_ms = int(latency_ms)
        self.target = self.out_path
        self.stopped = False
        self.stop_timeout_s = None
        _FakeCompressedRecorder.instances.append(self)

    def start(self):
        self.out_path.parent.mkdir(parents=True, exist_ok=True)
        self.out_path.touch()
        return self.target

    def stop(self, timeout_s=5.0, *, drain_pending=True):
        self.stop_timeout_s = timeout_s
        self.stopped = True

    def queue_size(self):
        return 0


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
        assert not bool(widget._capture_overlay.windowFlags() & Qt.WindowType.WindowStaysOnTopHint)
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


def test_direct_widget_prefers_compressed_rtp_recording(monkeypatch, tmp_path):
    app = _app()
    monkeypatch.setattr("gui.direct_gst_video_widget.DirectGstVideoWidget._start_connect", lambda self: None)
    monkeypatch.setattr("gui.direct_gst_video_widget.CompressedRtpRecorder", _FakeCompressedRecorder)
    _FakeCompressedRecorder.instances = []

    class _FakeProc:
        def poll(self):
            return None

    manager = _FakeManager()
    widget = DirectGstVideoWidget(manager, "Aux Camera")
    widget._proc = _FakeProc()
    widget._compressed_recording_available = True
    widget._compressed_record_port = 7002
    widget._compressed_record_host = "127.0.0.1"
    widget._compressed_record_latency_ms = 250
    try:
        target = widget.start_recording(out_dir=str(tmp_path), basename="aux", fps=30.0)
        assert target is not None
        assert widget.is_recording() is True
        assert manager.opened == 0
        assert len(_FakeCompressedRecorder.instances) == 1
        rec = _FakeCompressedRecorder.instances[0]
        assert rec.port == 7002
        assert rec.latency_ms == 250
        assert widget._record_thread is None

        widget.stop_recording()
        deadline = time.monotonic() + 1.0
        while not rec.stopped and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)
        assert rec.stopped is True
        assert rec.stop_timeout_s == 10.0
        assert widget.is_recording() is False
    finally:
        widget.close()
        widget.deleteLater()
        app.processEvents()


def test_direct_widget_prefers_viewport_frame_pipe_for_capture(monkeypatch, tmp_path):
    app = _app()
    monkeypatch.setattr("gui.direct_gst_video_widget.DirectGstVideoWidget._start_connect", lambda self: None)
    monkeypatch.setattr("gui.direct_gst_video_widget.VideoRecorder", _FakeRecorder)

    def _fake_save_snapshot(frame, out_path):
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        Path(out_path).touch()

    monkeypatch.setattr("gui.direct_gst_video_widget.save_snapshot", _fake_save_snapshot)

    frame = np.zeros((16, 24, 3), dtype=np.uint8)
    frame[:, :, 0] = 90
    packet = SimpleNamespace(
        frame_bgr=frame,
        seq=7,
        monotonic_ts=time.monotonic(),
        wall_ts=time.time(),
    )

    class _FakeViewportReader:
        def latest_frame_packet(self):
            return packet

        def read_frame_packet(self):
            return packet

        def recent_frame_packets(self, *, max_age_s=0.5):
            return [packet]

    manager = _FakeManager()
    widget = DirectGstVideoWidget(manager, "Primary Camera")
    widget._viewport_reader = _FakeViewportReader()
    widget._viewport_frame_pipe_enabled = True
    try:
        target = widget.start_recording(out_dir=str(tmp_path), basename="primary", fps=30.0)
        assert target is not None
        assert manager.opened == 0

        rec = widget._rec
        deadline = time.monotonic() + 1.0
        while rec is not None and not rec.frames and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)
        assert rec is not None
        assert rec.frames
        assert int(rec.frames[0][0, 0, 0]) == 90
        assert manager.opened == 0

        snap_path = widget.save_snapshot(out_dir=str(tmp_path), basename="viewport-snap")
        deadline = time.monotonic() + 1.0
        while snap_path and not Path(snap_path).exists() and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)
        assert snap_path is not None
        assert Path(snap_path).exists()
        assert manager.opened == 0

        widget.stop_recording()
        deadline = time.monotonic() + 1.0
        while rec is not None and not rec.stopped and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)
        assert rec is not None
        assert rec.stopped is True
    finally:
        widget.close()
        widget.deleteLater()
        app.processEvents()


def test_direct_widget_prefers_snapshot_frame_pipe_for_stills(monkeypatch, tmp_path):
    app = _app()
    monkeypatch.setattr("gui.direct_gst_video_widget.DirectGstVideoWidget._start_connect", lambda self: None)
    saved_frames = []

    def _fake_save_snapshot(frame, out_path):
        saved_frames.append(np.array(frame, copy=True))
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        Path(out_path).touch()

    monkeypatch.setattr("gui.direct_gst_video_widget.save_snapshot", _fake_save_snapshot)

    frame = _usable_frame(base=112)
    packet = SimpleNamespace(
        frame_bgr=frame,
        seq=7,
        monotonic_ts=time.monotonic(),
        wall_ts=time.time(),
    )

    class _FakeSnapshotReader:
        def latest_frame_packet(self):
            return packet

    manager = _FakeManager()
    widget = DirectGstVideoWidget(manager, "Primary Camera")
    widget._viewport_reader = _FakeSnapshotReader()
    widget._snapshot_frame_pipe_enabled = True
    widget._viewport_frame_pipe_enabled = False
    try:
        snap_path = widget.save_snapshot(out_dir=str(tmp_path), basename="snapshot-pipe")
        deadline = time.monotonic() + 1.0
        while snap_path and not Path(snap_path).exists() and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)

        assert snap_path is not None
        assert Path(snap_path).exists()
        assert manager.opened == 0
        assert saved_frames
        assert int(saved_frames[0][0, 0, 1]) >= 112
    finally:
        widget.close()
        widget.deleteLater()
        app.processEvents()


def test_direct_widget_snapshot_uses_rov_capture_rpc_before_capture_receiver(monkeypatch, tmp_path):
    app = _app()
    monkeypatch.setattr("gui.direct_gst_video_widget.DirectGstVideoWidget._start_connect", lambda self: None)
    monkeypatch.setenv("TRITON_SNAPSHOT_WAIT_S", "1.0")

    class _FakeRov:
        def __init__(self):
            self.calls = []

        def capture_frame(self, **kwargs):
            self.calls.append(dict(kwargs))
            return {
                "stream": kwargs.get("stream"),
                "seq": 33,
                "format": "png",
                "shape": [1080, 1920, 3],
                "image_b64": base64.b64encode(b"rov-png").decode("ascii"),
            }

    manager = _FakeManager()
    manager.rov = _FakeRov()
    widget = DirectGstVideoWidget(manager, "Primary Camera")
    try:
        snap_path = widget.save_snapshot(out_dir=str(tmp_path), basename="rov-snapshot")
        deadline = time.monotonic() + 1.0
        while snap_path and not Path(snap_path).exists() and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)

        assert snap_path is not None
        assert Path(snap_path).read_bytes() == b"rov-png"
        assert manager.opened == 0
        assert manager.rov.calls == [
            {"stream": "Primary Camera", "wait_s": 1.0, "format": "png"}
        ]
    finally:
        widget.close()
        widget.deleteLater()
        app.processEvents()


def test_direct_widget_screen_snapshot_avoids_capture_receiver(monkeypatch, tmp_path):
    app = _app()
    monkeypatch.setattr("gui.direct_gst_video_widget.DirectGstVideoWidget._start_connect", lambda self: None)
    saved_frames = []

    def _fake_save_snapshot(frame, out_path):
        saved_frames.append(np.array(frame, copy=True))
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        Path(out_path).touch()

    monkeypatch.setattr("gui.direct_gst_video_widget.save_snapshot", _fake_save_snapshot)

    frame = np.zeros((12, 20, 3), dtype=np.uint8)
    frame[:, :, 2] = 140
    manager = _FakeManager()
    widget = DirectGstVideoWidget(manager, "Primary Camera")
    monkeypatch.setattr(widget, "_direct_render_snapshot_frame", lambda: (frame, "direct3d_screen"))
    try:
        snap_path = widget.save_snapshot(out_dir=str(tmp_path), basename="direct-screen")
        deadline = time.monotonic() + 1.0
        while snap_path and not Path(snap_path).exists() and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)

        assert snap_path is not None
        assert Path(snap_path).exists()
        assert manager.opened == 0
        assert saved_frames
        assert int(saved_frames[0][0, 0, 2]) == 140
    finally:
        widget.close()
        widget.deleteLater()
        app.processEvents()


def test_direct_widget_snapshot_requires_fresh_usable_capture_frame(monkeypatch, tmp_path):
    app = _app()
    monkeypatch.setattr("gui.direct_gst_video_widget.DirectGstVideoWidget._start_connect", lambda self: None)
    monkeypatch.setenv("TRITON_SNAPSHOT_WAIT_S", "0.1")

    class _StaleOnlyCaptureCamera:
        def __init__(self):
            self.packet = SimpleNamespace(
                frame_bgr=np.full((12, 20, 3), 80, dtype=np.uint8),
                seq=4,
                monotonic_ts=time.monotonic(),
                wall_ts=time.time(),
            )

        def latest_frame_packet(self):
            return self.packet

        def read_frame_packet(self):
            return None

    manager = _FakeManager()
    manager.capture = _StaleOnlyCaptureCamera()
    widget = DirectGstVideoWidget(manager, "Primary Camera")
    try:
        snap_path = widget.save_snapshot(out_dir=str(tmp_path), basename="stale")

        assert snap_path is None
        assert manager.opened == 1
        assert not (tmp_path / "stale.png").exists()
    finally:
        widget.close()
        widget.deleteLater()
        app.processEvents()


def test_direct_widget_snapshot_retries_after_capture_restart(monkeypatch, tmp_path):
    app = _app()
    monkeypatch.setattr("gui.direct_gst_video_widget.DirectGstVideoWidget._start_connect", lambda self: None)
    monkeypatch.setenv("TRITON_SNAPSHOT_WAIT_S", "0.1")

    def _fake_save_snapshot(frame, out_path):
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        Path(out_path).touch()

    monkeypatch.setattr("gui.direct_gst_video_widget.save_snapshot", _fake_save_snapshot)

    class _RestartableCaptureCamera:
        def __init__(self):
            self.enabled = False
            self.seq = 0

        def latest_frame_packet(self):
            return None

        def read_frame_packet(self):
            if not self.enabled:
                return None
            self.seq += 1
            return SimpleNamespace(
                frame_bgr=_usable_frame(base=100),
                seq=self.seq,
                monotonic_ts=time.monotonic(),
                wall_ts=time.time(),
            )

    class _RestartManager(_FakeManager):
        def __init__(self):
            super().__init__()
            self.capture = _RestartableCaptureCamera()
            self.restarts = 0

        def restart_capture_stream(self, name):
            self.restarts += 1
            self.capture.enabled = True

    manager = _RestartManager()
    widget = DirectGstVideoWidget(manager, "Primary Camera")
    try:
        snap_path = widget.save_snapshot(out_dir=str(tmp_path), basename="retry")
        deadline = time.monotonic() + 1.0
        while snap_path and not Path(snap_path).exists() and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)

        assert snap_path is not None
        assert Path(snap_path).exists()
        assert manager.restarts == 1
    finally:
        widget.close()
        widget.deleteLater()
        app.processEvents()


def test_direct_widget_defers_square_reconnect_while_recording(monkeypatch):
    app = _app()
    monkeypatch.setattr("gui.direct_gst_video_widget.DirectGstVideoWidget._start_connect", lambda self: None)

    widget = DirectGstVideoWidget(_FakeManager(), "Primary Camera")
    try:
        reconnects = []
        monkeypatch.setattr(widget, "_force_reconnect", lambda *args, **kwargs: reconnects.append((args, kwargs)))
        widget._rec = object()
        widget._proc = SimpleNamespace(poll=lambda: None)

        widget.set_square_display_enabled(True)

        assert widget._square_display_enabled is True
        assert reconnects == []
    finally:
        widget._rec = None
        widget.close()
        widget.deleteLater()
        app.processEvents()


def test_direct_widget_capture_frame_pipe_request_does_not_reconnect_active_stream(monkeypatch):
    app = _app()
    monkeypatch.setattr("gui.direct_gst_video_widget.DirectGstVideoWidget._start_connect", lambda self: None)

    widget = DirectGstVideoWidget(_FakeManager(), "Primary Camera")
    try:
        reconnects = []
        monkeypatch.setattr(widget, "_force_reconnect", lambda *args, **kwargs: reconnects.append((args, kwargs)))
        widget._proc = SimpleNamespace(poll=lambda: None)

        widget.set_capture_frame_pipe_enabled(True)

        assert widget._capture_frame_pipe_requested is True
        assert reconnects == []
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
