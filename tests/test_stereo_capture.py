import json
from pathlib import Path

import pytest

from stereo.capture import StereoCaptureSession
from stereo.pairs import load_stereo_pairs
from video.cam import SnapshotImagePacket, StereoImagePairPacket


def _write_stereo_config(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "streams": [
                    {"name": "Primary Camera", "width": 1920, "height": 1080},
                    {"name": "Aux Camera", "width": 1920, "height": 1080},
                ],
                "stereo_pairs": [
                    {
                        "name": "Forward Stereo",
                        "left": "Primary Camera",
                        "right": "Aux Camera",
                        "rig_id": "explorehd_forward_v1",
                        "max_pair_delta_ms": 50,
                        "metadata": {"camera_model": "DeepWater Exploration exploreHD 3.0"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


class _FakeManager:
    def __init__(self):
        self.calls = []
        self.stream_defs = {
            "Primary Camera": {"name": "Primary Camera", "width": 1920, "height": 1080},
            "Aux Camera": {"name": "Aux Camera", "width": 1920, "height": 1080},
        }

    def capture_onboard_stereo_pair(self, left, right, *, timeout_s=2.0, max_pair_delta_ms=50.0):
        self.calls.append(
            {
                "left": left,
                "right": right,
                "timeout_s": timeout_s,
                "max_pair_delta_ms": max_pair_delta_ms,
            }
        )
        return StereoImagePairPacket(
            left=SnapshotImagePacket(
                source_name=left,
                image_bytes=b"left-jpeg",
                mime_type="image/jpeg",
                extension="jpg",
                wall_ts=1000.000,
                monotonic_ts=50.000,
                byte_count=len(b"left-jpeg"),
                seq=101,
                shape=(1080, 1920, 3),
                source_pts_ns=123456,
            ),
            right=SnapshotImagePacket(
                source_name=right,
                image_bytes=b"right-jpeg",
                mime_type="image/jpeg",
                extension="jpg",
                wall_ts=1000.008,
                monotonic_ts=50.008,
                byte_count=len(b"right-jpeg"),
                seq=202,
                shape=(1080, 1920, 3),
                source_pts_ns=124456,
            ),
            pair_delta_ms=8.0,
            timestamp_source="rov_snapshot_appsink_fresh_monotonic",
            attempts=1,
        )


def test_load_stereo_pairs_from_streams_config(tmp_path: Path):
    cfg_path = tmp_path / "streams.json"
    _write_stereo_config(cfg_path)

    pairs = load_stereo_pairs(cfg_path)

    assert len(pairs) == 1
    assert pairs[0].name == "Forward Stereo"
    assert pairs[0].left == "Primary Camera"
    assert pairs[0].right == "Aux Camera"
    assert pairs[0].max_pair_delta_ms == 50


def test_stereo_capture_session_writes_rov_pair_and_manifest(tmp_path: Path):
    cfg_path = tmp_path / "streams.json"
    _write_stereo_config(cfg_path)
    pair = load_stereo_pairs(cfg_path)[0]
    manager = _FakeManager()
    session = StereoCaptureSession(manager, pair, output_root=tmp_path, session_name="pool-run")

    session.start()
    record = session.capture_once(wait_s=0.25)
    session.stop()

    assert manager.calls == [
        {
            "left": "Primary Camera",
            "right": "Aux Camera",
            "timeout_s": 0.25,
            "max_pair_delta_ms": 50.0,
        }
    ]
    assert record["pair_delta_ms"] == pytest.approx(8.0)
    assert record["left_path"] == "left\\pair_000001_left.jpg"
    assert record["right_path"] == "right\\pair_000001_right.jpg"
    assert (session.session_dir / record["left_path"]).read_bytes() == b"left-jpeg"
    assert (session.session_dir / record["right_path"]).read_bytes() == b"right-jpeg"

    manifest = json.loads(session.manifest_path.read_text(encoding="utf-8"))
    assert manifest["schema"] == "tritonpilot.stereo_capture_manifest"
    assert manifest["schema_version"] == 1
    assert manifest["pair"]["rig_id"] == "explorehd_forward_v1"
    assert manifest["streams"]["left"]["name"] == "Primary Camera"
    assert manifest["streams"]["right"]["name"] == "Aux Camera"
    assert manifest["capture_notes"]["timestamp_source"] == "rov_snapshot_appsink_fresh_monotonic"
    assert len(manifest["frames"]) == 1
    assert manifest["frames"][0]["left"]["seq"] == 101
    assert manifest["frames"][0]["right"]["seq"] == 202
    assert manifest["frames"][0]["left"]["source_pts_ns"] == 123456


def test_stereo_capture_session_appends_existing_manifest(tmp_path: Path):
    cfg_path = tmp_path / "streams.json"
    _write_stereo_config(cfg_path)
    pair = load_stereo_pairs(cfg_path)[0]
    manager = _FakeManager()

    first = StereoCaptureSession(manager, pair, output_root=tmp_path, session_name="append-run")
    first.start()
    first.capture_once(wait_s=0.25)
    first.stop()

    second = StereoCaptureSession(manager, pair, output_root=tmp_path, session_name="append-run")
    second.start()
    record = second.capture_once(wait_s=0.25)
    second.stop()

    manifest = json.loads(second.manifest_path.read_text(encoding="utf-8"))
    assert record["index"] == 2
    assert len(manifest["frames"]) == 2
    assert manifest["frames"][0]["stem"] == "pair_000001"
    assert manifest["frames"][1]["stem"] == "pair_000002"
