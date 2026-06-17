import builtins
import subprocess
import time
from pathlib import Path

import numpy as np
import pytest

from recording.video_recorder import VideoRecorder, _ffmpeg_output_params, _preferred_mp4_backends, save_snapshot


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


def _ffmpeg_exe() -> str:
    imageio_ffmpeg = pytest.importorskip("imageio_ffmpeg")
    return str(imageio_ffmpeg.get_ffmpeg_exe())


def _assert_ffmpeg_can_read(path: Path) -> None:
    proc = subprocess.run(
        [_ffmpeg_exe(), "-hide_banner", "-v", "error", "-i", str(path), "-f", "null", "-"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=20,
    )
    assert proc.returncode == 0, proc.stderr


def test_video_recorder_defaults_to_high_quality_ffmpeg_backend(monkeypatch):
    monkeypatch.delenv("TRITON_VIDEO_RECORDER_BACKEND", raising=False)
    assert _preferred_mp4_backends()[0] == "ffmpeg"

    monkeypatch.setenv("TRITON_VIDEO_RECORDER_BACKEND", "imageio")
    assert _preferred_mp4_backends() == ("imageio", "ffmpeg", "opencv")


def test_video_recorder_ffmpeg_params_use_crf_unless_bitrate_is_forced(monkeypatch):
    monkeypatch.delenv("TRITON_VIDEO_RECORDER_BITRATE", raising=False)
    monkeypatch.setenv("TRITON_VIDEO_RECORDER_CRF", "12")
    monkeypatch.setenv("TRITON_VIDEO_RECORDER_PRESET", "medium")

    assert _ffmpeg_output_params() == ["-crf", "12", "-preset", "medium", "-movflags", "+faststart"]

    monkeypatch.setenv("TRITON_VIDEO_RECORDER_BITRATE", "24M")
    assert _ffmpeg_output_params() == ["-preset", "medium", "-movflags", "+faststart"]


def test_video_recorder_writes_mp4_with_available_backend(tmp_path: Path):
    target = _write_short_video(tmp_path / "single_camera.mp4")

    assert target.suffix == ".mp4"
    assert target.exists()
    assert target.stat().st_size > 0
    _assert_ffmpeg_can_read(target)
    assert not (tmp_path / "single_camera_frames").exists()


def test_video_recorder_hides_mp4_until_finalized(tmp_path: Path):
    recorder = VideoRecorder(tmp_path / "pending.mp4", fps=10.0)
    target = Path(recorder.start())
    try:
        recorder.add_frame(_sample_frame(40))
        deadline = time.monotonic() + 3.0
        while recorder._written_frames <= 0 and time.monotonic() < deadline:
            time.sleep(0.02)
        assert recorder._written_frames > 0
        assert not target.exists()
        assert not list(tmp_path.glob("*.mp4"))
    finally:
        recorder.stop(timeout_s=10.0)

    assert target.exists()
    _assert_ffmpeg_can_read(target)


def test_save_snapshot_publishes_readable_final_file(tmp_path: Path):
    cv2 = pytest.importorskip("cv2")
    target = tmp_path / "snapshot.png"

    save_snapshot(_sample_frame(90), target)

    loaded = cv2.imread(str(target), cv2.IMREAD_COLOR)
    assert loaded is not None
    assert loaded.shape == (48, 64, 3)
    assert not list(tmp_path.glob("*.partial*"))


def test_video_recorder_discards_empty_mp4(tmp_path: Path):
    target = tmp_path / "empty.mp4"
    recorder = VideoRecorder(target, fps=10.0)
    recorder.start()

    recorder.stop(timeout_s=10.0)

    assert not target.exists()
    assert not list(tmp_path.glob("*.partial"))


def test_video_recorder_uses_ffmpeg_mp4_when_imageio_is_missing(monkeypatch, tmp_path: Path):
    pytest.importorskip("imageio_ffmpeg")

    real_import = builtins.__import__

    def import_without_imageio(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "imageio" or name.startswith("imageio."):
            raise ModuleNotFoundError("No module named 'imageio'")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", import_without_imageio)

    recorder = VideoRecorder(tmp_path / "ffmpeg_without_imageio.mp4", fps=10.0)
    target = Path(recorder.start())
    try:
        assert recorder._writer_backend == "ffmpeg"
        for idx in range(8):
            recorder.add_frame(_sample_frame(20 + idx * 10))
    finally:
        recorder.stop(timeout_s=10.0)

    assert target.suffix == ".mp4"
    assert target.exists()
    assert target.stat().st_size > 0
    _assert_ffmpeg_can_read(target)
    assert not (tmp_path / "ffmpeg_without_imageio_frames").exists()


def test_video_recorder_prefers_high_quality_ffmpeg(monkeypatch, tmp_path: Path):
    pytest.importorskip("imageio_ffmpeg")

    recorder = VideoRecorder(tmp_path / "high_quality.mp4", fps=10.0)
    target = Path(recorder.start())
    try:
        assert recorder._writer_backend == "ffmpeg"
        for idx in range(4):
            recorder.add_frame(_sample_frame(40 + idx))
    finally:
        recorder.stop(timeout_s=10.0)

    assert target.suffix == ".mp4"
    assert target.exists()
    _assert_ffmpeg_can_read(target)


def test_video_recorder_uses_opencv_when_ffmpeg_and_imageio_are_missing(monkeypatch, tmp_path: Path):
    pytest.importorskip("cv2")

    real_import = builtins.__import__

    def import_without_ffmpeg_or_imageio(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "imageio_ffmpeg" or name.startswith("imageio_ffmpeg."):
            raise ModuleNotFoundError("No module named 'imageio_ffmpeg'")
        if name == "imageio" or name.startswith("imageio."):
            raise ModuleNotFoundError("No module named 'imageio'")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", import_without_ffmpeg_or_imageio)

    target = _write_short_video(tmp_path / "opencv_rescue.mp4")

    assert target.suffix == ".mp4"
    assert target.exists()
    assert target.stat().st_size > 0
    assert not (tmp_path / "opencv_rescue_frames").exists()


def test_video_recorder_can_drop_pending_frames_on_stop(tmp_path: Path):
    class _FakeThread:
        def join(self, timeout=None):
            return None

        def is_alive(self):
            return False

    recorder = VideoRecorder(tmp_path / "drop_pending.mp4", fps=30.0)
    recorder._started = True
    recorder._thread = _FakeThread()
    for idx in range(3):
        recorder._q.put_nowait(_sample_frame(idx))

    recorder.stop(drain_pending=False)

    remaining = []
    while not recorder._q.empty():
        remaining.append(recorder._q.get_nowait())
    assert remaining == [None]
