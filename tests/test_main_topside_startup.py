import os
import sys
import types

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt6")

import main_topside


def test_startup_window_mode_is_maximized_by_default(monkeypatch):
    monkeypatch.delenv("TRITON_START_FULLSCREEN", raising=False)
    monkeypatch.delenv("TRITON_START_MAXIMIZED", raising=False)

    assert main_topside._startup_window_mode(["main_topside.py"]) == "maximized"


def test_startup_window_mode_can_be_windowed_by_flag(monkeypatch):
    monkeypatch.setenv("TRITON_START_FULLSCREEN", "1")

    assert main_topside._startup_window_mode(["main_topside.py", "--windowed"]) == "windowed"


def test_startup_window_mode_can_be_fullscreen_by_flag(monkeypatch):
    monkeypatch.setenv("TRITON_START_FULLSCREEN", "0")

    assert main_topside._startup_window_mode(["main_topside.py", "--fullscreen"]) == "fullscreen"


def test_startup_window_mode_can_be_windowed_by_environment(monkeypatch):
    monkeypatch.delenv("TRITON_START_FULLSCREEN", raising=False)
    monkeypatch.setenv("TRITON_START_MAXIMIZED", "0")

    assert main_topside._startup_window_mode(["main_topside.py"]) == "windowed"


def test_startup_custom_args_are_not_passed_to_qt():
    argv = ["main_topside.py", "--no-splash", "--windowed", "--maximized", "--fullscreen", "-style", "Fusion"]

    assert main_topside._qt_argv(argv) == ["main_topside.py", "-style", "Fusion"]


def test_smoke_test_requires_ffmpeg_binary(monkeypatch, tmp_path):
    streams_path = tmp_path / "streams.json"
    icon_path = tmp_path / "tritonpilot_icon.ico"
    ffmpeg_path = tmp_path / "ffmpeg.exe"
    streams_path.write_text("{}", encoding="utf-8")
    icon_path.write_bytes(b"icon")
    ffmpeg_path.write_bytes(b"ffmpeg")

    monkeypatch.setattr(main_topside, "streams_file_path", lambda: streams_path)
    monkeypatch.setattr(main_topside, "app_icon_path", lambda: icon_path)
    monkeypatch.setitem(
        sys.modules,
        "imageio_ffmpeg",
        types.SimpleNamespace(get_ffmpeg_exe=lambda: str(ffmpeg_path)),
    )

    assert main_topside._smoke_test() == 0

    ffmpeg_path.unlink()

    assert main_topside._smoke_test() == 1
