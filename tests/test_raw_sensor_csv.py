import csv
from pathlib import Path

from recording.raw_sensor_csv import RawSensorCsvLogger


def test_raw_sensor_csv_logger_flattens_imu_mag_sources(tmp_path: Path):
    out = tmp_path / "raw_sensor_timeseries.csv"
    logger = RawSensorCsvLogger(out)
    logger.start()
    logger.record(
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
    logger.stop()

    rows = list(csv.DictReader(out.open(newline="", encoding="utf-8")))
    assert len(rows) == 1
    row = rows[0]
    assert row["sensor"] == "imu"
    assert row["type"] == "imu"
    assert row["mag_source"] == "ak09915"
    assert float(row["accel_norm"]) == 1.0
    assert float(row["ak_z"]) == 40.0
    assert float(row["mmc_x"]) == 21.0


def test_raw_sensor_csv_logger_flattens_depth_and_adc(tmp_path: Path):
    out = tmp_path / "raw_sensor_timeseries.csv"
    logger = RawSensorCsvLogger(out)
    logger.start()
    logger.record(
        {
            "ts": 11.0,
            "sensor": "bar30",
            "type": "external_depth",
            "depth_m": 1.25,
            "depth_sensor_m": 1.40,
            "pressure_mbar": 1138.4,
            "temperature_c": 18.5,
        }
    )
    logger.record({"ts": 12.0, "sensor": "adc", "type": "adc", "channels": [0.1, 0.2, 0.3, 0.4]})
    logger.stop()

    rows = list(csv.DictReader(out.open(newline="", encoding="utf-8")))
    assert float(rows[0]["depth_m"]) == 1.25
    assert rows[1]["adc_channels_json"] == "[0.1,0.2,0.3,0.4]"


def test_raw_sensor_csv_logger_flattens_attitude(tmp_path: Path):
    out = tmp_path / "raw_sensor_timeseries.csv"
    logger = RawSensorCsvLogger(out)
    logger.start()
    logger.record(
        {
            "ts": 20.0,
            "sensor": "roll_pitch_estimator",
            "type": "attitude",
            "roll_deg": 1.25,
            "pitch_deg": -2.5,
            "tilt_deg": 2.8,
            "gravity": {"x": 0.1, "y": -0.9, "z": 0.3},
            "reference_accel": {"x": 0.34, "y": -0.94, "z": -0.02, "norm": 9.91},
            "gyro_bias": {"x": -0.01, "y": 0.02, "z": 0.03},
            "calibration_state": "calibrated",
            "calibration_samples": 30,
        }
    )
    logger.stop()

    row = next(csv.DictReader(out.open(newline="", encoding="utf-8")))
    assert row["type"] == "attitude"
    assert float(row["roll_deg"]) == 1.25
    assert float(row["reference_accel_norm"]) == 9.91
    assert row["calibration_state"] == "calibrated"
