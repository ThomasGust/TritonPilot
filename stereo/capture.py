"""Synchronized stereo-pair capture for calibration and later analysis."""

from __future__ import annotations

import json
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

import numpy as np

from recording.capture_paths import safe_filename_component
from recording.save_location import DEFAULT_RECORDINGS_DIR
from recording.video_recorder import save_snapshot
from stereo.pairs import StereoPairConfig
from video.cam import CameraFramePacket, RemoteCameraManager
from video.frame_rotation import rotate_frame


class StereoCaptureError(RuntimeError):
    """Raised when a stereo capture cannot be completed."""


class StereoCaptureSession:
    """Capture timestamped left/right image pairs from existing ROV streams."""

    MANIFEST_VERSION = 1

    def __init__(
        self,
        manager: RemoteCameraManager,
        pair: StereoPairConfig,
        *,
        output_root: str | Path | None = None,
        session_name: str | None = None,
        close_on_stop: bool = False,
    ):
        self.manager = manager
        self.pair = pair
        self.output_root = Path(output_root or DEFAULT_RECORDINGS_DIR)
        self.session_name = session_name or time.strftime("%Y%m%d-%H%M%S")
        self.close_on_stop = bool(close_on_stop)
        self.session_dir = self.output_root / "stereo_sessions" / safe_filename_component(self.session_name, "session")
        self.left_dir = self.session_dir / "left"
        self.right_dir = self.session_dir / "right"
        self._left_camera = None
        self._right_camera = None
        self._frame_index = 0
        self._last_left_seq: int | None = None
        self._last_right_seq: int | None = None
        self._last_pair_key: tuple[int, int] | None = None
        self._started_wall_ts: float | None = None
        self._manifest: dict[str, Any] = {}

    @property
    def manifest_path(self) -> Path:
        return self.session_dir / "manifest.json"

    def start(self) -> Path:
        """Open both streams and initialize the on-disk session."""

        self.left_dir.mkdir(parents=True, exist_ok=True)
        self.right_dir.mkdir(parents=True, exist_ok=True)
        self._started_wall_ts = time.time()
        self._left_camera = self.manager.open(self.pair.left)
        self._right_camera = self.manager.open(self.pair.right)
        self._manifest = self._base_manifest()
        self._write_manifest()
        return self.session_dir

    def stop(self) -> None:
        """Flush the manifest. Stream ownership remains with the camera manager."""

        if self._manifest:
            self._manifest["ended_wall_ts"] = time.time()
            self._write_manifest()
        if self.close_on_stop:
            for stream_name in (self.pair.left, self.pair.right):
                try:
                    self.manager.close(stream_name)
                except Exception:
                    pass

    def capture_once(self, *, wait_s: float = 2.0, require_fresh: bool = True) -> dict[str, Any]:
        """Capture one stereo pair whose receiver timestamps are close enough."""

        if self._left_camera is None or self._right_camera is None:
            raise StereoCaptureError("StereoCaptureSession.start() must be called before capture")

        deadline = time.monotonic() + max(0.0, float(wait_s))
        best_delta_s: float | None = None

        while time.monotonic() <= deadline:
            match = self._best_recent_pair(require_fresh=require_fresh)
            if match is None:
                time.sleep(0.005)
                continue
            left, right = match
            delta_s = abs(float(left.monotonic_ts) - float(right.monotonic_ts))
            best_delta_s = delta_s if best_delta_s is None else min(best_delta_s, delta_s)
            if delta_s <= self.pair.max_pair_delta_s:
                return self._save_pair(left, right, delta_s)
            time.sleep(0.002)

        detail = "no frames received" if best_delta_s is None else f"best delta {best_delta_s * 1000.0:.1f} ms"
        raise StereoCaptureError(
            f"Could not capture stereo pair '{self.pair.name}' within "
            f"{self.pair.max_pair_delta_ms:.1f} ms ({detail})"
        )

    def capture_burst(self, count: int, *, interval_s: float = 0.25, wait_s: float = 2.0) -> list[dict[str, Any]]:
        """Capture a timed burst of stereo pairs."""

        captures: list[dict[str, Any]] = []
        for idx in range(max(0, int(count))):
            captures.append(self.capture_once(wait_s=wait_s, require_fresh=True))
            if idx < int(count) - 1:
                time.sleep(max(0.0, float(interval_s)))
        return captures

    def _recent_packets(self, camera) -> list[CameraFramePacket]:
        recent = getattr(camera, "recent_frame_packets", None)
        if callable(recent):
            packets = recent(max_age_s=max(0.25, self.pair.max_pair_delta_s + 0.2))
            if packets:
                return list(packets)
        latest = camera.latest_frame_packet()
        return [] if latest is None else [latest]

    def _best_recent_pair(self, *, require_fresh: bool) -> tuple[CameraFramePacket, CameraFramePacket] | None:
        left_frames = self._recent_packets(self._left_camera)
        right_frames = self._recent_packets(self._right_camera)
        best: tuple[float, CameraFramePacket, CameraFramePacket] | None = None
        for left in left_frames:
            for right in right_frames:
                pair_key = (int(left.seq), int(right.seq))
                if require_fresh and pair_key == self._last_pair_key:
                    continue
                # Avoid repeatedly pairing either side with the same old frame
                # when both cameras are live but one side briefly stalls.
                if require_fresh and (left.seq == self._last_left_seq or right.seq == self._last_right_seq):
                    continue
                delta_s = abs(float(left.monotonic_ts) - float(right.monotonic_ts))
                if best is None or delta_s < best[0]:
                    best = (delta_s, left, right)
        if best is None:
            return None
        return best[1], best[2]

    def _stream_def(self, name: str) -> dict[str, Any]:
        try:
            return dict(self.manager.stream_defs.get(name, {}) or {})
        except Exception:
            return {}

    def _base_manifest(self) -> dict[str, Any]:
        return {
            "schema": "tritonpilot.stereo_capture_manifest",
            "schema_version": self.MANIFEST_VERSION,
            "session_name": self.session_name,
            "started_wall_ts": self._started_wall_ts,
            "pair": asdict(self.pair),
            "streams": {
                "left": self._stream_def(self.pair.left),
                "right": self._stream_def(self.pair.right),
            },
            "capture_notes": {
                "timestamp_source": "topside receiver time after decoded frame read",
                "sync_quality": "best effort; exploreHD is rolling shutter and lacks external frame sync",
            },
            "frames": [],
        }

    def _write_manifest(self) -> None:
        self.session_dir.mkdir(parents=True, exist_ok=True)
        with self.manifest_path.open("w", encoding="utf-8") as f:
            json.dump(self._manifest, f, indent=2, sort_keys=True)
            f.write("\n")

    def _prepared_frame(self, packet: CameraFramePacket, stream_name: str) -> np.ndarray:
        frame = np.ascontiguousarray(packet.frame_bgr)
        if self.pair.apply_stream_rotation:
            rotation_deg = int(self._stream_def(stream_name).get("rotation_deg", 0) or 0)
            if rotation_deg:
                frame = rotate_frame(frame, rotation_deg)
        return frame

    def _save_pair(self, left: CameraFramePacket, right: CameraFramePacket, delta_s: float) -> dict[str, Any]:
        self._frame_index += 1
        stem = f"pair_{self._frame_index:06d}"
        left_path = self.left_dir / f"{stem}_left.png"
        right_path = self.right_dir / f"{stem}_right.png"

        left_frame = self._prepared_frame(left, self.pair.left)
        right_frame = self._prepared_frame(right, self.pair.right)
        save_snapshot(left_frame, left_path)
        save_snapshot(right_frame, right_path)
        if not left_path.exists() or not right_path.exists():
            raise StereoCaptureError("Failed to write one or both stereo images")

        self._last_left_seq = int(left.seq)
        self._last_right_seq = int(right.seq)
        self._last_pair_key = (int(left.seq), int(right.seq))
        record = {
            "index": self._frame_index,
            "stem": stem,
            "left_path": str(left_path.relative_to(self.session_dir)),
            "right_path": str(right_path.relative_to(self.session_dir)),
            "pair_delta_ms": float(delta_s * 1000.0),
            "left": {
                "stream": self.pair.left,
                "seq": int(left.seq),
                "wall_ts": float(left.wall_ts),
                "monotonic_ts": float(left.monotonic_ts),
                "shape": list(left_frame.shape),
            },
            "right": {
                "stream": self.pair.right,
                "seq": int(right.seq),
                "wall_ts": float(right.wall_ts),
                "monotonic_ts": float(right.monotonic_ts),
                "shape": list(right_frame.shape),
            },
        }
        self._manifest.setdefault("frames", []).append(record)
        self._write_manifest()
        return record
