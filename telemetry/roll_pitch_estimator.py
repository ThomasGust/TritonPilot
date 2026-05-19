from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass
from typing import Any, Iterable, Optional


Vec3 = tuple[float, float, float]


def _as_float(value: Any) -> Optional[float]:
    try:
        out = float(value)
    except Exception:
        return None
    return out if math.isfinite(out) else None


def _vec_from_msg(value: Any) -> Optional[Vec3]:
    if not isinstance(value, dict):
        return None
    x = _as_float(value.get("x"))
    y = _as_float(value.get("y"))
    z = _as_float(value.get("z"))
    if x is None or y is None or z is None:
        return None
    return (x, y, z)


def _add(a: Vec3, b: Vec3) -> Vec3:
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2])


def _sub(a: Vec3, b: Vec3) -> Vec3:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _scale(a: Vec3, s: float) -> Vec3:
    return (a[0] * s, a[1] * s, a[2] * s)


def _dot(a: Vec3, b: Vec3) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _cross(a: Vec3, b: Vec3) -> Vec3:
    return (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )


def _norm(a: Vec3) -> float:
    return math.sqrt(_dot(a, a))


def _normalize(a: Vec3) -> Optional[Vec3]:
    n = _norm(a)
    if n <= 1e-9 or not math.isfinite(n):
        return None
    return (a[0] / n, a[1] / n, a[2] / n)


def _mean_vec(samples: Iterable[Vec3]) -> Vec3:
    items = list(samples)
    if not items:
        return (0.0, 0.0, 0.0)
    inv = 1.0 / float(len(items))
    return (
        sum(v[0] for v in items) * inv,
        sum(v[1] for v in items) * inv,
        sum(v[2] for v in items) * inv,
    )


def _rotate_vector(v: Vec3, rot: Vec3) -> Vec3:
    """Rotate vector by a small/exact rotation vector in radians."""
    angle = _norm(rot)
    if angle <= 1e-12:
        return v
    axis = _scale(rot, 1.0 / angle)
    c = math.cos(angle)
    s = math.sin(angle)
    return _add(
        _add(_scale(v, c), _scale(_cross(axis, v), s)),
        _scale(axis, _dot(axis, v) * (1.0 - c)),
    )


def _project_axis(axis: Vec3, normal: Vec3) -> Optional[Vec3]:
    return _normalize(_sub(axis, _scale(normal, _dot(axis, normal))))


@dataclass(frozen=True)
class RollPitchConfig:
    calibration_samples: int = 30
    max_dt_s: float = 0.12
    accel_correction: float = 0.045
    accel_norm_gate: float = 0.18


class RollPitchEstimator:
    """Six-axis, rest-relative roll/pitch diagnostic estimator.

    The estimator deliberately treats the current calibration pose as zero.
    That lets us produce useful roll/pitch telemetry before we know the exact
    IMU-to-vehicle mount transform.
    """

    def __init__(self, config: RollPitchConfig | None = None):
        self.config = config or RollPitchConfig()
        self._lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        with self._lock:
            self._cal_accel_units: list[Vec3] = []
            self._cal_accel_raw: list[Vec3] = []
            self._cal_gyro: list[Vec3] = []
            self._reference_accel: Optional[Vec3] = None
            self._reference_norm: Optional[float] = None
            self._gyro_bias: Vec3 = (0.0, 0.0, 0.0)
            self._roll_axis: Optional[Vec3] = None
            self._pitch_axis: Optional[Vec3] = None
            self._gravity_est: Optional[Vec3] = None
            self._last_ts: Optional[float] = None
            self._last_output: Optional[dict[str, Any]] = None

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "calibration_state": "calibrated" if self._reference_accel is not None else "calibrating",
                "calibration_samples": len(self._cal_accel_units),
                "calibration_target_samples": int(self.config.calibration_samples),
                "reference_accel": self._reference_accel,
                "reference_norm": self._reference_norm,
                "gyro_bias": self._gyro_bias,
                "last_output": dict(self._last_output) if self._last_output else None,
            }

    def update(self, imu_msg: dict[str, Any], *, recv_time_s: float | None = None) -> Optional[dict[str, Any]]:
        accel = _vec_from_msg((imu_msg or {}).get("accel"))
        gyro = _vec_from_msg((imu_msg or {}).get("gyro"))
        if accel is None or gyro is None:
            return None
        accel_norm = _norm(accel)
        accel_unit = _normalize(accel)
        if accel_unit is None or accel_norm < 1.0:
            return None
        sensor_ts = _as_float((imu_msg or {}).get("ts"))
        if sensor_ts is None:
            sensor_ts = time.time()
        if recv_time_s is None:
            recv_time_s = time.time()

        with self._lock:
            if self._reference_accel is None:
                self._cal_accel_units.append(accel_unit)
                self._cal_accel_raw.append(accel)
                self._cal_gyro.append(gyro)
                if len(self._cal_accel_units) >= int(self.config.calibration_samples):
                    self._finish_calibration(sensor_ts)
                return None

            dt = 0.0
            if self._last_ts is not None:
                dt = max(0.0, min(float(self.config.max_dt_s), sensor_ts - self._last_ts))
            self._last_ts = sensor_ts

            gyro_unbiased = _sub(gyro, self._gyro_bias)
            if self._gravity_est is None:
                self._gravity_est = self._reference_accel

            predicted = self._gravity_est
            if dt > 0.0:
                predicted = _normalize(_rotate_vector(predicted, _scale(gyro_unbiased, -dt))) or predicted

            accel_weight = float(self.config.accel_correction)
            if self._reference_norm and self._reference_norm > 0:
                norm_err = abs(accel_norm - self._reference_norm) / self._reference_norm
                if norm_err > float(self.config.accel_norm_gate):
                    accel_weight = 0.0
                elif norm_err > float(self.config.accel_norm_gate) * 0.5:
                    accel_weight *= 0.35

            corrected = _normalize(
                _add(_scale(predicted, 1.0 - accel_weight), _scale(accel_unit, accel_weight))
            )
            self._gravity_est = corrected or predicted

            filtered = self._tilt_components(self._gravity_est)
            accel_only = self._tilt_components(accel_unit)
            if filtered is None or accel_only is None:
                return None

            roll_deg, pitch_deg, tilt_deg = filtered
            accel_roll_deg, accel_pitch_deg, accel_tilt_deg = accel_only
            out = {
                "ts": sensor_ts,
                "sensor": "roll_pitch_estimator",
                "type": "attitude",
                "source": "topside_imu_6axis",
                "recv_time_s": float(recv_time_s),
                "roll_deg": roll_deg,
                "pitch_deg": pitch_deg,
                "tilt_deg": tilt_deg,
                "accel_roll_deg": accel_roll_deg,
                "accel_pitch_deg": accel_pitch_deg,
                "accel_tilt_deg": accel_tilt_deg,
                "accel_norm": accel_norm,
                "gravity": {
                    "x": self._gravity_est[0],
                    "y": self._gravity_est[1],
                    "z": self._gravity_est[2],
                },
                "reference_accel": {
                    "x": self._reference_accel[0],
                    "y": self._reference_accel[1],
                    "z": self._reference_accel[2],
                    "norm": self._reference_norm,
                },
                "gyro_bias": {
                    "x": self._gyro_bias[0],
                    "y": self._gyro_bias[1],
                    "z": self._gyro_bias[2],
                },
                "gyro_unbiased": {
                    "x": gyro_unbiased[0],
                    "y": gyro_unbiased[1],
                    "z": gyro_unbiased[2],
                },
                "calibration_state": "calibrated",
                "calibration_samples": len(self._cal_accel_units),
            }
            self._last_output = out
            return dict(out)

    def _finish_calibration(self, sensor_ts: float) -> None:
        ref = _normalize(_mean_vec(self._cal_accel_units))
        if ref is None:
            self._cal_accel_units.clear()
            self._cal_accel_raw.clear()
            self._cal_gyro.clear()
            return
        raw_mean = _mean_vec(self._cal_accel_raw)
        self._reference_accel = ref
        self._reference_norm = _norm(raw_mean)
        self._gyro_bias = _mean_vec(self._cal_gyro)

        roll_axis = _project_axis((1.0, 0.0, 0.0), ref)
        if roll_axis is None:
            roll_axis = _project_axis((0.0, 0.0, 1.0), ref)
        if roll_axis is None:
            roll_axis = _project_axis((0.0, 1.0, 0.0), ref)
        if roll_axis is None:
            roll_axis = (1.0, 0.0, 0.0)
        pitch_axis = _normalize(_cross(ref, roll_axis)) or (0.0, 1.0, 0.0)

        self._roll_axis = roll_axis
        self._pitch_axis = pitch_axis
        self._gravity_est = ref
        self._last_ts = sensor_ts

    def _tilt_components(self, gravity: Vec3) -> Optional[tuple[float, float, float]]:
        if self._reference_accel is None or self._roll_axis is None or self._pitch_axis is None:
            return None
        cross = _cross(self._reference_accel, gravity)
        sin_angle = _norm(cross)
        cos_angle = max(-1.0, min(1.0, _dot(self._reference_accel, gravity)))
        angle = math.atan2(sin_angle, cos_angle)
        if sin_angle <= 1e-9:
            rot_vec = (0.0, 0.0, 0.0)
        else:
            rot_vec = _scale(cross, angle / sin_angle)
        return (
            math.degrees(_dot(rot_vec, self._roll_axis)),
            math.degrees(_dot(rot_vec, self._pitch_axis)),
            math.degrees(angle),
        )
