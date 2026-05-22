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
        return {"depth_hold": True, "roll_pitch_level": True, "yaw_hold": True}

    def toggle_depth_hold(self):
        return False

    def toggle_roll_pitch_level(self):
        return False

    def toggle_yaw_hold(self):
        return False


class _PilotTargetStub:
    def __init__(self):
        self.calls = []
        self._modes = {
            "depth_hold": False,
            "roll_pitch_level": False,
            "yaw_hold": False,
            "autopilot": {"depth": False, "roll": "off", "pitch": "off", "yaw": "off", "targets": {}},
        }

    def current_modes(self):
        return {
            **self._modes,
            "autopilot": {
                **self._modes["autopilot"],
                "targets": dict(self._modes["autopilot"]["targets"]),
            },
        }

    def set_depth_hold_target(self, target_m, enable=True):
        self.calls.append(("depth_target", float(target_m), bool(enable)))
        self._modes["depth_hold"] = bool(enable)
        self._modes["autopilot"]["depth"] = bool(enable)
        self._modes["autopilot"]["targets"]["depth_m"] = float(target_m)
        return True

    def set_depth_hold_enabled(self, enabled):
        self.calls.append(("depth_enabled", bool(enabled)))
        self._modes["depth_hold"] = bool(enabled)
        self._modes["autopilot"]["depth"] = bool(enabled)
        return True

    def set_autopilot_axis_target(self, axis, target_deg, mode="hold"):
        self.calls.append(("axis_target", axis, float(target_deg), mode))
        self._modes["autopilot"][axis] = str(mode)
        self._modes["autopilot"]["targets"][f"{axis}_deg"] = float(target_deg)
        if axis == "yaw":
            self._modes["yaw_hold"] = str(mode) == "hold"
        return True

    def set_autopilot_axis_mode(self, axis, mode):
        self.calls.append(("axis_mode", axis, mode))
        self._modes["autopilot"][axis] = str(mode)
        return True


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
        assert panel._runtime_labels["pilot_yaw_hold"].text() == "ON"

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
                                "yaw": {
                                    "mode": "hold",
                                    "enabled_cmd": True,
                                    "active": True,
                                    "reason": "hold",
                                    "angle_deg": 5.0,
                                    "target_deg": 1.0,
                                    "error_deg": -4.0,
                                    "rate_dps": 0.2,
                                    "u_out": -0.024,
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
        assert "error -2.0 deg" in panel._runtime_labels["runtime_roll_hold_detail"].text()
        assert "error 1.0 deg" in panel._runtime_labels["runtime_pitch_hold_detail"].text()
        assert "current 5.0 deg" in panel._runtime_labels["runtime_yaw_hold_detail"].text()
        assert "target 1.0 deg" in panel._runtime_labels["runtime_yaw_hold_detail"].text()
        assert "error -4.0 deg" in panel._runtime_labels["runtime_yaw_hold_detail"].text()
        assert "r 2.0 deg" in panel._runtime_labels["runtime_attitude_sensor"].text()
        assert "roll level" in panel._runtime_labels["runtime_attitude_debug"].text()
        assert "yaw hold" in panel._runtime_labels["runtime_attitude_debug"].text()
        assert "stream age 0.07 s" in panel._runtime_labels["runtime_depth_sensor"].text()
        assert "error -0.020 m" in panel._runtime_labels["runtime_depth_debug"].text()
        assert "out 0.030" in panel._runtime_labels["runtime_depth_debug"].text()
    finally:
        panel.shutdown()
        panel.close()
        panel.deleteLater()
        app.processEvents()


def test_hold_test_panel_can_set_manual_targets(monkeypatch):
    app = _app()
    monkeypatch.setattr("gui.instruments.ManagementRpcService", _RpcStub)
    pilot = _PilotTargetStub()

    panel = HoldTestPanel(pilot_svc=pilot, endpoint="tcp://127.0.0.1:5556")
    try:
        app.processEvents()
        panel.depth_target_spin.setValue(-0.35)
        panel._axis_target_spins["roll"].setValue(-6.0)
        panel._axis_target_spins["pitch"].setValue(-4.0)
        panel._axis_target_spins["yaw"].setValue(-90.0)

        panel._hold_depth_target()
        panel._hold_axis_target("roll")
        panel._hold_axis_target("pitch")
        panel._hold_axis_target("yaw")
        panel._set_axis_mode("roll", "level")
        panel._depth_hold_off()

        assert panel.depth_target_spin.minimum() < 0.0
        for spin in panel._axis_target_spins.values():
            assert spin.minimum() < 0.0
        assert ("depth_target", -0.35, True) in pilot.calls
        assert ("axis_target", "roll", -6.0, "hold") in pilot.calls
        assert ("axis_target", "pitch", -4.0, "hold") in pilot.calls
        assert ("axis_target", "yaw", -90.0, "hold") in pilot.calls
        assert ("axis_mode", "roll", "level") in pilot.calls
        assert ("depth_enabled", False) in pilot.calls
    finally:
        panel.shutdown()
        panel.close()
        panel.deleteLater()
        app.processEvents()


def test_hold_test_panel_does_not_overwrite_spinbox_while_line_edit_has_focus(monkeypatch):
    app = _app()
    monkeypatch.setattr("gui.instruments.ManagementRpcService", _RpcStub)
    pilot = _PilotTargetStub()
    pilot.set_autopilot_axis_target("yaw", 45.0)

    panel = HoldTestPanel(pilot_svc=pilot, endpoint="tcp://127.0.0.1:5556")
    try:
        app.processEvents()
        spin = panel._axis_target_spins["yaw"]
        spin.lineEdit().setFocus()
        spin.lineEdit().selectAll()
        spin.lineEdit().setText("-90.0")
        app.processEvents()

        panel._sync_target_spins({"yaw_deg": 45.0})

        assert spin.lineEdit().text().startswith("-90")
    finally:
        panel.shutdown()
        panel.close()
        panel.deleteLater()
        app.processEvents()
