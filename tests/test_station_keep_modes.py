import uuid

from input.pilot_service import PilotPublisherService


def _svc():
    # Construct without start() -> no controller/network, just the modes state.
    return PilotPublisherService(
        endpoint=f"inproc://sk_{uuid.uuid4().hex}", rate_hz=50.0, deadzone=0.0, debug=False
    )


def test_station_keep_defaults_off():
    svc = _svc()
    modes = svc.current_modes()
    assert modes["station_keep"] is False
    assert modes["autopilot"]["station_keep"] is False
    assert "visual" not in modes["autopilot"]
    assert svc.is_station_keep_enabled() is False


def test_enable_disable_station_keep_updates_modes():
    svc = _svc()
    assert svc.set_station_keep_enabled(True) is True
    modes = svc.current_modes()
    assert modes["station_keep"] is True
    assert modes["autopilot"]["station_keep"] is True
    assert svc.is_station_keep_enabled() is True

    # Idempotent: no change reported when already in the target state.
    assert svc.set_station_keep_enabled(True) is False

    assert svc.toggle_station_keep() is False
    assert svc.is_station_keep_enabled() is False


def test_visual_target_injection_and_cleared_on_disengage():
    svc = _svc()
    svc.set_station_keep_enabled(True)
    payload = {"valid": True, "ts": 1.0, "ex": 0.3, "command": {"sway": 0.2}}
    svc.set_visual_target(payload)

    ap = svc.current_modes()["autopilot"]
    assert ap["visual"]["ex"] == 0.3
    assert ap["visual"]["command"]["sway"] == 0.2

    # current_modes() returns a copy: mutating it must not affect the service.
    ap["visual"]["ex"] = 99.0
    ap["visual"]["command"]["sway"] = 99.0
    ap2 = svc.current_modes()["autopilot"]
    assert ap2["visual"]["ex"] == 0.3
    assert ap2["visual"]["command"]["sway"] == 0.2

    # Disengaging drops the stale lock so the ROV can't act on it.
    svc.set_station_keep_enabled(False)
    assert "visual" not in svc.current_modes()["autopilot"]


def test_clear_visual_target():
    svc = _svc()
    svc.set_station_keep_enabled(True)
    svc.set_visual_target({"valid": True, "ts": 1.0})
    assert "visual" in svc.current_modes()["autopilot"]
    svc.clear_visual_target()
    assert "visual" not in svc.current_modes()["autopilot"]
