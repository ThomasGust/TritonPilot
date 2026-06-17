import json
import time
from pathlib import Path

import numpy as np
import pytest

from stereo.capture import StereoCaptureInterrupted, StereoCaptureSession
from stereo.pairs import load_stereo_pairs
from video.cam import CameraFramePacket


def _usable_frame(seq: int, width: int = 6, height: int = 4) -> np.ndarray:
    base = 40 + (int(seq) % 9) * 18
    frame = np.zeros((height, width, 3), dtype=np.uint8)
    frame[:, :, 0] = min(base, 255)
    frame[:, :, 1] = min(base + 28, 255)
    frame[:, :, 2] = min(base + 12, 255)
    frame[::2, ::2, :] = min(base + 85, 255)
    return frame


def test_load_stereo_pairs_from_streams_config(tmp_path: Path):
    cfg_path = tmp_path / "streams.json"
    cfg_path.write_text(
        json.dumps(
            {
                "streams": [{"name": "Left"}, {"name": "Right"}],
                "stereo_pairs": [
                    {
                        "name": "Forward",
                        "left": "Left",
                        "right": "Right",
                        "rig_id": "rig-a",
                        "max_pair_delta_ms": 35,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    pairs = load_stereo_pairs(cfg_path)

    assert len(pairs) == 1
    assert pairs[0].name == "Forward"
    assert pairs[0].left == "Left"
    assert pairs[0].right == "Right"
    assert pairs[0].max_pair_delta_s == 0.035


class _FakeCamera:
    def __init__(self, name: str, seq: int, ts: float):
        self.packets = [
            CameraFramePacket(
                source_name=name,
                frame_bgr=_usable_frame(seq),
                seq=seq,
                monotonic_ts=ts,
                wall_ts=1000.0 + ts,
            )
        ]

    def latest_frame_packet(self):
        return self.packets[-1]

    def recent_frame_packets(self, *, max_age_s: float = 0.5):
        return list(self.packets)


class _FakeManager:
    def __init__(self):
        self.stream_defs = {
            "Left": {"name": "Left", "rotation_deg": 0, "width": 6, "height": 4},
            "Right": {"name": "Right", "rotation_deg": 0, "width": 6, "height": 4},
        }
        self.cameras = {
            "Left": _FakeCamera("Left", 1, 10.000),
            "Right": _FakeCamera("Right", 2, 10.012),
        }
        self.closed = []

    def open(self, name: str):
        return self.cameras[name]

    def close(self, name: str):
        self.closed.append(name)


def test_stereo_capture_session_writes_pair_and_manifest(tmp_path: Path):
    streams_path = tmp_path / "streams.json"
    streams_path.write_text(
        json.dumps(
            {
                "streams": [{"name": "Left"}, {"name": "Right"}],
                "stereo_pairs": [
                    {
                        "name": "Forward",
                        "left": "Left",
                        "right": "Right",
                        "rig_id": "rig-a",
                        "max_pair_delta_ms": 20,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    pair = load_stereo_pairs(streams_path)[0]
    manager = _FakeManager()
    session = StereoCaptureSession(
        manager,  # type: ignore[arg-type]
        pair,
        output_root=tmp_path,
        session_name="test-session",
        close_on_stop=True,
    )

    session.start()
    record = session.capture_once(wait_s=0.1)
    session.stop()

    assert record["pair_delta_ms"] == pytest.approx(12.0)
    assert (session.session_dir / record["left_path"]).exists()
    assert (session.session_dir / record["right_path"]).exists()
    manifest = json.loads(session.manifest_path.read_text(encoding="utf-8"))
    assert manifest["pair"]["rig_id"] == "rig-a"
    assert len(manifest["frames"]) == 1
    assert set(manager.closed) == {"Left", "Right"}


def test_stereo_capture_session_can_use_external_frame_sources(tmp_path: Path):
    streams_path = tmp_path / "streams.json"
    streams_path.write_text(
        json.dumps(
            {
                "streams": [{"name": "Left"}, {"name": "Right"}],
                "stereo_pairs": [
                    {
                        "name": "Forward",
                        "left": "Left",
                        "right": "Right",
                        "rig_id": "rig-a",
                        "max_pair_delta_ms": 20,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    pair = load_stereo_pairs(streams_path)[0]
    manager = _FakeManager()
    external_sources = {
        "Left": _FakeCamera("Left", 11, 20.000),
        "Right": _FakeCamera("Right", 12, 20.010),
    }
    session = StereoCaptureSession(
        manager,  # type: ignore[arg-type]
        pair,
        output_root=tmp_path,
        session_name="external-session",
        close_on_stop=True,
        frame_source_provider=lambda name: external_sources.get(name),
    )

    session.start()
    record = session.capture_once(wait_s=0.1)
    session.stop()

    assert record["left"]["seq"] == 11
    assert record["right"]["seq"] == 12
    assert record["pair_delta_ms"] == pytest.approx(10.0)
    assert manager.closed == []


def test_stereo_capture_session_appends_existing_manifest(tmp_path: Path):
    streams_path = tmp_path / "streams.json"
    streams_path.write_text(
        json.dumps(
            {
                "streams": [{"name": "Left"}, {"name": "Right"}],
                "stereo_pairs": [
                    {
                        "name": "Forward",
                        "left": "Left",
                        "right": "Right",
                        "rig_id": "rig-a",
                        "max_pair_delta_ms": 20,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    pair = load_stereo_pairs(streams_path)[0]
    manager = _FakeManager()

    first = StereoCaptureSession(
        manager,  # type: ignore[arg-type]
        pair,
        output_root=tmp_path,
        session_name="pool-session",
    )
    first.start()
    first.capture_once(wait_s=0.1)
    first.stop()

    manager.cameras["Left"] = _FakeCamera("Left", 3, 11.000)
    manager.cameras["Right"] = _FakeCamera("Right", 4, 11.010)
    second = StereoCaptureSession(
        manager,  # type: ignore[arg-type]
        pair,
        output_root=tmp_path,
        session_name="pool-session",
    )
    second.start()
    record = second.capture_once(wait_s=0.1)
    second.stop()

    manifest = json.loads(second.manifest_path.read_text(encoding="utf-8"))
    assert record["index"] == 2
    assert len(manifest["frames"]) == 2
    assert manifest["frames"][0]["stem"] == "pair_000001"
    assert manifest["frames"][1]["stem"] == "pair_000002"
    assert (second.session_dir / manifest["frames"][1]["left_path"]).exists()


def test_stereo_capture_can_defer_manifest_flush_until_stop(tmp_path: Path):
    streams_path = tmp_path / "streams.json"
    streams_path.write_text(
        json.dumps(
            {
                "streams": [{"name": "Left"}, {"name": "Right"}],
                "stereo_pairs": [
                    {
                        "name": "Forward",
                        "left": "Left",
                        "right": "Right",
                        "rig_id": "rig-a",
                        "max_pair_delta_ms": 20,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    pair = load_stereo_pairs(streams_path)[0]
    manager = _FakeManager()
    session = StereoCaptureSession(
        manager,  # type: ignore[arg-type]
        pair,
        output_root=tmp_path,
        session_name="deferred-session",
    )

    session.start()
    record = session.capture_once(wait_s=0.1, flush_manifest=False)
    manifest_before_stop = json.loads(session.manifest_path.read_text(encoding="utf-8"))
    assert manifest_before_stop["frames"] == []
    assert (session.session_dir / record["left_path"]).exists()
    assert (session.session_dir / record["right_path"]).exists()

    session.stop()

    manifest_after_stop = json.loads(session.manifest_path.read_text(encoding="utf-8"))
    assert len(manifest_after_stop["frames"]) == 1
    assert manifest_after_stop["frames"][0]["stem"] == "pair_000001"


def test_stereo_capture_can_save_images_asynchronously_until_stop(tmp_path: Path, monkeypatch):
    streams_path = tmp_path / "streams.json"
    streams_path.write_text(
        json.dumps(
            {
                "streams": [{"name": "Left"}, {"name": "Right"}],
                "stereo_pairs": [
                    {
                        "name": "Forward",
                        "left": "Left",
                        "right": "Right",
                        "rig_id": "rig-a",
                        "max_pair_delta_ms": 20,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    pair = load_stereo_pairs(streams_path)[0]
    manager = _FakeManager()
    session = StereoCaptureSession(
        manager,  # type: ignore[arg-type]
        pair,
        output_root=tmp_path,
        session_name="async-session",
    )

    def _slow_save(left_frame, left_path, right_frame, right_path):
        time.sleep(0.2)
        left_path.write_bytes(b"left")
        right_path.write_bytes(b"right")

    monkeypatch.setattr(session, "_save_images", _slow_save)

    session.start()
    started = time.monotonic()
    record = session.capture_once(wait_s=0.1, flush_manifest=False, async_save=True)
    capture_elapsed = time.monotonic() - started

    assert capture_elapsed < 0.15
    assert record["save_pending"] is True
    assert not (session.session_dir / record["left_path"]).exists()

    session.stop()

    manifest_after_stop = json.loads(session.manifest_path.read_text(encoding="utf-8"))
    assert (session.session_dir / record["left_path"]).exists()
    assert (session.session_dir / record["right_path"]).exists()
    assert manifest_after_stop["frames"][0]["save_pending"] is False


def test_stereo_capture_chooses_closest_buffered_frame_pair(tmp_path: Path):
    streams_path = tmp_path / "streams.json"
    streams_path.write_text(
        json.dumps(
            {
                "streams": [{"name": "Left"}, {"name": "Right"}],
                "stereo_pairs": [
                    {
                        "name": "Forward",
                        "left": "Left",
                        "right": "Right",
                        "rig_id": "rig-a",
                        "max_pair_delta_ms": 12,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    pair = load_stereo_pairs(streams_path)[0]
    manager = _FakeManager()
    manager.cameras["Left"].packets = [
        CameraFramePacket("Left", _usable_frame(10), 10, 100.000, 1000.000),
        CameraFramePacket("Left", _usable_frame(11), 11, 100.033, 1000.033),
    ]
    manager.cameras["Right"].packets = [
        CameraFramePacket("Right", _usable_frame(20), 20, 100.010, 1000.010),
        CameraFramePacket("Right", _usable_frame(21), 21, 100.060, 1000.060),
    ]
    session = StereoCaptureSession(
        manager,  # type: ignore[arg-type]
        pair,
        output_root=tmp_path,
        session_name="buffered-session",
    )

    session.start()
    record = session.capture_once(wait_s=0.1)
    session.stop()

    assert record["left"]["seq"] == 10
    assert record["right"]["seq"] == 20
    assert record["pair_delta_ms"] == pytest.approx(10.0)


def test_stereo_capture_skips_green_startup_artifact_packets(tmp_path: Path):
    streams_path = tmp_path / "streams.json"
    streams_path.write_text(
        json.dumps(
            {
                "streams": [{"name": "Left"}, {"name": "Right"}],
                "stereo_pairs": [
                    {
                        "name": "Forward",
                        "left": "Left",
                        "right": "Right",
                        "rig_id": "rig-a",
                        "max_pair_delta_ms": 20,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    pair = load_stereo_pairs(streams_path)[0]
    manager = _FakeManager()
    green = np.zeros((24, 32, 3), dtype=np.uint8)
    green[:, :, 1] = 74
    normal_left = np.zeros((24, 32, 3), dtype=np.uint8)
    normal_left[:, :, 0] = 140
    normal_left[:, :, 1] = 145
    normal_left[:, :, 2] = 110
    normal_left[::2, ::2, :] = 180
    normal_right = normal_left.copy()
    normal_right[:, :, 2] = 118
    manager.cameras["Left"].packets = [
        CameraFramePacket("Left", green, 10, 100.000, 1000.000),
        CameraFramePacket("Left", normal_left, 11, 100.050, 1000.050),
    ]
    manager.cameras["Right"].packets = [
        CameraFramePacket("Right", green, 20, 100.002, 1000.002),
        CameraFramePacket("Right", normal_right, 21, 100.058, 1000.058),
    ]
    session = StereoCaptureSession(
        manager,  # type: ignore[arg-type]
        pair,
        output_root=tmp_path,
        session_name="artifact-session",
    )

    session.start()
    record = session.capture_once(wait_s=0.1)
    session.stop()

    assert record["left"]["seq"] == 11
    assert record["right"]["seq"] == 21
    assert record["pair_delta_ms"] == pytest.approx(8.0)


def test_stereo_capture_once_honors_stop_request(tmp_path: Path):
    streams_path = tmp_path / "streams.json"
    streams_path.write_text(
        json.dumps(
            {
                "streams": [{"name": "Left"}, {"name": "Right"}],
                "stereo_pairs": [
                    {
                        "name": "Forward",
                        "left": "Left",
                        "right": "Right",
                        "rig_id": "rig-a",
                        "max_pair_delta_ms": 20,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    pair = load_stereo_pairs(streams_path)[0]
    manager = _FakeManager()
    session = StereoCaptureSession(
        manager,  # type: ignore[arg-type]
        pair,
        output_root=tmp_path,
        session_name="interrupt-session",
    )

    session.start()
    try:
        with pytest.raises(StereoCaptureInterrupted):
            session.capture_once(wait_s=5.0, stop_requested=lambda: True)
    finally:
        session.stop()
