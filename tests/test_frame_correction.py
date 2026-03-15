import numpy as np

from video.frame_correction import WaterCorrection


def test_water_correction_preserves_shape_and_type():
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    corr = WaterCorrection()

    out = corr.apply(frame)

    assert out.shape == frame.shape
    assert out.dtype == frame.dtype


def test_water_correction_rebuilds_for_new_sizes():
    corr = WaterCorrection()

    out_a = corr.apply(np.zeros((480, 640, 3), dtype=np.uint8))
    out_b = corr.apply(np.zeros((720, 1280, 3), dtype=np.uint8))

    assert out_a.shape == (480, 640, 3)
    assert out_b.shape == (720, 1280, 3)
