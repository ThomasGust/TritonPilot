import json
import io
from unittest.mock import patch

import numpy as np
import pytest

from video.frame_rotation import normalize_rotation_deg, rotate_frame
from video.cam import RemoteCameraManager

REAL_OPEN = open


def test_rotate_frame_90_180_270():
    frame = np.array(
        [
            [[1, 0, 0], [2, 0, 0], [3, 0, 0]],
            [[4, 0, 0], [5, 0, 0], [6, 0, 0]],
        ],
        dtype=np.uint8,
    )

    rot90 = rotate_frame(frame, 90)
    rot180 = rotate_frame(frame, 180)
    rot270 = rotate_frame(frame, 270)

    assert rot90.shape == (3, 2, 3)
    assert rot180.shape == (2, 3, 3)
    assert rot270.shape == (3, 2, 3)

    assert rot90[:, :, 0].tolist() == [[3, 6], [2, 5], [1, 4]]
    assert rot180[:, :, 0].tolist() == [[6, 5, 4], [3, 2, 1]]
    assert rot270[:, :, 0].tolist() == [[4, 1], [5, 2], [6, 3]]


def test_normalize_rotation_defaults_to_zero():
    assert normalize_rotation_deg(None) == 0
    assert normalize_rotation_deg("") == 0
    assert normalize_rotation_deg(0) == 0


def test_remote_camera_manager_normalizes_rotation_from_config():
    cfg = json.dumps(
        {
            "streams": [
                {
                    "name": "Test Camera",
                    "device": "/dev/video0",
                    "width": 640,
                    "height": 480,
                    "fps": 30,
                    "rotation_deg": 270,
                    "enabled": True,
                }
            ]
        }
    )

    with patch(
        "builtins.open",
        side_effect=lambda path, mode="r", *args, **kwargs: (
            io.StringIO(cfg) if path == "valid_streams.json" else REAL_OPEN(path, mode, *args, **kwargs)
        ),
    ):
        mgr = RemoteCameraManager("valid_streams.json")

    assert mgr.stream_defs["Test Camera"]["rotation_deg"] == 270


def test_remote_camera_manager_rejects_invalid_rotation():
    cfg = json.dumps(
        {
            "streams": [
                {
                    "name": "Bad Camera",
                    "device": "/dev/video0",
                    "width": 640,
                    "height": 480,
                    "fps": 30,
                    "rotation_deg": 45,
                    "enabled": True,
                }
            ]
        }
    )

    with patch(
        "builtins.open",
        side_effect=lambda path, mode="r", *args, **kwargs: (
            io.StringIO(cfg) if path == "invalid_streams.json" else REAL_OPEN(path, mode, *args, **kwargs)
        ),
    ):
        with pytest.raises(ValueError, match="Invalid stream rotation"):
            RemoteCameraManager("invalid_streams.json")
