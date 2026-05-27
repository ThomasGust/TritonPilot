"""Near-live stereo rectification and disparity preview generation."""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

from stereo.calibration import StereoCalibration


@dataclass(frozen=True)
class DisparityPreview:
    """A colorized disparity frame plus lightweight diagnostics."""

    preview_bgr: np.ndarray
    process_size: tuple[int, int]
    valid_fraction: float
    disparity_min: float
    disparity_max: float


def _scaled_camera_matrix(
    camera_matrix: np.ndarray,
    *,
    from_size: tuple[int, int],
    to_size: tuple[int, int],
) -> np.ndarray:
    from_w, from_h = from_size
    to_w, to_h = to_size
    if from_w <= 0 or from_h <= 0 or to_w <= 0 or to_h <= 0:
        raise RuntimeError("Camera matrix scaling requires positive image sizes")
    sx = float(to_w) / float(from_w)
    sy = float(to_h) / float(from_h)
    scaled = np.asarray(camera_matrix, dtype=np.float64).copy()
    scaled[0, 0] *= sx
    scaled[0, 1] *= sx
    scaled[0, 2] *= sx
    scaled[1, 0] *= sy
    scaled[1, 1] *= sy
    scaled[1, 2] *= sy
    return scaled


def _fit_size(source_size: tuple[int, int], max_width: int) -> tuple[int, int]:
    width, height = source_size
    width = max(1, int(width))
    height = max(1, int(height))
    max_width = max(160, int(max_width))
    if width <= max_width:
        return width, height
    scale = float(max_width) / float(width)
    return max(1, int(round(width * scale))), max(1, int(round(height * scale)))


def _num_disparities(width: int) -> int:
    raw = max(16, min(256, int(width) // 4))
    return max(16, int(raw / 16) * 16)


class StereoDisparityProcessor:
    """Rectify a stereo pair and compute a colorized disparity preview."""

    def __init__(
        self,
        calibration: StereoCalibration,
        *,
        source_size: tuple[int, int],
        max_width: int = 960,
    ):
        self.calibration = calibration
        self.source_size = (int(source_size[0]), int(source_size[1]))
        self.process_size = _fit_size(self.source_size, int(max_width))
        self._build_maps()
        self._build_matcher()

    def _build_maps(self) -> None:
        size = self.process_size
        k_left = _scaled_camera_matrix(
            self.calibration.left_camera_matrix,
            from_size=self.calibration.image_size,
            to_size=size,
        )
        k_right = _scaled_camera_matrix(
            self.calibration.right_camera_matrix,
            from_size=self.calibration.image_size,
            to_size=size,
        )
        d_left = self.calibration.left_dist_coeffs
        d_right = self.calibration.right_dist_coeffs
        rotation = self.calibration.rotation.astype(np.float64)
        translation = self.calibration.translation_mm.astype(np.float64).reshape(3, 1)

        r1, r2, p1, p2, _q, _roi1, _roi2 = cv2.stereoRectify(
            k_left,
            d_left,
            k_right,
            d_right,
            size,
            rotation,
            translation,
            flags=cv2.CALIB_ZERO_DISPARITY,
            alpha=0,
        )
        self._left_map = cv2.initUndistortRectifyMap(k_left, d_left, r1, p1, size, cv2.CV_16SC2)
        self._right_map = cv2.initUndistortRectifyMap(k_right, d_right, r2, p2, size, cv2.CV_16SC2)

    def _build_matcher(self) -> None:
        block_size = 5
        num_disparities = _num_disparities(self.process_size[0])
        self._min_disparity = 0
        self._matcher = cv2.StereoSGBM_create(
            minDisparity=self._min_disparity,
            numDisparities=num_disparities,
            blockSize=block_size,
            P1=8 * block_size * block_size,
            P2=32 * block_size * block_size,
            disp12MaxDiff=1,
            uniquenessRatio=8,
            speckleWindowSize=80,
            speckleRange=2,
            preFilterCap=31,
            mode=cv2.STEREO_SGBM_MODE_SGBM_3WAY,
        )

    def _resize(self, frame_bgr: np.ndarray) -> np.ndarray:
        width, height = self.process_size
        if frame_bgr.shape[1] == width and frame_bgr.shape[0] == height:
            return np.ascontiguousarray(frame_bgr)
        return cv2.resize(frame_bgr, (width, height), interpolation=cv2.INTER_AREA)

    def compute(self, left_bgr: np.ndarray, right_bgr: np.ndarray) -> DisparityPreview:
        left = self._resize(left_bgr)
        right = self._resize(right_bgr)
        left_rect = cv2.remap(left, self._left_map[0], self._left_map[1], cv2.INTER_LINEAR)
        right_rect = cv2.remap(right, self._right_map[0], self._right_map[1], cv2.INTER_LINEAR)
        left_gray = cv2.cvtColor(left_rect, cv2.COLOR_BGR2GRAY)
        right_gray = cv2.cvtColor(right_rect, cv2.COLOR_BGR2GRAY)

        raw = self._matcher.compute(left_gray, right_gray).astype(np.float32) / 16.0
        valid = np.isfinite(raw) & (raw > float(self._min_disparity))
        valid_count = int(np.count_nonzero(valid))
        if valid_count <= 0:
            normalized = np.zeros(raw.shape, dtype=np.uint8)
            disp_min = 0.0
            disp_max = 0.0
            valid_fraction = 0.0
        else:
            values = raw[valid]
            disp_min = float(np.percentile(values, 2.0))
            disp_max = float(np.percentile(values, 98.0))
            if disp_max <= disp_min:
                disp_max = disp_min + 1.0
            normalized = np.clip((raw - disp_min) * (255.0 / (disp_max - disp_min)), 0, 255).astype(np.uint8)
            normalized[~valid] = 0
            valid_fraction = float(valid_count) / float(raw.size)

        color_map = getattr(cv2, "COLORMAP_TURBO", cv2.COLORMAP_JET)
        preview = cv2.applyColorMap(normalized, color_map)
        preview[~valid] = (0, 0, 0)
        return DisparityPreview(
            preview_bgr=preview,
            process_size=self.process_size,
            valid_fraction=valid_fraction,
            disparity_min=disp_min,
            disparity_max=disp_max,
        )
