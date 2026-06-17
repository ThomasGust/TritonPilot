import json
import threading
import time
from types import SimpleNamespace

import numpy as np

from video import cam as cam_module


class _FakeRov:
    pass


def _write_streams_config(path):
    path.write_text(
        json.dumps(
            {
                "streams": [
                    {
                        "name": "Front",
                        "device": "/dev/video0",
                        "width": 2,
                        "height": 1,
                        "fps": 30,
                        "video_format": "h264",
                        "port": 5000,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )


def test_close_async_blocks_same_stream_reopen_until_release_finishes(monkeypatch, tmp_path):
    release_started = threading.Event()
    release_allowed = threading.Event()
    opened = []

    class _FakeCamera:
        def __init__(self, **kwargs):
            self.name = kwargs["name"]
            opened.append(self)

        def release(self):
            release_started.set()
            release_allowed.wait(timeout=1.0)

    monkeypatch.setattr(cam_module, "ROVStreams", lambda endpoint: _FakeRov())
    monkeypatch.setattr(cam_module, "RemoteCv2Camera", _FakeCamera)

    cfg_path = tmp_path / "streams.json"
    _write_streams_config(cfg_path)
    manager = cam_module.RemoteCameraManager(str(cfg_path))

    first = manager.open("Front")
    assert first is opened[0]
    assert manager.close_async("Front") is True
    assert release_started.wait(timeout=1.0)

    reopened = []
    reopen_thread = threading.Thread(target=lambda: reopened.append(manager.open("Front")))
    reopen_thread.start()
    time.sleep(0.05)

    assert reopened == []

    release_allowed.set()
    reopen_thread.join(timeout=1.0)

    assert len(opened) == 2
    assert reopened == [opened[1]]


def test_display_camera_decode_rejects_green_startup_artifact():
    camera = cam_module.RemoteCv2Camera.__new__(cam_module.RemoteCv2Camera)
    camera.name = "Front"
    camera.width = 32
    camera.height = 24
    camera._last_rejected_artifact_seq = None

    green = np.zeros((24, 32, 3), dtype=np.uint8)
    green[:, :, 1] = 74
    green_packet = SimpleNamespace(
        data=green.tobytes(),
        seq=1,
        monotonic_ts=time.monotonic(),
        wall_ts=time.time(),
    )

    assert camera._decode_packet(green_packet) is None

    black = np.zeros((24, 32, 3), dtype=np.uint8)
    black_packet = SimpleNamespace(
        data=black.tobytes(),
        seq=2,
        monotonic_ts=time.monotonic(),
        wall_ts=time.time(),
    )

    assert camera._decode_packet(black_packet) is None

    normal = np.zeros((24, 32, 3), dtype=np.uint8)
    normal[:, :, 0] = 140
    normal[:, :, 1] = 145
    normal[:, :, 2] = 110
    normal[::2, ::2, :] = 180
    normal_packet = SimpleNamespace(
        data=normal.tobytes(),
        seq=3,
        monotonic_ts=time.monotonic(),
        wall_ts=time.time(),
    )

    decoded = camera._decode_packet(normal_packet)

    assert decoded is not None
    assert decoded.seq == 3


def test_display_camera_decode_rejects_textured_green_channel_collapse():
    camera = cam_module.RemoteCv2Camera.__new__(cam_module.RemoteCv2Camera)
    camera.name = "Front"
    camera.width = 64
    camera.height = 48
    camera._last_rejected_artifact_seq = None

    y = np.linspace(55, 210, camera.height, dtype=np.uint8)[:, None]
    x = np.linspace(0, 35, camera.width, dtype=np.uint8)[None, :]
    artifact = np.zeros((camera.height, camera.width, 3), dtype=np.uint8)
    artifact[:, :, 1] = np.clip(y + x, 0, 255)
    artifact[::8, ::8, 0] = 8
    artifact[5::9, 4::7, 2] = 10
    artifact_packet = SimpleNamespace(
        data=artifact.tobytes(),
        seq=4,
        monotonic_ts=time.monotonic(),
        wall_ts=time.time(),
    )

    assert camera._decode_packet(artifact_packet) is None

    normal = np.zeros((camera.height, camera.width, 3), dtype=np.uint8)
    normal[:, :, 0] = 150
    normal[:, :, 1] = 158
    normal[:, :, 2] = 142
    normal[::2, ::2, :] = 210
    normal_packet = SimpleNamespace(
        data=normal.tobytes(),
        seq=5,
        monotonic_ts=time.monotonic(),
        wall_ts=time.time(),
    )

    decoded = camera._decode_packet(normal_packet)

    assert decoded is not None
    assert decoded.seq == 5
