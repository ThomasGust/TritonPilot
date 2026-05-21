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
        return {"depth_hold": True, "roll_pitch_level": True}

    def toggle_depth_hold(self):
        return False

    def toggle_roll_pitch_level(self):
        return False


def _app() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def test_hold_test_panel_uses_scroll_layout_and_shows_depth_debug(monkeypatch):
    app = _app()
    monkeypatch.setattr("gui.instruments.ManagementRpcService", _RpcStub)

    panel = HoldTestPanel(pilot_svc=_PilotStub(), endpoint="tcp://127.0.0.1:5556")
    try:
        app.processEvents()
        assert panel.minimumWidth() >= 520
        assert panel.findChild(QScrollArea) is not None
        assert panel._runtime_labels["pilot_depth_hold"].text() == "ON"
        assert panel._runtime_labels["pilot_rp_level"].text() == "ON"

        panel._apply_runtime_state(
            {
                "control_loop_available": True,
                "armed": True,
                "autopilot": {
                    "available": True,
                    "sensor_available": True,
                    "status_age_s": 0.04,
                    "status": {
                        "attitude": {
                            "enabled_cmd": True,
                            "active": True,
                            "reason": "active",
                            "source": "onboard_imu_mag_relative",
                            "axes": {
                                "roll": {
                                    "mode": "level",
                                    "active": True,
                                    "error_deg": -2.0,
                                    "u_out": -0.02,
                                },
                                "pitch": {
                                    "mode": "level",
                                    "active": True,
                                    "error_deg": 1.0,
                                    "u_out": 0.01,
                                },
                            },
                        },
                    },
                    "attitude_sensor": {
                        "available": True,
                        "sample_age_s": 0.03,
                        "source": "onboard_imu_mag_relative",
                        "raw": {"roll_deg": 2.0, "pitch_deg": -1.0, "yaw_deg": 5.0},
                    },
                },
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
            }
        )

        assert "target 1.25 m" in panel._runtime_labels["runtime_depth_hold"].text()
        assert "available yes" in panel._runtime_labels["runtime_autopilot"].text()
        assert "active yes" in panel._runtime_labels["runtime_attitude"].text()
        assert "r 2.0 deg" in panel._runtime_labels["runtime_attitude_sensor"].text()
        assert "roll level" in panel._runtime_labels["runtime_attitude_debug"].text()
        assert "stream age 0.07 s" in panel._runtime_labels["runtime_depth_sensor"].text()
        assert "error -0.020 m" in panel._runtime_labels["runtime_depth_debug"].text()
        assert "out 0.030" in panel._runtime_labels["runtime_depth_debug"].text()
    finally:
        panel.shutdown()
        panel.close()
        panel.deleteLater()
        app.processEvents()
