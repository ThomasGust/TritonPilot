"""Tests for the always-visible competition countdown clock."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt6")

import gui.competition_clock as competition_clock
from gui.competition_clock import CompetitionClock
from PyQt6.QtWidgets import QApplication


def _app():
    return QApplication.instance() or QApplication([])


def test_competition_clock_counts_down_through_urgency_colors(monkeypatch):
    app = _app()
    now = [100.0]
    monkeypatch.setattr(competition_clock, "monotonic", lambda: now[0])

    clock = CompetitionClock()
    try:
        assert clock.clock_label.text() == "15:00"
        assert clock.clock_label.property("tone") == "green"

        assert clock.start() is True
        for elapsed, text, tone in [
            (180.0, "12:00", "blue"),
            (360.0, "09:00", "yellow"),
            (540.0, "06:00", "orange"),
            (720.0, "03:00", "red"),
            (900.0, "00:00", "red"),
        ]:
            now[0] = 100.0 + elapsed
            clock._refresh_display()
            assert clock.clock_label.text() == text
            assert clock.clock_label.property("tone") == tone

        assert clock.is_running() is False
        assert clock.clock_label.property("state") == "complete"
    finally:
        clock.deleteLater()
        app.processEvents()


def test_competition_clock_keyboard_start_never_pauses(monkeypatch):
    app = _app()
    now = [500.0]
    monkeypatch.setattr(competition_clock, "monotonic", lambda: now[0])

    clock = CompetitionClock()
    try:
        assert clock.start_from_keyboard() == "started"
        assert clock.is_running() is True

        now[0] = 560.0
        assert clock.start_from_keyboard() == "running"
        assert clock.is_running() is True
        assert clock.remaining_seconds() == pytest.approx(14 * 60)

        clock.toggle_btn.click()
        app.processEvents()
        assert clock.is_running() is False
        assert clock.clock_label.text() == "PAUSED 14:00"

        now[0] = 600.0
        clock._refresh_display()
        assert clock.remaining_seconds() == pytest.approx(14 * 60)

        clock.duration_spin.setValue(10)
        assert clock.duration_seconds() == 10 * 60
        assert clock.remaining_seconds() == pytest.approx(10 * 60)
        assert clock.clock_label.text() == "10:00"

        clock.enable_act.trigger()
        app.processEvents()
        assert clock.clock_enabled() is False
        assert clock.start_from_keyboard() == "disabled"
        assert clock.is_running() is False
        assert clock.clock_label.text() == "CLOCK OFF"
    finally:
        clock.deleteLater()
        app.processEvents()
