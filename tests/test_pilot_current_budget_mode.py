"""The pilot service exposes a live enable/disable for the ROV current limiter."""

import threading

from input.pilot_service import PilotPublisherService


def _svc(initial: bool, *, max_a: float = 22.0, lo: float = 5.0, hi: float = 40.0,
         volts: float = 14.0, v_lo: float = 10.0, v_hi: float = 20.0,
         max_gain: float = 0.8, g_lo: float = 0.05, g_hi: float = 0.8):
    svc = object.__new__(PilotPublisherService)
    svc._mode_lock = threading.RLock()
    svc._current_budget_max_a_min = float(lo)
    svc._current_budget_max_a_max = float(hi)
    svc._current_budget_max_a = float(max_a)
    svc._current_budget_voltage_min = float(v_lo)
    svc._current_budget_voltage_max = float(v_hi)
    svc._current_budget_voltage_v = float(volts)
    svc._max_gain_min = float(g_lo)
    svc._max_gain_max = float(g_hi)
    svc._max_gain = float(max_gain)
    svc._modes = {
        "current_budget": bool(initial),
        "current_budget_max_a": float(max_a),
        "current_budget_voltage_v": float(volts),
        "max_gain": float(max_gain),
    }
    svc._emitted = []
    svc._emit_status = lambda payload: svc._emitted.append(payload)
    svc._status_payload = lambda *a, **k: {"modes": dict(svc._modes)}
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


def test_set_voltage_updates_mode_and_emits():
    svc = _svc(True, volts=14.0)
    changed = svc.set_current_budget_voltage_v(15.5)
    assert changed is True
    assert svc.current_budget_voltage_v() == 15.5
    assert svc.current_modes()["current_budget_voltage_v"] == 15.5
    assert len(svc._emitted) == 1


def test_set_voltage_clamps_and_rejects_garbage():
    svc = _svc(True, volts=14.0, v_lo=10.0, v_hi=20.0)
    svc.set_current_budget_voltage_v(999.0)
    assert svc.current_budget_voltage_v() == 20.0
    svc.set_current_budget_voltage_v(1.0)
    assert svc.current_budget_voltage_v() == 10.0
    assert svc.set_current_budget_voltage_v("nope") is False


def test_set_max_gain_absolute_clamps_and_emits():
    svc = _svc(True, max_gain=0.8, g_lo=0.05, g_hi=0.8)
    assert svc.set_max_gain(0.5) is True
    assert svc.current_max_gain() == 0.5
    assert svc.current_modes()["max_gain"] == 0.5
    # Clamp to the configured ceiling.
    svc.set_max_gain(999.0)
    assert svc.current_max_gain() == 0.8
    # No-op when unchanged.
    svc._emitted.clear()
    assert svc.set_max_gain(0.8) is False
    assert svc._emitted == []
