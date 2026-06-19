"""Tests for the transect page engage control + status chip (headless Qt)."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt6")

from PyQt6.QtWidgets import QApplication

from gui.transect_page import TransectPage


def _app():
    return QApplication.instance() or QApplication([])


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
