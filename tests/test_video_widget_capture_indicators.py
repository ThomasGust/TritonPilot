import os
import time
from pathlib import Path

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt6")

from PyQt6.QtWidgets import QApplication

from gui.video_widget import VideoWidget


class _DummyManager:
    def close(self, _stream_name: str) -> None:
        return


class _FakeRecorder:
    def __init__(self, out_path, fps=30.0):
        self.out_path = Path(out_path)
        self.fps = float(fps)
        self.target = self.out_path
        self.started = False
        self.stopped = False

    def start(self):
        self.started = True
        self.out_path.parent.mkdir(parents=True, exist_ok=True)
        self.out_path.touch()
        return self.target

    def stop(self, timeout_s: float = 5.0) -> None:
        self.stopped = True


def _app() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def test_video_widget_shows_record_and_snapshot_indicators(monkeypatch, tmp_path):
    app = _app()
    monkeypatch.setattr("gui.video_widget.VideoRecorder", _FakeRecorder)
    monkeypatch.setattr("gui.video_widget.VideoWidget._start_connect", lambda self: None)

    def _fake_save_snapshot(frame, out_path):
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        Path(out_path).touch()

    monkeypatch.setattr("gui.video_widget.save_snapshot", _fake_save_snapshot)

    widget = VideoWidget(_DummyManager(), "front")
    widget.resize(640, 360)
    widget.show()
    widget.last_frame = np.zeros((32, 32, 3), dtype=np.uint8)
    try:
        app.processEvents()

        target = widget.start_recording(out_dir=str(tmp_path), basename="front", fps=24.0)
        app.processEvents()

        assert target is not None
        assert widget.is_recording() is True
        assert widget._record_badge.isVisible() is True
        assert "REC" in widget._record_badge.text()

        snap_path = widget.save_snapshot(out_dir=str(tmp_path), basename="snap")
        app.processEvents()

        assert snap_path is not None
        assert widget._snapshot_badge.isVisible() is True
        assert widget._snapshot_badge.text() == "SNAP"

        widget._snapshot_indicator_until_ts = time.time() - 0.01
        widget._refresh_capture_indicators()
        app.processEvents()
        assert widget._snapshot_badge.isVisible() is False

        widget.stop_recording()
        app.processEvents()
        assert widget.is_recording() is False
        assert widget._record_badge.isVisible() is False
    finally:
        widget.close()
        widget.deleteLater()
        app.processEvents()


def test_video_widget_default_capture_names_are_flat_with_camera_and_time(monkeypatch, tmp_path):
    app = _app()
    monkeypatch.setattr("gui.video_widget.VideoRecorder", _FakeRecorder)
    monkeypatch.setattr("gui.video_widget.VideoWidget._start_connect", lambda self: None)
    monkeypatch.setattr(
        "gui.video_widget.timestamped_camera_stem",
        lambda camera_name, purpose=None: f"20260508-170000_{str(camera_name).replace(' ', '_')}_{purpose}",
    )

    def _fake_save_snapshot(frame, out_path):
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        Path(out_path).touch()

    monkeypatch.setattr("gui.video_widget.save_snapshot", _fake_save_snapshot)

    widget = VideoWidget(_DummyManager(), "Primary Camera")
    widget.last_frame = np.zeros((32, 32, 3), dtype=np.uint8)
    try:
        target = Path(widget.start_recording(out_dir=str(tmp_path), fps=24.0))
        snap_path = Path(widget.save_snapshot(out_dir=str(tmp_path)))

        assert target.parent == tmp_path
        assert target.name == "20260508-170000_Primary_Camera_video.mp4"
        assert snap_path.parent == tmp_path
        assert snap_path.name == "20260508-170000_Primary_Camera_snapshot.png"
    finally:
        widget.close()
        widget.deleteLater()
        app.processEvents()


def test_video_widget_scales_frames_to_fill_pane_without_stretching(monkeypatch):
    app = _app()
    monkeypatch.setattr("gui.video_widget.VideoWidget._start_connect", lambda self: None)

    widget = VideoWidget(_DummyManager(), "front")
    widget.resize(200, 100)
    widget.show()
    try:
        app.processEvents()
        widget._on_frame(np.zeros((108, 192, 3), dtype=np.uint8))
        pix = widget.label.pixmap()
        dpr = max(1.0, float(pix.devicePixelRatio()))
        assert pix.width() / dpr >= widget.label.width()
        assert pix.height() / dpr >= widget.label.height()
        assert pix.height() / dpr > widget.label.height()
    finally:
        widget.close()
        widget.deleteLater()
        app.processEvents()


def test_video_widget_display_fps_updates_tick_timer(monkeypatch):
    app = _app()
    monkeypatch.setattr("gui.video_widget.VideoWidget._start_connect", lambda self: None)

    widget = VideoWidget(_DummyManager(), "front")
    try:
        widget.set_display_fps(20.0)
        assert widget.display_fps() == 20.0
        assert widget._tick_timer.interval() == 50

        widget.set_display_fps(0.0)
        assert widget.display_fps() == 1.0
        assert widget._tick_timer.interval() == 1000
    finally:
        widget.close()
        widget.deleteLater()
        app.processEvents()


def test_video_widget_shutdown_can_wait_for_inflight_connect_worker(monkeypatch):
    app = _app()
    monkeypatch.setattr("gui.video_widget.VideoWidget._start_connect", lambda self: None)

    class _FakeConnectWorker:
        def __init__(self):
            self.quit_called = False
            self.wait_ms = None

        def quit(self):
            self.quit_called = True

        def wait(self, timeout_ms):
            self.wait_ms = int(timeout_ms)
            return True

    widget = VideoWidget(_DummyManager(), "front")
    worker = _FakeConnectWorker()
    widget._connect_worker = worker
    try:
        widget.shutdown(async_release=False)

        assert worker.quit_called is True
        assert worker.wait_ms == 5000
        assert widget._connect_worker is None
    finally:
        widget.close()
        widget.deleteLater()
        app.processEvents()


def test_video_widget_async_shutdown_does_not_wait_for_inflight_connect_worker(monkeypatch):
    app = _app()
    monkeypatch.setattr("gui.video_widget.VideoWidget._start_connect", lambda self: None)

    class _FakeConnectWorker:
        def __init__(self):
            self.quit_called = False
            self.wait_ms = None

        def quit(self):
            self.quit_called = True

        def wait(self, timeout_ms):
            self.wait_ms = int(timeout_ms)
            return True

        def setParent(self, _parent):
            return None

    widget = VideoWidget(_DummyManager(), "front")
    worker = _FakeConnectWorker()
    widget._connect_worker = worker
    try:
        widget.shutdown(async_release=True)

        assert worker.quit_called is True
        assert worker.wait_ms is None
        assert widget._connect_worker is None
    finally:
        widget.close()
        widget.deleteLater()
        app.processEvents()


def test_video_widget_link_loss_clears_stale_frame_and_recovers(monkeypatch):
    app = _app()
    monkeypatch.setattr("gui.video_widget.VideoWidget._start_connect", lambda self: None)

    widget = VideoWidget(_DummyManager(), "front")
    widget.show()
    try:
        app.processEvents()
        widget._on_frame(np.full((32, 32, 3), 100, dtype=np.uint8))
        assert widget.label.pixmap() is not None

        widget.set_rov_link_status("LOST")

        assert widget._rov_link_lost is True
        assert "ROV link lost" in widget.label.text()
        assert widget.label.pixmap().isNull()

        widget.set_rov_link_status("OK")

        assert widget._rov_link_lost is False
        assert "heartbeat recovered" in widget.label.text()
        assert widget._next_retry_ts > 0
    finally:
        widget.close()
        widget.deleteLater()
        app.processEvents()
