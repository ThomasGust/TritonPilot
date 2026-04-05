import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt6")

from PyQt6.QtWidgets import QApplication, QScrollArea

from gui.instruments import HoldTestPanel


class _RpcStub:
    def __init__(self, endpoint=None, on_result=None, timeout_ms=8000):
        self.endpoint = endpoint
        self.on_result = on_result
        self.timeout_ms = timeout_ms
        self.requests = []

    def start(self) -> None:
        return

    def stop(self) -> None:
        return

    def request(self, cmd: str, args=None, meta=None) -> int:
        self.requests.append((cmd, dict(args or {}), dict(meta or {})))
        return len(self.requests)


class _PilotStub:
    def current_modes(self):
        return {"depth_hold": True, "attitude_hold": True}

    def toggle_depth_hold(self):
        return False

    def toggle_attitude_hold(self):
        return False


def _app() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def test_hold_test_panel_uses_scroll_layout_and_shows_axis_debug(monkeypatch):
    app = _app()
    monkeypatch.setattr("gui.instruments.ManagementRpcService", _RpcStub)

    panel = HoldTestPanel(pilot_svc=_PilotStub(), endpoint="tcp://127.0.0.1:5556")
    try:
        app.processEvents()
        assert panel.minimumWidth() >= 520
        assert panel.findChild(QScrollArea) is not None
        assert panel._runtime_labels["pilot_depth_hold"].text() == "ON"
        assert panel._runtime_labels["pilot_attitude_hold"].text() == "ON"

        panel._apply_runtime_state(
            {
                "control_loop_available": True,
                "armed": True,
                "depth_hold": {
                    "available": True,
                    "sensor_available": True,
                    "target_m": 1.25,
                    "status_age_s": 0.08,
                    "status": {
                        "enabled_cmd": True,
                        "active": True,
                        "reason": "hold",
                        "depth_f_m": 1.23,
                        "error_m": -0.02,
                        "dz_mps": 0.00,
                        "u_out": 0.03,
                    },
                    "sensor": {
                        "depth_m": 1.24,
                        "sample_age_s": 0.05,
                        "stream_age_s": 0.07,
                        "sensor_name": "bar30",
                    },
                },
                "attitude_hold": {
                    "available": True,
                    "sensor_available": True,
                    "target_pitch_deg": 0.0,
                    "target_roll_deg": 0.0,
                    "status_age_s": 0.06,
                    "status": {
                        "enabled_cmd": True,
                        "active": True,
                        "reason": "hold",
                        "pitch": {
                            "angle_f_deg": 1.2,
                            "target_deg": 0.0,
                            "raw_error_deg": 1.2,
                            "error_deg": 0.0,
                            "da_dps": 0.4,
                            "u_out": 0.0,
                            "within_deadband": True,
                        },
                        "roll": {
                            "angle_f_deg": -1.7,
                            "target_deg": 0.0,
                            "raw_error_deg": -1.7,
                            "error_deg": 0.0,
                            "da_dps": 0.6,
                            "u_out": 0.0,
                            "within_deadband": True,
                        },
                    },
                    "sensor": {
                        "pitch_deg": 1.3,
                        "roll_deg": -1.8,
                        "yaw_deg": 90.0,
                        "sample_age_s": 0.05,
                        "stream_age_s": 0.09,
                    },
                },
            }
        )

        assert "stream age 0.09 s" in panel._runtime_labels["runtime_attitude_sensor"].text()
        assert panel._runtime_labels["runtime_attitude_target"].text() == "p 0.0 deg | r 0.0 deg"
        assert "raw err 1.2 deg" in panel._runtime_labels["runtime_attitude_pitch_debug"].text()
        assert "deadband yes" in panel._runtime_labels["runtime_attitude_pitch_debug"].text()
        assert "raw err -1.7 deg" in panel._runtime_labels["runtime_attitude_roll_debug"].text()
    finally:
        panel.shutdown()
        panel.close()
        panel.deleteLater()
        app.processEvents()
