"""Render the transect autopilot state onto a video frame.

Draws what the station-keeping model "sees and wants" so the pilot can trust the
lock before engaging: the target box (where the blue square should sit, from the
geometry/calibration), the position-tolerance window, the detected blue square
(when a CV detector is wired), the error vector, any red-violation edges, and a
HUD with the lock light + cm diagnostics.

Pure drawing (numpy/cv2) over a BGR frame -> annotated BGR frame. Used both by
the offline demo tool (``tools/transect_overlay_demo.py``) and, converted to a
QImage, by the live transect-tab overlay. Kept separate from
``transect_policy`` so the model stays dependency-free.
"""

from __future__ import annotations

import math
from typing import Optional, Tuple

import cv2
import numpy as np

from tracking.transect_policy import TransectEstimate, TransectModel, TransectObservation


# BGR
_GREEN = (0, 200, 0)
_AMBER = (0, 180, 255)
_RED = (0, 0, 255)
_CYAN = (235, 235, 0)
_GRAY = (165, 165, 165)
_WHITE = (255, 255, 255)
_BLACK = (0, 0, 0)

_LOCK_COLORS = {
    "lock": _GREEN,
    "acquiring": _AMBER,
    "lost": _RED,
    "no_target": _GRAY,
}


def _px(nx: float, ny: float, w: int, h: int) -> Tuple[int, int]:
    return int(round(float(nx) * w)), int(round(float(ny) * h))


def square_state(er: float) -> Tuple[str, Tuple[int, int, int]]:
    """Pilot-facing 'how square does the target look' read-out from the rotation error.

    er is the blue square's apparent rotation normalized so 0 == head-on (squared up,
    max margin) and |er| == 1 at the 45deg diamond. Shared by the overlay HUD and the
    live transect status chip so both speak the same language.
    """
    a = abs(float(er))
    if a < 0.15:
        return "SQUARE", _GREEN        # head-on -- what we want
    if a < 0.5:
        return "TILTED", _AMBER
    return "DIAMOND", _RED             # approaching 45deg -- corners clip


def _draw_label(img: np.ndarray, lines, org, *, scale=0.5, fg=_WHITE, pad=6) -> None:
    """Draw left-aligned text lines on a translucent dark plate."""
    font = cv2.FONT_HERSHEY_SIMPLEX
    th = 1
    sizes = [cv2.getTextSize(t, font, scale, th)[0] for t, _ in lines] or [(0, 0)]
    bw = max(s[0] for s in sizes) + 2 * pad
    line_h = max(s[1] for s in sizes) + 8
    bh = line_h * len(lines) + pad
    x0, y0 = org
    x1, y1 = min(x0 + bw, img.shape[1]), min(y0 + bh, img.shape[0])
    roi = img[y0:y1, x0:x1]
    if roi.size:
        roi[:] = (0.35 * roi + 0.65 * np.array(_BLACK, dtype=np.float32)).astype(np.uint8)
    y = y0 + line_h - 6
    for text, color in lines:
        cv2.putText(img, text, (x0 + pad, y), font, scale, color, th, cv2.LINE_AA)
        y += line_h


def _dashed_rect(img, p0, p1, color, *, thickness=1, dash=10) -> None:
    x0, y0 = p0
    x1, y1 = p1
    for (a, b) in (((x0, y0), (x1, y0)), ((x1, y0), (x1, y1)),
                   ((x1, y1), (x0, y1)), ((x0, y1), (x0, y0))):
        dist = int(np.hypot(b[0] - a[0], b[1] - a[1]))
        if dist == 0:
            continue
        for s in range(0, dist, dash * 2):
            t0, t1 = s / dist, min((s + dash) / dist, 1.0)
            pa = (int(a[0] + (b[0] - a[0]) * t0), int(a[1] + (b[1] - a[1]) * t0))
            pb = (int(a[0] + (b[0] - a[0]) * t1), int(a[1] + (b[1] - a[1]) * t1))
            cv2.line(img, pa, pb, color, thickness, cv2.LINE_AA)


def draw_transect_overlay(
    frame_bgr: np.ndarray,
    model: TransectModel,
    estimate: TransectEstimate,
    observation: Optional[TransectObservation] = None,
    *,
    hud: bool = True,
) -> np.ndarray:
    """Annotate ``frame_bgr`` in place (and return it) with the transect state."""
    img = frame_bgr
    h, w = img.shape[:2]
    lock_state = estimate.lock_state
    state_color = _LOCK_COLORS.get(lock_state, _GRAY)

    # --- target box: where the blue square should sit when on-station ---------
    tcx, tcy = model.target_cx, model.target_cy
    side = model.nominal_blue_fraction * w
    half = side / 2.0
    cx_px, cy_px = _px(tcx, tcy, w, h)
    _dashed_rect(
        img,
        (int(cx_px - half), int(cy_px - half)),
        (int(cx_px + half), int(cy_px + half)),
        _CYAN, thickness=2, dash=14,
    )
    # Position-tolerance window the centroid may wander within.
    tol_px = int(model.image_pos_tol * w)
    cv2.circle(img, (cx_px, cy_px), tol_px, _CYAN, 1, cv2.LINE_AA)
    cv2.drawMarker(img, (cx_px, cy_px), _CYAN, cv2.MARKER_CROSS, 18, 1, cv2.LINE_AA)

    # --- detected blue square (when a detector supplies an observation) -------
    if observation is not None and observation.blue_found:
        bcx, bcy = _px(observation.blue_cx, observation.blue_cy, w, h)
        bside = max(2.0, observation.blue_fraction * w)
        box = cv2.boxPoints(((bcx, bcy), (bside, bside), float(observation.blue_rotation_deg)))
        det_color = state_color if lock_state in ("lock", "acquiring") else _GRAY
        cv2.polylines(img, [box.astype(np.int32)], True, det_color, 2, cv2.LINE_AA)
        cv2.drawMarker(img, (bcx, bcy), det_color, cv2.MARKER_TILTED_CROSS, 16, 2, cv2.LINE_AA)
        # Rotation axis: the square's measured edge direction. Green when squared up
        # (|er| small), amber when rotated toward a diamond -- the at-a-glance yaw read.
        ang = math.radians(observation.blue_rotation_deg)
        ax = bside * 0.7
        dx, dy = math.cos(ang) * ax, math.sin(ang) * ax
        sq_color = _GREEN if abs(estimate.error.er) < 0.15 else _AMBER
        cv2.line(img, (int(bcx - dx), int(bcy - dy)), (int(bcx + dx), int(bcy + dy)),
                 sq_color, 2, cv2.LINE_AA)
        # Error vector from the detected center to the target.
        cv2.arrowedLine(img, (bcx, bcy), (cx_px, cy_px), _WHITE, 1, cv2.LINE_AA, tipLength=0.2)

    # --- red-violation edge bars ---------------------------------------------
    if observation is not None:
        bar = max(6, h // 50)
        for mag, p0, p1 in (
            (observation.red_left, (0, 0), (bar, h)),
            (observation.red_right, (w - bar, 0), (w, h)),
            (observation.red_top, (0, 0), (w, bar)),
            (observation.red_bottom, (0, h - bar), (w, h)),
        ):
            if mag > 0.02:
                ov = img.copy()
                cv2.rectangle(ov, p0, p1, _RED, -1)
                a = float(np.clip(mag, 0.0, 1.0))
                cv2.addWeighted(ov, a, img, 1 - a, 0, img)

    # --- HUD ------------------------------------------------------------------
    if hud:
        cv2.circle(img, (w - 26, 26), 12, state_color, -1, cv2.LINE_AA)
        cv2.circle(img, (w - 26, 26), 12, _BLACK, 1, cv2.LINE_AA)
        lines = [(f"{lock_state.upper()}  conf {estimate.confidence*100:4.0f}%", state_color)]
        if estimate.clean:
            lines.append(("CLEAN (no red)", _GREEN))
        elif estimate.violation > 0:
            lines.append((f"RED VISIBLE  {estimate.violation*100:3.0f}%", _RED))
        e = estimate.error
        er_txt = f"er {e.er:+.2f}"
        if observation is not None and observation.blue_found:
            er_txt += f" (rel {observation.rotation_reliability:.2f})"
        lines.append((f"ex {e.ex:+.2f}  ey {e.ey:+.2f}  es {e.es:+.2f}  {er_txt}", _WHITE))
        if e.valid:
            state, scol = square_state(e.er)
            lines.append((f"TARGET: {state}", scol))
        if estimate.footprint_cm is not None:
            lines.append((f"footprint {estimate.footprint_cm:5.0f}cm  (W* {model.footprint_target_cm:.0f})", _WHITE))
        if estimate.margin_cm is not None:
            mcol = _GREEN if estimate.margin_cm > 5 else _AMBER if estimate.margin_cm > 0 else _RED
            lines.append((f"margin {estimate.margin_cm:+5.1f}cm", mcol))
        _draw_label(img, lines, (12, 12))

    return img
