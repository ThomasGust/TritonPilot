import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

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

    def set_arm_tune(self, key, value):
        self.tune_calls.append((key, value))

    def clear_arm_tune(self):
        self.clear_calls += 1


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
