import cv2
import numpy as np
import pytest

from analysis.color_corr import FixedIntervalFrameExportWorker, ProcessingSettings, VideoProcessor


pytestmark = pytest.mark.vision


def test_pvc_mask_rejects_flat_bright_water_like_background():
    frame = np.full((240, 320, 3), (175, 185, 165), dtype=np.uint8)

    mask_source = VideoProcessor.detection_source(frame)
    pvc_mask = VideoProcessor.create_pvc_mask(mask_source)

    assert np.count_nonzero(pvc_mask) < frame.shape[0] * frame.shape[1] * 0.01


def test_red_mask_keeps_center_target_and_rejects_border_blob():
    frame = np.full((240, 320, 3), (170, 178, 160), dtype=np.uint8)
    cv2.rectangle(frame, (0, 0), (80, 70), (110, 115, 170), thickness=cv2.FILLED)
    cv2.rectangle(frame, (145, 105), (175, 135), (85, 80, 165), thickness=cv2.FILLED)

    mask_source = VideoProcessor.detection_source(frame)
    red_mask = VideoProcessor.create_red_mask(mask_source)

    center = red_mask[105:136, 145:176]
    border = red_mask[0:71, 0:81]

    assert np.count_nonzero(center) > center.size * 0.65
    assert np.count_nonzero(border) == 0


def test_target_processing_does_not_bake_mask_overlay_into_output():
    frame = np.full((120, 160, 3), (150, 170, 165), dtype=np.uint8)
    cv2.rectangle(frame, (70, 50), (88, 68), (85, 80, 165), thickness=cv2.FILLED)
    settings = ProcessingSettings(red_target_boost=0.1, draw_masks=False)

    corrected, _pvc_mask, red_mask = VideoProcessor.process_frame(frame, settings)

    assert np.count_nonzero(red_mask) > 0
    assert not np.any(np.all(corrected == np.array([0, 0, 255], dtype=np.uint8), axis=2))


def test_default_processing_skips_diagnostic_masks():
    frame = np.full((120, 160, 3), (150, 170, 165), dtype=np.uint8)

    _corrected, pvc_mask, red_mask = VideoProcessor.process_frame(frame, ProcessingSettings())

    assert np.count_nonzero(pvc_mask) == 0
    assert np.count_nonzero(red_mask) == 0


def test_fixed_interval_export_targets_every_tenth_second_at_30fps():
    indices = FixedIntervalFrameExportWorker.target_frame_indices(30.0, 30, 0.1)

    assert indices == [0, 3, 6, 9, 12, 15, 18, 21, 24, 27]


def test_fixed_interval_export_skips_duplicate_targets_for_low_fps():
    indices = FixedIntervalFrameExportWorker.target_frame_indices(5.0, 5, 0.1)

    assert indices == [0, 1, 2, 3, 4]
