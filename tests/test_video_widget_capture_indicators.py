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
