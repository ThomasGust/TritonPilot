"""Tests for the blue-square rotation tracker (structure tensor + white pipe).

Synthetic masks/frames with a KNOWN rotation exercise the estimator the way the
real pool target does: a hollow square outline (4 long edges) and, optionally, a
collinear white pipe. The key properties under test are the ones the pool history
needed: rotation is recovered to a few degrees, the measurement is 90 deg-periodic
(a square at 0 and at 90 both read "squared up"), reliability collapses when there
is no orientation to measure, and the pipe agrees with the tensor.
"""

from __future__ import annotations

import cv2
import numpy as np

from tracking.rotation_tracker import RotationTracker, _fold90


def _ang_diff(a: float, b: float) -> float:
    """Smallest 90 deg-periodic angular distance, in degrees."""
    return abs(_fold90(a - b))


def _square_mask(angle_deg: float, size: int = 400, side: int = 200,
                 thickness: int = 8) -> np.ndarray:
    """A binary mask of a hollow square outline rotated by ``angle_deg``."""
    mask = np.zeros((size, size), np.uint8)
    c = size / 2.0
    box = cv2.boxPoints(((c, c), (side, side), float(angle_deg))).astype(np.int32)
    cv2.polylines(mask, [box], True, 255, thickness, cv2.LINE_AA)
    return mask


def _blank_hsv(size: int = 400) -> np.ndarray:
    return np.zeros((size, size, 3), np.uint8)


def test_recovers_known_rotation_within_a_few_degrees():
    trk = RotationTracker()
    hsv = _blank_hsv()
    for angle in (0.0, 10.0, 22.5, 35.0, -15.0, 44.0):
        est = trk.estimate(hsv, _square_mask(angle))
        assert est.reliability > 0.5, f"unreliable at {angle}"
        assert _ang_diff(est.angle_deg, angle) < 3.5, (
            f"angle {angle}: got {est.angle_deg:.1f}")


def test_is_90deg_periodic_square_and_diamond_endpoints():
    # A square at 0 and at 90 are the same orientation -> both read squared up (~0).
    trk = RotationTracker()
    hsv = _blank_hsv()
    for angle in (0.0, 90.0, 180.0):
        est = trk.estimate(hsv, _square_mask(angle))
        assert abs(est.angle_deg) < 3.5
    # The 45 deg diamond folds to the far edge of the range (|angle| ~ 45).
    est45 = trk.estimate(hsv, _square_mask(45.0))
    assert abs(est45.angle_deg) > 41.0


def test_reliability_collapses_without_a_clear_orientation():
    # A filled disk has gradients in every direction -> no orientation to measure.
    trk = RotationTracker()
    mask = np.zeros((400, 400), np.uint8)
    cv2.circle(mask, (200, 200), 120, 255, -1)
    est = trk.estimate(_blank_hsv(), mask)
    assert est.reliability < 0.3


def test_empty_mask_returns_no_estimate():
    trk = RotationTracker()
    est = trk.estimate(_blank_hsv(), np.zeros((400, 400), np.uint8))
    assert est.reliability == 0.0
    assert est.sources == ()


def test_white_pipe_is_detected_and_agrees_with_the_tensor():
    # A horizontal white pipe (angle 0) collinear with a squared-up square: the pipe
    # is found, agrees with the tensor, and the fused read stays squared up.
    trk = RotationTracker()
    mask = _square_mask(0.0)
    hsv = _blank_hsv()
    # white bar to the LEFT of the square (outside it), horizontal, long & thin.
    bgr = np.zeros((400, 400, 3), np.uint8)
    cv2.line(bgr, (20, 200), (130, 200), (255, 255, 255), 12, cv2.LINE_AA)
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    est = trk.estimate(hsv, mask)
    assert "pipe" in est.sources
    assert est.pipe_angle_deg is not None
    assert _ang_diff(est.pipe_angle_deg, 0.0) < 5.0
    assert abs(est.angle_deg) < 3.5
    assert est.reliability > 0.7


def test_pipe_can_be_disabled():
    trk = RotationTracker()
    trk.cfg.enable_pipe = False
    bgr = np.zeros((400, 400, 3), np.uint8)
    cv2.line(bgr, (20, 200), (200, 200), (255, 255, 255), 12, cv2.LINE_AA)
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    est = trk.estimate(hsv, _square_mask(0.0))
    assert "pipe" not in est.sources


def test_never_raises_on_garbage():
    trk = RotationTracker()
    # mismatched / odd shapes should be swallowed, not raised.
    est = trk.estimate(np.zeros((10, 10, 3), np.uint8), np.zeros((10, 10), np.uint8))
    assert est.reliability == 0.0
