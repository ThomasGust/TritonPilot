"""Run the transect policy + overlay over images, an mp4, or a live mirror port.

This is the offline/standalone verification harness for the transect autopilot's
*perception->model->overlay* path, before the real CV detector exists:

    frame --(detector)--> TransectObservation --(TransectPolicy)--> TransectEstimate
          --(draw_transect_overlay)--> annotated frame

Detectors available now:
  --mode stub   no detection (blue_found=False) -> shows the target box / "NO LOCK"
                overlaid on real frames (handy for eyeballing target_cy / size
                calibration against the actual blue square).
  --mode fake   a synthetic on-station detection (centered, nominal size) -> shows
                the LOCK rendering end to end.

Sources:
  --images DIR_OR_GLOB     annotate still frames (writes *_overlay.jpg to --out)
  --video PATH             annotate an mp4 (writes <out>.mp4 if --out, else --show)
  --port N [--codec h264]  pull a LIVE raw feed off a UDP mirror port via the same
                           ReceiverProcess the app uses (verifies the frame source)

Examples:
  python -m tools.transect_overlay_demo --images "C:/.../20260617-210206" --mode stub --out out
  python -m tools.transect_overlay_demo --images "C:/.../shot.jpg" --mode fake --out out
  python -m tools.transect_overlay_demo --port 5300 --codec h264 --width 1920 --height 1080 --show
"""

from __future__ import annotations

import argparse
import glob
import os
import time
from pathlib import Path
from typing import Optional

import cv2
import numpy as np

from tracking.transect_overlay import draw_transect_overlay
from tracking.transect_policy import TransectModel, TransectObservation, TransectPolicy


def _stub(_frame, _model) -> TransectObservation:
    return TransectObservation.no_target(ts=time.monotonic())


def _fake(_frame, model: TransectModel) -> TransectObservation:
    # A clean, perfectly on-station detection (no red) for verifying the lock UI.
    return TransectObservation(
        blue_found=True,
        blue_cx=model.target_cx,
        blue_cy=model.target_cy,
        blue_fraction=model.nominal_blue_fraction,
        blue_rotation_deg=0.0,
        fit_quality=0.95,
        ts=time.monotonic(),
    )


_DETECTORS = {"stub": _stub, "fake": _fake}


def _build_detector(args):
    """Return a callable(frame, model) -> TransectObservation for the chosen mode."""
    if args.mode == "classical":
        from tracking.transect_cv import ClassicalDetectorConfig, ClassicalTransectDetector

        cfg = ClassicalDetectorConfig()
        if args.wb:
            cfg.white_balance = True
        if args.no_gripper_mask:
            cfg.gripper_roi = None
        det = ClassicalTransectDetector(cfg)
        return lambda frame, _model: det.detect(frame)
    return _DETECTORS[args.mode]


def _build_model(args) -> TransectModel:
    kw = dict(blue_cm=args.blue_cm, red_cm=args.red_cm,
              target_cx=args.target_cx, target_cy=args.target_cy)
    if args.target_blue_fraction is not None:
        kw["target_blue_fraction"] = args.target_blue_fraction
    return TransectModel(**kw)


def _run_images(args, model, policy, detector) -> None:
    pattern = args.images
    if os.path.isdir(pattern):
        files = sorted(
            f for ext in ("jpg", "jpeg", "png", "bmp")
            for f in glob.glob(os.path.join(pattern, f"*.{ext}"))
        )
    else:
        files = sorted(glob.glob(pattern))
    if not files:
        raise SystemExit(f"No images matched: {pattern}")
    out_dir = Path(args.out) if args.out else None
    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)
    for f in files:
        frame = cv2.imread(f)
        if frame is None:
            print(f"  skip (unreadable): {f}")
            continue
        policy.reset()  # each still is independent; don't carry lock state across
        obs = detector(frame, model)
        # Settle the lock FSM so a clean detection actually reads "lock" on a still.
        for _ in range(model.min_lock_frames):
            est = policy.evaluate(obs)
        draw_transect_overlay(frame, model, est, obs)
        if out_dir:
            dst = out_dir / f"{Path(f).stem}_overlay.jpg"
            cv2.imwrite(str(dst), frame)
            print(f"  wrote {dst}  [{est.lock_state} conf={est.confidence:.2f}]")
        if args.show:
            cv2.imshow("transect", frame)
            if cv2.waitKey(0) & 0xFF == 27:
                break


def _run_video(args, model, policy, detector) -> None:
    cap = cv2.VideoCapture(args.video)
    if not cap.isOpened():
        raise SystemExit(f"Cannot open video: {args.video}")
    writer: Optional[cv2.VideoWriter] = None
    if args.out:
        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        writer = cv2.VideoWriter(args.out, cv2.VideoWriter_fourcc(*"mp4v"), fps, (w, h))
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        obs = detector(frame, model)
        est = policy.evaluate(obs)
        draw_transect_overlay(frame, model, est, obs)
        if writer is not None:
            writer.write(frame)
        if args.show:
            cv2.imshow("transect", frame)
            if cv2.waitKey(1) & 0xFF == 27:
                break
    cap.release()
    if writer is not None:
        writer.release()
        print(f"  wrote {args.out}")


def _run_live(args, model, policy, detector) -> None:
    from video.gst_receiver import ReceiverProcess, RxConfig

    rx = ReceiverProcess(RxConfig(
        name="transect-cv", codec=args.codec, port=args.port, mode="raw",
        width=args.width, height=args.height, latency_ms=args.latency_ms,
    ))
    rx.start()
    print(f"Live raw receiver on UDP {args.port} ({args.width}x{args.height} {args.codec}). ESC to quit.")
    try:
        while True:
            pkt = rx.read_frame_packet()
            if pkt is None:
                time.sleep(0.005)
                continue
            frame = np.frombuffer(pkt.data, np.uint8).reshape((args.height, args.width, 3)).copy()
            obs = detector(frame, model)
            est = policy.evaluate(obs)
            draw_transect_overlay(frame, model, est, obs)
            if args.show:
                cv2.imshow("transect (live)", frame)
                if cv2.waitKey(1) & 0xFF == 27:
                    break
    except KeyboardInterrupt:
        pass
    finally:
        rx.stop()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--images", help="image file, dir, or glob")
    src.add_argument("--video", help="mp4 path")
    src.add_argument("--port", type=int, help="live UDP mirror port (raw receiver)")
    ap.add_argument("--mode", choices=list(_DETECTORS) + ["classical"], default="stub")
    ap.add_argument("--out", help="output dir (images) or mp4 path (video)")
    ap.add_argument("--show", action="store_true", help="display in a window")
    ap.add_argument("--wb", action="store_true", help="classical: enable gray-world white balance (off by default)")
    ap.add_argument("--no-gripper-mask", action="store_true", help="classical: don't mask the gripper ROI for red")
    # live
    ap.add_argument("--codec", choices=["h264", "jpeg"], default="h264")
    ap.add_argument("--width", type=int, default=1920)
    ap.add_argument("--height", type=int, default=1080)
    ap.add_argument("--latency-ms", type=int, default=60)
    # model / calibration
    ap.add_argument("--blue-cm", type=float, default=50.0)
    ap.add_argument("--red-cm", type=float, default=130.0)
    ap.add_argument("--target-cx", type=float, default=0.5)
    ap.add_argument("--target-cy", type=float, default=0.5)
    ap.add_argument("--target-blue-fraction", type=float, default=None)
    args = ap.parse_args()

    model = _build_model(args)
    policy = TransectPolicy(model)
    detector = _build_detector(args)

    if args.images:
        _run_images(args, model, policy, detector)
    elif args.video:
        _run_video(args, model, policy, detector)
    else:
        _run_live(args, model, policy, detector)
    if args.show:
        cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
