from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

import cv2
import numpy as np


PointLike = Sequence[float] | np.ndarray


class MeasurementError(ValueError):
    """Raised when clicked planar geometry cannot support a measurement."""


@dataclass(frozen=True)
class PlanarHeightMeasurementResult:
    height_cm: float
    plane_width_cm: float
    plane_height_cm: float
    reference_rmse_cm: float
    reference_estimates_cm: tuple[float, ...]
    height_start_plane: tuple[float, float]
    height_end_plane: tuple[float, float]
    homography_image_to_plane: np.ndarray
    scale_metric_cm2: tuple[float, float]


@dataclass(frozen=True)
class PlanarSegmentMeasurementResult:
    length_cm: float
    plane_width_cm: float
    plane_height_cm: float
    reference_rmse_cm: float
    reference_estimates_cm: tuple[float, ...]
    segment_start_plane: tuple[float, float]
    segment_end_plane: tuple[float, float]
    homography_image_to_plane: np.ndarray
    scale_metric_cm2: tuple[float, float]


def _as_point(point: PointLike, name: str) -> np.ndarray:
    value = np.asarray(point, dtype=np.float64).reshape(-1)
    if value.shape != (2,):
        raise MeasurementError(f"{name} must be a 2D point")
    if not np.all(np.isfinite(value)):
        raise MeasurementError(f"{name} must contain finite coordinates")
    return value


def _as_segments(segments: Iterable[Sequence[PointLike]], name: str) -> np.ndarray:
    value = np.asarray(
        [[_as_point(point, f"{name}_point") for point in segment] for segment in segments],
        dtype=np.float64,
    )
    if value.ndim != 3 or value.shape[1:] != (2, 2):
        raise MeasurementError(f"{name} must be a list of 2-point segments")
    return value


def _transform_points(points: np.ndarray, homography: np.ndarray) -> np.ndarray:
    transformed = cv2.perspectiveTransform(points.reshape(1, -1, 2).astype(np.float32), homography)[0]
    return transformed.astype(np.float64)


def _fit_rectangle_scales(
    plane_segments: np.ndarray,
    lengths_cm: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, float]:
    deltas = plane_segments[:, 1, :] - plane_segments[:, 0, :]
    rows = np.column_stack((deltas[:, 0] * deltas[:, 0], deltas[:, 1] * deltas[:, 1]))
    targets = lengths_cm * lengths_cm

    segment_units = np.linalg.norm(deltas, axis=1)
    if np.any(segment_units < 1.0e-7):
        raise MeasurementError("Reference segment endpoints are too close together")
    if np.linalg.matrix_rank(rows, tol=1.0e-9) < 2:
        raise MeasurementError("Reference segments must constrain both rectangle axes")

    metric, *_ = np.linalg.lstsq(rows, targets, rcond=None)
    if not np.all(np.isfinite(metric)) or np.any(metric <= 0.0):
        raise MeasurementError("Reference lengths produced an invalid plane scale")

    estimates = np.sqrt(np.maximum(rows @ metric, 0.0))
    rmse = float(np.sqrt(np.mean((estimates - lengths_cm) ** 2)))
    return metric.astype(np.float64), estimates.astype(np.float64), rmse


def measure_planar_height(
    *,
    rectangle_image_points: Iterable[PointLike],
    reference_image_segments: Iterable[Sequence[PointLike]],
    reference_lengths_cm: Iterable[float],
    height_start: PointLike,
    height_end: PointLike,
) -> PlanarHeightMeasurementResult:
    """Measure an in-plane height after rectifying a clicked rectangle.

    The four rectangle corners are clicked in order around the rectangle. The
    rectangle's real dimensions are not required; they are inferred from known
    in-plane reference segments. This assumes the clicked rectangle is a true
    geometric rectangle on the same plane as the references and measured height.
    """

    rectangle_image = np.asarray(
        [_as_point(point, "rectangle_image_point") for point in rectangle_image_points],
        dtype=np.float64,
    )
    if rectangle_image.shape != (4, 2):
        raise MeasurementError("Exactly four rectangle corner points are required")

    reference_segments = _as_segments(reference_image_segments, "reference_image_segment")
    lengths_cm = np.asarray([float(length) for length in reference_lengths_cm], dtype=np.float64)
    if len(reference_segments) != len(lengths_cm):
        raise MeasurementError("Reference segment and length counts must match")
    if len(reference_segments) < 2:
        raise MeasurementError("At least two reference segments are required")
    if not np.all(np.isfinite(lengths_cm)) or np.any(lengths_cm <= 0.0):
        raise MeasurementError("Reference lengths must be positive finite values")

    unit_rectangle = np.array(
        [
            [0.0, 0.0],
            [1.0, 0.0],
            [1.0, 1.0],
            [0.0, 1.0],
        ],
        dtype=np.float64,
    )
    homography, _ = cv2.findHomography(rectangle_image.astype(np.float32), unit_rectangle.astype(np.float32), method=0)
    if homography is None or not np.all(np.isfinite(homography)):
        raise MeasurementError("Could not solve a homography from those rectangle corners")

    flat_reference_points = reference_segments.reshape(-1, 2)
    plane_reference_segments = _transform_points(flat_reference_points, homography).reshape(-1, 2, 2)

    height_points = np.asarray(
        [_as_point(height_start, "height_start"), _as_point(height_end, "height_end")],
        dtype=np.float64,
    )
    height_plane = _transform_points(height_points, homography)
    return measure_planar_height_from_plane(
        reference_plane_segments=plane_reference_segments,
        reference_lengths_cm=lengths_cm,
        height_start_plane=height_plane[0],
        height_end_plane=height_plane[1],
        plane_size_units=(1.0, 1.0),
        homography_image_to_plane=homography,
    )


def measure_planar_height_from_plane(
    *,
    reference_plane_segments: Iterable[Sequence[PointLike]],
    reference_lengths_cm: Iterable[float],
    height_start_plane: PointLike,
    height_end_plane: PointLike,
    plane_size_units: PointLike = (1.0, 1.0),
    homography_image_to_plane: np.ndarray | None = None,
) -> PlanarHeightMeasurementResult:
    """Measure height from already-rectified plane coordinates."""

    result = measure_planar_segment_from_plane(
        reference_plane_segments=reference_plane_segments,
        reference_lengths_cm=reference_lengths_cm,
        segment_start_plane=height_start_plane,
        segment_end_plane=height_end_plane,
        plane_size_units=plane_size_units,
        homography_image_to_plane=homography_image_to_plane,
    )
    return PlanarHeightMeasurementResult(
        height_cm=result.length_cm,
        plane_width_cm=result.plane_width_cm,
        plane_height_cm=result.plane_height_cm,
        reference_rmse_cm=result.reference_rmse_cm,
        reference_estimates_cm=result.reference_estimates_cm,
        height_start_plane=result.segment_start_plane,
        height_end_plane=result.segment_end_plane,
        homography_image_to_plane=result.homography_image_to_plane,
        scale_metric_cm2=result.scale_metric_cm2,
    )


def measure_planar_segment_from_plane(
    *,
    reference_plane_segments: Iterable[Sequence[PointLike]],
    reference_lengths_cm: Iterable[float],
    segment_start_plane: PointLike,
    segment_end_plane: PointLike,
    plane_size_units: PointLike = (1.0, 1.0),
    homography_image_to_plane: np.ndarray | None = None,
) -> PlanarSegmentMeasurementResult:
    """Measure a segment from already-rectified plane coordinates."""

    plane_reference_segments = _as_segments(reference_plane_segments, "reference_plane_segment")
    lengths_cm = np.asarray([float(length) for length in reference_lengths_cm], dtype=np.float64)
    if len(plane_reference_segments) != len(lengths_cm):
        raise MeasurementError("Reference segment and length counts must match")
    if len(plane_reference_segments) < 2:
        raise MeasurementError("At least two reference segments are required")
    if not np.all(np.isfinite(lengths_cm)) or np.any(lengths_cm <= 0.0):
        raise MeasurementError("Reference lengths must be positive finite values")

    plane_size = _as_point(plane_size_units, "plane_size_units")
    if np.any(plane_size <= 0.0):
        raise MeasurementError("plane_size_units must be positive")

    metric, reference_estimates, reference_rmse = _fit_rectangle_scales(
        plane_reference_segments,
        lengths_cm,
    )

    height_plane = np.asarray(
        [
            _as_point(segment_start_plane, "segment_start_plane"),
            _as_point(segment_end_plane, "segment_end_plane"),
        ],
        dtype=np.float64,
    )
    segment_delta = height_plane[1] - height_plane[0]
    length_cm = float(np.sqrt(max(metric[0] * segment_delta[0] ** 2 + metric[1] * segment_delta[1] ** 2, 0.0)))
    if homography_image_to_plane is None:
        homography_image_to_plane = np.eye(3, dtype=np.float64)

    return PlanarSegmentMeasurementResult(
        length_cm=length_cm,
        plane_width_cm=float(np.sqrt(metric[0]) * plane_size[0]),
        plane_height_cm=float(np.sqrt(metric[1]) * plane_size[1]),
        reference_rmse_cm=reference_rmse,
        reference_estimates_cm=tuple(float(value) for value in reference_estimates),
        segment_start_plane=(float(height_plane[0, 0]), float(height_plane[0, 1])),
        segment_end_plane=(float(height_plane[1, 0]), float(height_plane[1, 1])),
        homography_image_to_plane=np.asarray(homography_image_to_plane, dtype=np.float64),
        scale_metric_cm2=(float(metric[0]), float(metric[1])),
    )
