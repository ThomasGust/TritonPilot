from pathlib import Path

import app_paths


def test_streams_file_path_uses_environment_override(monkeypatch, tmp_path: Path):
    override = tmp_path / "streams.json"
    monkeypatch.setenv("TRITON_STREAMS_FILE", str(override))

    assert app_paths.streams_file_path() == override


def test_default_recordings_dir_uses_environment_override(monkeypatch, tmp_path: Path):
    override = tmp_path / "captures"
    monkeypatch.setenv("TRITON_RECORDINGS_DIR", str(override))

    assert app_paths.default_recordings_dir() == override


def test_default_recordings_dir_is_operator_documents_tree(monkeypatch, tmp_path: Path):
    monkeypatch.delenv("TRITON_RECORDINGS_DIR", raising=False)
    monkeypatch.delenv("TRITON_DOCUMENTS_DIR", raising=False)
    monkeypatch.setattr(app_paths.os, "name", "nt", raising=False)
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "pilot"))

    assert app_paths.default_recordings_dir() == tmp_path / "pilot" / "Documents" / "TritonPilot" / "Recordings"
