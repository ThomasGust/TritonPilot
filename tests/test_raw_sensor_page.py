import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import csv

import pytest

pytest.importorskip("PyQt6")

from PyQt6.QtWidgets import QApplication

import config
from gui.raw_sensor_page import Attitude3DWidget, RawSensorPage


def _app() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def test_raw_sensor_page_updates_vectors_and_records_csv(tmp_path):
    app = _app()
    page = RawSensorPage(recording_session_provider=lambda: tmp_path)
    try:
        page.start_recording()
        page.record_message(
            {
                "ts": 10.0,
                "sensor": "imu",
                "type": "imu",
                "accel": {"x": 0.0, "y": 0.0, "z": 1.0},
                "gyro": {"x": 0.1, "y": 0.2, "z": 0.3},
                "mag": {"x": 20.0, "y": 0.0, "z": 40.0},
                "mag_source": "ak09915",
                "mag_sources": {
                    "ak09915": {"x": 20.0, "y": 0.0, "z": 40.0},
                    "mmc5983": {"x": 21.0, "y": 1.0, "z": 39.0},
                },
            }
        )
        page.update_from_sensor(
            {
                "ts": 10.0,
                "sensor": "imu",
                "type": "imu",
                "accel": {"x": 0.0, "y": 0.0, "z": 1.0},
                "gyro": {"x": 0.1, "y": 0.2, "z": 0.3},
                "mag": {"x": 20.0, "y": 0.0, "z": 40.0},
                "mag_source": "ak09915",
                "mag_sources": {
                    "ak09915": {"x": 20.0, "y": 0.0, "z": 40.0},
                    "mmc5983": {"x": 21.0, "y": 1.0, "z": 39.0},
                },
            }
        )
        app.processEvents()

        assert "x 0.000" in page._labels["accel"].text()
        assert "ak09915" in page._labels["mag"].text()
        assert len(page.accel_plot.samples) == 1
        assert len(page.ak_plot.samples) == 1
        assert len(page.mmc_plot.samples) == 1

        page.stop_recording()
        rows = list(csv.DictReader((tmp_path / "raw_sensor_timeseries.csv").open(newline="", encoding="utf-8")))
        assert len(rows) == 1
        assert rows[0]["sensor"] == "imu"
        assert float(rows[0]["mag_norm"]) > 40.0
    finally:
        page.shutdown()
        page.close()
        page.deleteLater()
        app.processEvents()


def test_raw_sensor_page_fallback_estimator_uses_configured_vehicle_axis(tmp_path, monkeypatch):
    app = _app()
    monkeypatch.setattr(config, "ATTITUDE_VEHICLE_ROLL_AXIS", "z")
    page = RawSensorPage(recording_session_provider=lambda: tmp_path)
    try:
        assert page._attitude_estimator.config.vehicle_roll_axis == "z"
    finally:
        page.shutdown()
        page.close()
        page.deleteLater()
        app.processEvents()


def test_raw_sensor_page_updates_separate_mag_and_attitude_rows(tmp_path):
    app = _app()
    page = RawSensorPage(recording_session_provider=lambda: tmp_path)
    try:
        page.start_recording()
        mag_msg = {
            "ts": 20.0,
            "sensor": "mag",
            "type": "mag",
            "mag": {"x": 50.0, "y": 10.0, "z": -5.0},
            "mag_source": "ak09915",
            "mag_sources": {
                "ak09915": {"x": 50.0, "y": 10.0, "z": -5.0},
                "mmc5983": {"x": 35.0, "y": -44.0, "z": -9.0},
            },
        }
        page.record_message(mag_msg)
        page.update_from_sensor(mag_msg)

        attitude = None
        for i in range(36):
            msg = {
                "ts": 30.0 + i * 0.05,
                "sensor": "imu",
                "type": "imu",
                "accel": {"x": 3.379, "y": -9.315, "z": -0.232},
                "gyro": {"x": -0.0178, "y": -0.0102, "z": 0.0077},
            }
            derived = page.record_message(msg)
            if derived:
                attitude = derived[-1]
                page.update_from_sensor(attitude)

        app.processEvents()

        assert "50.00" in page._labels["ak"].text()
        assert len(page.ak_plot.samples) == 1
        assert attitude is not None
        assert attitude["type"] == "attitude"
        assert abs(attitude["roll_deg"]) < 0.05
        assert len(page.attitude_plot.samples) >= 1
        assert page.attitude_view.roll_deg == pytest.approx(attitude["roll_deg"])

        page.stop_recording()
        rows = list(csv.DictReader((tmp_path / "raw_sensor_timeseries.csv").open(newline="", encoding="utf-8")))
        assert any(row["type"] == "mag" for row in rows)
        assert any(row["type"] == "attitude" for row in rows)
    finally:
        page.shutdown()
        page.close()
        page.deleteLater()
        app.processEvents()


def test_raw_sensor_page_does_not_clear_mag_on_plain_imu(tmp_path):
    app = _app()
    page = RawSensorPage(recording_session_provider=lambda: tmp_path)
    try:
        mag_msg = {
            "ts": 20.0,
            "sensor": "mag",
            "type": "mag",
            "mag": {"x": 50.0, "y": 10.0, "z": -5.0},
            "mag_source": "ak09915",
            "mag_sources": {
                "ak09915": {"x": 50.0, "y": 10.0, "z": -5.0},
                "mmc5983": {"x": 35.0, "y": -44.0, "z": -9.0},
            },
        }
        page.update_from_sensor(mag_msg)
        before_text = page._labels["mag"].text()
        before_samples = len(page.mag_plot.samples)

        page.update_from_sensor(
            {
                "ts": 20.05,
                "sensor": "imu",
                "type": "imu",
                "accel": {"x": 0.0, "y": 0.0, "z": 1.0},
                "gyro": {"x": 0.0, "y": 0.0, "z": 0.0},
            }
        )
        app.processEvents()

        assert page._labels["mag"].text() == before_text
        assert len(page.mag_plot.samples) == before_samples
    finally:
        page.shutdown()
        page.close()
        page.deleteLater()
        app.processEvents()


def test_raw_sensor_page_depth_plot_and_rest_zero(tmp_path):
    app = _app()
    page = RawSensorPage(recording_session_provider=lambda: tmp_path)
    try:
        depth_msg = {
            "ts": 50.0,
            "sensor": "external_depth",
            "type": "external_depth",
            "depth_m": 1.25,
            "depth_sensor_m": 1.40,
            "pressure_mbar": 1125.0,
            "temperature_c": 12.5,
        }
        page.update_from_sensor(depth_msg)
        app.processEvents()

        assert "depth 1.250 m" in page._labels["depth"].text()
        assert "raw 1.250 m" in page._labels["depth"].text()
        assert page.depth_plot.samples[-1][1]["depth"] == pytest.approx(1.25)

        page._reset_attitude_reference()
        app.processEvents()

        assert "zero 1.250 m" in page._labels["depth_ref"].text()
        assert "depth 0.000 m" in page._labels["depth"].text()
        assert page.depth_plot.samples[-1][1]["depth"] == pytest.approx(0.0)
        assert page.depth_plot.samples[-1][1]["sensor"] == pytest.approx(0.15)

        page.update_from_sensor(
            {
                "ts": 50.1,
                "sensor": "external_depth",
                "type": "external_depth",
                "depth_m": 1.40,
                "depth_sensor_m": 1.55,
                "pressure_mbar": 1140.0,
                "temperature_c": 12.6,
            }
        )
        app.processEvents()

        assert "depth 0.150 m" in page._labels["depth"].text()
        assert page.depth_plot.samples[-1][1]["depth"] == pytest.approx(0.15)
    finally:
        page.shutdown()
        page.close()
        page.deleteLater()
        app.processEvents()


def test_raw_sensor_page_visible_set_rest_requests_persistent_local_rest(tmp_path, monkeypatch):
    app = _app()
    calls = []

    class _RpcStub:
        def __init__(self, endpoint, on_result):
            self.endpoint = endpoint
            self.on_result = on_result

        def start(self):
            calls.append(("start", self.endpoint))

        def stop(self):
            calls.append(("stop", self.endpoint))

        def request(self, cmd, args):
            calls.append((cmd, dict(args or {})))
            return 7

    monkeypatch.setattr("gui.raw_sensor_page.ManagementRpcService", _RpcStub)
    page = RawSensorPage(recording_session_provider=lambda: tmp_path)
    try:
        page.show()
        app.processEvents()
        page.update_from_sensor(
            {
                "ts": 50.0,
                "sensor": "external_depth",
                "type": "external_depth",
                "depth_m": 0.25,
                "depth_sensor_m": 0.40,
                "pressure_mbar": 1018.0,
                "temperature_c": 12.5,
            }
        )

        page._reset_attitude_reference()
        app.processEvents()

        assert ("start", config.MANAGEMENT_RPC_ENDPOINT) in calls
        assert ("capture_local_rest", {"samples": 20, "delay_s": 0.02, "include_depth": True}) in calls
        assert page._rest_request_pending is True
        assert "zero 0.250 m" in page._labels["depth_ref"].text()

        page._handle_rest_rpc_result(
            {
                "cmd": "capture_local_rest",
                "ok": True,
                "data": {
                    "depth": {"surface_pressure_mbar": 1018.0},
                    "errors": {},
                },
            }
        )
        app.processEvents()

        assert page._rest_request_pending is False
        assert "saved onboard rest" in page._labels["attitude_ref"].text()
        assert "surface 1018.00 mbar" in page._labels["attitude_ref"].text()
    finally:
        page.shutdown()
        page.close()
        page.deleteLater()
        app.processEvents()


def test_raw_sensor_page_prefers_onboard_attitude_over_local_fallback(tmp_path):
    app = _app()
    page = RawSensorPage(recording_session_provider=lambda: tmp_path)
    try:
        onboard = {
            "ts": 40.0,
            "sensor": "roll_pitch_estimator",
            "type": "attitude",
            "source": "onboard_imu_mag_relative",
            "roll_deg": 1.0,
            "pitch_deg": 2.0,
            "yaw_deg": 3.0,
            "reference_accel": {"x": 0.0, "y": 0.0, "z": 1.0},
            "gyro_bias": {"x": 0.0, "y": 0.0, "z": 0.0},
            "yaw_source": "mmc5983",
            "yaw_status": "ready",
            "vehicle_roll_axis": "z",
        }
        assert page.record_message(onboard) == []
        page.update_from_sensor(onboard)

        mag_msg = {
            "ts": 41.0,
            "sensor": "mag",
            "type": "mag",
            "mag": {"x": 50.0, "y": 10.0, "z": -5.0},
            "mag_source": "ak09915",
        }
        page.record_message(mag_msg)
        for i in range(36):
            derived = page.record_message(
                {
                    "ts": 42.0 + i * 0.05,
                    "sensor": "imu",
                    "type": "imu",
                    "accel": {"x": 0.0, "y": 0.0, "z": 9.80665},
                    "gyro": {"x": 0.0, "y": 0.0, "z": 0.0},
                }
            )
            assert derived == []

        app.processEvents()
        assert "roll 1.00 deg" in page._labels["attitude"].text()
        assert "src onboard_imu_mag_relative" in page._labels["attitude_ref"].text()
        assert "axis z" in page._labels["attitude_ref"].text()
        assert page.attitude_view.roll_deg == pytest.approx(1.0)
        assert page.attitude_view.pitch_deg == pytest.approx(2.0)
        assert page.attitude_view.yaw_deg == pytest.approx(3.0)

        page._reset_attitude_reference()
        app.processEvents()

        assert "yaw 0.00 deg" in page._labels["attitude"].text()
        assert "display zeroed" in page._labels["attitude_ref"].text()
        assert page.attitude_view.roll_deg == pytest.approx(0.0)
        assert page.attitude_view.pitch_deg == pytest.approx(0.0)
        assert page.attitude_view.yaw_deg == pytest.approx(0.0)

        moved = dict(onboard)
        moved.update({"roll_deg": 1.5, "pitch_deg": 1.5, "yaw_deg": 4.5, "recv_time_s": 41.0})
        page.update_from_sensor(moved)
        app.processEvents()

        assert "roll 0.50 deg" in page._labels["attitude"].text()
        assert "pitch -0.50 deg" in page._labels["attitude"].text()
        assert "yaw 1.50 deg" in page._labels["attitude"].text()
        assert page.attitude_view.yaw_deg == pytest.approx(1.5)
    finally:
        page.shutdown()
        page.close()
        page.deleteLater()
        app.processEvents()


def test_attitude_3d_widget_tracks_and_clears_values():
    app = _app()
    widget = Attitude3DWidget()
    try:
        widget.set_attitude({"roll_deg": -4.0, "pitch_deg": 5.5, "yaw_deg": 12.0})
        assert widget.roll_deg == pytest.approx(-4.0)
        assert widget.pitch_deg == pytest.approx(5.5)
        assert widget.yaw_deg == pytest.approx(12.0)

        widget.clear()
        assert widget.roll_deg is None
        assert widget.pitch_deg is None
        assert widget.yaw_deg is None
    finally:
        widget.close()
        widget.deleteLater()
        app.processEvents()
