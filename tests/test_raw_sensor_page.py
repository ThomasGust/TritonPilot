import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import csv

import pytest

pytest.importorskip("PyQt6")

from PyQt6.QtWidgets import QApplication

from gui.raw_sensor_page import RawSensorPage


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

        page.stop_recording()
        rows = list(csv.DictReader((tmp_path / "raw_sensor_timeseries.csv").open(newline="", encoding="utf-8")))
        assert any(row["type"] == "mag" for row in rows)
        assert any(row["type"] == "attitude" for row in rows)
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
    finally:
        page.shutdown()
        page.close()
        page.deleteLater()
        app.processEvents()
