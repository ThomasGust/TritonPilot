import base64
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


def test_capture_onboard_snapshot_decodes_rov_jpeg(monkeypatch, tmp_path):
    jpeg_bytes = b"\xff\xd8rov snapshot\xff\xd9"

    class _SnapshotRov:
        def __init__(self):
            self.calls = []

        def capture_snapshot(self, **kwargs):
            self.calls.append(dict(kwargs))
            return {
                "name": "Front",
                "mime_type": "image/jpeg",
                "extension": "jpg",
                "encoding": "base64",
                "data_b64": base64.b64encode(jpeg_bytes).decode("ascii"),
                "byte_count": len(jpeg_bytes),
                "caps": "image/jpeg,width=32,height=24",
                "wall_ts": 123.5,
                "monotonic_ts": 45.25,
            }

    fake_rov = _SnapshotRov()
    monkeypatch.setattr(cam_module, "ROVStreams", lambda endpoint: fake_rov)

    cfg_path = tmp_path / "streams.json"
    _write_streams_config(cfg_path)
    manager = cam_module.RemoteCameraManager(str(cfg_path))

    packet = manager.capture_onboard_snapshot("Front", timeout_s=0.75)

    assert fake_rov.calls == [{"name": "Front", "timeout_s": 0.75}]
    assert packet.source_name == "Front"
    assert packet.image_bytes == jpeg_bytes
    assert packet.mime_type == "image/jpeg"
    assert packet.extension == "jpg"
    assert packet.byte_count == len(jpeg_bytes)
    assert packet.caps == "image/jpeg,width=32,height=24"
    assert packet.wall_ts == 123.5
    assert packet.monotonic_ts == 45.25


def test_capture_onboard_stereo_pair_decodes_rov_jpegs(monkeypatch, tmp_path):
    left_bytes = b"\xff\xd8left\xff\xd9"
    right_bytes = b"\xff\xd8right\xff\xd9"

    class _StereoRov:
        def __init__(self):
            self.calls = []

        def capture_stereo_pair(self, **kwargs):
            self.calls.append(dict(kwargs))
            return {
                "timestamp_source": "rov_snapshot_appsink_fresh_monotonic",
                "pair_delta_ms": 7.5,
                "attempts": 2,
                "left": {
                    "stream": "Front",
                    "mime_type": "image/jpeg",
                    "extension": "jpg",
                    "encoding": "base64",
                    "image_b64": base64.b64encode(left_bytes).decode("ascii"),
                    "byte_count": len(left_bytes),
                    "seq": 11,
                    "shape": [1080, 1920, 3],
                    "wall_ts": 1000.0,
                    "monotonic_ts": 50.0,
                    "source_pts_ns": 123,
                },
                "right": {
                    "stream": "Front",
                    "mime_type": "image/jpeg",
                    "extension": "jpg",
                    "encoding": "base64",
                    "image_b64": base64.b64encode(right_bytes).decode("ascii"),
                    "byte_count": len(right_bytes),
                    "seq": 12,
                    "shape": [1080, 1920, 3],
                    "wall_ts": 1000.0075,
                    "monotonic_ts": 50.0075,
                    "source_pts_ns": 456,
                },
            }

    fake_rov = _StereoRov()
    monkeypatch.setattr(cam_module, "ROVStreams", lambda endpoint: fake_rov)

    cfg_path = tmp_path / "streams.json"
    cfg_path.write_text(
        json.dumps(
            {
                "streams": [
                    {"name": "Left", "device": "/dev/video0", "width": 2, "height": 1, "fps": 30, "port": 5000},
                    {"name": "Right", "device": "/dev/video1", "width": 2, "height": 1, "fps": 30, "port": 5002},
                ]
            }
        ),
        encoding="utf-8",
    )
    manager = cam_module.RemoteCameraManager(str(cfg_path))

    packet = manager.capture_onboard_stereo_pair("Left", "Right", timeout_s=0.8, max_pair_delta_ms=25.0)

    assert fake_rov.calls == [{"left": "Left", "right": "Right", "timeout_s": 0.8, "max_pair_delta_ms": 25.0}]
    assert packet.pair_delta_ms == 7.5
    assert packet.timestamp_source == "rov_snapshot_appsink_fresh_monotonic"
    assert packet.attempts == 2
    assert packet.left.image_bytes == left_bytes
    assert packet.right.image_bytes == right_bytes
    assert packet.left.seq == 11
    assert packet.right.shape == (1080, 1920, 3)
    assert packet.left.source_pts_ns == 123


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


def test_snapshot_capture_keeps_udp_mirror_warm_until_closed(monkeypatch, tmp_path):
    class _FakeRov:
        def __init__(self, *_args, **_kwargs):
            self.extra = {"sender_leaky_queues": True}
            self.updates = []

        def list_status(self):
            return {"Front": {"extra": dict(self.extra)}}

        def update_stream(self, **kwargs):
            self.extra = dict(kwargs["extra"])
            self.updates.append(dict(kwargs))
            return {"note": "updated"}

    class _FakeReceiver:
        instances = []

        def __init__(self, cfg):
            self.cfg = cfg
            self.started = False
            self.stopped = False
            self.calls = 0
            _FakeReceiver.instances.append(self)

        def start(self):
            self.started = True

        def stop(self, grace_s=0.2):
            self.stopped = True

        def latest_frame_packet(self):
            self.calls += 1
            if self.calls == 1:
                frame = np.zeros((24, 32, 3), dtype=np.uint8)
            else:
                frame = np.zeros((24, 32, 3), dtype=np.uint8)
                frame[:, :, 0] = 120
                frame[:, :, 1] = 135
                frame[:, :, 2] = 150
                frame[::2, ::2, :] = 210
            return SimpleNamespace(
                data=frame.tobytes(),
                seq=self.calls,
                monotonic_ts=time.monotonic(),
                wall_ts=time.time(),
            )

    cfg_path = tmp_path / "streams.json"
    cfg_path.write_text(
        json.dumps(
            {
                "windows_host": "127.0.0.1",
                "streams": [
                    {
                        "name": "Front",
                        "device": "/dev/video0",
                        "width": 32,
                        "height": 24,
                        "fps": 30,
                        "video_format": "h264",
                        "port": 5000,
                        "receiver_h264_decoder": "openh264dec",
                        "extra": {"sender_leaky_queues": True},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    fake_rov = _FakeRov()
    monkeypatch.setattr(cam_module, "ROVStreams", lambda endpoint: fake_rov)
    monkeypatch.setattr(cam_module, "ReceiverProcess", _FakeReceiver)
    monkeypatch.setattr(cam_module.RemoteCameraManager, "_allocate_udp_port", staticmethod(lambda _host=None: 61000))

    manager = cam_module.RemoteCameraManager(str(cfg_path))
    packet = manager.capture_snapshot_frame("Front", timeout_s=0.5)

    assert packet.source_name == "Front"
    assert packet.seq == 2
    assert packet.frame_bgr.shape == (24, 32, 3)
    receiver = _FakeReceiver.instances[0]
    assert receiver.started is True
    assert receiver.stopped is False
    assert receiver.cfg.port == 61000
    assert receiver.cfg.extra["receiver_kill_port_users"] is False
    assert receiver.cfg.extra["receiver_output_fps"] == 8
    assert fake_rov.updates[0]["name"] == "Front"
    assert fake_rov.updates[0]["extra"]["udp_mirror_ports"] == [61000]
    assert fake_rov.extra["udp_mirror_ports"] == [61000]

    second = manager.capture_snapshot_frame("Front", timeout_s=0.5)

    assert second.source_name == "Front"
    assert second.seq == 3
    assert len(_FakeReceiver.instances) == 1
    assert len(fake_rov.updates) == 1

    manager.close_snapshot_taps(reason="test")

    assert receiver.stopped is True
    assert fake_rov.updates[-1]["extra"] == {"sender_leaky_queues": True}


def test_snapshot_prewarm_starts_configured_four_mirrors(monkeypatch, tmp_path):
    stream_names = ["Primary", "Aux", "Arm", "Back", "Spare"]

    class _FakeRov:
        def __init__(self, *_args, **_kwargs):
            self.extra = {name: {"sender_leaky_queues": True} for name in stream_names}
            self.updates = []

        def list_status(self):
            return {name: {"extra": dict(extra)} for name, extra in self.extra.items()}

        def update_stream(self, **kwargs):
            name = kwargs["name"]
            self.extra[name] = dict(kwargs["extra"])
            self.updates.append(dict(kwargs))
            return {"note": "updated"}

    class _FakeReceiver:
        instances = []

        def __init__(self, cfg):
            self.cfg = cfg
            self.started = False
            self.stopped = False
            _FakeReceiver.instances.append(self)

        def start(self):
            self.started = True

        def stop(self, grace_s=0.2):
            self.stopped = True

    class _ImmediateThread:
        def __init__(self, *, target, name=None, daemon=None):
            self.target = target

        def start(self):
            self.target()

    cfg_path = tmp_path / "streams.json"
    cfg_path.write_text(
        json.dumps(
            {
                "windows_host": "127.0.0.1",
                "snapshot_prewarm_count": 4,
                "default_pane_order": ["Primary", "Aux", "Arm", "Back", "Spare"],
                "streams": [
                    {
                        "name": name,
                        "device": f"/dev/video{idx}",
                        "width": 32,
                        "height": 24,
                        "fps": 30,
                        "video_format": "h264",
                        "port": 5000 + idx,
                        "receiver_h264_decoder": "openh264dec",
                        "extra": {"sender_leaky_queues": True},
                    }
                    for idx, name in enumerate(stream_names)
                ],
            }
        ),
        encoding="utf-8",
    )
    fake_rov = _FakeRov()
    ports = iter([61000, 61001, 61002, 61003])

    monkeypatch.setattr(cam_module, "ROVStreams", lambda endpoint: fake_rov)
    monkeypatch.setattr(cam_module, "ReceiverProcess", _FakeReceiver)
    monkeypatch.setattr(cam_module.RemoteCameraManager, "_allocate_udp_port", staticmethod(lambda _host=None: next(ports)))
    monkeypatch.setattr(cam_module.threading, "Thread", _ImmediateThread)

    manager = cam_module.RemoteCameraManager(str(cfg_path))
    scheduled = manager.prewarm_snapshot_taps()

    assert scheduled == ["Primary", "Aux", "Arm", "Back"]
    assert [rx.started for rx in _FakeReceiver.instances] == [True, True, True, True]
    assert [update["name"] for update in fake_rov.updates] == ["Primary", "Aux", "Arm", "Back"]
    assert fake_rov.extra["Primary"]["udp_mirror_ports"] == [61000]
    assert fake_rov.extra["Back"]["udp_mirror_ports"] == [61003]
    assert "udp_mirror_ports" not in fake_rov.extra["Spare"]

    manager.close_snapshot_taps(reason="test")

    assert all(rx.stopped for rx in _FakeReceiver.instances)
    assert fake_rov.updates[-4]["extra"] == {"sender_leaky_queues": True}


def test_snapshot_capture_readds_missing_mirror_before_saving(monkeypatch, tmp_path):
    class _FakeRov:
        def __init__(self, *_args, **_kwargs):
            self.extra = {"sender_leaky_queues": True}
            self.updates = []

        def list_status(self):
            return {"Front": {"extra": dict(self.extra)}}

        def update_stream(self, **kwargs):
            self.extra = dict(kwargs["extra"])
            self.updates.append(dict(kwargs))
            return {"note": "updated"}

    class _FakeReceiver:
        def __init__(self, cfg):
            self.cfg = cfg
            self.calls = 0

        def start(self):
            return None

        def stop(self, grace_s=0.2):
            return None

        def latest_frame_packet(self):
            self.calls += 1
            frame = np.zeros((24, 32, 3), dtype=np.uint8)
            frame[:, :, 0] = 120
            frame[:, :, 1] = 135
            frame[:, :, 2] = 150
            frame[::2, ::2, :] = 210
            return SimpleNamespace(
                data=frame.tobytes(),
                seq=self.calls,
                monotonic_ts=time.monotonic(),
                wall_ts=time.time(),
            )

    cfg_path = tmp_path / "streams.json"
    cfg_path.write_text(
        json.dumps(
            {
                "windows_host": "127.0.0.1",
                "streams": [
                    {
                        "name": "Front",
                        "device": "/dev/video0",
                        "width": 32,
                        "height": 24,
                        "fps": 30,
                        "video_format": "h264",
                        "port": 5000,
                        "receiver_h264_decoder": "openh264dec",
                        "extra": {"sender_leaky_queues": True},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    fake_rov = _FakeRov()
    monkeypatch.setattr(cam_module, "ROVStreams", lambda endpoint: fake_rov)
    monkeypatch.setattr(cam_module, "ReceiverProcess", _FakeReceiver)
    monkeypatch.setattr(cam_module.RemoteCameraManager, "_allocate_udp_port", staticmethod(lambda _host=None: 61000))

    manager = cam_module.RemoteCameraManager(str(cfg_path))
    first = manager.capture_snapshot_frame("Front", timeout_s=0.5)
    assert first.seq == 1
    assert fake_rov.extra["udp_mirror_ports"] == [61000]

    fake_rov.extra = {"sender_leaky_queues": True}
    second = manager.capture_snapshot_frame("Front", timeout_s=0.5)

    assert second.seq == 2
    assert fake_rov.updates[-1]["extra"]["udp_mirror_ports"] == [61000]


def test_snapshot_capture_reuses_recent_frame_when_fresh_frame_is_slow(monkeypatch, tmp_path):
    class _FakeRov:
        def __init__(self, *_args, **_kwargs):
            self.extra = {"sender_leaky_queues": True}

        def list_status(self):
            return {"Front": {"extra": dict(self.extra)}}

        def update_stream(self, **kwargs):
            self.extra = dict(kwargs["extra"])
            return {"note": "updated"}

    class _FakeReceiver:
        def __init__(self, cfg):
            self.cfg = cfg
            self.packet = None

        def start(self):
            return None

        def stop(self, grace_s=0.2):
            return None

        def latest_frame_packet(self):
            if self.packet is None:
                frame = np.zeros((24, 32, 3), dtype=np.uint8)
                frame[:, :, 0] = 120
                frame[:, :, 1] = 135
                frame[:, :, 2] = 150
                frame[::2, ::2, :] = 210
                self.packet = SimpleNamespace(
                    data=frame.tobytes(),
                    seq=7,
                    monotonic_ts=time.monotonic(),
                    wall_ts=time.time(),
                )
            return self.packet

    cfg_path = tmp_path / "streams.json"
    cfg_path.write_text(
        json.dumps(
            {
                "windows_host": "127.0.0.1",
                "snapshot_fresh_wait_s": 0.01,
                "snapshot_reuse_max_age_s": 2.0,
                "streams": [
                    {
                        "name": "Front",
                        "device": "/dev/video0",
                        "width": 32,
                        "height": 24,
                        "fps": 30,
                        "video_format": "h264",
                        "port": 5000,
                        "receiver_h264_decoder": "openh264dec",
                        "extra": {"sender_leaky_queues": True},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    fake_rov = _FakeRov()
    monkeypatch.setattr(cam_module, "ROVStreams", lambda endpoint: fake_rov)
    monkeypatch.setattr(cam_module, "ReceiverProcess", _FakeReceiver)
    monkeypatch.setattr(cam_module.RemoteCameraManager, "_allocate_udp_port", staticmethod(lambda _host=None: 61000))

    manager = cam_module.RemoteCameraManager(str(cfg_path))
    first = manager.capture_snapshot_frame("Front", timeout_s=0.5)
    second = manager.capture_snapshot_frame("Front", timeout_s=0.5)

    assert first.seq == 7
    assert second.seq == 7


def test_noop_rpc_endpoint_refresh_does_not_close_snapshot_taps(monkeypatch, tmp_path):
    class _FakeRov:
        def __init__(self, endpoint):
            self.endpoint = endpoint
            self.extra = {"sender_leaky_queues": True}
            self.updates = []
            self.closed = False

        def list_status(self):
            return {"Front": {"extra": dict(self.extra)}}

        def update_stream(self, **kwargs):
            self.extra = dict(kwargs["extra"])
            self.updates.append(dict(kwargs))
            return {"note": "updated"}

        def close(self):
            self.closed = True

    class _FakeReceiver:
        instances = []

        def __init__(self, cfg):
            self.cfg = cfg
            self.started = False
            self.stopped = False
            _FakeReceiver.instances.append(self)

        def start(self):
            self.started = True

        def stop(self, grace_s=0.2):
            self.stopped = True

    class _ImmediateThread:
        def __init__(self, *, target, name=None, daemon=None):
            self.target = target

        def start(self):
            self.target()

    cfg_path = tmp_path / "streams.json"
    cfg_path.write_text(
        json.dumps(
            {
                "windows_host": "127.0.0.1",
                "snapshot_prewarm_count": 1,
                "default_pane_order": ["Front"],
                "streams": [
                    {
                        "name": "Front",
                        "device": "/dev/video0",
                        "width": 32,
                        "height": 24,
                        "fps": 30,
                        "video_format": "h264",
                        "port": 5000,
                        "receiver_h264_decoder": "openh264dec",
                        "extra": {"sender_leaky_queues": True},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    fake_rov = _FakeRov("tcp://rov.local:5555")
    monkeypatch.setattr(cam_module, "ROVStreams", lambda endpoint: fake_rov)
    monkeypatch.setattr(cam_module, "ReceiverProcess", _FakeReceiver)
    monkeypatch.setattr(cam_module.RemoteCameraManager, "_allocate_udp_port", staticmethod(lambda _host=None: 61000))
    monkeypatch.setattr(cam_module.threading, "Thread", _ImmediateThread)

    manager = cam_module.RemoteCameraManager(str(cfg_path))
    manager.prewarm_snapshot_taps()

    receiver = _FakeReceiver.instances[0]
    assert receiver.started is True
    assert receiver.stopped is False
    assert fake_rov.extra["udp_mirror_ports"] == [61000]

    changed = manager.set_rpc_endpoint(fake_rov.endpoint, windows_host="127.0.0.1")

    assert changed is False
    assert receiver.stopped is False
    assert fake_rov.closed is False
    assert fake_rov.extra["udp_mirror_ports"] == [61000]


def test_snapshot_capture_uses_dedicated_tap_not_display_receiver(monkeypatch, tmp_path):
    class _FakeRov:
        def __init__(self, *_args, **_kwargs):
            self.extra = {"sender_leaky_queues": True}

        def list_status(self):
            return {"Front": {"extra": dict(self.extra)}}

        def update_stream(self, **kwargs):
            self.extra = dict(kwargs["extra"])
            return {"note": "updated"}

    class _FakeDisplayCamera:
        def latest_frame_packet(self):
            raise AssertionError("snapshot capture should not read the display receiver")

    class _FakeReceiver:
        def __init__(self, cfg):
            self.cfg = cfg
            self.started = False
            self.stopped = False

        def start(self):
            self.started = True

        def stop(self, grace_s=0.2):
            self.stopped = True

        def latest_frame_packet(self):
            frame = np.zeros((24, 32, 3), dtype=np.uint8)
            frame[:, :, 0] = 120
            frame[:, :, 1] = 135
            frame[:, :, 2] = 150
            frame[::2, ::2, :] = 210
            return SimpleNamespace(
                data=frame.tobytes(),
                seq=1,
                monotonic_ts=time.monotonic(),
                wall_ts=time.time(),
            )

    cfg_path = tmp_path / "streams.json"
    cfg_path.write_text(
        json.dumps(
            {
                "windows_host": "127.0.0.1",
                "snapshot_prewarm_count": 1,
                "default_pane_order": ["Front"],
                "streams": [
                    {
                        "name": "Front",
                        "device": "/dev/video0",
                        "width": 32,
                        "height": 24,
                        "fps": 30,
                        "video_format": "h264",
                        "port": 5000,
                        "receiver_h264_decoder": "openh264dec",
                        "extra": {"sender_leaky_queues": True},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    fake_rov = _FakeRov()
    monkeypatch.setattr(cam_module, "ROVStreams", lambda endpoint: fake_rov)
    monkeypatch.setattr(cam_module, "ReceiverProcess", _FakeReceiver)
    monkeypatch.setattr(cam_module.RemoteCameraManager, "_allocate_udp_port", staticmethod(lambda _host=None: 61000))

    manager = cam_module.RemoteCameraManager(str(cfg_path))
    manager._opened["Front"] = _FakeDisplayCamera()

    packet = manager.capture_snapshot_frame("Front", timeout_s=0.5)

    assert packet.source_name == "Front"
    assert packet.seq == 1
