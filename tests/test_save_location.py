from pathlib import Path

from recording.save_location import resolve_recordings_dir


def test_resolve_recordings_dir_uses_available_preferred(tmp_path: Path):
    preferred = tmp_path / "operator-captures"
    fallback = tmp_path / "repo" / "recordings"
    preferred.mkdir()

    location = resolve_recordings_dir(preferred, fallback=fallback)

    assert location.path == preferred.resolve()
    assert location.used_fallback is False
    assert location.reason == ""
    assert not fallback.exists()


def test_resolve_recordings_dir_falls_back_when_preferred_is_missing(tmp_path: Path):
    preferred = tmp_path / "missing-drive" / "captures"
    fallback = tmp_path / "repo" / "recordings"

    location = resolve_recordings_dir(preferred, fallback=fallback)

    assert location.path == fallback.resolve()
    assert location.used_fallback is True
    assert "Selected save directory is not available" in location.reason
    assert fallback.is_dir()
