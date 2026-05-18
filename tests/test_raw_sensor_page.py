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
