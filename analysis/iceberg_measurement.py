from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

import numpy as np


PointLike = Sequence[float] | np.ndarray


class MeasurementError(ValueError):
    """Raised when clicked geometry cannot support a measurement."""


@dataclass(frozen=True)
class AffineMeasurementResult:
    length_cm: float
    parallel_only_length_cm: float
    parallel_component_cm: float
    perpendicular_component_cm: float
    alignment_error_degrees: float
    parallel_reference_px: float
    perpendicular_reference_px: float
    variable_px: float
    world_delta_cm: tuple[float, float]


@dataclass(frozen=True)
class SpatialIcebergMeasurementResult:
    length_cm: float
    variable_start_cm: tuple[float, float, float]
    variable_end_cm: tuple[float, float, float]
    reprojection_rmse_px: float
    variable_reprojection_error_px: float
    camera_matrix_image_from_world: np.ndarray
    top_joint_inset_cm: float | None = None
    known_post_start_offsets_cm: tuple[float, float, float] | None = None


def _as_point(point: PointLike, name: str) -> np.ndarray:
    value = np.asarray(point, dtype=np.float64).reshape(-1)
    if value.shape != (2,):
        raise MeasurementError(f"{name} must be a 2D point")
    if not np.all(np.isfinite(value)):
        raise MeasurementError(f"{name} must contain finite coordinates")
    return value


def _as_world_point(point: Sequence[float] | np.ndarray, name: str) -> np.ndarray:
    value = np.asarray(point, dtype=np.float64).reshape(-1)
    if value.shape != (3,):
        raise MeasurementError(f"{name} must be a 3D point")
    if not np.all(np.isfinite(value)):
        raise MeasurementError(f"{name} must contain finite coordinates")
    return value


def _vector_length(vector: np.ndarray) -> float:
    return float(np.linalg.norm(vector))


def _normalize_points_2d(points: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    mean = np.mean(points, axis=0)
    centered = points - mean
    rms = float(np.sqrt(np.mean(np.sum(centered * centered, axis=1))))
    if rms < 1.0e-9:
        raise MeasurementError("Image calibration points are too close together")
    scale = np.sqrt(2.0) / rms
    transform = np.array(
        [
            [scale, 0.0, -scale * mean[0]],
            [0.0, scale, -scale * mean[1]],
            [0.0, 0.0, 1.0],
        ],
        dtype=np.float64,
    )
    homogeneous = np.column_stack((points, np.ones(len(points), dtype=np.float64)))
    normalized = (transform @ homogeneous.T).T[:, :2]
    return normalized, transform


def _normalize_points_3d(points: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    mean = np.mean(points, axis=0)
    centered = points - mean
    rms = float(np.sqrt(np.mean(np.sum(centered * centered, axis=1))))
    if rms < 1.0e-9:
        raise MeasurementError("World calibration points are too close together")
    scale = np.sqrt(3.0) / rms
    transform = np.array(
        [
            [scale, 0.0, 0.0, -scale * mean[0]],
            [0.0, scale, 0.0, -scale * mean[1]],
            [0.0, 0.0, scale, -scale * mean[2]],
            [0.0, 0.0, 0.0, 1.0],
        ],
        dtype=np.float64,
    )
    homogeneous = np.column_stack((points, np.ones(len(points), dtype=np.float64)))
    normalized = (transform @ homogeneous.T).T[:, :3]
    return normalized, transform


def _solve_camera_matrix_dlt(
    *,
    world_points_cm: Iterable[Sequence[float] | np.ndarray],
    image_points: Iterable[PointLike],
) -> np.ndarray:
    world = np.asarray(
        [_as_world_point(point, "world_point_cm") for point in world_points_cm],
        dtype=np.float64,
    )
    image = np.asarray(
        [_as_point(point, "image_point") for point in image_points],
        dtype=np.float64,
    )

    if world.shape[0] != image.shape[0]:
        raise MeasurementError("World and image calibration point counts must match")
    if len(world) < 6:
        raise MeasurementError("At least six 3D calibration points are required")

    image_normalized, image_transform = _normalize_points_2d(image)
    world_normalized, world_transform = _normalize_points_3d(world)

    rows = []
    for world_point, image_point in zip(world_normalized, image_normalized):
        x, y, z = world_point
        u, v = image_point
        homogeneous_world = np.array([x, y, z, 1.0], dtype=np.float64)
        rows.append(np.concatenate((np.zeros(4), -homogeneous_world, v * homogeneous_world)))
        rows.append(np.concatenate((homogeneous_world, np.zeros(4), -u * homogeneous_world)))
    system = np.asarray(rows, dtype=np.float64)

    try:
        _, singular_values, vh = np.linalg.svd(system)
    except np.linalg.LinAlgError as exc:
        raise MeasurementError("Could not solve a camera calibration from those points") from exc

    if len(singular_values) < 12 or singular_values[-2] < 1.0e-12:
        raise MeasurementError("Calibration geometry is degenerate; use non-coplanar points")

    camera_normalized = vh[-1].reshape(3, 4)
    camera = np.linalg.inv(image_transform) @ camera_normalized @ world_transform
    norm = float(np.linalg.norm(camera))
    if norm < 1.0e-12 or not np.all(np.isfinite(camera)):
        raise MeasurementError("Could not solve a usable camera calibration from those points")
    camera /= norm
    if np.linalg.matrix_rank(camera) < 3:
        raise MeasurementError("Calibration camera matrix is degenerate")
    return camera


def _project_world_points(camera: np.ndarray, world_points_cm: np.ndarray) -> np.ndarray:
    homogeneous = np.column_stack(
        (
            world_points_cm,
            np.ones(len(world_points_cm), dtype=np.float64),
        )
    )
    projected = (camera @ homogeneous.T).T
    depth = projected[:, 2:3]
    if np.any(np.abs(depth) < 1.0e-9):
        raise MeasurementError("A calibrated point projects too close to infinity")
    return projected[:, :2] / depth


def _solve_depth_on_projected_line(
    *,
    camera: np.ndarray,
    line_origin_cm: np.ndarray,
    line_direction_cm: np.ndarray,
    image_point: PointLike,
    point_name: str,
) -> tuple[float, np.ndarray, float]:
    origin_homogeneous = np.array(
        [line_origin_cm[0], line_origin_cm[1], line_origin_cm[2], 1.0],
        dtype=np.float64,
    )
    direction_homogeneous = np.array(
        [line_direction_cm[0], line_direction_cm[1], line_direction_cm[2], 0.0],
        dtype=np.float64,
    )
    projected_origin = camera @ origin_homogeneous
    projected_direction = camera @ direction_homogeneous
    observed = _as_point(image_point, point_name)
    observed_homogeneous = np.array([observed[0], observed[1], 1.0], dtype=np.float64)

    collinearity_at_origin = np.cross(observed_homogeneous, projected_origin)
    collinearity_direction = np.cross(observed_homogeneous, projected_direction)
    denominator = float(np.dot(collinearity_direction, collinearity_direction))
    if denominator < 1.0e-12:
        raise MeasurementError(f"{point_name} projects too close to the calibrated vanishing point")

    depth_cm = -float(np.dot(collinearity_direction, collinearity_at_origin)) / denominator
    if not np.isfinite(depth_cm):
        raise MeasurementError(f"Could not solve {point_name} along the variable post")

    world_point = line_origin_cm + line_direction_cm * depth_cm
    reprojected = _project_world_points(camera, world_point.reshape(1, 3))[0]
    reprojection_error = float(np.linalg.norm(reprojected - observed))
    return depth_cm, world_point, reprojection_error


def _as_line_segments(
    segments: Iterable[Sequence[PointLike]],
    *,
    expected_count: int,
    name: str,
) -> np.ndarray:
    value = np.asarray(
        [[_as_point(point, f"{name}_point") for point in segment] for segment in segments],
        dtype=np.float64,
    )
    if value.shape != (expected_count, 2, 2):
        raise MeasurementError(f"Exactly {expected_count} {name} segments are required")
    return value


def _line_endpoint_model_points(
    *,
    top_pipe_cm: float,
    known_post_cm: float,
    top_joint_inset_cm: float,
    known_post_start_offsets_cm: Sequence[float],
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    inset = float(top_joint_inset_cm)
    top_pipe = float(top_pipe_cm)
    known_post = float(known_post_cm)
    post_offsets = np.asarray(known_post_start_offsets_cm, dtype=np.float64).reshape(-1)
    if post_offsets.shape != (3,):
        raise MeasurementError("known_post_start_offsets_cm must contain three values")

    square_side = top_pipe + 2.0 * inset
    top_segments = np.array(
        [
            [[inset, 0.0, 0.0], [square_side - inset, 0.0, 0.0]],
            [[square_side, inset, 0.0], [square_side, square_side - inset, 0.0]],
            [[square_side - inset, square_side, 0.0], [inset, square_side, 0.0]],
            [[0.0, square_side - inset, 0.0], [0.0, inset, 0.0]],
        ],
        dtype=np.float64,
    )
    known_post_corners = np.array(
        [
            [0.0, 0.0],
            [square_side, 0.0],
            [square_side, square_side],
        ],
        dtype=np.float64,
    )
    known_post_segments = []
    for (x, y), post_start_z in zip(known_post_corners, post_offsets):
        known_post_segments.append(
            [
                [x, y, post_start_z],
                [x, y, post_start_z + known_post],
            ]
        )

    variable_anchor = np.array([0.0, square_side, 0.0], dtype=np.float64)
    return top_segments, np.asarray(known_post_segments, dtype=np.float64), variable_anchor


def _fit_line_endpoint_camera(
    *,
    top_line_image_segments: np.ndarray,
    known_post_image_segments: np.ndarray,
    top_pipe_cm: float,
    known_post_cm: float,
) -> tuple[np.ndarray, float, tuple[float, float, float], float]:
    try:
        from scipy.optimize import least_squares
    except ImportError as exc:
        raise MeasurementError("Line endpoint calibration requires scipy.optimize") from exc

    calibration_image = np.vstack(
        (
            top_line_image_segments.reshape(-1, 2),
            known_post_image_segments.reshape(-1, 2),
        )
    )

    def residuals(parameters: np.ndarray) -> np.ndarray:
        inset = float(parameters[0])
        post_offsets = parameters[1:4]
        try:
            top_world, known_post_world, _ = _line_endpoint_model_points(
                top_pipe_cm=top_pipe_cm,
                known_post_cm=known_post_cm,
                top_joint_inset_cm=inset,
                known_post_start_offsets_cm=post_offsets,
            )
            calibration_world = np.vstack(
                (
                    top_world.reshape(-1, 3),
                    known_post_world.reshape(-1, 3),
                )
            )
            camera = _solve_camera_matrix_dlt(
                world_points_cm=calibration_world,
                image_points=calibration_image,
            )
            projected = _project_world_points(camera, calibration_world)
            return (projected - calibration_image).reshape(-1)
        except MeasurementError:
            return np.full(calibration_image.size, 1.0e6, dtype=np.float64)

    lower_bounds = np.array([0.0, -30.0, -30.0, -30.0], dtype=np.float64)
    upper_bounds = np.array([30.0, 30.0, 30.0, 30.0], dtype=np.float64)
    starts = [
        np.array([inset, 0.0, 0.0, 0.0], dtype=np.float64)
        for inset in (0.0, 2.5, 5.0, 8.0, 12.0)
    ]

    best_result = None
    best_cost = np.inf
    for start in starts:
        result = least_squares(
            residuals,
            start,
            bounds=(lower_bounds, upper_bounds),
            xtol=1.0e-10,
            ftol=1.0e-10,
            gtol=1.0e-10,
            max_nfev=500,
        )
        cost = float(np.dot(result.fun, result.fun))
        if result.success and cost < best_cost:
            best_result = result
            best_cost = cost

    if best_result is None:
        raise MeasurementError("Could not fit the line endpoint calibration")

    inset = float(best_result.x[0])
    post_offsets = tuple(float(value) for value in best_result.x[1:4])
    top_world, known_post_world, _ = _line_endpoint_model_points(
        top_pipe_cm=top_pipe_cm,
        known_post_cm=known_post_cm,
        top_joint_inset_cm=inset,
        known_post_start_offsets_cm=post_offsets,
    )
    calibration_world = np.vstack(
        (
            top_world.reshape(-1, 3),
            known_post_world.reshape(-1, 3),
        )
    )
    camera = _solve_camera_matrix_dlt(
        world_points_cm=calibration_world,
        image_points=calibration_image,
    )
    projected = _project_world_points(camera, calibration_world)
    errors = np.linalg.norm(projected - calibration_image, axis=1)
    rmse = float(np.sqrt(np.mean(errors * errors)))
    return camera, inset, post_offsets, rmse


def measure_affine_variable_length(
    *,
    parallel_reference_start: PointLike,
    parallel_reference_end: PointLike,
    perpendicular_reference_start: PointLike,
    perpendicular_reference_end: PointLike,
    variable_start: PointLike,
    variable_end: PointLike,
    parallel_reference_cm: float = 15.0,
    perpendicular_reference_cm: float = 55.0,
) -> AffineMeasurementResult:
    """Measure the variable segment from two perpendicular in-plane references.

    This treats the selected frame as an affine view of the prop plane. In
    practical terms, that is the right first-pass model when the camera is
    mostly square to the plane or the prop occupies a modest part of the frame.
    It is not a substitute for a projective calibration when perspective is
    visible.
    """

    if parallel_reference_cm <= 0.0:
        raise MeasurementError("parallel_reference_cm must be positive")
    if perpendicular_reference_cm <= 0.0:
        raise MeasurementError("perpendicular_reference_cm must be positive")

    parallel_start = _as_point(parallel_reference_start, "parallel_reference_start")
    parallel_end = _as_point(parallel_reference_end, "parallel_reference_end")
    perpendicular_start = _as_point(perpendicular_reference_start, "perpendicular_reference_start")
    perpendicular_end = _as_point(perpendicular_reference_end, "perpendicular_reference_end")
    var_start = _as_point(variable_start, "variable_start")
    var_end = _as_point(variable_end, "variable_end")

    parallel_image = parallel_end - parallel_start
    perpendicular_image = perpendicular_end - perpendicular_start
    variable_image = var_end - var_start

    parallel_px = _vector_length(parallel_image)
    perpendicular_px = _vector_length(perpendicular_image)
    variable_px = _vector_length(variable_image)
    if parallel_px < 1.0:
        raise MeasurementError("Parallel reference points are too close together")
    if perpendicular_px < 1.0:
        raise MeasurementError("Perpendicular reference points are too close together")
    if variable_px < 1.0:
        raise MeasurementError("Variable endpoints are too close together")

    image_basis = np.column_stack((parallel_image, perpendicular_image))
    determinant = float(np.linalg.det(image_basis))
    basis_scale = max(parallel_px * perpendicular_px, 1.0)
    if abs(determinant) / basis_scale < 1.0e-4:
        raise MeasurementError("Reference segments are too close to parallel in the image")

    image_to_world = np.array(
        [
            [parallel_reference_cm, 0.0],
            [0.0, perpendicular_reference_cm],
        ],
        dtype=np.float64,
    ) @ np.linalg.inv(image_basis)
    world_delta = image_to_world @ variable_image

    parallel_component = float(world_delta[0])
    perpendicular_component = float(world_delta[1])
    length_cm = _vector_length(world_delta)
    parallel_only_length_cm = variable_px * parallel_reference_cm / parallel_px
    alignment_error = float(
        np.degrees(np.arctan2(abs(perpendicular_component), max(abs(parallel_component), 1.0e-9)))
    )

    return AffineMeasurementResult(
        length_cm=length_cm,
        parallel_only_length_cm=parallel_only_length_cm,
        parallel_component_cm=parallel_component,
        perpendicular_component_cm=perpendicular_component,
        alignment_error_degrees=alignment_error,
        parallel_reference_px=parallel_px,
        perpendicular_reference_px=perpendicular_px,
        variable_px=variable_px,
        world_delta_cm=(parallel_component, perpendicular_component),
    )


def measure_spatial_iceberg_variable_length(
    *,
    square_corner_image_points: Iterable[PointLike],
    known_post_end_image_points: Iterable[PointLike],
    variable_start: PointLike,
    variable_end: PointLike,
    square_side_cm: float = 55.0,
    known_post_cm: float = 15.0,
) -> SpatialIcebergMeasurementResult:
    """Measure the variable iceberg post from the full 3D prop geometry.

    Click the square top corners A, B, C, D around the 55 cm square, choosing D
    as the corner with the variable post. Then click the 15 cm post endpoints
    E, F, G below A, B, C, followed by the two measured variable endpoints H
    and I along the post below D.
    """

    if square_side_cm <= 0.0:
        raise MeasurementError("square_side_cm must be positive")
    if known_post_cm <= 0.0:
        raise MeasurementError("known_post_cm must be positive")

    square_image = np.asarray(
        [_as_point(point, "square_corner_image_point") for point in square_corner_image_points],
        dtype=np.float64,
    )
    known_post_image = np.asarray(
        [_as_point(point, "known_post_end_image_point") for point in known_post_end_image_points],
        dtype=np.float64,
    )
    variable_start_image = _as_point(variable_start, "variable_start")
    variable_end_image = _as_point(variable_end, "variable_end")

    if square_image.shape != (4, 2):
        raise MeasurementError("Exactly four square corner image points are required")
    if known_post_image.shape != (3, 2):
        raise MeasurementError("Exactly three known post endpoint image points are required")

    side = float(square_side_cm)
    post = float(known_post_cm)
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
            [0.0, 0.0, post],
            [side, 0.0, post],
            [side, side, post],
        ],
        dtype=np.float64,
    )
    calibration_world = np.vstack((square_world, known_post_world))
    calibration_image = np.vstack((square_image, known_post_image))

    camera = _solve_camera_matrix_dlt(
        world_points_cm=calibration_world,
        image_points=calibration_image,
    )

    reprojected = _project_world_points(camera, calibration_world)
    reprojection_errors = np.linalg.norm(reprojected - calibration_image, axis=1)
    reprojection_rmse = float(np.sqrt(np.mean(reprojection_errors * reprojection_errors)))

    variable_anchor = square_world[3]
    variable_direction = np.array([0.0, 0.0, 1.0], dtype=np.float64)
    start_depth, variable_start_world, start_error = _solve_depth_on_projected_line(
        camera=camera,
        line_origin_cm=variable_anchor,
        line_direction_cm=variable_direction,
        image_point=variable_start_image,
        point_name="variable_start",
    )
    end_depth, variable_end_world, end_error = _solve_depth_on_projected_line(
        camera=camera,
        line_origin_cm=variable_anchor,
        line_direction_cm=variable_direction,
        image_point=variable_end_image,
        point_name="variable_end",
    )
    length_cm = abs(end_depth - start_depth)
    variable_error = float(np.sqrt((start_error * start_error + end_error * end_error) / 2.0))

    return SpatialIcebergMeasurementResult(
        length_cm=length_cm,
        variable_start_cm=(
            float(variable_start_world[0]),
            float(variable_start_world[1]),
            float(variable_start_world[2]),
        ),
        variable_end_cm=(
            float(variable_end_world[0]),
            float(variable_end_world[1]),
            float(variable_end_world[2]),
        ),
        reprojection_rmse_px=reprojection_rmse,
        variable_reprojection_error_px=variable_error,
        camera_matrix_image_from_world=camera,
    )


def measure_line_endpoint_iceberg_variable_length(
    *,
    top_line_image_segments: Iterable[Sequence[PointLike]],
    known_post_image_segments: Iterable[Sequence[PointLike]],
    variable_image_segment: Sequence[PointLike],
    top_pipe_cm: float = 55.0,
    known_post_cm: float = 15.0,
) -> SpatialIcebergMeasurementResult:
    """Measure the variable post after clicking both endpoints of every pipe.

    The top square is clicked as four 55 cm pipe segments in order around the
    square: A side, B side, C side, D side. The three known posts are clicked
    from upper endpoint to lower endpoint as E, F, G. The variable post is
    clicked from upper endpoint to lower endpoint as H.

    A small connector inset for the square and the start offsets of the known
    posts are fitted from the clicked endpoints, so the calibration uses the
    pipe lengths without requiring those endpoints to be shared corner points.
    """

    if top_pipe_cm <= 0.0:
        raise MeasurementError("top_pipe_cm must be positive")
    if known_post_cm <= 0.0:
        raise MeasurementError("known_post_cm must be positive")

    top_segments = _as_line_segments(
        top_line_image_segments,
        expected_count=4,
        name="top_line_image",
    )
    known_post_segments = _as_line_segments(
        known_post_image_segments,
        expected_count=3,
        name="known_post_image",
    )
    variable_segment = _as_line_segments(
        [variable_image_segment],
        expected_count=1,
        name="variable_image",
    )[0]

    camera, inset, post_offsets, reprojection_rmse = _fit_line_endpoint_camera(
        top_line_image_segments=top_segments,
        known_post_image_segments=known_post_segments,
        top_pipe_cm=float(top_pipe_cm),
        known_post_cm=float(known_post_cm),
    )
    _, _, variable_anchor = _line_endpoint_model_points(
        top_pipe_cm=float(top_pipe_cm),
        known_post_cm=float(known_post_cm),
        top_joint_inset_cm=inset,
        known_post_start_offsets_cm=post_offsets,
    )
    variable_direction = np.array([0.0, 0.0, 1.0], dtype=np.float64)
    start_depth, variable_start_world, start_error = _solve_depth_on_projected_line(
        camera=camera,
        line_origin_cm=variable_anchor,
        line_direction_cm=variable_direction,
        image_point=variable_segment[0],
        point_name="variable_start",
    )
    end_depth, variable_end_world, end_error = _solve_depth_on_projected_line(
        camera=camera,
        line_origin_cm=variable_anchor,
        line_direction_cm=variable_direction,
        image_point=variable_segment[1],
        point_name="variable_end",
    )
    length_cm = abs(end_depth - start_depth)
    variable_error = float(np.sqrt((start_error * start_error + end_error * end_error) / 2.0))

    return SpatialIcebergMeasurementResult(
        length_cm=length_cm,
        variable_start_cm=(
            float(variable_start_world[0]),
            float(variable_start_world[1]),
            float(variable_start_world[2]),
        ),
        variable_end_cm=(
            float(variable_end_world[0]),
            float(variable_end_world[1]),
            float(variable_end_world[2]),
        ),
        reprojection_rmse_px=reprojection_rmse,
        variable_reprojection_error_px=variable_error,
        camera_matrix_image_from_world=camera,
        top_joint_inset_cm=float(inset),
        known_post_start_offsets_cm=post_offsets,
    )
