"""Tests for the top-bar current-limiter panel (checkbox + draw readout)."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt6")

from PyQt6.QtWidgets import QApplication

from gui.current_budget_panel import CurrentBudgetPanel


def _app() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def _close(app, panel) -> None:
    panel.close()
    panel.deleteLater()
    app.processEvents()


def test_toggle_emits_signal():
    app = _app()
    panel = CurrentBudgetPanel(enabled=True)
    seen = []
    panel.toggled.connect(seen.append)
    try:
        panel.check.setChecked(False)
        panel.check.setChecked(True)
        assert seen == [False, True]
    finally:
        _close(app, panel)


def test_set_enabled_state_does_not_emit():
    app = _app()
    panel = CurrentBudgetPanel(enabled=False)
    seen = []
    panel.toggled.connect(seen.append)
    try:
        panel.set_enabled_state(True)
        assert panel.is_enabled_state() is True
        assert seen == []  # reflecting external state must not echo back
    finally:
        _close(app, panel)


def test_estimate_text_and_clear():
    app = _app()
    panel = CurrentBudgetPanel()
    try:
        panel.update_estimate(12.4, active=True, applied=False, budget_a=20.0)
        assert panel.readout.text() == "12 A"
        panel.clear_estimate()
        assert panel.readout.text() == "— A"
        panel.update_estimate(None)
        assert panel.readout.text() == "— A"
    finally:
        _close(app, panel)


def test_applied_shows_limit_marker():
    app = _app()
    panel = CurrentBudgetPanel()
    try:
        panel.update_estimate(40.0, active=True, applied=True, budget_a=20.0)
        assert "LIM" in panel.readout.text()
    finally:
        _close(app, panel)


def test_budget_box_emits_and_reflects():
    app = _app()
    panel = CurrentBudgetPanel(budget_a=22.0, budget_min=5.0, budget_max=40.0)
    seen = []
    panel.budget_changed.connect(seen.append)
    try:
        assert panel.budget_value() == 22.0
        panel.budget_spin.setValue(18)
        assert seen == [18.0]
        # Reflecting an external value must not echo back through budget_changed.
        panel.set_budget_value(25.0)
        assert panel.budget_value() == 25.0
        assert seen == [18.0]
    finally:
        _close(app, panel)


def test_budget_box_clamps_to_range():
    app = _app()
    panel = CurrentBudgetPanel(budget_a=22.0, budget_min=10.0, budget_max=30.0)
    try:
        panel.set_budget_value(999.0)
        assert panel.budget_value() == 30.0
        panel.set_budget_value(1.0)
        assert panel.budget_value() == 10.0
    finally:
        _close(app, panel)


def test_tone_colors_track_budget():
    app = _app()
    panel = CurrentBudgetPanel()
    try:
        panel.update_estimate(10.0, budget_a=20.0)  # under budget -> green
        assert "#3fb950" in panel.readout.styleSheet()
        panel.update_estimate(22.0, budget_a=20.0)  # just over -> amber
        assert "#d29922" in panel.readout.styleSheet()
        panel.update_estimate(40.0, budget_a=20.0)  # well over -> red
        assert "#f85149" in panel.readout.styleSheet()
    finally:
        _close(app, panel)
