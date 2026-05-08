import numpy as np
import pytest

from analysis.iceberg_measurement import (
    MeasurementError,
    measure_affine_variable_length,
    measure_line_endpoint_iceberg_variable_length,
    measure_spatial_iceberg_variable_length,
)


def _project_points(points: np.ndarray, homography: np.ndarray) -> np.ndarray:
    homogeneous = np.column_stack((points, np.ones(len(points), dtype=np.float64)))
    projected = (homography @ homogeneous.T).T
    return projected[:, :2] / projected[:, 2:3]


def _project_world_points(points: np.ndarray, camera: np.ndarray) -> np.ndarray:
    homogeneous = np.column_stack((points, np.ones(len(points), dtype=np.float64)))
    projected = (camera @ homogeneous.T).T
    return projected[:, :2] / projected[:, 2:3]


def test_affine_measurement_uses_parallel_and_perpendicular_references():
    parallel_ref_start = np.array([120.0, 90.0])
    parallel_image_vector = np.array([18.0, 54.0])
    perpendicular_ref_start = np.array([205.0, 135.0])
    perpendicular_image_vector = np.array([96.0, -24.0])
    variable_start = np.array([260.0, 210.0])
    variable_length_cm = 37.5
    variable_end = variable_start + parallel_image_vector * (variable_length_cm / 15.0)

    result = measure_affine_variable_length(
        parallel_reference_start=parallel_ref_start,
        parallel_reference_end=parallel_ref_start + parallel_image_vector,
        perpendicular_reference_start=perpendicular_ref_start,
        perpendicular_reference_end=perpendicular_ref_start + perpendicular_image_vector,
        variable_start=variable_start,
        variable_end=variable_end,
        parallel_reference_cm=15.0,
        perpendicular_reference_cm=55.0,
    )

    assert result.length_cm == pytest.approx(variable_length_cm)
    assert result.parallel_only_length_cm == pytest.approx(variable_length_cm)
    assert result.parallel_component_cm == pytest.approx(variable_length_cm)
    assert result.perpendicular_component_cm == pytest.approx(0.0, abs=1.0e-9)
    assert result.alignment_error_degrees == pytest.approx(0.0)


def test_affine_measurement_rejects_degenerate_references():
    with pytest.raises(MeasurementError):
        measure_affine_variable_length(
            parallel_reference_start=(0.0, 0.0),
            parallel_reference_end=(10.0, 0.0),
            perpendicular_reference_start=(1.0, 1.0),
            perpendicular_reference_end=(11.0, 1.0),
            variable_start=(2.0, 2.0),
            variable_end=(20.0, 2.0),
        )


def test_affine_measurement_is_not_projective_perspective_correction():
    world_points = np.array(
        [
            [0.0, 0.0],
            [15.0, 0.0],
            [0.0, 0.0],
            [0.0, 55.0],
            [0.0, 80.0],
            [40.0, 80.0],
        ],
        dtype=np.float64,
    )
    world_to_image = np.array(
        [
            [4.2, 0.6, 120.0],
            [-0.2, 3.8, 80.0],
            [0.004, -0.006, 1.0],
        ],
        dtype=np.float64,
    )
    image_points = _project_points(world_points, world_to_image)

    result = measure_affine_variable_length(
        parallel_reference_start=image_points[0],
        parallel_reference_end=image_points[1],
        perpendicular_reference_start=image_points[2],
        perpendicular_reference_end=image_points[3],
        variable_start=image_points[4],
        variable_end=image_points[5],
    )

    assert result.length_cm > 60.0
    assert result.length_cm != pytest.approx(40.0)


def test_spatial_iceberg_measurement_recovers_variable_post_after_perspective_warp():
    side = 55.0
    known_post = 15.0
    variable_top_depth = -3.25
    variable_length = 42.75
    square_world = np.array(
        [
            [0.0, 0.0, 0.0],
            [side, 0.0, 0.0],
            [side, side, 0.0],
            [0.0, side, 0.0],
        ],
        dtype=np.float64,
    )
    known_post_world = np.array(
        [
            [0.0, 0.0, known_post],
            [side, 0.0, known_post],
            [side, side, known_post],
        ],
        dtype=np.float64,
    )
    variable_world = np.array(
        [
            [0.0, side, variable_top_depth],
            [0.0, side, variable_top_depth + variable_length],
        ],
        dtype=np.float64,
    )
    camera = np.array(
        [
            [4.0, 0.55, 1.35, 120.0],
            [-0.25, 3.7, 0.8, 85.0],
            [0.003, -0.004, 0.012, 1.0],
        ],
        dtype=np.float64,
    )

    square_image = _project_world_points(square_world, camera)
    known_post_image = _project_world_points(known_post_world, camera)
    variable_image = _project_world_points(variable_world, camera)

    result = measure_spatial_iceberg_variable_length(
        square_corner_image_points=square_image,
        known_post_end_image_points=known_post_image,
        variable_start=variable_image[0],
        variable_end=variable_image[1],
        square_side_cm=side,
        known_post_cm=known_post,
    )

    assert result.length_cm == pytest.approx(variable_length, abs=1.0e-6)
    assert result.variable_start_cm == pytest.approx((0.0, side, variable_top_depth), abs=1.0e-6)
    assert result.variable_end_cm == pytest.approx((0.0, side, variable_top_depth + variable_length), abs=1.0e-6)
    assert result.reprojection_rmse_px == pytest.approx(0.0, abs=1.0e-6)
    assert result.variable_reprojection_error_px == pytest.approx(0.0, abs=1.0e-6)


def test_spatial_iceberg_measurement_requires_full_geometry():
    with pytest.raises(MeasurementError):
        measure_spatial_iceberg_variable_length(
            square_corner_image_points=[(0.0, 0.0), (1.0, 0.0), (1.0, 1.0)],
            known_post_end_image_points=[(0.0, 2.0), (1.0, 2.0), (2.0, 2.0)],
            variable_start=(2.0, 3.0),
            variable_end=(2.0, 4.0),
        )


def test_line_endpoint_measurement_recovers_variable_with_joint_gaps():
    top_pipe = 55.0
    known_post = 15.0
    inset = 4.25
    post_offsets = (2.0, 1.25, 3.0)
    variable_top_depth = -2.5
    variable_length = 46.5
    side = top_pipe + 2.0 * inset
    top_segments = np.array(
        [
            [[inset, 0.0, 0.0], [side - inset, 0.0, 0.0]],
            [[side, inset, 0.0], [side, side - inset, 0.0]],
            [[side - inset, side, 0.0], [inset, side, 0.0]],
            [[0.0, side - inset, 0.0], [0.0, inset, 0.0]],
        ],
        dtype=np.float64,
    )
    post_corners = np.array(
        [
            [0.0, 0.0],
            [side, 0.0],
            [side, side],
        ],
        dtype=np.float64,
    )
    known_post_segments = np.array(
        [
            [[x, y, z], [x, y, z + known_post]]
            for (x, y), z in zip(post_corners, post_offsets)
        ],
        dtype=np.float64,
    )
    variable_segment = np.array(
        [
            [0.0, side, variable_top_depth],
            [0.0, side, variable_top_depth + variable_length],
        ],
        dtype=np.float64,
    )
    camera = np.array(
        [
            [4.5, 0.25, 1.1, 130.0],
            [-0.3, 4.1, 0.9, 95.0],
            [0.003, -0.004, 0.013, 1.0],
        ],
        dtype=np.float64,
    )

    top_image = _project_world_points(top_segments.reshape(-1, 3), camera).reshape(4, 2, 2)
    known_post_image = _project_world_points(known_post_segments.reshape(-1, 3), camera).reshape(3, 2, 2)
    variable_image = _project_world_points(variable_segment, camera)

    result = measure_line_endpoint_iceberg_variable_length(
        top_line_image_segments=top_image,
        known_post_image_segments=known_post_image,
        variable_image_segment=variable_image,
        top_pipe_cm=top_pipe,
        known_post_cm=known_post,
    )

    assert result.length_cm == pytest.approx(variable_length, abs=1.0e-5)
    assert result.variable_start_cm == pytest.approx(tuple(variable_segment[0]), abs=1.0e-5)
    assert result.variable_end_cm == pytest.approx(tuple(variable_segment[1]), abs=1.0e-5)
    assert result.top_joint_inset_cm == pytest.approx(inset, abs=1.0e-5)
    assert result.known_post_start_offsets_cm == pytest.approx(post_offsets, abs=1.0e-5)
    assert result.reprojection_rmse_px == pytest.approx(0.0, abs=1.0e-6)


def test_line_endpoint_measurement_requires_all_segments():
    with pytest.raises(MeasurementError):
        measure_line_endpoint_iceberg_variable_length(
            top_line_image_segments=[
                [(0.0, 0.0), (1.0, 0.0)],
                [(1.0, 0.0), (1.0, 1.0)],
            ],
            known_post_image_segments=[
                [(0.0, 0.0), (0.0, 1.0)],
                [(1.0, 0.0), (1.0, 1.0)],
                [(1.0, 1.0), (1.0, 2.0)],
            ],
            variable_image_segment=[(0.0, 1.0), (0.0, 2.0)],
        )
