import os

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
