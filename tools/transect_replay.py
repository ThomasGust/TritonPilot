"""Offline replay + auto-calibrate harness for the transect optical-hold autopilot.

The debugging path for tuning the station-keep without re-flying. Runs a recorded
clip through the SAME pipeline the live system uses --

    frame --(ClassicalTransectDetector)--> TransectObservation
          --(TransectPolicy)--> TransectEstimate (the ex/ey/es/er the ROV consumes)
          --(offline control replica)--> the surge/sway/heave/yaw the ROV WOULD command

-- and reports where it breaks: detection rate, lock stability, which error channels
saturate, and whether the controller is railing a thruster. ``--calibrate`` reads the
oblique arm-cam setpoints straight off the footage so the size/position errors stop
saturating.

The control replica mirrors ``TritonOS/control/station_keep.py`` (per-axis PID with
deadband / out_limit / anti-windup) seeded with the current ``rov_config`` STATION_KEEP_*
gains, so a gain change can be evaluated against a recording with zero water time. Pass
``--proposed`` to A/B the post-fix gains (yaw disabled, gentler surge/sway).

Examples:
  python -m tools.transect_replay recordings/20260619-161115 --calibrate
  python -m tools.transect_replay <session_or_mp4> --calibrate --apply --out review.mp4
  python -m tools.transect_replay <mp4> --proposed            # A/B the tamer gains
"""

from __future__ import annotations

import argparse
import glob
import os
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

import cv2

from tracking.transect_cv import ClassicalDetectorConfig, ClassicalTransectDetector
from tracking.transect_overlay import draw_transect_overlay
from tracking.transect_policy import TransectModel, TransectObservation, TransectPolicy


# --- offline control replica (mirrors TritonOS/control/station_keep.py) -----------
@dataclass
class _Axis:
    dof: str
    error_key: str
    kp: float
    ki: float = 0.0
    kd: float = 0.0
    deadband: float = 0.05
    i_limit: float = 0.18
    out_limit: float = 0.30
    sign: float = 1.0
    slew: float = 0.0


# Current TritonOS/rov_config.py STATION_KEEP_* (the gains that produced the recording).
_GAINS_CURRENT: List[_Axis] = [
    _Axis("sway", "ex", kp=0.45, ki=0.06, deadband=0.05, i_limit=0.18, out_limit=0.30),
    _Axis("surge", "ey", kp=0.45, ki=0.06, deadband=0.05, i_limit=0.18, out_limit=0.30),
    _Axis("heave", "es", kp=0.12, ki=0.0, deadband=0.08, i_limit=0.05, out_limit=0.15),
    _Axis("yaw", "er", kp=0.25, ki=0.0, deadband=0.06, i_limit=0.05, out_limit=0.15),
]

# Proposed post-fix gains: yaw-align off, gentler/slower surge+sway so the vehicle
# stops lurching the lock loose. Keep in sync with the rov_config.py change.
_GAINS_PROPOSED: List[_Axis] = [
    _Axis("sway", "ex", kp=0.30, ki=0.06, deadband=0.06, i_limit=0.18, out_limit=0.20, slew=0.6),
    _Axis("surge", "ey", kp=0.30, ki=0.06, deadband=0.06, i_limit=0.18, out_limit=0.20, slew=0.6),
    _Axis("heave", "es", kp=0.12, ki=0.0, deadband=0.08, i_limit=0.05, out_limit=0.15, slew=0.4),
    _Axis("yaw", "er", kp=0.0, ki=0.0, deadband=0.06, i_limit=0.05, out_limit=0.15, slew=0.5),
]


class _ControlReplica:
    """Per-axis PID matching StationKeepController.step (pilot assumed neutral)."""

    def __init__(self, axes: List[_Axis]):
        self.axes = axes
        self._i: Dict[str, float] = {}
        self._last: Dict[str, float] = {}
        self._last_u: Dict[str, float] = {}

    def step(self, err_valid: bool, errors: Dict[str, float], dt: float) -> Dict[str, float]:
        out = {a.dof: 0.0 for a in self.axes}
        if not err_valid:
            # No lock -> controller holds manual (neutral); bleed integrators + slew.
            self._i.clear()
            self._last.clear()
            self._last_u.clear()
            return out
        for a in self.axes:
            err = float(errors.get(a.error_key, 0.0))
            if abs(err) < a.deadband:
                err = 0.0
            prev = self._last.get(a.dof)
            d_err = ((err - prev) / dt) if prev is not None else 0.0
            self._last[a.dof] = err
            i_state = self._i.get(a.dof, 0.0)
            u_raw = a.sign * (a.kp * err + a.ki * i_state + a.kd * d_err)
            u = max(-a.out_limit, min(a.out_limit, u_raw))
            saturated = abs(u - u_raw) > 1e-9
            if (not saturated) and a.ki != 0.0 and err != 0.0:
                lim = a.i_limit / abs(a.ki)
                i_state = max(-lim, min(lim, i_state + err * dt))
                self._i[a.dof] = i_state
                u_raw = a.sign * (a.kp * err + a.ki * i_state + a.kd * d_err)
                u = max(-a.out_limit, min(a.out_limit, u_raw))
            if a.slew > 0.0:
                pu = self._last_u.get(a.dof, 0.0)
                step = a.slew * dt
                u = max(pu - step, min(pu + step, u))
            self._last_u[a.dof] = u
            out[a.dof] = u
        return out


# --- helpers ----------------------------------------------------------------------
def _resolve_video(path: str) -> str:
    if os.path.isdir(path):
        vids = sorted(glob.glob(os.path.join(path, "**", "*.mp4"), recursive=True))
        arm = [v for v in vids if "Arm_Camera" in v] or vids
        if not arm:
            raise SystemExit(f"No .mp4 found under {path}")
        # Largest = the longest engaged clip, usually what we want.
        return max(arm, key=os.path.getsize)
    return path


def _dist(name: str, xs: List[float]) -> str:
    if not xs:
        return f"  {name:14s} (none)"
    s = sorted(xs)
    p = lambda q: s[min(len(s) - 1, int(q * len(s)))]
    return (f"  {name:14s} n={len(xs):4d}  min={min(xs):+.3f}  p25={p(.25):+.3f}  "
            f"p50={p(.5):+.3f}  p75={p(.75):+.3f}  max={max(xs):+.3f}  std={statistics.pstdev(xs):.3f}")


def _pct(a: int, b: int) -> float:
    return 100.0 * a / b if b else 0.0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path", help="session dir or mp4 (dir -> largest Arm_Camera clip)")
    ap.add_argument("--calibrate", action="store_true", help="derive target_cx/cy/blue_fraction from on-station frames")
    ap.add_argument("--apply", action="store_true", help="with --calibrate, run the rest of the report using the derived setpoints")
    ap.add_argument("--out", help="write an annotated mp4 here")
    ap.add_argument("--proposed", action="store_true", help="use the post-fix control gains (yaw off, gentler surge/sway)")
    # model / detector overrides
    ap.add_argument("--target-cx", type=float, default=0.5)
    ap.add_argument("--target-cy", type=float, default=0.5)
    ap.add_argument("--target-blue-fraction", type=float, default=None)
    ap.add_argument("--wb", action="store_true", help="detector: enable gray-world white balance")
    ap.add_argument("--no-gripper-mask", action="store_true", help="detector: don't mask the gripper ROI for red")
    ap.add_argument("--max-frames", type=int, default=0, help="limit frames (0 = all)")
    args = ap.parse_args()

    video = _resolve_video(args.path)
    cap = cv2.VideoCapture(video)
    if not cap.isOpened():
        raise SystemExit(f"Cannot open video: {video}")
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    dt = 1.0 / fps
    print(f"[replay] {video}\n[replay] {fps:.1f} fps, {total} frames (~{total / fps:.0f}s)")

    det_cfg = ClassicalDetectorConfig()
    if args.wb:
        det_cfg.white_balance = True
    if args.no_gripper_mask:
        det_cfg.gripper_roi = None
    detector = ClassicalTransectDetector(det_cfg)

    # First pass collects the raw detector observations (used for --calibrate and
    # the detector report); a single decode pass also feeds the policy/control.
    model = _build_model(args, None)
    obs_list: List[TransectObservation] = []
    frames = []  # keep frames only if we need to write output
    keep_frames = bool(args.out)
    n = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        n += 1
        obs_list.append(detector.detect(frame))
        if keep_frames:
            frames.append(frame)
        if args.max_frames and n >= args.max_frames:
            break
    cap.release()

    # --- calibration -------------------------------------------------------------
    calib = _calibrate(model, obs_list)
    if args.calibrate:
        _print_calibration(model, calib)
        if args.apply and calib is not None:
            # Apply ONLY the position centering (cx/cy). The size setpoint
            # (target_blue_fraction) stays geometric for a nadir camera -- it must
            # NOT be the flight median, which just reflects whatever altitude the
            # ROV happened to fly at (usually too high). See _print_calibration.
            model = TransectModel(target_cx=round(calib["cx"], 3), target_cy=round(calib["cy"], 3))
            print(f"[replay] applying derived position centering for the report below\n")

    # --- detector report ---------------------------------------------------------
    found = [o for o in obs_list if o.blue_found]
    print(f"\n=== DETECTOR ===  blue_found {len(found)}/{len(obs_list)} = {_pct(len(found), len(obs_list)):.1f}%")
    print(_dist("blue_cx", [o.blue_cx for o in found]))
    print(_dist("blue_cy", [o.blue_cy for o in found]))
    print(_dist("blue_fraction", [o.blue_fraction for o in found]))
    print(_dist("blue_rotation", [o.blue_rotation_deg for o in found]))
    print(_dist("fit_quality", [o.fit_quality for o in found]))

    # --- policy + control replay -------------------------------------------------
    policy = TransectPolicy(model)
    axes = _GAINS_PROPOSED if args.proposed else _GAINS_CURRENT
    ctrl = _ControlReplica(axes)
    valid_flags: List[bool] = []
    exs: List[float] = []; eys: List[float] = []; ess: List[float] = []; ers: List[float] = []
    es_railed = 0
    cmds: Dict[str, List[float]] = {a.dof: [] for a in axes}
    writer = None
    if args.out:
        h, w = frames[0].shape[:2]
        writer = cv2.VideoWriter(args.out, cv2.VideoWriter_fourcc(*"mp4v"), fps, (w, h))
    for i, obs in enumerate(obs_list):
        est = policy.evaluate(obs)
        e = est.error
        valid_flags.append(bool(e.valid))
        errs = {"ex": e.ex, "ey": e.ey, "es": e.es, "er": e.er, "violation": est.violation}
        cmd = ctrl.step(bool(e.valid), errs, dt)
        if e.valid:
            exs.append(e.ex); eys.append(e.ey); ess.append(e.es); ers.append(e.er)
            if abs(e.es) >= 0.999:
                es_railed += 1
            for dof, u in cmd.items():
                cmds[dof].append(u)
        if writer is not None:
            draw_transect_overlay(frames[i], model, est, obs)
            writer.write(frames[i])
    if writer is not None:
        writer.release()
        print(f"\n[replay] wrote annotated video -> {args.out}")

    nv = sum(valid_flags)
    transitions = sum(1 for a, b in zip(valid_flags, valid_flags[1:]) if a != b)
    print(f"\n=== POLICY ===  valid-lock {nv}/{len(valid_flags)} = {_pct(nv, len(valid_flags)):.1f}%"
          f"   valid<->invalid transitions: {transitions}")
    print(f"  es railed -1 (ROV too high/far): {es_railed}/{max(1, nv)} = {_pct(es_railed, nv):.1f}% of locked frames")
    print(_dist("ex", exs)); print(_dist("ey", eys)); print(_dist("es", ess)); print(_dist("er", ers))

    print(f"\n=== CONTROL (offline replica, {'PROPOSED' if args.proposed else 'CURRENT'} gains, pilot neutral) ===")
    for a in axes:
        xs = cmds[a.dof]
        if not xs:
            print(f"  {a.dof:6s} <- {a.error_key}: (never commanded)")
            continue
        sat = sum(1 for u in xs if abs(u) >= a.out_limit - 1e-6)
        s = sorted((abs(u) for u in xs))
        p95 = s[min(len(s) - 1, int(0.95 * len(s)))]
        print(f"  {a.dof:6s} <- {a.error_key} kp={a.kp:.2f} out_limit={a.out_limit:.2f}: "
              f"|u| p50={statistics.median(abs(u) for u in xs):.3f} p95={p95:.3f}  "
              f"saturated {sat}/{len(xs)} = {_pct(sat, len(xs)):.1f}%")
    print()


def _build_model(args, calib) -> TransectModel:
    kw = dict(target_cx=args.target_cx, target_cy=args.target_cy)
    if args.target_blue_fraction is not None:
        kw["target_blue_fraction"] = args.target_blue_fraction
    return TransectModel(**kw)


def _calibrate(model: TransectModel, obs_list: List[TransectObservation]) -> Optional[dict]:
    """Position centering (cx/cy median over well-framed frames) + an honest size/
    altitude assessment.

    Position is a real calibration (where does the operator center the target). Size
    is NOT calibrated from the flight: the on-station ``blue_fraction`` is fixed by
    geometry (``blue_cm / W*``) for a nadir camera, and the flight median just tells
    you how high the ROV flew -- usually too high (red still in frame). So we report
    the geometric size target and whether the flight ever reached it, rather than
    suggesting a (misleading) size setpoint.
    """
    em = model.edge_margin
    good = [o for o in obs_list if o.blue_found
            and model.min_blue_fraction <= o.blue_fraction <= model.max_blue_fraction
            and em <= o.blue_cx <= 1.0 - em and em <= o.blue_cy <= 1.0 - em]
    if len(good) < 10:
        return None
    fracs = [o.blue_fraction for o in (o for o in obs_list if o.blue_found)]
    geo = model.nominal_blue_fraction  # geometric on-station fraction (blue_cm / W*)
    # "on-station" ~ within +/-10% of the geometric size (red just leaving frame).
    on_station = sum(1 for f in fracs if abs(f - geo) <= 0.1 * geo)
    return {
        "cx": statistics.median(o.blue_cx for o in good),
        "cy": statistics.median(o.blue_cy for o in good),
        "geo_fraction": geo,
        "max_fraction": max(fracs) if fracs else 0.0,
        "median_fraction": statistics.median(fracs) if fracs else 0.0,
        "on_station_pct": _pct(on_station, len(fracs)),
    }


def _print_calibration(model: TransectModel, calib: Optional[dict]) -> None:
    print("\n=== CALIBRATE ===")
    if calib is None:
        print("  not enough well-framed detections (<10) -- fly on-station and re-record")
        return
    cx, cy = calib["cx"], calib["cy"]
    off = max(abs(cx - 0.5), abs(cy - 0.5))
    print(f"  POSITION centering: target_cx~{cx:.3f}  target_cy~{cy:.3f}")
    if off <= 0.05:
        print(f"    -> within noise of nadir 0.5/0.5; keep TransectModel() defaults.")
    else:
        print(f"    -> camera not perfectly nadir; consider "
              f"TransectModel(target_cx={cx:.2f}, target_cy={cy:.2f}) in gui/main_window.py")
    geo, mx = calib["geo_fraction"], calib["max_fraction"]
    print(f"  SIZE/ALTITUDE (geometry-fixed, NOT calibrated from flight):")
    print(f"    on-station blue_fraction = {geo:.3f}  (footprint {model.footprint_target_cm:.0f}cm)")
    print(f"    flight reached max {mx:.3f} (footprint {model.blue_cm / mx:.0f}cm), "
          f"median {calib['median_fraction']:.3f}; on-station {calib['on_station_pct']:.0f}% of frames")
    if calib["on_station_pct"] < 25:
        print(f"    -> ROV was mostly TOO HIGH (es rails negative). Descend until the blue")
        print(f"       square ~fills the frame and red just leaves (es~0) BEFORE engaging.")


if __name__ == "__main__":
    main()
