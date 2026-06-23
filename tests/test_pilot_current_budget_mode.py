"""The pilot service exposes a live enable/disable for the ROV current limiter."""

import threading

from input.pilot_service import PilotPublisherService


def _svc(initial: bool, *, max_a: float = 22.0, lo: float = 5.0, hi: float = 40.0):
    svc = object.__new__(PilotPublisherService)
    svc._mode_lock = threading.RLock()
    svc._current_budget_max_a_min = float(lo)
    svc._current_budget_max_a_max = float(hi)
    svc._current_budget_max_a = float(max_a)
    svc._modes = {"current_budget": bool(initial), "current_budget_max_a": float(max_a)}
    svc._emitted = []
    svc._emit_status = lambda payload: svc._emitted.append(payload)
    svc._status_payload = lambda: {"modes": dict(svc._modes)}
    return svc


def test_get_reflects_mode():
    assert _svc(True).is_current_budget_enabled() is True
    assert _svc(False).is_current_budget_enabled() is False


def test_set_changes_and_emits_once():
    svc = _svc(True)
    changed = svc.set_current_budget_enabled(False)
    assert changed is True
    assert svc.is_current_budget_enabled() is False
    assert len(svc._emitted) == 1  # status pushed to ROV exactly once


def test_set_to_same_value_is_noop():
    svc = _svc(True)
    changed = svc.set_current_budget_enabled(True)
    assert changed is False
    assert svc._emitted == []  # no redundant status push


def test_toggle_flips_state():
    svc = _svc(True)
    assert svc.toggle_current_budget_enabled() is False
    assert svc.toggle_current_budget_enabled() is True
    assert svc.is_current_budget_enabled() is True


def test_set_max_a_updates_mode_and_emits():
    svc = _svc(True, max_a=22.0)
    changed = svc.set_current_budget_max_a(18.0)
    assert changed is True
    assert svc.current_budget_max_a() == 18.0
    assert svc.current_modes()["current_budget_max_a"] == 18.0
    assert len(svc._emitted) == 1


def test_set_max_a_clamps_to_bounds():
    svc = _svc(True, max_a=22.0, lo=5.0, hi=40.0)
    svc.set_current_budget_max_a(999.0)
    assert svc.current_budget_max_a() == 40.0
    svc.set_current_budget_max_a(-3.0)
    assert svc.current_budget_max_a() == 5.0


def test_set_max_a_same_value_is_noop():
    svc = _svc(True, max_a=22.0)
    assert svc.set_current_budget_max_a(22.0) is False
    assert svc._emitted == []


def test_set_max_a_rejects_garbage():
    svc = _svc(True, max_a=22.0)
    assert svc.set_current_budget_max_a("nope") is False
    assert svc.current_budget_max_a() == 22.0
