import json
import threading
import time

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
