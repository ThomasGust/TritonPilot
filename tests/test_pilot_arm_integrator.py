"""Pilot-side differential-arm position integrator (gamepad-only, armed-gated)."""

from types import SimpleNamespace

import pytest

from config import (
    ARM_INIT_PITCH,
    ARM_INIT_WRIST,
    ARM_PARK_PITCH,
    ARM_PARK_WRIST,
    ARM_RATE,
)
from input.pilot_service import PilotPublisherService


def _svc(name: str) -> PilotPublisherService:
    return PilotPublisherService(endpoint=f"inproc://{name}", rate_hz=50.0, deadzone=0.0, debug=False)


def _armed_svc(name: str) -> PilotPublisherService:
    svc = _svc(name)
    svc.set_armed(True)
    return svc


def test_starts_at_init_pose_and_disarmed():
    svc = _svc("arm_init")
    p0, w0 = svc.arm_position()
    assert p0 == pytest.approx(ARM_INIT_PITCH)
    assert w0 == pytest.approx(ARM_INIT_WRIST)
    # Default disarmed: the integrator is frozen until a heartbeat arms it.
    assert svc.is_arm_frozen() is True


def test_disarmed_integrator_is_frozen():
    svc = _svc("arm_frozen")
    svc.set_arm_position(0.0, 0.0)
    snap = SimpleNamespace(rx=1.0, ry=1.0)  # full stick deflection
    p, w = svc._integrate_arm(snap, modifier_held=True, dt=0.1)
    # No motion while disarmed even with the modifier held and the stick pinned.
    assert p == pytest.approx(0.0)
    assert w == pytest.approx(0.0)


def test_stick_requires_modifier_and_drives_pitch_when_armed():
    svc = _armed_svc("arm_stick")
    svc.set_arm_position(0.0, 0.0)
    snap = SimpleNamespace(rx=0.0, ry=1.0)  # full deflection on the pitch axis

    # Without the modifier the stick is ignored (it is driving the ROV).
    p_before, _ = svc.arm_position()
    p_idle, _ = svc._integrate_arm(snap, modifier_held=False, dt=0.1)
    assert p_idle == pytest.approx(p_before)

    # With the modifier held the stick reverses the raw controller pitch direction.
    p_held, _ = svc._integrate_arm(snap, modifier_held=True, dt=0.1)
    assert p_held < p_idle


def test_stick_deadzone_blocks_small_input():
    svc = _armed_svc("arm_dz")
    svc.set_arm_position(0.0, 0.0)
    _, w0 = svc.arm_position()
    # rx below the 0.12 deadzone -> no wrist motion even with the modifier held.
    _, w = svc._integrate_arm(SimpleNamespace(rx=0.05, ry=0.0), modifier_held=True, dt=0.1)
    assert w == pytest.approx(w0)


def test_arm_gain_scales_speed():
    fast = _armed_svc("arm_fast")
    fast._arm_gain = 1.0
    fast.set_arm_position(0.0, 0.0)
    p_fast, _ = fast._integrate_arm(SimpleNamespace(rx=0.0, ry=1.0), modifier_held=True, dt=0.05)

    slow = _armed_svc("arm_slow")
    slow._arm_gain = 0.5
    slow.set_arm_position(0.0, 0.0)
    p_slow, _ = slow._integrate_arm(SimpleNamespace(rx=0.0, ry=1.0), modifier_held=True, dt=0.05)

    assert p_fast == pytest.approx(-ARM_RATE * 1.0 * 0.05)
    assert p_slow == pytest.approx(-ARM_RATE * 0.5 * 0.05)
    assert p_fast == pytest.approx(2.0 * p_slow)


def test_disarm_snaps_to_park_and_freezes():
    svc = _armed_svc("arm_park_snap")
    svc.set_arm_position(0.3, -0.2)

    svc.set_armed(False)
    p, w = svc.arm_position()
    assert p == pytest.approx(ARM_PARK_PITCH)
    assert w == pytest.approx(ARM_PARK_WRIST)

    # Frozen at park: full stick + modifier does not move it while disarmed.
    p2, w2 = svc._integrate_arm(SimpleNamespace(rx=1.0, ry=1.0), modifier_held=True, dt=0.1)
    assert p2 == pytest.approx(ARM_PARK_PITCH)
    assert w2 == pytest.approx(ARM_PARK_WRIST)


def test_move_arm_to_park_commands_the_park_pose():
    svc = _armed_svc("arm_to_park")
    svc.set_arm_park_position(-1.0, 0.5)
    svc.set_arm_position(0.2, -0.3)  # somewhere else

    p, w = svc.move_arm_to_park()
    assert (p, w) == pytest.approx((-1.0, 0.5))
    assert svc.arm_position() == pytest.approx((-1.0, 0.5))


def test_set_arm_park_position_updates_held_pose_while_disarmed():
    svc = _svc("arm_park_set")  # disarmed by default
    pp, pw = svc.set_arm_park_position(0.2, -0.3)
    assert (pp, pw) == pytest.approx((0.2, -0.3))
    # While disarmed the held target follows the new park pose.
    assert svc.arm_position() == pytest.approx((0.2, -0.3))


def test_range_clamp_limits_pilot_travel():
    svc = _armed_svc("arm_range")
    svc.set_arm_range(-0.5, 0.5, -1.0, 1.0)
    svc.set_arm_position(0.0, 0.0)
    # Drive pitch hard negative; it must stop at the configured pitch_min.
    snap = SimpleNamespace(rx=0.0, ry=1.0)  # ry -> negative pitch intent (invert)
    p = 0.0
    for _ in range(6):
        p, _w = svc._integrate_arm(snap, modifier_held=True, dt=0.1)
    assert p == pytest.approx(-0.5)


def test_range_clamp_pulls_target_into_range_on_next_frame():
    svc = _armed_svc("arm_range_pull")
    svc.set_arm_position(0.0, 1.0)  # wrist above the limit set next
    svc.set_arm_range(-1.0, 1.0, -1.0, -0.8)  # wrist limited to <= -0.8
    # The integrator clamps the streamed target to the configured range (the ROV
    # applies the same clamp), so the wrist target lands at the -0.8 limit.
    _p, w = svc._integrate_arm(SimpleNamespace(rx=0.0, ry=0.0), modifier_held=False, dt=0.1)
    assert w == pytest.approx(-0.8)


def test_arm_tune_overrides_ride_in_modes():
    svc = _svc("arm_tune")
    assert svc.current_arm_tune() == {}

    svc.set_arm_tune("right_invert", -1.0)
    svc.set_arm_tune("pitch_neutral_deg", 30.0)
    svc.set_arm_tune("pitch_min", -0.5)
    svc.set_arm_tune("wrist_max", -0.8)
    svc.set_arm_tune("bogus_key", 5.0)  # ignored

    tune = svc.current_modes()["arm_tune"]
    assert tune["right_invert"] == -1.0
    assert tune["pitch_neutral_deg"] == 30.0
    assert tune["pitch_min"] == -0.5
    assert tune["wrist_max"] == -0.8
    assert "bogus_key" not in tune

    svc.set_arm_tune("pitch_min", None)  # clear one key
    assert "pitch_min" not in svc.current_arm_tune()

    svc.clear_arm_tune()
    assert svc.current_arm_tune() == {}


def test_set_arm_position_clamps_to_unit_not_operating_range():
    svc = _armed_svc("arm_set_pose")
    svc.set_arm_range(-0.5, 0.5, -0.5, 0.5)

    # Deliberate alignment poses bypass the operating range (full [-1, 1]).
    p, w = svc.set_arm_position(-2.0, 1.0)
    assert p == pytest.approx(-1.0)
    assert w == pytest.approx(1.0)
