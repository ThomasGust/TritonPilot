"""Synthetic-image tests for the classical transect detector."""

import numpy as np
import pytest

from tracking.transect_cv import ClassicalDetectorConfig, ClassicalTransectDetector
from tracking.transect_policy import TransectObservation

W, H = 400, 320
BLUE = (255, 0, 0)   # BGR
RED = (0, 0, 255)


def _det(**over):
    cfg = ClassicalDetectorConfig(white_balance=False, proc_width=0, **over)
    return ClassicalTransectDetector(cfg)


def _blank():
    return np.zeros((H, W, 3), np.uint8)


def test_detects_blue_square_ring_center_and_size():
    frame = _blank()
    # Hollow blue square (a PVC outline), centered-ish.
    import cv2
    cv2.rectangle(frame, (100, 80), (300, 280), BLUE, thickness=8)
    obs = _det().detect(frame)
    assert obs.blue_found is True
    assert obs.blue_cx == pytest.approx(0.5, abs=0.05)      # (100+300)/2 / 400
    assert obs.blue_cy == pytest.approx(0.5625, abs=0.06)   # (80+280)/2 / 320
    assert obs.blue_fraction == pytest.approx(0.5, abs=0.08)  # ~200/400
    assert obs.fit_quality > 0.4
    assert max(obs.red_left, obs.red_right, obs.red_top, obs.red_bottom) == 0.0


def test_no_blue_reports_no_target():
    obs = _det().detect(_blank())
    assert obs.blue_found is False


def test_red_at_right_edge_is_directional_violation():
    import cv2
    frame = _blank()
    cv2.rectangle(frame, (W - 18, 0), (W, int(H * 0.5)), RED, thickness=-1)
    obs = _det().detect(frame)
    assert obs.red_right > 0.05
    assert obs.red_left == pytest.approx(0.0, abs=1e-6)
    assert obs.red_top > 0.0   # the bar also touches the top strip


def test_red_inside_gripper_roi_is_ignored():
    import cv2
    frame = _blank()
    # Red only in the bottom gripper band (default ROI y>=0.80) -> must be masked.
    cv2.rectangle(frame, (0, int(H * 0.85)), (W, H), RED, thickness=-1)
    obs = _det().detect(frame)
    assert obs.red_bottom == pytest.approx(0.0, abs=1e-6)
    assert obs.red_left == pytest.approx(0.0, abs=1e-6)


def test_gripper_roi_disabled_sees_the_red():
    import cv2
    frame = _blank()
    cv2.rectangle(frame, (0, int(H * 0.85)), (W, H), RED, thickness=-1)
    obs = _det(gripper_roi=None).detect(frame)
    assert obs.red_bottom > 0.5


def test_detect_never_raises_on_garbage():
    det = _det()
    assert det.detect(np.zeros((4, 4, 3), np.uint8)).blue_found is False
    # Wrong shape -> guarded, returns no_target rather than raising.
    assert isinstance(det.detect(np.zeros((10, 10), np.uint8)), TransectObservation)


def test_white_balance_path_runs_on_tinted_scene():
    import cv2
    frame = np.full((H, W, 3), (90, 70, 40), np.uint8)  # teal-ish cast (BGR)
    cv2.rectangle(frame, (120, 90), (300, 270), BLUE, thickness=10)
    # Explicitly exercise the (opt-in) white-balance code path.
    cfg = ClassicalDetectorConfig(proc_width=0, white_balance=True)
    obs = ClassicalTransectDetector(cfg).detect(frame)
    assert obs.blue_found is True
