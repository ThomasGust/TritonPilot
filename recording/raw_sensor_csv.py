"""CSV logging for flattened raw telemetry and derived attitude rows."""

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
        "control_reason",
        "control_mix_mode",
        "control_status_age_s",
        "armed",
        "sink_armed",
        "dry_run",
        "pilot_available",
        "pilot_fresh",
        "pilot_seq",
        "pilot_age_s",
        "pilot_modes_json",
        "ap_enabled_cmd",
        "ap_active",
        "ap_reason",
        "ap_status_age_s",
        "depth_enabled_cmd",
        "depth_active",
        "depth_reason",
        "depth_target_m",
        "depth_error_m",
        "depth_f_m",
        "depth_dz_mps",
        "depth_u_raw",
        "depth_u_out",
        "att_enabled_cmd",
        "att_active",
        "att_reason",
        "att_sample_age_s",
        "roll_mode",
        "roll_enabled",
        "roll_active",
        "roll_reason",
        "roll_angle_deg",
        "roll_target_deg",
        "roll_error_deg",
        "roll_rate_dps",
        "roll_u_raw",
        "roll_u_out",
        "roll_manual_cmd",
        "pitch_mode",
        "pitch_enabled",
        "pitch_active",
        "pitch_reason",
        "pitch_angle_deg",
        "pitch_target_deg",
        "pitch_error_deg",
        "pitch_rate_dps",
        "pitch_u_raw",
        "pitch_u_out",
        "pitch_manual_cmd",
        "yaw_mode",
        "yaw_enabled",
        "yaw_active",
        "yaw_reason",
        "yaw_angle_deg",
        "yaw_target_deg",
        "yaw_error_deg",
        "yaw_rate_dps_control",
        "yaw_u_raw",
        "yaw_u_out",
        "yaw_manual_cmd",
        "cmd_manual_surge",
        "cmd_manual_sway",
        "cmd_manual_heave",
        "cmd_manual_yaw",
        "cmd_manual_pitch",
        "cmd_manual_roll",
        "cmd_final_surge",
        "cmd_final_sway",
        "cmd_final_heave",
        "cmd_final_yaw",
        "cmd_final_pitch",
        "cmd_final_roll",
        "thr_H_FL",
        "thr_H_FR",
        "thr_H_RL",
        "thr_H_RR",
        "thr_V_FL",
        "thr_V_FR",
        "thr_V_RL",
        "thr_V_RR",
        "control_payload_json",
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
        if str(msg.get("type", "")) == "autopilot_status":
            cls._flatten_autopilot_status(msg, row)
        return row

    @classmethod
    def _flatten_autopilot_status(cls, msg: Dict[str, Any], row: Dict[str, Any]) -> None:
        def put_bool(key: str, value: Any) -> None:
            if value is not None:
                row[key] = int(bool(value))

        def put_num(key: str, value: Any) -> None:
            row[key] = _float_or_blank(value)

        def put_cmd(prefix: str, values: Any) -> None:
            if not isinstance(values, dict):
                return
            for axis in ("surge", "sway", "heave", "yaw", "pitch", "roll"):
                put_num(f"{prefix}_{axis}", values.get(axis))

        def put_axis(axis: str, status: Any) -> None:
            if not isinstance(status, dict):
                return
            row[f"{axis}_mode"] = str(status.get("mode", ""))
            put_bool(f"{axis}_enabled", status.get("enabled_cmd"))
            put_bool(f"{axis}_active", status.get("active"))
            row[f"{axis}_reason"] = str(status.get("reason", ""))
            put_num(f"{axis}_angle_deg", status.get("angle_deg"))
            put_num(f"{axis}_target_deg", status.get("target_deg"))
            put_num(f"{axis}_error_deg", status.get("error_deg"))
            if axis == "yaw":
                put_num("yaw_rate_dps_control", status.get("rate_dps"))
            else:
                put_num(f"{axis}_rate_dps", status.get("rate_dps"))
            put_num(f"{axis}_u_raw", status.get("u_raw"))
            put_num(f"{axis}_u_out", status.get("u_out"))
            put_num(f"{axis}_manual_cmd", status.get("manual_cmd"))

        control = msg.get("control") if isinstance(msg.get("control"), dict) else {}
        control_status = control.get("status") if isinstance(control.get("status"), dict) else {}
        autopilot = msg.get("autopilot") if isinstance(msg.get("autopilot"), dict) else {}
        ap_status = autopilot.get("status") if isinstance(autopilot.get("status"), dict) else {}
        depth_hold = msg.get("depth_hold") if isinstance(msg.get("depth_hold"), dict) else {}
        depth_status = depth_hold.get("status") if isinstance(depth_hold.get("status"), dict) else {}
        if isinstance(ap_status.get("depth_hold"), dict):
            merged_depth = dict(ap_status.get("depth_hold") or {})
            merged_depth.update(depth_status)
            depth_status = merged_depth
        attitude = ap_status.get("attitude") if isinstance(ap_status.get("attitude"), dict) else {}
        axes = attitude.get("axes") if isinstance(attitude.get("axes"), dict) else {}
        pilot = control_status.get("pilot") if isinstance(control_status.get("pilot"), dict) else {}

        row["control_reason"] = str(control_status.get("reason", ""))
        row["control_mix_mode"] = str(control_status.get("mix_mode", ""))
        put_num("control_status_age_s", control.get("status_age_s"))
        put_bool("armed", msg.get("armed", control_status.get("armed")))
        put_bool("sink_armed", control_status.get("sink_armed"))
        put_bool("dry_run", control_status.get("dry_run"))
        put_bool("pilot_available", pilot.get("available"))
        put_bool("pilot_fresh", pilot.get("fresh"))
        put_num("pilot_seq", pilot.get("seq"))
        put_num("pilot_age_s", pilot.get("age_s"))
        if "modes" in pilot:
            row["pilot_modes_json"] = _json_text(pilot.get("modes"))

        put_bool("ap_enabled_cmd", ap_status.get("enabled_cmd"))
        put_bool("ap_active", ap_status.get("active"))
        row["ap_reason"] = str(ap_status.get("reason", ""))
        put_num("ap_status_age_s", autopilot.get("status_age_s"))

        put_bool("depth_enabled_cmd", depth_status.get("enabled_cmd"))
        put_bool("depth_active", depth_status.get("active"))
        row["depth_reason"] = str(depth_status.get("reason", ""))
        put_num("depth_target_m", depth_hold.get("target_m", depth_status.get("target_m")))
        put_num("depth_error_m", depth_status.get("error_m"))
        put_num("depth_f_m", depth_status.get("depth_f_m"))
        put_num("depth_dz_mps", depth_status.get("dz_mps"))
        put_num("depth_u_raw", depth_status.get("u_raw"))
        put_num("depth_u_out", depth_status.get("u_out"))

        put_bool("att_enabled_cmd", attitude.get("enabled_cmd"))
        put_bool("att_active", attitude.get("active"))
        row["att_reason"] = str(attitude.get("reason", ""))
        put_num("att_sample_age_s", attitude.get("sample_age_s"))
        for axis in ("roll", "pitch", "yaw"):
            put_axis(axis, axes.get(axis))

        put_cmd("cmd_manual", control_status.get("cmd_manual"))
        put_cmd("cmd_final", control_status.get("cmd_final"))
        thr = control_status.get("thrusters_final")
        if isinstance(thr, dict):
            for name in ("H_FL", "H_FR", "H_RL", "H_RR", "V_FL", "V_FR", "V_RL", "V_RR"):
                put_num(f"thr_{name}", thr.get(name))
        if isinstance(control_status.get("payload"), dict):
            row["control_payload_json"] = _json_text(control_status.get("payload"))
