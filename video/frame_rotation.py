from __future__ import annotations

import numpy as np


VALID_ROTATIONS_DEG = (0, 90, 180, 270)


def normalize_rotation_deg(value: object) -> int:
    """Return a canonical quarter-turn rotation in degrees."""
    if value is None or value == "":
        return 0

    try:
        deg = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"Invalid stream rotation {value!r}; expected one of {VALID_ROTATIONS_DEG}"
        ) from exc

    if deg not in VALID_ROTATIONS_DEG:
        raise ValueError(
            f"Invalid stream rotation {deg!r}; expected one of {VALID_ROTATIONS_DEG}"
        )
    return deg


def rotate_frame(frame: np.ndarray, rotation_deg: int) -> np.ndarray:
    """Rotate a BGR frame by a multiple of 90 degrees."""
    deg = normalize_rotation_deg(rotation_deg)
    if deg == 0:
        return frame

    turns = deg // 90
    return np.ascontiguousarray(np.rot90(frame, k=turns))
