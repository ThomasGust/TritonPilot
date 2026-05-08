from pathlib import Path

from recording import capture_paths


def test_timestamped_camera_stem_sorts_by_time_and_sanitizes_camera_name(monkeypatch):
    monkeypatch.setattr(capture_paths.time, "strftime", lambda _fmt: "20260508-170102")

    stem = capture_paths.timestamped_camera_stem("Primary Camera / Left", "snapshot")

    assert stem == "20260508-170102_Primary_Camera_Left_snapshot"


def test_unique_capture_path_adds_suffix_when_file_exists(tmp_path: Path):
    existing = tmp_path / "20260508-170102_Primary_Camera_video.mp4"
    existing.touch()

    path = capture_paths.unique_capture_path(tmp_path, "20260508-170102_Primary_Camera_video", ".mp4")

    assert path == tmp_path / "20260508-170102_Primary_Camera_video-02.mp4"
