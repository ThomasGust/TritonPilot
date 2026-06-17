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


def test_capture_receiver_uses_capture_port_and_refcounts(monkeypatch, tmp_path):
    opened = []

    class _FakeCaptureCamera:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.released = False
            opened.append(self)

        def release(self):
            self.released = True

    monkeypatch.setattr(cam_module, "ROVStreams", lambda endpoint: _FakeRov())
    monkeypatch.setattr(cam_module, "RemoteCaptureCamera", _FakeCaptureCamera)

    cfg_path = tmp_path / "streams.json"
    cfg_path.write_text(
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
                        "capture_port": 6000,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    manager = cam_module.RemoteCameraManager(str(cfg_path))

    first = manager.open_capture("Front")
    second = manager.open_capture("Front")

    assert first is second
    assert len(opened) == 1
    assert opened[0].kwargs["port"] == 6000

    assert manager.close_capture("Front") is False
    assert opened[0].released is False
    assert manager.close_capture("Front") is True
    assert opened[0].released is True


def test_capture_receiver_can_close_async_after_refcount_reaches_zero(monkeypatch, tmp_path):
    released = threading.Event()
    opened = []

    class _FakeCaptureCamera:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            opened.append(self)

        def release(self):
            released.set()

    monkeypatch.setattr(cam_module, "ROVStreams", lambda endpoint: _FakeRov())
    monkeypatch.setattr(cam_module, "RemoteCaptureCamera", _FakeCaptureCamera)

    cfg_path = tmp_path / "streams.json"
    cfg_path.write_text(
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
                        "capture_port": 6000,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    manager = cam_module.RemoteCameraManager(str(cfg_path))

    first = manager.open_capture("Front")
    second = manager.open_capture("Front")

    assert first is second
    assert manager.close_capture_async("Front") is False
    assert released.is_set() is False
    assert manager.close_capture_async("Front") is True
    assert released.wait(timeout=1.0)


def test_restart_capture_stream_preserves_capture_mirror(monkeypatch, tmp_path):
    calls = []

    class _RestartRov:
        def stop_stream(self, **kwargs):
            calls.append(("stop", kwargs))

        def start_stream(self, **kwargs):
            calls.append(("start", kwargs))
            return {"ok": True}

    monkeypatch.setattr(cam_module, "ROVStreams", lambda endpoint: _RestartRov())

    cfg_path = tmp_path / "streams.json"
    cfg_path.write_text(
        json.dumps(
            {
                "windows_host": "192.168.1.1",
                "streams": [
                    {
                        "name": "Front",
                        "device": "/dev/video0",
                        "width": 2,
                        "height": 1,
                        "fps": 30,
                        "video_format": "h264",
                        "port": 5000,
                        "capture_port": 6000,
                        "extra": {"udp_mirror_ports": [6100]},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    manager = cam_module.RemoteCameraManager(str(cfg_path))

    manager.restart_capture_stream("Front")

    assert calls[0] == ("stop", {"name": "Front"})
    assert calls[1][0] == "start"
    start = calls[1][1]
    assert start["name"] == "Front"
    assert start["host"] == "192.168.1.1"
    assert start["port"] == 5000
    assert start["extra"]["udp_mirror_ports"] == [6100, 6000]


def test_capture_camera_decode_rejects_green_startup_artifact():
    camera = cam_module.RemoteCaptureCamera.__new__(cam_module.RemoteCaptureCamera)
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
