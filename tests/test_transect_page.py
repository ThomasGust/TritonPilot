"""Tests for the transect page engage control + status chip (headless Qt)."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt6")

from PyQt6.QtWidgets import QApplication

import gui.transect_page as transect_page
from gui.transect_page import TransectPage


def _app():
    return QApplication.instance() or QApplication([])


def test_runtime_servo_controls_default_to_yaw_er_on_and_50_percent_blue_width():
    app = _app()
    page = TransectPage(stream_names=["Arm Camera"])
    try:
        assert page.rotation_servo_check.isChecked() is True
        assert page.target_blue_width_spin.value() == pytest.approx(50.0)
    finally:
        page.deleteLater()
        app.processEvents()


def test_engage_button_emits_toggled_both_ways():
    app = _app()
    page = TransectPage(stream_names=["Arm Camera"])
    try:
        events = []
        page.engageToggled.connect(events.append)
        page.engage_btn.click()         # engage
        page.engage_btn.click()         # release
        app.processEvents()
        assert events == [True, False]
    finally:
        page.deleteLater()
        app.processEvents()


def test_runtime_servo_controls_emit_operator_changes():
    app = _app()
    page = TransectPage(
        stream_names=["Arm Camera"],
        rotation_servo_enabled=False,
        target_blue_width_percent=50.0,
    )
    try:
        rotation_events = []
        blue_width_events = []
        page.rotationServoToggled.connect(rotation_events.append)
        page.targetBlueWidthChanged.connect(blue_width_events.append)

        page.rotation_servo_check.click()
        page.target_blue_width_spin.setValue(44.6)
        app.processEvents()

        assert rotation_events == [True]
        assert blue_width_events[-1] == pytest.approx(44.6)
    finally:
        page.deleteLater()
        app.processEvents()


def test_runtime_servo_controls_sync_without_emitting():
    app = _app()
    page = TransectPage(stream_names=["Arm Camera"])
    try:
        rotation_events = []
        blue_width_events = []
        page.rotationServoToggled.connect(rotation_events.append)
        page.targetBlueWidthChanged.connect(blue_width_events.append)

        page.set_rotation_servo_enabled(True)
        page.set_target_blue_width_percent(45.0)
        app.processEvents()

        assert page.rotation_servo_check.isChecked() is True
        assert page.target_blue_width_spin.value() == pytest.approx(45.0)
        assert rotation_events == []
        assert blue_width_events == []
    finally:
        page.deleteLater()
        app.processEvents()


def test_update_engage_state_reflects_without_emitting():
    app = _app()
    page = TransectPage(stream_names=["Arm Camera"])
    try:
        events = []
        page.engageToggled.connect(events.append)

        page.update_engage_state(engaged=False, lock_ready=True)
        assert page.engage_btn.isChecked() is False
        assert page.engage_btn.property("tone") == "ready"
        assert "lock" in page.engage_btn.text().lower()

        page.update_engage_state(engaged=True, lock_ready=True)
        assert page.engage_btn.isChecked() is True
        assert page.engage_btn.property("tone") == "engaged"
        assert "HOLDING" in page.engage_btn.text()

        page.update_engage_state(engaged=False, lock_ready=False)
        assert page.engage_btn.property("tone") == "idle"

        # Programmatic state updates must not re-emit the toggle signal.
        app.processEvents()
        assert events == []
    finally:
        page.deleteLater()
        app.processEvents()


def test_cv_status_chip_updates_text_and_tone():
    app = _app()
    page = TransectPage(stream_names=["Arm Camera"])
    try:
        page.set_cv_status("Autopilot CV: LOCK · 15 fps", "ok")
        assert page.cv_status_label.property("tone") == "ok"
        assert "LOCK" in page.cv_status_label.text()
    finally:
        page.deleteLater()
        app.processEvents()


def test_stopwatch_toggle_pause_and_reset(monkeypatch):
    app = _app()
    now = [100.0]
    monkeypatch.setattr(transect_page, "monotonic", lambda: now[0])

    page = TransectPage(stream_names=["Arm Camera"])
    try:
        assert page.stopwatch_running() is False
        assert page.stopwatch_elapsed_seconds() == pytest.approx(0.0)
        assert "00:00.0" in page.stopwatch_label.text()

        page.toggle_stopwatch()
        assert page.stopwatch_running() is True
        assert page.stopwatch_label.property("tone") == "running"

        now[0] = 112.34
        page._refresh_stopwatch_label()
        assert page.stopwatch_elapsed_seconds() == pytest.approx(12.34)
        assert "00:12.3" in page.stopwatch_label.text()

        page.toggle_stopwatch()
        assert page.stopwatch_running() is False
        assert page.stopwatch_label.property("tone") == "paused"
        frozen = page.stopwatch_elapsed_seconds()

        now[0] = 150.0
        page._refresh_stopwatch_label()
        assert page.stopwatch_elapsed_seconds() == pytest.approx(frozen)
        assert "00:12.3" in page.stopwatch_label.text()

        page.reset_stopwatch()
        assert page.stopwatch_running() is False
        assert "00:00.0" in page.stopwatch_label.text()

        now[0] = 181.0
        page._refresh_stopwatch_label()
        assert "00:00.0" in page.stopwatch_label.text()
        assert page.stopwatch_label.property("tone") == "idle"

        page.toggle_stopwatch()
        now[0] = 212.0
        page._refresh_stopwatch_label()
        assert "00:31.0" in page.stopwatch_label.text()
        assert page.stopwatch_label.property("tone") == "complete"
    finally:
        page.deleteLater()
        app.processEvents()
