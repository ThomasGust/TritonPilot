import numpy as np
import pytest

from analysis.planar_measurement import (
    MeasurementError,
    measure_planar_height,
    measure_planar_height_from_plane,
    measure_planar_segment_from_plane,
)


def _project_points(points: np.ndarray, homography: np.ndarray) -> np.ndarray:
    homogeneous = np.column_stack((points, np.ones(len(points), dtype=np.float64)))
    projected = (homography @ homogeneous.T).T
    return projected[:, :2] / projected[:, 2:3]


def test_planar_height_uses_reference_lengths_after_perspective_warp():
    rectangle = np.array(
        [
            [0.0, 0.0],
            [120.0, 0.0],
            [120.0, 80.0],
            [0.0, 80.0],
        ],
        dtype=np.float64,
    )
    references = np.array(
        [
            [[0.0, 0.0], [55.0, 0.0]],
            [[0.0, 0.0], [0.0, 35.0]],
            [[20.0, 10.0], [64.0, 43.0]],
            [[80.0, 20.0], [110.0, 20.0]],
            [[90.0, 40.0], [90.0, 70.0]],
        ],
        dtype=np.float64,
    )
    reference_lengths = np.linalg.norm(references[:, 1, :] - references[:, 0, :], axis=1)
    height_segment = np.array([[100.0, 12.0], [100.0, 68.5]], dtype=np.float64)
    true_height = float(np.linalg.norm(height_segment[1] - height_segment[0]))
    world_to_image = np.array(
        [
            [3.8, 0.55, 140.0],
            [-0.35, 4.1, 90.0],
            [0.0035, -0.0025, 1.0],
        ],
        dtype=np.float64,
    )

    image_rectangle = _project_points(rectangle, world_to_image)
    image_references = _project_points(references.reshape(-1, 2), world_to_image).reshape(-1, 2, 2)
    image_height = _project_points(height_segment, world_to_image)

    result = measure_planar_height(
        rectangle_image_points=image_rectangle,
        reference_image_segments=image_references,
        reference_lengths_cm=reference_lengths,
        height_start=image_height[0],
        height_end=image_height[1],
    )

    assert result.height_cm == pytest.approx(true_height, abs=1.0e-4)
    assert result.plane_width_cm == pytest.approx(120.0, abs=1.0e-4)
    assert result.plane_height_cm == pytest.approx(80.0, abs=1.0e-4)
    assert result.reference_rmse_cm == pytest.approx(0.0, abs=1.0e-5)


def test_planar_height_rejects_references_that_do_not_constrain_both_axes():
    with pytest.raises(MeasurementError):
        measure_planar_height(
            rectangle_image_points=[
                (0.0, 0.0),
                (10.0, 0.0),
                (10.0, 10.0),
                (0.0, 10.0),
            ],
            reference_image_segments=[
                [(1.0, 1.0), (5.0, 1.0)],
                [(2.0, 2.0), (7.0, 2.0)],
            ],
            reference_lengths_cm=[20.0, 25.0],
            height_start=(3.0, 3.0),
            height_end=(3.0, 8.0),
        )


def test_planar_height_from_plane_uses_unwrapped_coordinates():
    references = np.array(
        [
            [[0.0, 0.0], [550.0, 0.0]],
            [[0.0, 0.0], [0.0, 350.0]],
            [[200.0, 100.0], [640.0, 430.0]],
            [[800.0, 200.0], [1100.0, 200.0]],
            [[900.0, 400.0], [900.0, 700.0]],
        ],
        dtype=np.float64,
    )
    reference_lengths = np.linalg.norm(references[:, 1, :] - references[:, 0, :], axis=1) * 0.1
    height_segment = np.array([[1000.0, 120.0], [1000.0, 685.0]], dtype=np.float64)

    result = measure_planar_height_from_plane(
        reference_plane_segments=references,
        reference_lengths_cm=reference_lengths,
        height_start_plane=height_segment[0],
        height_end_plane=height_segment[1],
        plane_size_units=(1200.0, 800.0),
    )

    assert result.height_cm == pytest.approx(56.5, abs=1.0e-9)
    assert result.plane_width_cm == pytest.approx(120.0, abs=1.0e-9)
    assert result.plane_height_cm == pytest.approx(80.0, abs=1.0e-9)


def test_planar_segment_from_plane_can_measure_length():
    references = np.array(
        [
            [[0.0, 0.0], [500.0, 0.0]],
            [[0.0, 0.0], [0.0, 500.0]],
            [[600.0, 100.0], [900.0, 100.0]],
            [[1000.0, 200.0], [1000.0, 450.0]],
        ],
        dtype=np.float64,
    )
    reference_lengths = np.array([50.0, 50.0, 30.0, 25.0], dtype=np.float64)
    result = measure_planar_segment_from_plane(
        reference_plane_segments=references,
        reference_lengths_cm=reference_lengths,
        segment_start_plane=(50.0, 300.0),
        segment_end_plane=(1375.0, 300.0),
        plane_size_units=(1480.0, 700.0),
    )

    assert result.length_cm == pytest.approx(132.5, abs=1.0e-9)
