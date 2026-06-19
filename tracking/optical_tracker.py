"""Optical target-tracking interface for the station-keeping autopilot.

The MATE RANGER "hold position in current" task needs the ROV to keep a target
(e.g. a blue square) framed while keeping forbidden content (the red square it is
inscribed in) out of frame, viewed through the transect/arm camera. The control
that counters current lives ROV-side in ``control/station_keep.py``; the
*perception* that produces the error signal will live here, topside, where the
video and GPU are.

This module defines the contract only. A future CV implements ``OpticalTracker``
and returns a :class:`VisualTargetError` per frame; that error is serialized with
:meth:`VisualTargetError.to_visual_payload` and sent to the ROV inside the pilot
command at ``modes["autopilot"]["visual"]`` (with ``modes["autopilot"]
["station_keep"] = True``). The ROV ``StationKeepController`` consumes exactly
that payload. Keep this schema in sync with ``StationKeepController``.

Error sign convention (normalized to roughly [-1, 1]):
    ex  horizontal offset of the target center from where we want it
        (+ = target is right of desired)        -> drives sway by default
    ey  vertical offset (+ = target below desired) -> (optional) heave
    es  scale/size error (+ = target too large => too close) -> drives surge
    er  rotation error (+ = target rotated CW in frame; 0 = squared-on) -> yaw.
        Squaring the target up maximizes the see-all-blue/no-red margin.
    violation  0..1 amount of forbidden content visible (red border) -> bias

The exact error-component -> thrust-DOF mapping, gains, and signs are tuned in
``rov_config`` (``STATION_KEEP_*``); this layer only reports what it sees.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


def _clamp(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return lo if x < lo else hi if x > hi else x


@dataclass(frozen=True)
class VisualTargetError:
    """One frame's worth of target-tracking error (the CV output)."""

    valid: bool = False
    ex: float = 0.0
    ey: float = 0.0
    es: float = 0.0
    er: float = 0.0          # rotation error (+ = target rotated CW); 0 = squared-on
    violation: float = 0.0
    confidence: float = 0.0
    ts: Optional[float] = None

    @classmethod
    def no_lock(cls, *, ts: Optional[float] = None) -> "VisualTargetError":
        """No confident target this frame -> controller falls back to manual."""
        return cls(valid=False, ts=ts if ts is not None else time.monotonic())

    def to_visual_payload(self) -> Dict[str, Any]:
        """Serialize to the dict the ROV StationKeepController expects.

        Values are clamped to the controller's normalized range. Always stamps a
        timestamp so the ROV can detect a frozen producer.
        """
        ts = self.ts if self.ts is not None else time.monotonic()
        if not self.valid:
            return {"valid": False, "ts": float(ts)}
        return {
            "valid": True,
            "ts": float(ts),
            "ex": _clamp(float(self.ex)),
            "ey": _clamp(float(self.ey)),
            "es": _clamp(float(self.es)),
            "er": _clamp(float(self.er)),
            "violation": _clamp(float(self.violation), 0.0, 1.0),
            "confidence": _clamp(float(self.confidence), 0.0, 1.0),
        }


_DIRECT_DOFS = ("surge", "sway", "heave", "roll", "pitch", "yaw")


@dataclass
class StationKeepCommand:
    """Full control intent the model can emit for "good target lock in current".

    The model has three, freely-combinable ways to drive the vehicle:

    1. **Error** (``error``): let the ROV's tuned PID map the visual error to
       DOFs. Good for a hand-tuned classical baseline.
    2. **Direct DOF outputs** (``surge``/``sway``/``heave``/``roll``/``pitch``/
       ``yaw``, each normalized [-1, 1] or ``None``): the model *is* the
       controller and commands thrust straight through (capped ROV-side by
       ``STATION_KEEP_DIRECT_LIMIT``). This is the path for a learned policy.
    3. **Setpoints** (``depth_m``/``yaw_deg``/``roll_deg``/``pitch_deg`` +
       hold-enable flags): drive the vehicle's *well-tuned, drift-free* depth and
       attitude holds dynamically (e.g. "track to depth 1.5 m, heading 30°")
       instead of fighting them with raw thrust.

    So yes -- dynamic depth control, full translation, and roll/pitch/yaw are all
    supported, per-DOF, mixing direct thrust and setpoints however the model wants.
    A DOF left ``None`` is not touched by station-keep (pilot/other holds keep it).
    """

    error: VisualTargetError = field(default_factory=VisualTargetError.no_lock)
    # Direct normalized thrust outputs (model-as-controller); None = don't drive.
    surge: Optional[float] = None
    sway: Optional[float] = None
    heave: Optional[float] = None
    roll: Optional[float] = None
    pitch: Optional[float] = None
    yaw: Optional[float] = None
    # Dynamic setpoints for the depth/attitude holds; None = leave target as-is.
    depth_m: Optional[float] = None
    yaw_deg: Optional[float] = None
    roll_deg: Optional[float] = None
    pitch_deg: Optional[float] = None
    # Which holds the model wants enabled this tick.
    depth_hold: bool = False
    yaw_hold: bool = False
    roll_pitch_level: bool = False

    def to_autopilot_modes(self, *, station_keep: bool = True) -> Dict[str, Any]:
        visual = self.error.to_visual_payload()
        direct = {
            dof: _clamp(float(getattr(self, dof)))
            for dof in _DIRECT_DOFS
            if getattr(self, dof) is not None
        }
        # Direct commands only take effect with a valid lock (safety): the ROV
        # falls back to manual when the model isn't confident.
        if direct and visual.get("valid"):
            visual["command"] = direct

        ap: Dict[str, Any] = {"station_keep": bool(station_keep), "visual": visual}

        targets: Dict[str, Any] = {}
        if self.depth_m is not None:
            targets["depth_m"] = float(self.depth_m)
        if self.yaw_deg is not None:
            targets["yaw_deg"] = float(self.yaw_deg)
        if self.roll_deg is not None:
            targets["roll_deg"] = float(self.roll_deg)
        if self.pitch_deg is not None:
            targets["pitch_deg"] = float(self.pitch_deg)
        if targets:
            ap["targets"] = targets

        if self.depth_hold:
            ap["depth"] = True
        if self.yaw_hold:
            ap["yaw"] = "hold"
        if self.roll_pitch_level:
            ap["roll_pitch_level"] = True
        return {"autopilot": ap}


def station_keep_modes(error: VisualTargetError, *, enabled: bool = True) -> Dict[str, Any]:
    """Build the ``modes`` fragment to merge into the outgoing pilot command.

    Usage (once the CV exists)::

        err = tracker.process(frame)
        modes = merge(modes, station_keep_modes(err, enabled=hold_active))

    The ROV reads ``modes["autopilot"]["station_keep"]`` and
    ``modes["autopilot"]["visual"]``.
    """
    return {
        "autopilot": {
            "station_keep": bool(enabled),
            "visual": error.to_visual_payload(),
        }
    }


class OpticalTracker(ABC):
    """Interface a CV target tracker must implement.

    Implementations are stateful (they may track across frames) and must be
    cheap enough to run on the topside video cadence. ``process`` should never
    raise for a missing/poor target -- return ``VisualTargetError.no_lock()``.
    """

    @abstractmethod
    def process(self, frame: Any) -> VisualTargetError:
        """Return the target error for one BGR frame (or no_lock())."""
        raise NotImplementedError

    def reset(self) -> None:  # pragma: no cover - optional hook
        """Clear any per-track state (e.g. when the hold is re-armed)."""
        return None


class NullOpticalTracker(OpticalTracker):
    """Placeholder tracker until the real CV exists: always reports no lock.

    Wiring this in end-to-end exercises the full path safely -- the ROV
    StationKeepController will report ``reason="no_lock"`` and pass manual
    control through, commanding nothing.
    """

    def process(self, frame: Any) -> VisualTargetError:
        return VisualTargetError.no_lock()
