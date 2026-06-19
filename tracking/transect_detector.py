"""Detector seam for the transect autopilot: a BGR frame -> TransectObservation.

This is the boundary the real computer vision will fill next: pixels in, the
geometric primitives the :class:`~tracking.transect_policy.TransectPolicy` needs
out (blue-square center/size/rotation + fit quality, gripper occlusion, per-edge
red incursion). Keeping it a tiny ``Protocol`` lets the live frame source and the
offline demo run today with a stub, then swap in the classical (or learned)
detector without touching the control/overlay code.

The planned classical detector (next): HSV/Lab blue-square segmentation -> quad
fit for center/size/rotation; red detection for ``violation`` with the **fixed
lower-frame gripper ROI masked out** (the gripper is the same orange-red as the
red square and must not be read as a violation).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from tracking.transect_policy import TransectObservation


@runtime_checkable
class TransectDetector(Protocol):
    """One BGR frame -> one :class:`TransectObservation` (never raises)."""

    def detect(self, frame_bgr) -> TransectObservation:
        ...

    def reset(self) -> None:  # optional per-arm state clear
        ...


class StubTransectDetector:
    """Placeholder until the CV exists: reports no target for every frame.

    Wiring this in exercises the full live path safely -- the policy reports
    ``no_target`` and the ROV holds manual, while the overlay still shows the
    target reticle so the operator can eyeball framing/calibration.
    """

    def detect(self, frame_bgr) -> TransectObservation:  # noqa: ARG002
        return TransectObservation.no_target()

    def reset(self) -> None:
        return None
