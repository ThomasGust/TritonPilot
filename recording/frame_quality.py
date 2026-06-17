"""Frame-quality checks shared by capture and recording paths."""

from __future__ import annotations

import numpy as np


def looks_like_green_startup_artifact(frame: np.ndarray) -> bool:
    """Detect the one-color H.264 startup filler frames seen before keyframe lock."""

    try:
        arr = np.asarray(frame)
        if arr.ndim != 3 or arr.shape[2] < 3:
            return False
        h, w = int(arr.shape[0]), int(arr.shape[1])
        sample = arr[:: max(1, h // 120), :: max(1, w // 160), :3].astype(np.float32, copy=False)
        flat = sample.reshape(-1, 3)
        mean_b, mean_g, mean_r = [float(v) for v in flat.mean(axis=0)]
        std_mean = float(flat.std(axis=0).mean())
        if mean_g < 35.0 or mean_b > 16.0 or mean_r > 16.0:
            return False
        if mean_g < (max(mean_b, mean_r) * 4.0 + 18.0):
            return False
        b = flat[:, 0]
        g = flat[:, 1]
        r = flat[:, 2]
        greenish = ((g > r * 1.35 + 12.0) & (g > b * 1.35 + 12.0) & (g > 45.0)).mean()
        return bool(greenish > 0.90 and std_mean < 18.0)
    except Exception:
        return False


def looks_like_green_channel_collapse_artifact(frame: np.ndarray) -> bool:
    """Detect H.264 loss artifacts where almost all luminance lands in green."""

    try:
        arr = np.asarray(frame)
        if arr.ndim != 3 or arr.shape[2] < 3:
            return False
        h, w = int(arr.shape[0]), int(arr.shape[1])
        sample = arr[:: max(1, h // 120), :: max(1, w // 160), :3].astype(np.float32, copy=False)
        flat = sample.reshape(-1, 3)
        mean_b, mean_g, mean_r = [float(v) for v in flat.mean(axis=0)]
        if mean_g < 45.0:
            return False
        max_non_green = max(mean_b, mean_r)
        if max_non_green > 35.0 or (mean_g - max_non_green) < 40.0:
            return False
        b = flat[:, 0]
        g = flat[:, 1]
        r = flat[:, 2]
        greenish = ((g > r * 1.35 + 12.0) & (g > b * 1.35 + 12.0) & (g > 45.0)).mean()
        dead_rb = ((r < 24.0) & (b < 24.0) & (g > 45.0)).mean()
        return bool(greenish > 0.92 and dead_rb > 0.80)
    except Exception:
        return False


def looks_like_blank_startup_artifact(frame: np.ndarray) -> bool:
    """Detect near-black/blank frames that are not useful camera captures."""

    try:
        arr = np.asarray(frame)
        if arr.ndim != 3 or arr.shape[2] < 3:
            return False
        h, w = int(arr.shape[0]), int(arr.shape[1])
        sample = arr[:: max(1, h // 120), :: max(1, w // 160), :3].astype(np.float32, copy=False)
        flat = sample.reshape(-1, 3)
        channel_means = flat.mean(axis=0)
        channel_stds = flat.std(axis=0)
        overall_mean = float(channel_means.mean())
        overall_max = float(flat.max())
        std_mean = float(channel_stds.mean())
        # Keep this conservative: a genuinely dark underwater frame can still
        # contain texture, highlights, or noise. The startup failure is almost
        # completely flat and close to zero.
        return bool(overall_mean < 8.0 and overall_max < 24.0 and std_mean < 4.0)
    except Exception:
        return False


def capture_frame_rejection_reason(frame: np.ndarray) -> str | None:
    if looks_like_green_startup_artifact(frame):
        return "green_startup_artifact"
    if looks_like_green_channel_collapse_artifact(frame):
        return "green_channel_collapse_artifact"
    if looks_like_blank_startup_artifact(frame):
        return "blank_startup_artifact"
    return None


def is_usable_capture_frame(frame: np.ndarray) -> bool:
    """Return False for decoded frames that are not useful media captures."""

    return capture_frame_rejection_reason(frame) is None
