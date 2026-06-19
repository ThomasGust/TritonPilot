"""Pilot-side differential-arm position integrator."""

from types import SimpleNamespace

import pytest

from config import ARM_INIT_PITCH, ARM_RATE
from input.pilot_service import PilotPublisherService


def _svc(name: str) -> PilotPublisherService:
    return PilotPublisherService(endpoint=f"inproc://{name}", rate_hz=50.0, deadzone=0.0, debug=False)


def test_keyboard_intent_integrates_pitch_position():
    svc = _svc("arm_kb")
    p0, w0 = svc.arm_position()
    assert p0 == pytest.approx(ARM_INIT_PITCH)

    svc.set_arm_keyboard_intent(1.0, 0.0)
    p, w = svc._integrate_arm(SimpleNamespace(rx=0.0, ry=0.0), modifier_held=False, dt=0.1)

    gain = svc.current_arm_gain()
    assert p == pytest.approx(min(1.0, p0 + ARM_RATE * gain * 0.1))
    assert w == pytest.approx(w0)  # wrist untouched


def test_stick_requires_modifier_and_drives_pitch():
    svc = _svc("arm_stick")
    svc.set_arm_keyboard_intent(0.0, 0.0)
    snap = SimpleNamespace(rx=0.0, ry=1.0)  # full deflection on the pitch axis

    # Without the modifier the stick is ignored (it is driving the ROV).
    p_before, _ = svc.arm_position()
    p_idle, _ = svc._integrate_arm(snap, modifier_held=False, dt=0.1)
    assert p_idle == pytest.approx(p_before)

    # With the modifier held the stick advances the arm pitch.
    p_held, _ = svc._integrate_arm(snap, modifier_held=True, dt=0.1)
    assert p_held > p_idle


def test_stick_deadzone_blocks_small_input():
    svc = _svc("arm_dz")
    svc.set_arm_keyboard_intent(0.0, 0.0)
    _, w0 = svc.arm_position()
    # rx below the 0.12 deadzone -> no wrist motion even with the modifier held.
    _, w = svc._integrate_arm(SimpleNamespace(rx=0.05, ry=0.0), modifier_held=True, dt=0.1)
    assert w == pytest.approx(w0)


def test_arm_gain_scales_speed():
    fast = _svc("arm_fast")
    fast._arm_gain = 1.0
    fast.set_arm_keyboard_intent(1.0, 0.0)
    p_fast, _ = fast._integrate_arm(SimpleNamespace(rx=0.0, ry=0.0), modifier_held=False, dt=0.05)

    slow = _svc("arm_slow")
    slow._arm_gain = 0.5
    slow.set_arm_keyboard_intent(1.0, 0.0)
    p_slow, _ = slow._integrate_arm(SimpleNamespace(rx=0.0, ry=0.0), modifier_held=False, dt=0.05)

    moved_fast = p_fast - ARM_INIT_PITCH
    moved_slow = p_slow - ARM_INIT_PITCH
    assert moved_fast == pytest.approx(2.0 * moved_slow)


def test_arm_tune_overrides_ride_in_modes():
    svc = _svc("arm_tune")
    assert svc.current_arm_tune() == {}

    svc.set_arm_tune("right_invert", -1.0)
    svc.set_arm_tune("pitch_neutral_deg", 30.0)
    svc.set_arm_tune("servo_range_deg", 100.0)
    svc.set_arm_tune("pitch_span_deg", 90.0)
    svc.set_arm_tune("bogus_key", 5.0)  # ignored

    tune = svc.current_modes()["arm_tune"]
    assert tune["right_invert"] == -1.0
    assert tune["pitch_neutral_deg"] == 30.0
    assert tune["servo_range_deg"] == 100.0
    assert tune["pitch_span_deg"] == 90.0
    assert "bogus_key" not in tune

    svc.set_arm_tune("right_invert", None)  # clear one key
    assert "right_invert" not in svc.current_arm_tune()

    svc.clear_arm_tune()
    assert svc.current_arm_tune() == {}


def test_clear_keyboard_intent_stops_motion():
    svc = _svc("arm_clear")
    svc.set_arm_keyboard_intent(1.0, 1.0)
    svc.clear_arm_keyboard_intent()
    p0, w0 = svc.arm_position()
    p, w = svc._integrate_arm(SimpleNamespace(rx=0.0, ry=0.0), modifier_held=False, dt=0.1)
    assert p == pytest.approx(p0)
    assert w == pytest.approx(w0)
