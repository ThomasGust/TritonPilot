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
        self.park_positions = []
        self.park_pitch = -1.0
        self.park_wrist = 1.0

    def set_arm_tune(self, key, value):
        self.tune_calls.append((key, value))

    def clear_arm_tune(self):
        self.clear_calls += 1

    def set_arm_position(self, pitch, wrist):
        self.positions.append((float(pitch), float(wrist)))
        return float(pitch), float(wrist)

    def set_arm_park_position(self, pitch, wrist):
        self.park_pitch = float(pitch)
        self.park_wrist = float(wrist)
        self.park_positions.append((self.park_pitch, self.park_wrist))
        return self.park_pitch, self.park_wrist

    def park_arm(self):
        return self.set_arm_position(self.park_pitch, self.park_wrist)


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
                "runtime": {"armed": True},
                "config": {
                    "GRIPPER_LEFT_INVERT": 1.0,
                    "GRIPPER_RIGHT_INVERT": -1.0,
                    "GRIPPER_PITCH_INVERT": 1.0,
                    "GRIPPER_YAW_INVERT": 1.0,
                    "GRIPPER_PITCH_SPAN_DEG": 90.0,
                    "GRIPPER_PITCH_NEUTRAL_DEG": 45.0,
                    "GRIPPER_PITCH_MIN": -0.80,
                    "GRIPPER_PITCH_MAX": 0.60,
                    "GRIPPER_YAW_MIN": -0.25,
                    "GRIPPER_YAW_MAX": 0.70,
                },
            }
        )

        assert page._arm_tune_checks["right_invert"].isChecked()
        assert not page._arm_tune_checks["left_invert"].isChecked()
        assert page._arm_tune_spins["servo_range_deg"].value() == 100.0
        assert page._arm_tune_spins["pitch_span_deg"].value() == 90.0
        assert page._arm_tune_spins["pitch_neutral_deg"].value() == 45.0
        assert page._arm_tune_spins["pitch_min"].value() == pytest.approx(-0.80)
        assert page._arm_tune_spins["pitch_max"].value() == pytest.approx(0.60)
        assert page._arm_tune_spins["yaw_min"].value() == pytest.approx(-0.25)
        assert page._arm_tune_spins["yaw_max"].value() == pytest.approx(0.70)
        assert pilot.tune_calls == []

        page._save_arm_tune_config()
        cmd, args = page._svc.requests[-1]
        assert cmd == "set_config"
        assert args["updates"]["GRIPPER_RIGHT_INVERT"] == -1.0
        assert args["updates"]["GRIPPER_PITCH_SPAN_DEG"] == 90.0
        assert args["updates"]["GRIPPER_PITCH_NEUTRAL_DEG"] == 45.0
        assert args["updates"]["GRIPPER_PITCH_MIN"] == pytest.approx(-0.80)
        assert args["updates"]["GRIPPER_PITCH_MAX"] == pytest.approx(0.60)
        assert args["updates"]["GRIPPER_YAW_MIN"] == pytest.approx(-0.25)
        assert args["updates"]["GRIPPER_YAW_MAX"] == pytest.approx(0.70)
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
                "runtime": {"armed": True},
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


def test_arm_alignment_pose_is_blocked_when_disarmed(monkeypatch):
    app = _app()
    monkeypatch.setattr(management_page, "ManagementRpcService", _FakeManagementRpcService)

    pilot = _FakePilotService()
    page = management_page.ManagementPage(endpoint="inproc://arm-align-disarmed-test", pilot_svc=pilot)
    try:
        page._apply_state(
            {
                "commands": ["get_state", "set_config"],
                "config_path": "rov_config.py",
                "references": {},
                "runtime": {"armed": False},
                "config": {},
            }
        )

        assert page._send_arm_alignment_pose("flat_wrist_90") is False
        assert pilot.positions == []
        assert "disarmed" in page.feedback_label.text()
    finally:
        page.shutdown()
        page.close()
        app.processEvents()


def test_arm_park_pose_loads_commands_and_saves_rov_config(monkeypatch):
    app = _app()
    monkeypatch.setattr(management_page, "ManagementRpcService", _FakeManagementRpcService)

    pilot = _FakePilotService()
    page = management_page.ManagementPage(endpoint="inproc://arm-park-test", pilot_svc=pilot)
    try:
        page._apply_state(
            {
                "commands": ["get_state", "set_config"],
                "config_path": "rov_config.py",
                "references": {},
                "runtime": {"armed": True},
                "config": {
                    "GRIPPER_PITCH_INVERT": -1.0,
                    "GRIPPER_YAW_INVERT": 1.0,
                    "GRIPPER_ARM_PITCH": -0.50,
                    "GRIPPER_ARM_YAW": 0.75,
                    "GRIPPER_DISARM_PITCH": -1.0,
                    "GRIPPER_DISARM_YAW": 1.0,
                },
            }
        )

        assert page._arm_park_spins["park_pitch"].value() == pytest.approx(-0.50)
        assert page._arm_park_spins["park_wrist"].value() == pytest.approx(0.75)
        assert pilot.park_positions[-1] == pytest.approx((0.50, 0.75))

        assert page._send_arm_park_pose() is True
        assert pilot.positions[-1] == pytest.approx((0.50, 0.75))

        page._arm_park_spins["park_pitch"].setValue(-0.25)
        page._arm_park_spins["park_wrist"].setValue(1.0)
        page._save_arm_park_config()
        cmd, args = page._svc.requests[-1]
        assert cmd == "set_config"
        assert args["updates"] == {
            "GRIPPER_DISARM_PITCH": -0.25,
            "GRIPPER_DISARM_YAW": 1.0,
            "GRIPPER_ARM_PITCH": -0.25,
            "GRIPPER_ARM_YAW": 1.0,
        }
    finally:
        page.shutdown()
        page.close()
        app.processEvents()


def test_arm_park_pose_command_respects_configured_limits(monkeypatch):
    app = _app()
    monkeypatch.setattr(management_page, "ManagementRpcService", _FakeManagementRpcService)

    pilot = _FakePilotService()
    page = management_page.ManagementPage(endpoint="inproc://arm-park-limit-test", pilot_svc=pilot)
    try:
        page._apply_state(
            {
                "commands": ["get_state", "set_config"],
                "config_path": "rov_config.py",
                "references": {},
                "runtime": {"armed": True},
                "config": {
                    "GRIPPER_PITCH_INVERT": 1.0,
                    "GRIPPER_YAW_INVERT": 1.0,
                    "GRIPPER_ARM_PITCH": 1.0,
                    "GRIPPER_ARM_YAW": 1.0,
                    "GRIPPER_PITCH_MIN": -0.50,
                    "GRIPPER_PITCH_MAX": 0.25,
                    "GRIPPER_YAW_MIN": -0.25,
                    "GRIPPER_YAW_MAX": 0.50,
                },
            }
        )

        assert pilot.park_positions[-1] == pytest.approx((0.25, 0.50))
        assert page._send_arm_park_pose() is True
        assert pilot.positions[-1] == pytest.approx((0.25, 0.50))
    finally:
        page.shutdown()
        page.close()
        app.processEvents()
