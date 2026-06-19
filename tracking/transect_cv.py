"""Classical (training-free) detector for the transect target.

Implements the ``TransectDetector`` seam: a BGR frame in, a
:class:`~tracking.transect_policy.TransectObservation` out (blue-square
geometry + per-edge red incursion + a fit-quality score). No learned model, so
it needs no training data -- only HSV thresholds the operator tunes per pool /
lighting (use ``tools/transect_overlay_demo.py --mode classical`` against the
recordings to dial them in).

Pipeline:
  1. optional gray-world white balance (recovers the red the water absorbs and
     pulls the cyan cast toward neutral so blue/red separate cleanly);
  2. HSV threshold for the **blue** PVC square -> morphology -> largest blob ->
     ``minAreaRect`` gives center / side / rotation (robust to the square being a
     hollow ring); fit quality from squareness + ring-ness + size;
  3. HSV threshold for **red** (two hue wraps) with the fixed lower-frame
     **gripper ROI masked out** (the gripper is the same orange-red as the red
     square and must NOT read as a violation), then per-edge red fraction for the
     directional ``violation`` signal.

Everything is config-driven and clamped; ``detect`` never raises (returns
``no_target`` on any failure) so it is safe on the live video cadence.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple
import time

import cv2
import numpy as np

from tracking.transect_policy import TransectObservation

_HSV = Tuple[int, int, int]


@dataclass
class ClassicalDetectorConfig:
    # Blue PVC in HSV (OpenCV: H 0-179, S/V 0-255). The pool water is a paler,
    # less-saturated cyan, so a high-ish S floor separates the vivid blue PVC.
    blue_lo: _HSV = (90, 70, 40)
    blue_hi: _HSV = (132, 255, 255)
    # Red PVC wraps the hue circle -> two ranges.
    red1_lo: _HSV = (0, 90, 60)
    red1_hi: _HSV = (10, 255, 255)
    red2_lo: _HSV = (170, 90, 60)
    red2_hi: _HSV = (179, 255, 255)
    # Gripper region to ignore for RED (normalized x0,y0,x1,y1). Default masks the
    # bottom band where the arm/gripper sits; set None to disable (e.g. once the
    # gripper is out of frame). Tune to the actual mount.
    gripper_roi: Optional[Tuple[float, float, float, float]] = (0.0, 0.80, 1.0, 1.0)
    edge_band: float = 0.10           # width (frame fraction) of each red-incursion strip
    min_blue_area_frac: float = 0.004  # reject tiny blue blobs
    # A real transect square fills a substantial part of the frame when it is
    # usable; reject anything smaller than this fraction of the frame width. This
    # is the main false-positive killer (small blue specks / distant blue objects).
    min_side_frac: float = 0.12
    min_select_score: float = 0.30     # higher bar: must clearly be a hollow square
    # Quad check: a true square outline approximates to ~4 convex corners.
    approx_eps_frac: float = 0.04
    min_corners: int = 4
    max_corners: int = 8
    morph_ksize: int = 5
    # Gray-world white balance is OFF by default: on the cyan pool water it shifts
    # pale surfaces (water, the white photo board) toward blue and creates false
    # detections. The saturated blue PVC is already distinct without it. Enable
    # only if a specific pool's lighting needs red recovery.
    white_balance: bool = False
    proc_width: int = 640             # downscale wider frames to this for speed (0 = off)
    # Ring-ness: a hollow square's blue pixels fill only a fraction of its bbox.
    ring_fill_lo: float = 0.04
    ring_fill_hi: float = 0.60


def _gray_world(bgr: np.ndarray) -> np.ndarray:
    f = bgr.astype(np.float32)
    means = [max(1.0, float(f[:, :, c].mean())) for c in range(3)]
    gray = sum(means) / 3.0
    for c in range(3):
        f[:, :, c] *= gray / means[c]
    return np.clip(f, 0, 255).astype(np.uint8)


class ClassicalTransectDetector:
    """Threshold + geometry detector returning a :class:`TransectObservation`."""

    def __init__(self, config: Optional[ClassicalDetectorConfig] = None):
        self.cfg = config or ClassicalDetectorConfig()

    def reset(self) -> None:
        return None

    def _ring_score(self, fill: float) -> float:
        """High for a hollow outline; low for a solid blob (e.g. the photo board)."""
        cfg = self.cfg
        if fill <= cfg.ring_fill_lo:
            return 0.2                       # too sparse (noise)
        if fill <= cfg.ring_fill_hi:
            return 1.0                       # hollow ring -> the PVC square
        if fill >= 0.7:
            return 0.05                      # solid region -> reject (board/glare)
        return float(np.clip(1.0 - (fill - cfg.ring_fill_hi) / (0.7 - cfg.ring_fill_hi), 0.3, 1.0))

    def detect(self, frame_bgr) -> TransectObservation:
        try:
            return self._detect(frame_bgr)
        except Exception:
            return TransectObservation.no_target(ts=time.monotonic())

    # -- internals -----------------------------------------------------------
    def _red_incursion(self, red: np.ndarray) -> Tuple[float, float, float, float]:
        h, w = red.shape[:2]
        b = max(1, int(self.cfg.edge_band * w))
        bh = max(1, int(self.cfg.edge_band * h))

        def frac(region: np.ndarray) -> float:
            n = region.size
            return float(cv2.countNonZero(region)) / n if n else 0.0

        return (
            frac(red[:, :b]),        # left
            frac(red[:, w - b:]),    # right
            frac(red[:bh, :]),       # top
            frac(red[h - bh:, :]),   # bottom
        )

    def _detect(self, frame_bgr) -> TransectObservation:
        cfg = self.cfg
        ts = time.monotonic()
        H0, W0 = frame_bgr.shape[:2]
        img = frame_bgr
        if cfg.proc_width and W0 > cfg.proc_width:
            s = cfg.proc_width / float(W0)
            img = cv2.resize(frame_bgr, (int(W0 * s), int(H0 * s)), interpolation=cv2.INTER_AREA)
        if cfg.white_balance:
            img = _gray_world(img)
        h, w = img.shape[:2]
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)

        k = max(1, int(cfg.morph_ksize))
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k))

        # --- red (for violation), gripper ROI masked out ---------------------
        red = cv2.inRange(hsv, np.array(cfg.red1_lo), np.array(cfg.red1_hi))
        red |= cv2.inRange(hsv, np.array(cfg.red2_lo), np.array(cfg.red2_hi))
        red = cv2.morphologyEx(red, cv2.MORPH_OPEN, kernel)
        if cfg.gripper_roi is not None:
            x0, y0, x1, y1 = cfg.gripper_roi
            red[int(y0 * h):int(y1 * h), int(x0 * w):int(x1 * w)] = 0
        r_l, r_r, r_t, r_b = self._red_incursion(red)

        # --- blue square -----------------------------------------------------
        blue = cv2.inRange(hsv, np.array(cfg.blue_lo), np.array(cfg.blue_hi))
        blue = cv2.morphologyEx(blue, cv2.MORPH_OPEN, kernel)
        blue = cv2.morphologyEx(blue, cv2.MORPH_CLOSE, kernel)

        cnts, _ = cv2.findContours(blue, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        min_area = cfg.min_blue_area_frac * (w * h)
        cands = sorted(
            (c for c in cnts if cv2.contourArea(c) >= min_area),
            key=cv2.contourArea, reverse=True,
        )[:8]

        no_blue = TransectObservation(
            blue_found=False, red_left=r_l, red_right=r_r, red_top=r_t, red_bottom=r_b, ts=ts,
        )
        if not cands:
            return no_blue

        # Score each blue region as "a hollow square": squareness x ring-ness x
        # plausible size. This is what rejects the solid white photo board / glare
        # (high fill -> low ring score) in favour of the real PVC outline, even
        # when the board is the larger blue blob.
        best = None
        best_score = 0.0
        for c in cands:
            (cx, cy), (rw, rh), angle = cv2.minAreaRect(c)
            side, short = max(rw, rh), min(rw, rh)
            if side <= 1.0:
                continue
            # Hard size gate: too small to be a usable transect square -> skip.
            if side / w < cfg.min_side_frac:
                continue
            # Squareness (tighter: clearly square, not a thin/oblong blob).
            aspect = short / side
            aspect_score = float(np.clip((aspect - 0.7) / 0.3, 0.0, 1.0))
            if aspect_score <= 0.0:
                continue
            # Quad-outline check: a real square approximates to a small number of
            # corners. Kept lenient (corner-count only, no strict convexity) so a
            # noisy/partially-broken underwater outline still passes.
            peri = cv2.arcLength(c, True)
            approx = cv2.approxPolyDP(c, cfg.approx_eps_frac * peri, True)
            if not (cfg.min_corners <= len(approx) <= cfg.max_corners):
                continue
            # Ring-ness: hollow outline (low fill) vs solid blob (high fill).
            box = cv2.boxPoints(((cx, cy), (rw, rh), angle)).astype(np.int32)
            rmask = np.zeros((h, w), np.uint8)
            cv2.fillConvexPoly(rmask, box, 255)
            rect_px = max(1, cv2.countNonZero(rmask))
            fill = float(cv2.countNonZero(cv2.bitwise_and(blue, rmask))) / rect_px
            ring_score = self._ring_score(fill)
            score = aspect_score * ring_score
            if score > best_score:
                best_score = score
                best = (cx, cy, rw, rh, angle)

        if best is None or best_score < cfg.min_select_score:
            return no_blue

        cx, cy, rw, rh, angle = best
        side = max(rw, rh)
        fit_quality = float(np.clip(best_score, 0.0, 1.0))

        # Normalize rotation to [-45, 45] (square is 90deg-symmetric).
        rot = float(angle)
        if rw < rh:
            rot += 90.0
        rot = ((rot + 45.0) % 90.0) - 45.0

        return TransectObservation(
            blue_found=True,
            blue_cx=float(cx) / w,
            blue_cy=float(cy) / h,
            blue_fraction=float(side) / w,
            blue_rotation_deg=rot,
            fit_quality=fit_quality,
            occlusion=0.0,
            red_left=r_l, red_right=r_r, red_top=r_t, red_bottom=r_b,
            ts=ts,
        )
