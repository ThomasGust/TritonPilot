import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtWidgets import QApplication

from gui import management_page


class _FakeManagementRpcService:
    def __init__(self, endpoint, on_result=None, timeout_ms=0):
        self.endpoint = endpoint
        self.on_result = on_result
        self.timeout_ms = timeout_ms
        self.requests = []

    def start(self):
        pass

    def stop(self):
        pass

    def request(self, cmd, args=None):
        self.requests.append((cmd, dict(args or {})))
        return len(self.requests)


class _FakePilotService:
    def __init__(self):
        self.tune_calls = []
        self.clear_calls = 0
        self.positions = []
        self.park_calls = []
        self.range_calls = []

    def set_arm_tune(self, key, value):
        self.tune_calls.append((key, value))

    def clear_arm_tune(self):
        self.clear_calls += 1

    def set_arm_position(self, pitch, wrist):
        self.positions.append((float(pitch), float(wrist)))
        return float(pitch), float(wrist)

    def set_arm_park_position(self, pitch, wrist):
        self.park_calls.append((float(pitch), float(wrist)))
        return float(pitch), float(wrist)

    def set_arm_range(self, pitch_min, pitch_max, wrist_min, wrist_max):
        self.range_calls.append((float(pitch_min), float(pitch_max), float(wrist_min), float(wrist_max)))


def _app():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def test_arm_tuning_controls_load_and_save_rov_config(monkeypatch):
    app = _app()
    monkeypatch.setattr(management_page, "ManagementRpcService", _FakeManagementRpcService)

    pilot = _FakePilotService()
    page = management_page.ManagementPage(endpoint="inproc://arm-tune-test", pilot_svc=pilot)
    try:
        page._apply_state(
            {
                "commands": ["get_state", "set_config"],
                "config_path": "rov_config.py",
                "references": {},
                "config": {
                    "GRIPPER_LEFT_INVERT": 1.0,
                    "GRIPPER_RIGHT_INVERT": -1.0,
                    "GRIPPER_PITCH_INVERT": 1.0,
                    "GRIPPER_YAW_INVERT": 1.0,
                    "GRIPPER_PITCH_SPAN_DEG": 90.0,
                    "GRIPPER_PITCH_NEUTRAL_DEG": 45.0,
                },
            }
        )

        assert page._arm_tune_checks["right_invert"].isChecked()
        assert not page._arm_tune_checks["left_invert"].isChecked()
        assert page._arm_tune_spins["servo_range_deg"].value() == 100.0
        assert page._arm_tune_spins["pitch_span_deg"].value() == 90.0
        assert page._arm_tune_spins["pitch_neutral_deg"].value() == 45.0
        assert pilot.tune_calls == []

        page._save_arm_tune_config()
        cmd, args = page._svc.requests[-1]
        assert cmd == "set_config"
        assert args["updates"]["GRIPPER_RIGHT_INVERT"] == -1.0
        assert args["updates"]["GRIPPER_PITCH_SPAN_DEG"] == 90.0
        assert args["updates"]["GRIPPER_PITCH_NEUTRAL_DEG"] == 45.0
    finally:
        page.shutdown()
        page.close()
        app.processEvents()


def test_arm_limits_controls_load_save_and_stream(monkeypatch):
    app = _app()
    monkeypatch.setattr(management_page, "ManagementRpcService", _FakeManagementRpcService)

    pilot = _FakePilotService()
    page = management_page.ManagementPage(endpoint="inproc://arm-limits-test", pilot_svc=pilot)
    try:
        page._apply_state(
            {
                "commands": ["get_state", "set_config"],
                "config_path": "rov_config.py",
                "references": {},
                "config": {
                    "GRIPPER_DISARM_PITCH": -1.0,
                    "GRIPPER_DISARM_YAW": 1.0,
                    "GRIPPER_PITCH_MIN_NORM": -0.6,
                    "GRIPPER_PITCH_MAX_NORM": 0.6,
                    "GRIPPER_WRIST_MIN_NORM": -1.0,
                    "GRIPPER_WRIST_MAX_NORM": -0.8,
                },
            }
        )

        # Spins seeded from rov_config.
        assert page._arm_park_spins["park_pitch"].value() == pytest.approx(-1.0)
        assert page._arm_park_spins["park_wrist"].value() == pytest.approx(1.0)
        assert page._arm_limit_spins["pitch_min"].value() == pytest.approx(-0.6)
        assert page._arm_limit_spins["wrist_max"].value() == pytest.approx(-0.8)

        # Seeding pushes the pilot park + range but does NOT stream arm_tune
        # (the ROV already booted with these rov_config values).
        assert pilot.park_calls[-1] == pytest.approx((-1.0, 1.0))
        assert pilot.range_calls[-1] == pytest.approx((-0.6, 0.6, -1.0, -0.8))
        assert pilot.tune_calls == []

        # An operator edit streams the live arm_tune override to the ROV.
        page._arm_limit_spins["wrist_max"].setValue(-0.5)
        assert dict(pilot.tune_calls)["wrist_max"] == pytest.approx(-0.5)

        # Save persists all six keys to rov_config.
        page._save_arm_limits_config()
        cmd, args = page._svc.requests[-1]
        assert cmd == "set_config"
        updates = args["updates"]
        assert updates["GRIPPER_DISARM_PITCH"] == pytest.approx(-1.0)
        assert updates["GRIPPER_DISARM_YAW"] == pytest.approx(1.0)
        assert updates["GRIPPER_PITCH_MIN_NORM"] == pytest.approx(-0.6)
        assert updates["GRIPPER_WRIST_MAX_NORM"] == pytest.approx(-0.5)
    finally:
        page.shutdown()
        page.close()
        app.processEvents()


def test_arm_alignment_pose_sets_pilot_target_and_previews_servo_pulses(monkeypatch):
    app = _app()
    monkeypatch.setattr(management_page, "ManagementRpcService", _FakeManagementRpcService)

    pilot = _FakePilotService()
    page = management_page.ManagementPage(endpoint="inproc://arm-align-test", pilot_svc=pilot)
    try:
        page._apply_state(
            {
                "commands": ["get_state", "set_config"],
                "config_path": "rov_config.py",
                "references": {},
                "runtime": {"armed": True},
                "config": {
                    "GRIPPER_LEFT_INVERT": 1.0,
                    "GRIPPER_RIGHT_INVERT": -1.0,
                    "GRIPPER_PITCH_INVERT": 1.0,
                    "GRIPPER_YAW_INVERT": 1.0,
                    "GRIPPER_SERVO_RANGE_DEG": 100.0,
                    "GRIPPER_PITCH_SPAN_DEG": 90.0,
                    "GRIPPER_WRIST_SPAN_DEG": 90.0,
                    "GRIPPER_PITCH_NEUTRAL_DEG": 45.0,
                    "GRIPPER_WRIST_NEUTRAL_DEG": 45.0,
                    "GRIPPER_SERVO_CENTER_US": 1500,
                    "GRIPPER_SERVO_PULSE_HALFSPAN_US": 800.0,
                },
            }
        )

        assert page._send_arm_alignment_pose("flat_wrist_90") is True

        assert pilot.positions[-1] == (-1.0, 1.0)
        status = page.arm_alignment_status_label.text()
        assert "Flat / Wrist 90" in status
        assert "left +0.000 (1500 us)" in status
        assert "right +0.900 (2220 us)" in status
    finally:
        page.shutdown()
        page.close()
        app.processEvents()


def test_arm_alignment_pose_compensates_axis_inverts(monkeypatch):
    app = _app()
    monkeypatch.setattr(management_page, "ManagementRpcService", _FakeManagementRpcService)

    pilot = _FakePilotService()
    page = management_page.ManagementPage(endpoint="inproc://arm-align-invert-test", pilot_svc=pilot)
    try:
        page._apply_state(
            {
                "commands": ["get_state", "set_config"],
                "config_path": "rov_config.py",
                "references": {},
                "config": {
                    "GRIPPER_PITCH_INVERT": -1.0,
                    "GRIPPER_YAW_INVERT": -1.0,
                    "GRIPPER_PITCH_SPAN_DEG": 90.0,
                    "GRIPPER_WRIST_SPAN_DEG": 90.0,
                    "GRIPPER_PITCH_NEUTRAL_DEG": 45.0,
                    "GRIPPER_WRIST_NEUTRAL_DEG": 45.0,
                },
            }
        )

        assert page._send_arm_alignment_pose("flat_wrist_0") is True

        assert pilot.positions[-1] == (1.0, 1.0)
    finally:
        page.shutdown()
        page.close()
        app.processEvents()
