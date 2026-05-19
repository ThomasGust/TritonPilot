import math

import pytest

from telemetry.roll_pitch_estimator import RollPitchConfig, RollPitchEstimator


def _imu(ts, accel, gyro=(0.0, 0.0, 0.0)):
    return {
        "ts": ts,
        "sensor": "imu",
        "type": "imu",
        "accel": {"x": accel[0], "y": accel[1], "z": accel[2]},
        "gyro": {"x": gyro[0], "y": gyro[1], "z": gyro[2]},
    }


def test_roll_pitch_estimator_zeros_current_rest_pose():
    est = RollPitchEstimator(RollPitchConfig(calibration_samples=8, accel_correction=1.0))
    rest = (3.379, -9.315, -0.232)
    bias = (-0.0178, -0.0102, 0.0077)

    out = None
    for i in range(12):
        out = est.update(_imu(10.0 + i * 0.05, rest, bias))

    assert out is not None
    assert abs(out["roll_deg"]) < 0.01
    assert abs(out["pitch_deg"]) < 0.01
    assert out["calibration_state"] == "calibrated"
    assert math.isclose(out["gyro_bias"]["x"], bias[0])


def test_roll_pitch_estimator_reports_known_tilt_after_reference():
    est = RollPitchEstimator(RollPitchConfig(calibration_samples=5, accel_correction=1.0))
    for i in range(5):
        assert est.update(_imu(float(i), (0.0, 0.0, 9.80665))) is None

    theta = math.radians(10.0)
    roll_accel = (0.0, -math.sin(theta) * 9.80665, math.cos(theta) * 9.80665)
    out = est.update(_imu(5.05, roll_accel))

    assert out is not None
    assert out["roll_deg"] == pytest.approx(10.0, abs=0.15)
    assert out["pitch_deg"] == pytest.approx(0.0, abs=0.15)
