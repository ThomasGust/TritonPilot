"""Robust in-frame rotation estimate for the transect blue square.

The hard part of optical yaw on this target is that the blue PVC frame is a
near-perfect **square**, and the obvious rotation measurement -- the angle of
``cv2.minAreaRect`` of the blue contour -- is *degenerate* for a square: the
``rw<rh`` branch flips at rw~=rh, so the reported angle jumps +/-45 deg on tiny
contour noise. Driving a yaw loop on that phantom signal made the vehicle rock.
(See the optical-station-keeping history; this module is the fix.)

What works instead -- verified by prototyping on real pool footage -- is to
measure orientation from **long, well-conditioned lines** rather than a
degenerate blob:

* **Primary: a magnitude-weighted gradient structure tensor** over the blue
  mask's edge pixels. A square's four edges fall into two families 90 deg apart,
  so orientation is naturally a **90 deg-periodic** quantity: accumulate
  ``cos/sin(4*theta)`` weighted by gradient magnitude, take the circular mean,
  and divide by 4. This is continuous (no discrete ``rw<rh`` flip), uses every
  edge pixel, detects in every frame, and yields a built-in reliability ``R``
  (the resultant length). On consecutive frames it measured rotation to ~0.1-2
  deg std where minAreaRect swung ~20 deg.

* **Secondary: the white PVC pipe axis.** Two white pipes attach to the
  midpoints of opposite edges and are collinear -- a single long line through the
  square, **parallel to two of the edges**. When visible it is the single best
  rotation cue (long lever arm => low angular noise) and also breaks the square's
  4-fold symmetry. It is intermittent (~50% of frames -- pipes leave frame / the
  pale pool floor), so it is a cross-check / reliability booster fused with the
  tensor, not the primary.

The **angle comes from the structure tensor alone** -- it is continuous frame to
frame, and validation on pool footage showed that blending the *intermittent* pipe
into the angle injects a step every time the pipe appears/disappears (a spurious
yaw kick: it added medium reversals on a clip where the tensor alone had none).
The pipe instead acts as a **reliability cross-check**: it reports the same
orientation mod 90 deg (it is parallel to an edge pair), so agreement boosts
confidence and disagreement flags an ambiguous read (cut reliability -> the policy
holds yaw). It also remains available to disambiguate mod-180 absolute orientation
later. The output ``angle_deg`` is in ``[-45, 45]`` with ``0 = squared up`` (edges
axis-aligned with the frame, the orientation that maximizes the see-all-blue /
no-red margin); the controller drives it to 0, never to the 45 deg diamond.

This module is **pure and stateless** (one mask in, one estimate out) so it
composes with the temporal smoothing already in ``TransectPolicy`` and is unit
tested with synthetic rotated squares. Sign convention matches the existing
``TransectObservation.blue_rotation_deg`` it replaces.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Optional, Tuple

import cv2
import numpy as np


def _fold90(deg: float) -> float:
    """Fold an angle into [-45, 45] (90 deg-periodic)."""
    return ((float(deg) + 45.0) % 90.0) - 45.0


@dataclass
class RotationConfig:
    # --- structure tensor (primary) ---
    # Only accumulate where the gradient magnitude exceeds this fraction of the
    # max (rejects flat interior / anti-aliasing fuzz; the mask edges dominate).
    grad_mag_floor_frac: float = 0.15
    min_edge_pixels: int = 200          # too few edge px -> unreliable

    # --- white pipe (secondary) ---
    enable_pipe: bool = True
    white_s_max: int = 90               # HSV: low saturation = white/grey
    white_v_min: int = 150              # HSV: bright
    pipe_blue_dilate: int = 11          # px to grow the blue mask before excluding it
    pipe_min_aspect: float = 3.0        # length/width to count as a pipe (not a blob)
    pipe_min_len_frac: float = 0.14     # min pipe length as a fraction of frame width
    pipe_min_area_frac: float = 0.0004  # min component area as a fraction of frame
    pipe_weight: float = 1.0            # reliability weight scale for a full-frame pipe
    pipe_agree_tol_deg: float = 12.0    # pipe within this of the tensor = "agrees"

    # --- reliability ---
    reliable_R: float = 0.5             # tensor R at/above this reads as fully reliable


@dataclass
class RotationEstimate:
    """One frame's rotation read-out (image space)."""
    angle_deg: float                    # [-45, 45], 0 = squared up
    reliability: float                  # [0, 1]
    tensor_angle_deg: Optional[float] = None
    tensor_R: float = 0.0
    pipe_angle_deg: Optional[float] = None
    pipe_segment: Optional[Tuple[int, int, int, int]] = None  # x1,y1,x2,y2 (mask px)
    sources: Tuple[str, ...] = ()

    @classmethod
    def none(cls) -> "RotationEstimate":
        return cls(angle_deg=0.0, reliability=0.0, sources=())


class RotationTracker:
    """Stateless blue-square rotation estimator (structure tensor + white pipe)."""

    def __init__(self, config: Optional[RotationConfig] = None):
        self.cfg = config or RotationConfig()

    def reset(self) -> None:
        return None

    # -- primary: structure tensor on the blue mask edges --------------------
    def _tensor(self, blue_mask: np.ndarray) -> Tuple[Optional[float], float, int]:
        cfg = self.cfg
        m = blue_mask.astype(np.float32)
        gx = cv2.Sobel(m, cv2.CV_32F, 1, 0, ksize=3)
        gy = cv2.Sobel(m, cv2.CV_32F, 0, 1, ksize=3)
        mag = cv2.magnitude(gx, gy)
        peak = float(mag.max())
        if peak <= 0.0:
            return None, 0.0, 0
        keep = mag >= cfg.grad_mag_floor_frac * peak
        n = int(np.count_nonzero(keep))
        if n < cfg.min_edge_pixels:
            return None, 0.0, n
        gxk, gyk, w = gx[keep], gy[keep], mag[keep]
        # gradient angle theta; fold orientation to 90 deg by working in 4*theta.
        theta = np.arctan2(gyk, gxk)
        c = float(np.sum(w * np.cos(4.0 * theta)))
        s = float(np.sum(w * np.sin(4.0 * theta)))
        wsum = float(np.sum(w))
        R = math.hypot(c, s) / wsum if wsum > 0 else 0.0
        angle = _fold90(math.degrees(math.atan2(s, c)) / 4.0)
        return angle, R, n

    # -- secondary: longest white pipe axis ----------------------------------
    def _pipe(self, hsv: np.ndarray, blue_mask: np.ndarray
              ) -> Tuple[Optional[float], Optional[Tuple[int, int, int, int]], float]:
        cfg = self.cfg
        h, w = blue_mask.shape[:2]
        white = cv2.inRange(hsv, (0, 0, cfg.white_v_min), (179, cfg.white_s_max, 255))
        if cfg.pipe_blue_dilate > 0:
            k = np.ones((cfg.pipe_blue_dilate, cfg.pipe_blue_dilate), np.uint8)
            white = cv2.bitwise_and(white, cv2.bitwise_not(cv2.dilate(blue_mask, k)))
        white = cv2.morphologyEx(white, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
        white = cv2.morphologyEx(white, cv2.MORPH_CLOSE, np.ones((9, 9), np.uint8))
        cnts, _ = cv2.findContours(white, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        best_angle: Optional[float] = None
        best_seg: Optional[Tuple[int, int, int, int]] = None
        best_len = 0.0
        min_area = cfg.pipe_min_area_frac * w * h
        for c in cnts:
            if cv2.contourArea(c) < min_area:
                continue
            (rcx, rcy), (rw, rh), _ = cv2.minAreaRect(c)
            length, width = max(rw, rh), min(rw, rh) + 1e-6
            if length / width < cfg.pipe_min_aspect or length < cfg.pipe_min_len_frac * w:
                continue
            if length <= best_len:
                continue
            vx, vy, x0, y0 = (float(v) for v in cv2.fitLine(c, cv2.DIST_L2, 0, 0.01, 0.01).flatten())
            best_len = length
            best_angle = _fold90(math.degrees(math.atan2(vy, vx)))
            half = length / 2.0
            best_seg = (int(x0 - vx * half), int(y0 - vy * half),
                        int(x0 + vx * half), int(y0 + vy * half))
        weight = 0.0 if best_angle is None else cfg.pipe_weight * min(1.5, best_len / w / cfg.pipe_min_len_frac)
        return best_angle, best_seg, weight

    # -- fuse ----------------------------------------------------------------
    def estimate(self, hsv: np.ndarray, blue_mask: np.ndarray) -> RotationEstimate:
        """Estimate the square's rotation from its blue mask (+ HSV for the pipe).

        ``hsv`` and ``blue_mask`` are the same arrays the detector already computes
        (same resolution). Returns a :class:`RotationEstimate`; never raises.
        """
        try:
            t_angle, t_R, _ = self._tensor(blue_mask)
        except Exception:
            t_angle, t_R = None, 0.0
        p_angle, p_seg, p_w = (None, None, 0.0)
        if self.cfg.enable_pipe:
            try:
                p_angle, p_seg, p_w = self._pipe(hsv, blue_mask)
            except Exception:
                p_angle, p_seg, p_w = None, None, 0.0

        if t_angle is None and p_angle is None:
            return RotationEstimate.none()

        # The angle comes from the structure tensor alone -- it is continuous frame to
        # frame. The pipe is INTERMITTENT, so blending it into the angle injects a step
        # every time it appears/disappears (a spurious yaw kick); instead it is a
        # reliability cross-check: agreement confirms (boost), disagreement flags an
        # ambiguous read (cut so the policy holds yaw). If the tensor ever fails
        # outright, fall back to the pipe.
        sources: List[str] = []
        cfg = self.cfg
        if t_angle is not None:
            sources.append("tensor")
            angle = t_angle
            reliability = min(1.0, t_R / cfg.reliable_R) if cfg.reliable_R > 0 else (1.0 if t_R > 0 else 0.0)
            if p_angle is not None:
                sources.append("pipe")
                disagree = abs(_fold90(p_angle - t_angle))
                agree = max(0.0, 1.0 - disagree / max(1e-6, cfg.pipe_agree_tol_deg))
                reliability = min(1.0, reliability * (0.6 + 0.4 * agree) + 0.2 * agree)
        else:  # pipe-only fallback
            sources.append("pipe")
            angle = p_angle
            reliability = min(1.0, 0.55 + 0.25 * min(1.0, p_w))

        return RotationEstimate(
            angle_deg=angle,
            reliability=float(max(0.0, min(1.0, reliability))),
            tensor_angle_deg=t_angle,
            tensor_R=t_R,
            pipe_angle_deg=p_angle,
            pipe_segment=p_seg,
            sources=tuple(sources),
        )
