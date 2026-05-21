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
            "source": "onboard_imu_mag_relative",
            "roll_deg": 1.25,
            "pitch_deg": -2.5,
            "yaw_deg": 3.0,
            "tilt_deg": 2.8,
            "roll_pitch_ready": True,
            "attitude_ready": True,
            "yaw_ready": True,
            "mag_ready": False,
            "sample_age_s": 0.02,
            "gyro_bias_alpha": 0.001,
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
    assert row["source"] == "onboard_imu_mag_relative"
    assert float(row["roll_deg"]) == 1.25
    assert float(row["yaw_deg"]) == 3.0
    assert row["attitude_ready"] == "1"
    assert row["mag_ready"] == "0"
    assert float(row["sample_age_s"]) == 0.02
    assert float(row["gyro_bias_alpha"]) == 0.001
    assert float(row["reference_accel_norm"]) == 9.91
    assert row["calibration_state"] == "calibrated"


def test_raw_sensor_csv_logger_flattens_autopilot_status(tmp_path: Path):
    out = tmp_path / "raw_sensor_timeseries.csv"
    logger = RawSensorCsvLogger(out)
    logger.start()
    logger.record(
        {
            "ts": 30.0,
            "sensor": "autopilot_status",
            "type": "autopilot_status",
            "source": "control_service",
            "armed": True,
            "control": {
                "status_age_s": 0.01,
                "status": {
                    "reason": "armed_apply",
                    "mix_mode": "six_dof",
                    "armed": True,
                    "sink_armed": True,
                    "dry_run": False,
                    "pilot": {
                        "available": True,
                        "fresh": True,
                        "seq": 123,
                        "age_s": 0.04,
                        "modes": {"autopilot": {"depth": True, "yaw": "hold"}},
                    },
                    "cmd_manual": {"heave": 0.0, "yaw": 0.0, "surge": 0.1},
                    "cmd_final": {"heave": 0.12, "yaw": -0.08, "surge": 0.1},
                    "thrusters_final": {"H_FL": -0.08, "H_FR": 0.08, "V_FL": 0.12},
                    "payload": {"H_FL": -0.08, "H_FR": 0.08, "V_FL": 0.12, "lights": 0.75},
                },
            },
            "autopilot": {
                "status_age_s": 0.02,
                "status": {
                    "enabled_cmd": True,
                    "active": True,
                    "reason": "active",
                    "depth_hold": {
                        "enabled_cmd": True,
                        "active": True,
                        "reason": "hold",
                        "target_m": 1.0,
                        "error_m": 0.2,
                        "depth_f_m": 1.2,
                        "dz_mps": 0.01,
                        "u_raw": 0.12,
                        "u_out": 0.12,
                    },
                    "attitude": {
                        "enabled_cmd": True,
                        "active": True,
                        "reason": "active",
                        "sample_age_s": 0.03,
                        "axes": {
                            "yaw": {
                                "mode": "hold",
                                "enabled_cmd": True,
                                "active": True,
                                "reason": "hold",
                                "angle_deg": 12.0,
                                "target_deg": 10.0,
                                "error_deg": -2.0,
                                "rate_dps": 0.5,
                                "u_raw": -0.08,
                                "u_out": -0.08,
                                "manual_cmd": 0.0,
                            }
                        },
                    },
                },
            },
            "depth_hold": {"target_m": 1.0, "status": {"enabled_cmd": True, "active": True}},
        }
    )
    logger.stop()

    row = next(csv.DictReader(out.open(newline="", encoding="utf-8")))
    assert row["type"] == "autopilot_status"
    assert row["armed"] == "1"
    assert row["control_reason"] == "armed_apply"
    assert row["control_mix_mode"] == "six_dof"
    assert float(row["pilot_seq"]) == 123.0
    assert float(row["cmd_final_heave"]) == 0.12
    assert float(row["yaw_error_deg"]) == -2.0
    assert float(row["depth_error_m"]) == 0.2
    assert float(row["thr_H_FL"]) == -0.08
    assert "lights" in row["control_payload_json"]
