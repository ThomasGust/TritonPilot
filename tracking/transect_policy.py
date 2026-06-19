"""Transect station-keeping policy/model (the "model" layer).

Turns *geometric observations* of the inscribed-square transect target into the
normalized visual error the ROV ``StationKeepController`` consumes. This module
encodes the **task geometry and the control setpoints**; it does NOT do any
vision. A future :class:`~tracking.optical_tracker.OpticalTracker` runs the
computer vision, packs what it sees into a :class:`TransectObservation`, hands it
here, and forwards the resulting :class:`~tracking.optical_tracker.VisualTargetError`
to the ROV (see ``MainWindow.publish_visual_target``).

Why this split: perception (pixels -> blue square geometry, red presence) is a
detection problem that will be iterated on independently; the *policy* (geometry
-> normalized error + lock decision) is the stable, testable control model. Keep
this seam clean so the CV can be swapped (classical now, learned later) without
touching the control math.

The task (MATE world championship "hold position in current"): for 30 s, keep
**all** of the blue square in frame and **none** of the surrounding red square,
viewed through the down-looking arm camera, while a current pushes the ROV.

Geometry -- the setpoint falls right out of the dimensions
----------------------------------------------------------
Blue is a ``blue_cm`` square concentric inside a ``red_cm`` square. Model the
camera's floor footprint as a window of half-size ``s`` whose center is offset by
``d`` from the transect center (analyze each axis independently, units = cm)::

    contain all blue:   s >= blue_cm/2 + |d|
    exclude all red:    s <= red_cm/2  - |d|

These are simultaneously satisfiable only while ``|d| <= (red_cm - blue_cm)/4``.
The footprint that maximizes the *minimum* margin to either failure is the
midpoint, independent of ``d``::

    target footprint  W* = (blue_cm + red_cm) / 2          (= 90 cm for 50/130)
    position tol      d_max = (red_cm - blue_cm) / 4       (= 20 cm)
    size  tol         (red_cm - blue_cm) / 2               (= 40 cm of footprint)
    min margin (cm)   = d_max - |d|                         -> 20 cm at perfect center

So the control is intentionally *forgiving* (±20 cm of slack at the sweet spot),
which is exactly what noisy underwater vision + a vehicle in current needs.
Critically, because blue is concentric inside red, simply keeping blue centered
and at the target size keeps red out automatically -- the primary loop *is* the
safety mechanism, and ``violation`` (red seen) is the backup/abort signal.

We regulate in **image space** against this invariant -- no camera calibration or
3-D reconstruction. Normalization is anchored to the geometry so the ROV gains
are physically meaningful: ``|ex|`` / ``|ey|`` reach ``1.0`` at the ``d_max``
position-failure boundary, and ``|es|`` reaches ``1.0`` at the footprint where
blue fills the frame / red touches the frame.

Error sign conventions (match ``StationKeepController`` / the contract docstring):
    ex  + = blue is right of where we want it
    ey  + = blue is below where we want it
    es  + = blue too large => footprint too small => too close / too low
    violation  0..1 amount of red visible (0 = none)
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Optional, Tuple
import time

from tracking.optical_tracker import StationKeepCommand, VisualTargetError


def _clamp(x: float, lo: float, hi: float) -> float:
    x = float(x)
    return lo if x < lo else hi if x > hi else x


@dataclass(frozen=True)
class TransectObservation:
    """One frame of raw geometric detections from the CV (image space).

    Positions are frame fractions in ``[0, 1]`` with origin at the top-left.
    ``blue_fraction`` is the blue square's apparent side length as a fraction of
    the frame width (so the known blue size doubles as a metric ruler:
    ``footprint_cm = blue_cm / blue_fraction``). Red presence is reported as a
    per-edge incursion in ``[0, 1]`` (how strongly red intrudes from each frame
    edge); all zero means no red is visible. Everything here is something a
    classical detector or a learned model can produce; none of it requires camera
    calibration.
    """

    blue_found: bool = False
    blue_cx: float = 0.5
    blue_cy: float = 0.5
    blue_fraction: float = 0.0
    blue_rotation_deg: float = 0.0
    fit_quality: float = 0.0          # 0..1 geometric detection quality (CV-supplied)
    occlusion: float = 0.0            # 0..1 fraction of blue obscured (e.g. gripper)
    red_left: float = 0.0
    red_right: float = 0.0
    red_top: float = 0.0
    red_bottom: float = 0.0
    ts: Optional[float] = None

    @classmethod
    def no_target(cls, *, ts: Optional[float] = None) -> "TransectObservation":
        return cls(blue_found=False, ts=ts)


@dataclass(frozen=True)
class TransectModel:
    """Task geometry + policy tuning. Derived setpoints come from the dimensions.

    For a true nadir camera the on-station blue centroid sits at frame center,
    so ``target_cx/cy = 0.5``. The arm camera is oblique (blue rides high in
    frame), so calibrate ``target_cy`` (and ``target_blue_fraction`` if the
    oblique perspective shrinks the apparent size) from recorded on-station
    frames; the geometry defaults below are the nadir-correct starting point.
    """

    blue_cm: float = 50.0
    red_cm: float = 130.0

    # Image-space setpoint: where the blue centroid should sit when on-station.
    target_cx: float = 0.5
    target_cy: float = 0.5
    # Optional override of the on-station apparent blue size (fraction of frame).
    # None => use the nadir-geometric value blue_cm / W*.
    target_blue_fraction: Optional[float] = None
    # Rotation (deg) at which |er| reaches 1.0. The square is 90deg-symmetric, so
    # the detector reports rotation in [-45, 45]; squaring up (er -> 0) maximizes
    # the see-all-blue/no-red margin.
    rot_norm_deg: float = 45.0

    # Lock hysteresis (the "good lock" the pilot needs before engaging).
    lock_on_conf: float = 0.6
    lock_off_conf: float = 0.4
    min_lock_frames: int = 3
    lock_drop_frames: int = 5         # consecutive low-confidence frames -> lost

    # Plausibility gates feeding the confidence score.
    min_blue_fraction: float = 0.25
    max_blue_fraction: float = 0.95
    edge_margin: float = 0.04         # blue this close to a frame edge => clipped
    occlusion_conf_weight: float = 0.7
    jitter_conf_gain: float = 6.0     # confidence penalty per unit center jitter

    # Temporal smoothing of the centroid/size before computing the error.
    ema_alpha: float = 0.4

    # Optional directional retreat-from-red bias added to ex/ey. OFF by default
    # (the centering loop already keeps red out, and the sign depends on the
    # camera's image orientation -- confirm in the pool before enabling).
    red_bias_gain: float = 0.0
    # Treat red as a hard "not clean" once the worst edge incursion exceeds this.
    violation_clean_eps: float = 0.05

    @property
    def footprint_target_cm(self) -> float:
        """Footprint side that maximizes the min margin: the midpoint of the squares."""
        return 0.5 * (self.blue_cm + self.red_cm)

    @property
    def position_tol_cm(self) -> float:
        """Max centering offset before blue clips or red enters (at W*)."""
        return 0.25 * (self.red_cm - self.blue_cm)

    @property
    def size_tol_cm(self) -> float:
        """Footprint half-range: W can swing this far from W* before failure (at d=0)."""
        return 0.5 * (self.red_cm - self.blue_cm)

    @property
    def image_pos_tol(self) -> float:
        """Frame-fraction offset at which |ex|/|ey| == 1.0 (the position-failure edge)."""
        return self.position_tol_cm / self.footprint_target_cm

    @property
    def nominal_blue_fraction(self) -> float:
        """On-station apparent blue size (fraction of frame)."""
        if self.target_blue_fraction is not None:
            return float(self.target_blue_fraction)
        return self.blue_cm / self.footprint_target_cm


@dataclass
class TransectEstimate:
    """Policy output for one frame: the control error plus diagnostics for the UI."""

    error: VisualTargetError
    lock_state: str                       # no_target | acquiring | lock | lost
    confidence: float
    violation: float
    clean: bool                           # locked AND no red visible (safe to hold)
    footprint_cm: Optional[float]         # estimated camera footprint (metric ruler)
    offset_cm: Optional[Tuple[float, float]]
    margin_cm: Optional[float]            # min cm-margin to blue-clip / red-enter
    target_center: Tuple[float, float]    # effective (cx, cy) setpoint used
    reasons: List[str] = field(default_factory=list)

    def to_command(
        self,
        *,
        enable_depth_hold: bool = True,
        enable_level: bool = True,
    ) -> StationKeepCommand:
        """Wrap the error as a full StationKeepCommand for the engage path.

        Engaging the transect hold should also run the (drift-free) depth hold
        that owns bulk altitude and keep the vehicle level so the camera geometry
        stays stable -- the error here drives only sway/surge (+ a gentle heave
        size trim). Depth-hold captures the pilot's trimmed depth (no setpoint
        commanded), so the operator sets altitude first, then engages.
        """
        return StationKeepCommand(
            error=self.error,
            depth_hold=bool(enable_depth_hold),
            roll_pitch_level=bool(enable_level),
        )


class TransectPolicy:
    """Stateful obs -> VisualTargetError model for the transect hold.

    Smooths the centroid/size, scores lock confidence with hysteresis, and emits
    a geometry-normalized error. Cheap and dependency-free so it runs on the
    topside video cadence and is exercised entirely with synthetic observations
    in tests. Call :meth:`reset` when the hold is re-armed.
    """

    def __init__(self, model: Optional[TransectModel] = None):
        self.model = model or TransectModel()
        self.reset()

    def reset(self) -> None:
        self._cx: Optional[float] = None
        self._cy: Optional[float] = None
        self._frac: Optional[float] = None
        self._jitter: float = 0.0
        self._high_conf_run: int = 0
        self._low_conf_run: int = 0
        self._locked: bool = False
        self._recent: Deque[float] = deque(maxlen=8)

    # -- internals -----------------------------------------------------------
    def _smooth(self, raw: Optional[float], state: Optional[float]) -> float:
        a = float(self.model.ema_alpha)
        if state is None or raw is None:
            return float(raw if raw is not None else (state or 0.0))
        return a * float(raw) + (1.0 - a) * float(state)

    def _confidence(self, obs: TransectObservation, jitter: float) -> Tuple[float, List[str]]:
        m = self.model
        reasons: List[str] = []
        conf = _clamp(obs.fit_quality, 0.0, 1.0)
        if conf <= 0.0:
            reasons.append("no_fit")

        f = float(obs.blue_fraction)
        if f < m.min_blue_fraction or f > m.max_blue_fraction:
            conf = 0.0
            reasons.append("size_implausible")

        # Blue clipped against a frame edge -> we may be losing part of it.
        em = m.edge_margin
        if not (em <= obs.blue_cx <= 1.0 - em and em <= obs.blue_cy <= 1.0 - em):
            conf *= 0.4
            reasons.append("near_edge")

        if obs.occlusion > 0.0:
            conf *= max(0.0, 1.0 - m.occlusion_conf_weight * _clamp(obs.occlusion, 0.0, 1.0))
            if obs.occlusion > 0.3:
                reasons.append("occluded")

        conf *= _clamp(1.0 - m.jitter_conf_gain * jitter, 0.0, 1.0)
        if jitter > 0.05:
            reasons.append("jittery")

        return _clamp(conf, 0.0, 1.0), reasons

    def _update_lock(self, found: bool, conf: float) -> str:
        """Confidence-hysteresis lock FSM. Returns no_target|acquiring|lock|lost.

        Engage after ``min_lock_frames`` consecutive high-confidence frames; drop
        after ``lock_drop_frames`` consecutive low-confidence (or no-target)
        frames. The drop grace tolerates brief blue dropouts without flapping the
        pilot's indicator; per-frame ``error.valid`` still follows the actual
        detection, so a dropout frame commands nothing regardless.
        """
        m = self.model
        high = found and conf >= m.lock_on_conf
        low = (not found) or conf < m.lock_off_conf
        self._high_conf_run = self._high_conf_run + 1 if high else 0
        self._low_conf_run = self._low_conf_run + 1 if low else 0

        if self._locked:
            if self._low_conf_run >= m.lock_drop_frames:
                self._locked = False
                return "lost"
            return "lock"
        if self._high_conf_run >= m.min_lock_frames:
            self._locked = True
            self._low_conf_run = 0
            return "lock"
        return "acquiring" if found else "no_target"

    # -- main ----------------------------------------------------------------
    def evaluate(self, obs: TransectObservation) -> TransectEstimate:
        m = self.model
        ts = obs.ts if obs.ts is not None else time.monotonic()
        violation = _clamp(
            max(obs.red_left, obs.red_right, obs.red_top, obs.red_bottom), 0.0, 1.0
        )

        if not obs.blue_found:
            lock_state = self._update_lock(False, 0.0)
            return TransectEstimate(
                error=VisualTargetError.no_lock(ts=ts),
                lock_state=lock_state,
                confidence=0.0,
                violation=violation,
                clean=False,
                footprint_cm=None,
                offset_cm=None,
                margin_cm=None,
                target_center=(m.target_cx, m.target_cy),
                reasons=["no_target"],
            )

        # Smooth centroid/size and track jitter (raw-vs-smoothed residual, low-passed).
        cx = self._smooth(obs.blue_cx, self._cx)
        cy = self._smooth(obs.blue_cy, self._cy)
        frac = self._smooth(obs.blue_fraction, self._frac)
        resid = abs(obs.blue_cx - cx) + abs(obs.blue_cy - cy)
        self._jitter = self._smooth(resid, self._jitter)
        self._cx, self._cy, self._frac = cx, cy, frac

        confidence, reasons = self._confidence(obs, self._jitter)
        lock_state = self._update_lock(True, confidence)

        # Effective setpoint, optionally biased to retreat from red (off by default).
        tcx, tcy = m.target_cx, m.target_cy
        bias_x = -m.red_bias_gain * (obs.red_right - obs.red_left)
        bias_y = -m.red_bias_gain * (obs.red_bottom - obs.red_top)

        tol = m.image_pos_tol
        ex = _clamp((cx - tcx) / tol + bias_x, -1.0, 1.0)
        ey = _clamp((cy - tcy) / tol + bias_y, -1.0, 1.0)

        # Rotation error: drive yaw to square the target up (er -> 0). Uses the raw
        # detector rotation (already in [-rot_norm, rot_norm]); not EMA-smoothed to
        # avoid wrap artifacts near the +/-45deg symmetry boundary (the loop drives
        # away from that boundary anyway).
        er = _clamp(float(obs.blue_rotation_deg) / m.rot_norm_deg, -1.0, 1.0)

        # Size error: anchored to the on-station apparent size so es == 0 when
        # blue_fraction == nominal, for ANY calibration. We compare the metric-
        # ruler footprint (blue_cm / fraction) against the on-station footprint
        # W0 = blue_cm / nominal_fraction and normalize by the geometric size
        # tolerance. For a nadir camera nominal == blue_cm/W* so W0 == W* (90 cm)
        # and this is the exact geometry; for the oblique arm cam it stays
        # monotonic and zero on-station (the cm value is then an apparent-size
        # proxy, not the true ground footprint).
        footprint_cm: Optional[float] = None
        es = 0.0
        if frac > 1e-3:
            footprint_cm = m.blue_cm / frac
            w0 = m.blue_cm / m.nominal_blue_fraction
            es = _clamp((w0 - footprint_cm) / m.size_tol_cm, -1.0, 1.0)

        # Diagnostics: cm offset of blue from the setpoint + worst-case margin.
        offset_cm: Optional[Tuple[float, float]] = None
        margin_cm: Optional[float] = None
        if footprint_cm is not None:
            dx = (cx - tcx) * footprint_cm
            dy = (cy - tcy) * footprint_cm
            offset_cm = (dx, dy)
            s = 0.5 * footprint_cm
            bh, rh = 0.5 * m.blue_cm, 0.5 * m.red_cm
            margins = [
                s - (bh + abs(dx)), (rh - abs(dx)) - s,
                s - (bh + abs(dy)), (rh - abs(dy)) - s,
            ]
            margin_cm = min(margins)

        valid = lock_state == "lock"
        clean = valid and violation <= m.violation_clean_eps
        if violation > m.violation_clean_eps:
            reasons.append("red_visible")

        error = VisualTargetError(
            valid=valid,
            ex=ex,
            ey=ey,
            es=es,
            er=er,
            violation=violation,
            confidence=confidence,
            ts=ts,
        )
        return TransectEstimate(
            error=error,
            lock_state=lock_state,
            confidence=confidence,
            violation=violation,
            clean=clean,
            footprint_cm=footprint_cm,
            offset_cm=offset_cm,
            margin_cm=margin_cm,
            target_center=(tcx + bias_x, tcy + bias_y),
            reasons=reasons,
        )
