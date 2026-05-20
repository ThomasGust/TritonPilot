from __future__ import annotations

import csv
import json
import math
import queue
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional


def _json_text(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"))
    except Exception:
        return ""


def _float_or_blank(value: Any) -> Any:
    try:
        v = float(value)
    except Exception:
        return ""
    return v if math.isfinite(v) else ""


def _vec_norm(vec: Dict[str, Any] | None) -> Any:
    if not isinstance(vec, dict):
        return ""
    x = _float_or_blank(vec.get("x"))
    y = _float_or_blank(vec.get("y"))
    z = _float_or_blank(vec.get("z"))
    if x == "" or y == "" or z == "":
        return ""
    return math.sqrt(float(x) * float(x) + float(y) * float(y) + float(z) * float(z))


class RawSensorCsvLogger:
    """Thread-safe CSV logger for flattened ROV raw sensor telemetry."""

    FIELDNAMES = [
        "recv_time_s",
        "sensor_ts",
        "sensor",
        "type",
        "source",
        "accel_x",
        "accel_y",
        "accel_z",
        "accel_norm",
        "gyro_x",
        "gyro_y",
        "gyro_z",
        "gyro_norm",
        "roll_deg",
        "pitch_deg",
        "yaw_deg",
        "tilt_deg",
        "yaw_mag_deg",
        "yaw_weight",
        "yaw_rate_dps",
        "yaw_mag_age_s",
        "yaw_mag_norm",
        "yaw_mag_norm_error",
        "yaw_status",
        "yaw_source",
        "roll_pitch_ready",
        "attitude_ready",
        "yaw_ready",
        "mag_ready",
        "sample_age_s",
        "accel_roll_deg",
        "accel_pitch_deg",
        "accel_tilt_deg",
        "accel_weight",
        "accel_error_deg",
        "accel_norm_error",
        "gyro_rate_dps",
        "gyro_bias_alpha",
        "gravity_x",
        "gravity_y",
        "gravity_z",
        "reference_accel_x",
        "reference_accel_y",
        "reference_accel_z",
        "reference_accel_norm",
        "reference_mag_x",
        "reference_mag_y",
        "reference_mag_z",
        "reference_mag_norm",
        "leveled_mag_x",
        "leveled_mag_y",
        "leveled_mag_z",
        "leveled_mag_norm",
        "gyro_bias_x",
        "gyro_bias_y",
        "gyro_bias_z",
        "gyro_unbiased_x",
        "gyro_unbiased_y",
        "gyro_unbiased_z",
        "calibration_state",
        "calibration_samples",
        "calibration_tilt_std_deg",
        "calibration_gyro_rms_dps",
        "mag_x",
        "mag_y",
        "mag_z",
        "mag_norm",
        "mag_source",
        "ak_x",
        "ak_y",
        "ak_z",
        "ak_norm",
        "mmc_x",
        "mmc_y",
        "mmc_z",
        "mmc_norm",
        "depth_m",
        "depth_sensor_m",
        "pressure_mbar",
        "temperature_c",
        "env_pressure_kpa",
        "voltage_v",
        "current_a",
        "power_w",
        "leak",
        "adc_channels_json",
        "error",
        "raw_json",
    ]

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._q: "queue.Queue[Dict[str, Any] | None]" = queue.Queue(maxsize=20_000)
        self._thread: threading.Thread | None = None
        self._fh = None
        self._writer: Optional[csv.DictWriter] = None
        self._closed = False

    def start(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "a", buffering=1, newline="", encoding="utf-8")
        self._writer = csv.DictWriter(self._fh, fieldnames=self.FIELDNAMES, extrasaction="ignore")
        if self.path.stat().st_size == 0:
            self._writer.writeheader()
        self._closed = False
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        with self._lock:
            self._closed = True
        thread = self._thread
        if thread is not None:
            try:
                self._q.put(None, timeout=2.0)
            except queue.Full:
                pass
            thread.join(timeout=2.0)
        with self._lock:
            fh = self._fh
            self._fh = None
            self._writer = None
            self._thread = None
        if fh is not None:
            try:
                fh.flush()
                fh.close()
            except Exception:
                pass

    def record(self, msg: Dict[str, Any]) -> None:
        if self._closed:
            return
        try:
            self._q.put_nowait(dict(msg or {}))
        except queue.Full:
            # Keep telemetry/UI responsive if disk cannot keep up.
            pass

    def _run(self) -> None:
        while True:
            msg = self._q.get()
            if msg is None:
                break
            row = self.flatten(msg)
            with self._lock:
                writer = self._writer
                if writer is None:
                    continue
                try:
                    writer.writerow(row)
                except Exception:
                    pass
        with self._lock:
            fh = self._fh
            if fh is not None:
                try:
                    fh.flush()
                except Exception:
                    pass

    @classmethod
    def flatten(cls, msg: Dict[str, Any]) -> Dict[str, Any]:
        msg = dict(msg or {})
        row: Dict[str, Any] = {key: "" for key in cls.FIELDNAMES}
        row["recv_time_s"] = time.time()
        row["sensor_ts"] = _float_or_blank(msg.get("ts"))
        row["sensor"] = str(msg.get("sensor", ""))
        row["type"] = str(msg.get("type", ""))
        row["source"] = str(msg.get("source", ""))
        row["error"] = str(msg.get("error", ""))
        row["raw_json"] = _json_text(msg)

        def put_vec(prefix: str, vec: Dict[str, Any] | None) -> None:
            if not isinstance(vec, dict):
                return
            row[f"{prefix}_x"] = _float_or_blank(vec.get("x"))
            row[f"{prefix}_y"] = _float_or_blank(vec.get("y"))
            row[f"{prefix}_z"] = _float_or_blank(vec.get("z"))
            row[f"{prefix}_norm"] = _vec_norm(vec)

        put_vec("accel", msg.get("accel"))
        put_vec("gyro", msg.get("gyro"))
        put_vec("gravity", msg.get("gravity"))
        put_vec("reference_accel", msg.get("reference_accel"))
        put_vec("gyro_bias", msg.get("gyro_bias"))
        put_vec("gyro_unbiased", msg.get("gyro_unbiased"))
        ref = msg.get("reference_accel")
        if isinstance(ref, dict):
            row["reference_accel_norm"] = _float_or_blank(ref.get("norm"))
        for key in (
            "roll_deg",
            "pitch_deg",
            "yaw_deg",
            "tilt_deg",
            "yaw_mag_deg",
            "yaw_weight",
            "yaw_rate_dps",
            "yaw_mag_age_s",
            "yaw_mag_norm",
            "yaw_mag_norm_error",
            "sample_age_s",
            "accel_roll_deg",
            "accel_pitch_deg",
            "accel_tilt_deg",
            "accel_weight",
            "accel_error_deg",
            "accel_norm_error",
            "gyro_rate_dps",
            "gyro_bias_alpha",
            "calibration_samples",
            "calibration_tilt_std_deg",
            "calibration_gyro_rms_dps",
        ):
            row[key] = _float_or_blank(msg.get(key))
        row["calibration_state"] = str(msg.get("calibration_state", ""))
        row["yaw_status"] = str(msg.get("yaw_status", ""))
        row["yaw_source"] = str(msg.get("yaw_source", ""))
        for key in ("roll_pitch_ready", "attitude_ready", "yaw_ready", "mag_ready"):
            if key in msg:
                row[key] = int(bool(msg.get(key)))
        put_vec("mag", msg.get("mag") or msg.get("magnetometer"))
        row["mag_source"] = str(msg.get("mag_source", ""))
        put_vec("reference_mag", msg.get("reference_mag"))
        put_vec("leveled_mag", msg.get("leveled_mag"))

        mag_sources = msg.get("mag_sources") or {}
        if isinstance(mag_sources, dict):
            put_vec("ak", mag_sources.get("ak09915"))
            put_vec("mmc", mag_sources.get("mmc5983"))

        row["depth_m"] = _float_or_blank(msg.get("depth_m"))
        row["depth_sensor_m"] = _float_or_blank(msg.get("depth_sensor_m"))
        row["pressure_mbar"] = _float_or_blank(msg.get("pressure_mbar"))
        row["temperature_c"] = _float_or_blank(msg.get("temperature_c"))
        row["env_pressure_kpa"] = _float_or_blank(msg.get("pressure_kpa"))
        row["voltage_v"] = _float_or_blank(msg.get("voltage_v"))
        row["current_a"] = _float_or_blank(msg.get("current_a"))
        row["power_w"] = _float_or_blank(msg.get("power_w"))
        if "leak" in msg:
            row["leak"] = int(bool(msg.get("leak")))
        if "channels" in msg:
            row["adc_channels_json"] = _json_text(msg.get("channels"))
        return row
