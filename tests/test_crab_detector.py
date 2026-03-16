from pathlib import Path

import cv2

from crab_detector_cv import detect_crabs


def test_crab_detector_finds_expected_counts_on_bundled_sample():
    sample_path = Path(__file__).resolve().parents[1] / "data" / "crab_samples" / "crabby.jpg"
    image = cv2.imread(str(sample_path))
    assert image is not None

    result = detect_crabs(image)

    assert result is not None
    assert result["count"] == 8
    assert result["green_count"] == 4
    assert result["other_count"] == 4
