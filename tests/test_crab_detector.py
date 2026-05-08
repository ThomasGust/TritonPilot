from pathlib import Path

import cv2
import numpy as np

from analysis.crab_detector_cv import (
    apply_channel_gains,
    classify_crab_crop,
    detect_crabs_in_video,
    detect_crabs,
    estimate_board_white_balance_gains,
    unwrap_board,
)


def test_crab_detector_finds_expected_counts_on_bundled_sample():
    sample_path = Path(__file__).resolve().parents[1] / "analysis" / "data" / "crab_samples" / "crabby.jpg"
    image = cv2.imread(str(sample_path))
    assert image is not None

    result = detect_crabs(image)

    assert result is not None
    assert result["count"] == 8
    assert result["green_count"] == 4
    assert result["other_count"] == 4


def test_crab_detector_accepts_manual_board_polygon():
    sample_path = Path(__file__).resolve().parents[1] / "analysis" / "data" / "crab_samples" / "crabby.jpg"
    image = cv2.imread(str(sample_path))
    assert image is not None

    auto_result = detect_crabs(image)
    assert auto_result is not None

    unordered_polygon = np.roll(auto_result["board_polygon"], 2, axis=0)
    manual_result = detect_crabs(image, board_polygon=unordered_polygon)

    assert manual_result is not None
    assert manual_result["board_polygon_source"] == "manual"
    assert manual_result["count"] == 8
    assert manual_result["green_count"] == 4
    assert manual_result["other_count"] == 4


def test_crab_classifier_keeps_native_rock_under_red_attenuation():
    sample_path = Path(__file__).resolve().parents[1] / "analysis" / "data" / "crab_samples" / "crabby.jpg"
    image = cv2.imread(str(sample_path))
    assert image is not None

    baseline = detect_crabs(image)
    assert baseline is not None

    underwater = image.astype(np.float32)
    underwater[:, :, 2] *= 0.55
    underwater[:, :, 1] = underwater[:, :, 1] * 1.02 + 10.0
    underwater[:, :, 0] = underwater[:, :, 0] * 1.05 + 16.0
    underwater = np.clip(underwater, 0, 255).astype(np.uint8)

    unwrapped, _, _ = unwrap_board(
        underwater,
        polygon=baseline["board_polygon"],
        output_size=baseline["unwrapped_image"].shape[1::-1],
    )
    assert unwrapped is not None

    gains = estimate_board_white_balance_gains(unwrapped, baseline["unwrapped_mask"])
    corrected = apply_channel_gains(unwrapped, gains)
    green_count = 0

    for detection in baseline["detections"]:
        x, y, width, height = detection["unwrapped_box"]
        crop = corrected[y : y + height, x : x + width]
        classification = classify_crab_crop(crop)
        green_count += int(classification["is_european_green"])

    assert green_count == 4


def test_reference_copy_detector_counts_underwater_aux_video_frame():
    video_path = (
        Path(__file__).resolve().parents[1]
        / "recordings"
        / "20260506-184600"
        / "Aux Camera.mp4"
    )
    if not video_path.exists():
        import pytest

        pytest.skip("underwater auxiliary camera recording is not available")

    capture = cv2.VideoCapture(str(video_path))
    capture.set(cv2.CAP_PROP_POS_MSEC, 5000)
    ok, frame = capture.read()
    capture.release()
    assert ok

    result = detect_crabs(frame)

    assert result is not None
    assert result["detector"] == "reference_copy"
    assert result["count"] == 8
    assert result["green_count"] == 4
    assert result["species_counts"]["jonah"] == 2
    assert result["species_counts"]["native_rock"] == 2


def test_video_detector_selects_underwater_frame_with_expected_counts():
    video_path = (
        Path(__file__).resolve().parents[1]
        / "recordings"
        / "20260506-184600"
        / "Aux Camera.mp4"
    )
    if not video_path.exists():
        import pytest

        pytest.skip("underwater auxiliary camera recording is not available")

    result = detect_crabs_in_video(
        video_path,
        start_seconds=4.5,
        end_seconds=5.1,
        sample_interval_seconds=0.5,
    )

    assert result is not None
    detection_result = result["detection_result"]
    assert detection_result["count"] == 8
    assert detection_result["green_count"] == 4
    assert result["temporal_vote"] is not None
    assert result["temporal_vote"]["signature"][:3] == (4, 2, 2)
    assert result["quality"]["confidence"] > 0.0


def test_hard_pool_video_rejects_compression_artifact_frame():
    video_path = (
        Path(__file__).resolve().parents[1]
        / "recordings"
        / "20260507-154235"
        / "Aux Camera.mp4"
    )
    if not video_path.exists():
        import pytest

        pytest.skip("hard pool auxiliary camera recording is not available")

    capture = cv2.VideoCapture(str(video_path))
    capture.set(cv2.CAP_PROP_POS_MSEC, 2500)
    ok, frame = capture.read()
    capture.release()
    assert ok

    result = detect_crabs(frame)

    assert result is None or result["count"] <= 12


def test_video_detector_selects_plausible_frame_in_hard_pool_video():
    video_path = (
        Path(__file__).resolve().parents[1]
        / "recordings"
        / "20260507-154235"
        / "Aux Camera.mp4"
    )
    if not video_path.exists():
        import pytest

        pytest.skip("hard pool auxiliary camera recording is not available")

    result = detect_crabs_in_video(
        video_path,
        start_seconds=0.0,
        end_seconds=5.0,
        sample_interval_seconds=0.5,
    )

    assert result is not None
    detection_result = result["detection_result"]
    assert detection_result["count"] == 8
    assert detection_result["green_count"] == 4
    assert detection_result["species_counts"]["jonah"] == 2
    assert detection_result["species_counts"]["native_rock"] == 2
    assert result["temporal_vote"] is not None
    assert result["temporal_vote"]["signature"][:3] == (4, 2, 2)


def test_hard_pool_video_keeps_edge_touching_green_crab():
    video_path = (
        Path(__file__).resolve().parents[1]
        / "recordings"
        / "20260507-154235"
        / "Aux Camera.mp4"
    )
    if not video_path.exists():
        import pytest

        pytest.skip("hard pool auxiliary camera recording is not available")

    capture = cv2.VideoCapture(str(video_path))
    capture.set(cv2.CAP_PROP_POS_FRAMES, 47)
    ok, frame = capture.read()
    capture.release()
    assert ok

    result = detect_crabs(frame)

    assert result is not None
    assert result["count"] == 8
    assert result["green_count"] == 4
    assert result["species_counts"]["jonah"] == 2
    assert result["species_counts"]["native_rock"] == 2
