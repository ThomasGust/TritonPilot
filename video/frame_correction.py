"""Out-of-water lens correction for DWE ExploreHD cameras.

This module converts the bench image from a wide fisheye-like projection into a
more rectilinear view so it better resembles the camera once submerged.

Instead of trying to "undistort" with a rough pinhole model, we explicitly
reproject from an assumed fisheye lens into a perspective camera. That makes
the geometry much easier to reason about and tune:

- `air_hfov_deg` describes the captured in-air lens width.
- `target_hfov_deg` describes the desired corrected horizontal field of view.
- `zoom` is a small trim on top of that target projection.

The remap is cached per frame size, so the runtime cost remains a single
`cv2.remap` call per frame.
"""
from __future__ import annotations

import cv2
import numpy as np


def _focal_px(size_px: float, fov_deg: float) -> float:
    """Convert a rectilinear field of view to focal length in pixels."""
    fov_deg = max(1.0, min(179.0, float(fov_deg)))
    return 0.5 * float(size_px) / np.tan(np.deg2rad(fov_deg * 0.5))


class WaterCorrection:
    """Pre-computed remap that reprojects ExploreHD bench frames."""

    def __init__(
        self,
        *,
        zoom: float = 1.0,
        k1: float = 0.0,
        k2: float = 0.0,
        k3: float = 0.0,
        air_hfov_deg: float = 138.0,
        target_hfov_deg: float = 96.0,
    ) -> None:
        self._w: int = 0
        self._h: int = 0
        self.zoom = max(0.5, float(zoom))
        self.k1 = float(k1)
        self.k2 = float(k2)
        self.k3 = float(k3)
        self.air_hfov_deg = float(air_hfov_deg)
        self.target_hfov_deg = float(target_hfov_deg)
        self._map_x: np.ndarray | None = None
        self._map_y: np.ndarray | None = None

    def _build(self) -> None:
        w, h = self._w, self._h
        cx = (w - 1) * 0.5
        cy = (h - 1) * 0.5

        fx_out = _focal_px(w, self.target_hfov_deg) * self.zoom
        fy_out = fx_out

        max_theta = np.deg2rad(max(1.0, min(179.0, self.air_hfov_deg)) * 0.5)
        fisheye_f = (w * 0.5) / max(max_theta, 1e-6)

        yy, xx = np.indices((h, w), dtype=np.float32)
        x = (xx - cx) / fx_out
        y = (yy - cy) / fy_out

        r = np.sqrt(x * x + y * y)
        theta = np.arctan(r)
        phi = np.arctan2(y, x)

        theta2 = theta * theta
        theta_d = theta * (
            1.0
            + self.k1 * theta2
            + self.k2 * theta2 * theta2
            + self.k3 * theta2 * theta2 * theta2
        )
        src_r = fisheye_f * theta_d

        self._map_x = (cx + src_r * np.cos(phi)).astype(np.float32)
        self._map_y = (cy + src_r * np.sin(phi)).astype(np.float32)

    def apply(self, frame: np.ndarray) -> np.ndarray:
        """Return a corrected copy of *frame*."""
        h, w = frame.shape[:2]
        if w != self._w or h != self._h:
            self._w, self._h = w, h
            self._build()
        return cv2.remap(
            frame,
            self._map_x,
            self._map_y,
            cv2.INTER_LINEAR,
            borderMode=cv2.BORDER_REPLICATE,
        )
