"""Batch-run the transect CV model over recorded data and save annotated output.

Loads the classical detector + policy + overlay once, then annotates every
recorded video (and any still-image folders) into one easy-to-browse output
folder, plus a ``summary.csv`` / ``summary.txt`` ranking which clips actually
produced detections (so you can jump straight to the useful transect footage).

    python -m tools.transect_cv_batch                      # defaults below
    python -m tools.transect_cv_batch --out D:/review --arm-only
"""

from __future__ import annotations

import argparse
import csv
import glob
import os
import time
from pathlib import Path
from typing import Optional

import cv2
import numpy as np

from tracking.transect_cv import ClassicalDetectorConfig, ClassicalTransectDetector
from tracking.transect_overlay import draw_transect_overlay
from tracking.transect_policy import TransectModel, TransectPolicy

DEFAULT_OUT = os.path.join(os.path.expanduser("~"), "Desktop", "transect_cv_review")
DEFAULT_STILL_DIRS = [
    "C:/Users/TritonRobotics/Desktop/20260617-202117",
    "C:/Users/TritonRobotics/Desktop/20260617-210206",
]


def _model(args) -> TransectModel:
    kw = dict(target_cx=args.target_cx, target_cy=args.target_cy)
    if args.target_blue_fraction is not None:
        kw["target_blue_fraction"] = args.target_blue_fraction
    return TransectModel(**kw)


def _detector(args) -> ClassicalTransectDetector:
    cfg = ClassicalDetectorConfig()
    if args.wb:
        cfg.white_balance = True
    if args.no_gripper_mask:
        cfg.gripper_roi = None
    return ClassicalTransectDetector(cfg)


def _process_video(path: str, out_path: str, model, detector) -> dict:
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        return {"file": path, "error": "unreadable"}
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    writer = cv2.VideoWriter(out_path, cv2.VideoWriter_fourcc(*"mp4v"), fps, (w, h))
    policy = TransectPolicy(model)  # fresh temporal/lock state per clip
    frames = blue = lock = clean = 0
    max_violation = 0.0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        frames += 1
        obs = detector.detect(frame)
        est = policy.evaluate(obs)
        draw_transect_overlay(frame, model, est, obs)
        writer.write(frame)
        if obs.blue_found:
            blue += 1
        if est.lock_state == "lock":
            lock += 1
        if est.clean:
            clean += 1
        max_violation = max(max_violation, est.violation)
    cap.release()
    writer.release()
    return {
        "file": os.path.basename(path), "out": os.path.basename(out_path),
        "frames": frames, "blue_pct": _pct(blue, frames), "lock_pct": _pct(lock, frames),
        "clean_pct": _pct(clean, frames), "max_violation": round(max_violation, 2),
    }


def _process_images(folder: str, out_dir: str, model, detector) -> dict:
    files = sorted(
        f for ext in ("jpg", "jpeg", "png") for f in glob.glob(os.path.join(folder, f"*.{ext}"))
    )
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    n = blue = lock = 0
    for f in files:
        frame = cv2.imread(f)
        if frame is None:
            continue
        n += 1
        policy = TransectPolicy(model)  # each still independent
        obs = detector.detect(frame)
        for _ in range(model.min_lock_frames):
            est = policy.evaluate(obs)
        draw_transect_overlay(frame, model, est, obs)
        cv2.imwrite(os.path.join(out_dir, f"{Path(f).stem}_overlay.jpg"), frame)
        if obs.blue_found:
            blue += 1
        if est.lock_state == "lock":
            lock += 1
    return {"file": os.path.basename(folder.rstrip("/\\")) + "/", "out": os.path.basename(out_dir) + "/",
            "frames": n, "blue_pct": _pct(blue, n), "lock_pct": _pct(lock, n),
            "clean_pct": "", "max_violation": ""}


def _pct(a: int, b: int) -> float:
    return round(100.0 * a / b, 1) if b else 0.0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--recordings-dir", default="recordings")
    ap.add_argument("--images", action="append", default=None, help="still folder (repeatable)")
    ap.add_argument("--arm-only", action="store_true", help="only Arm_Camera videos")
    ap.add_argument("--wb", action="store_true")
    ap.add_argument("--no-gripper-mask", action="store_true")
    ap.add_argument("--target-cx", type=float, default=0.5)
    ap.add_argument("--target-cy", type=float, default=0.5)
    ap.add_argument("--target-blue-fraction", type=float, default=None)
    args = ap.parse_args()

    model, detector = _model(args), _detector(args)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    vids = sorted(glob.glob(os.path.join(args.recordings_dir, "**", "*_video.mp4"), recursive=True))
    if args.arm_only:
        vids = [v for v in vids if "Arm_Camera" in v]
    still_dirs = args.images if args.images is not None else [d for d in DEFAULT_STILL_DIRS if os.path.isdir(d)]

    rows = []
    t0 = time.time()
    print(f"[batch] {len(vids)} videos + {len(still_dirs)} still folders -> {out}", flush=True)
    for i, v in enumerate(vids, 1):
        session = Path(v).parent.name
        out_path = str(out / "videos" / f"{session}_{Path(v).stem}_cv.mp4")
        print(f"[batch] ({i}/{len(vids)}) {v}", flush=True)
        try:
            row = _process_video(v, out_path, model, detector)
        except Exception as exc:
            row = {"file": os.path.basename(v), "error": str(exc)}
        rows.append(row)
        print(f"         -> {row}", flush=True)
    for d in still_dirs:
        out_dir = str(out / "stills" / Path(d.rstrip("/\\")).name)
        print(f"[batch] stills {d}", flush=True)
        try:
            rows.append(_process_images(d, out_dir, model, detector))
        except Exception as exc:
            rows.append({"file": d, "error": str(exc)})

    # Summary, ranked by lock% so the useful transect footage floats to the top.
    fields = ["file", "out", "frames", "blue_pct", "lock_pct", "clean_pct", "max_violation", "error"]
    with open(out / "summary.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fields})
    ranked = sorted(rows, key=lambda r: float(r.get("lock_pct") or 0), reverse=True)
    lines = [f"Transect CV batch — {len(vids)} videos, {len(still_dirs)} still folders, {time.time()-t0:.0f}s",
             "ranked by lock% (detections):", ""]
    for r in ranked:
        if "error" in r:
            lines.append(f"  ERROR {r['file']}: {r['error']}")
        else:
            lines.append(f"  lock {str(r['lock_pct']):>5}%  blue {str(r['blue_pct']):>5}%  "
                         f"maxViol {str(r['max_violation']):>4}  {r['file']}")
    (out / "summary.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines), flush=True)
    print(f"[batch] done -> {out}", flush=True)


if __name__ == "__main__":
    main()
