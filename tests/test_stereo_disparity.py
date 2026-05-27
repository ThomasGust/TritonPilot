import json
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("cv2")

from stereo.calibration import load_stereo_calibration, resolve_stereo_calibration_path
from stereo.disparity import StereoDisparityProcessor


def _write_calibration(path: Path, *, width: int = 96, height: int = 64) -> Path:
    calibration = {
        "image_size": [width, height],
        "rig_id": "unit_test_rig",
        "left": {
            "camera_matrix": [[80.0, 0.0, width / 2.0], [0.0, 80.0, height / 2.0], [0.0, 0.0, 1.0]],
            "dist_coeffs": [0.0, 0.0, 0.0, 0.0, 0.0],
        },
        "right": {
            "camera_matrix": [[80.0, 0.0, width / 2.0], [0.0, 80.0, height / 2.0], [0.0, 0.0, 1.0]],
            "dist_coeffs": [0.0, 0.0, 0.0, 0.0, 0.0],
        },
        "stereo": {
            "baseline": 50.0,
            "rotation": [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]],
            "translation": [-50.0, 0.0, 0.0],
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(calibration), encoding="utf-8")
    return path


def test_resolve_and_load_stereo_calibration(tmp_path: Path):
    data_dir = tmp_path / "data"
    calibration_path = _write_calibration(data_dir / "calibrations" / "rig-a.json")

    resolved = resolve_stereo_calibration_path("rig-a", base_dir=data_dir)
    calibration = load_stereo_calibration(resolved)

    assert resolved == calibration_path.resolve()
    assert calibration.image_size == (96, 64)
    assert calibration.baseline_mm == pytest.approx(50.0)
    assert calibration.translation_mm.tolist() == [-50.0, 0.0, 0.0]


def test_stereo_disparity_processor_returns_color_preview(tmp_path: Path):
    calibration = load_stereo_calibration(_write_calibration(tmp_path / "stereo_calibration.json"))
    rng = np.random.default_rng(7)
    left = rng.integers(0, 255, size=(64, 96, 3), dtype=np.uint8)
    right = np.roll(left, shift=-4, axis=1)

    processor = StereoDisparityProcessor(calibration, source_size=(96, 64), max_width=64)
    preview = processor.compute(left, right)

    width, height = preview.process_size
    assert preview.preview_bgr.shape == (height, width, 3)
    assert preview.preview_bgr.dtype == np.uint8
    assert 0.0 <= preview.valid_fraction <= 1.0
