from tracking import (
    NullOpticalTracker,
    OpticalTracker,
    StationKeepCommand,
    VisualTargetError,
    station_keep_modes,
)


def test_no_lock_payload_is_minimal_and_invalid():
    payload = VisualTargetError.no_lock().to_visual_payload()
    assert payload["valid"] is False
    assert "ts" in payload
    # No error components when there's no lock.
    assert "ex" not in payload


def test_valid_payload_has_full_schema_and_clamps():
    err = VisualTargetError(valid=True, ex=2.0, ey=-2.0, es=0.5, violation=3.0, confidence=0.9, ts=123.0)
    payload = err.to_visual_payload()
    assert payload == {
        "valid": True,
        "ts": 123.0,
        "ex": 1.0,        # clamped to [-1, 1]
        "ey": -1.0,
        "es": 0.5,
        "violation": 1.0,  # clamped to [0, 1]
        "confidence": 0.9,
    }


def test_station_keep_modes_targets_autopilot_namespace():
    err = VisualTargetError(valid=True, ex=0.25, ts=5.0)
    modes = station_keep_modes(err, enabled=True)
    assert modes["autopilot"]["station_keep"] is True
    assert modes["autopilot"]["visual"]["ex"] == 0.25
    # Disabled toggle still carries the latest error payload.
    modes_off = station_keep_modes(err, enabled=False)
    assert modes_off["autopilot"]["station_keep"] is False


def test_station_keep_command_supports_full_dof_and_dynamic_depth():
    cmd = StationKeepCommand(
        error=VisualTargetError(valid=True, ex=0.2, ts=9.0),
        surge=0.3, sway=-0.2, heave=0.1, roll=0.0, pitch=0.0, yaw=0.5,
        depth_m=1.5, yaw_deg=30.0,
        depth_hold=True, yaw_hold=True,
    )
    ap = cmd.to_autopilot_modes()["autopilot"]
    # Direct DOF outputs (translation + roll/pitch/yaw) ride on the visual payload.
    direct = ap["visual"]["command"]
    assert direct["surge"] == 0.3 and direct["sway"] == -0.2 and direct["yaw"] == 0.5
    # Dynamic setpoints drive the depth/attitude holds.
    assert ap["targets"]["depth_m"] == 1.5
    assert ap["targets"]["yaw_deg"] == 30.0
    assert ap["depth"] is True
    assert ap["yaw"] == "hold"


def test_station_keep_command_direct_requires_valid_lock():
    cmd = StationKeepCommand(error=VisualTargetError.no_lock(), surge=0.5)
    ap = cmd.to_autopilot_modes()["autopilot"]
    assert ap["visual"]["valid"] is False
    assert "command" not in ap["visual"]  # no direct thrust without a lock


def test_station_keep_command_direct_outputs_are_clamped():
    cmd = StationKeepCommand(error=VisualTargetError(valid=True), surge=2.0, sway=-2.0)
    direct = cmd.to_autopilot_modes()["autopilot"]["visual"]["command"]
    assert direct["surge"] == 1.0
    assert direct["sway"] == -1.0


def test_null_tracker_is_an_optical_tracker_and_reports_no_lock():
    tracker = NullOpticalTracker()
    assert isinstance(tracker, OpticalTracker)
    err = tracker.process(frame=object())
    assert err.valid is False
    assert err.to_visual_payload()["valid"] is False
