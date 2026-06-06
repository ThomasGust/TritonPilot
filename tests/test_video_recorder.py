import builtins
from pathlib import Path

import numpy as np
import pytest

from recording.video_recorder import VideoRecorder


def _sample_frame(value: int) -> np.ndarray:
    frame = np.zeros((48, 64, 3), dtype=np.uint8)
    frame[:, :, 0] = value
    frame[:, :, 1] = np.arange(64, dtype=np.uint8)
    frame[:, :, 2] = np.arange(48, dtype=np.uint8)[:, None]
    return frame


def _write_short_video(out_path: Path) -> Path:
    recorder = VideoRecorder(out_path, fps=10.0)
    target = Path(recorder.start())
    for idx in range(8):
        recorder.add_frame(_sample_frame(20 + idx * 10))
    recorder.stop(timeout_s=10.0)
    return target


def test_video_recorder_writes_mp4_with_available_backend(tmp_path: Path):
    target = _write_short_video(tmp_path / "single_camera.mp4")

    assert target.suffix == ".mp4"
    assert target.exists()
    assert target.stat().st_size > 0
    assert not (tmp_path / "single_camera_frames").exists()


def test_video_recorder_uses_opencv_mp4_when_imageio_is_missing(monkeypatch, tmp_path: Path):
    pytest.importorskip("cv2")

    real_import = builtins.__import__

    def import_without_imageio(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "imageio" or name.startswith("imageio."):
            raise ModuleNotFoundError("No module named 'imageio'")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", import_without_imageio)

    target = _write_short_video(tmp_path / "opencv_fallback.mp4")

    assert target.suffix == ".mp4"
    assert target.exists()
    assert target.stat().st_size > 0
    assert not (tmp_path / "opencv_fallback_frames").exists()
