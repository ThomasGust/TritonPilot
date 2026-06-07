"""Synchronized stereo-pair capture for calibration and later analysis."""

from __future__ import annotations

import json
import threading
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable

import numpy as np

from recording.capture_paths import safe_filename_component
from recording.save_location import DEFAULT_RECORDINGS_DIR
from recording.video_recorder import save_snapshot
from stereo.pairs import StereoPairConfig
from video.cam import CameraFramePacket, RemoteCameraManager
from video.frame_rotation import rotate_frame


class StereoCaptureError(RuntimeError):
    """Raised when a stereo capture cannot be completed."""


class StereoCaptureInterrupted(StereoCaptureError):
    """Raised when an in-progress stereo capture is stopped by the caller."""


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
        self._manifest_dirty = False
        self._last_manifest_write_ts = 0.0
        self._manifest_flush_interval_s = 1.0
        self._using_capture_receivers = False

    @property
    def manifest_path(self) -> Path:
        return self.session_dir / "manifest.json"

    def start(self) -> Path:
        """Open both streams and initialize the on-disk session."""

        self.left_dir.mkdir(parents=True, exist_ok=True)
        self.right_dir.mkdir(parents=True, exist_ok=True)
        self._started_wall_ts = time.time()
        open_capture = getattr(self.manager, "open_capture", None)
        if callable(open_capture):
            self._left_camera = open_capture(self.pair.left)
            self._right_camera = open_capture(self.pair.right)
            self._using_capture_receivers = True
        else:
            self._left_camera = self.manager.open(self.pair.left)
            self._right_camera = self.manager.open(self.pair.right)
            self._using_capture_receivers = False
        self._manifest = self._load_existing_manifest() or self._base_manifest()
        self._resume_frame_state()
        self._write_manifest()
        return self.session_dir

    def stop(self) -> None:
        """Flush the manifest. Stream ownership remains with the camera manager."""

        if self._manifest:
            self._manifest["ended_wall_ts"] = time.time()
            self._manifest_dirty = True
            self._write_manifest()
        if self.close_on_stop:
            for stream_name in (self.pair.left, self.pair.right):
                try:
                    if self._using_capture_receivers and callable(getattr(self.manager, "close_capture", None)):
                        self.manager.close_capture(stream_name)
                    else:
                        self.manager.close(stream_name)
                except Exception:
                    pass
        elif self._using_capture_receivers and callable(getattr(self.manager, "close_capture", None)):
            for stream_name in (self.pair.left, self.pair.right):
                try:
                    self.manager.close_capture(stream_name)
                except Exception:
                    pass

    def capture_once(
        self,
        *,
        wait_s: float = 2.0,
        require_fresh: bool = True,
        flush_manifest: bool = True,
        stop_requested: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        """Capture one stereo pair whose receiver timestamps are close enough."""

        if self._left_camera is None or self._right_camera is None:
            raise StereoCaptureError("StereoCaptureSession.start() must be called before capture")

        deadline = time.monotonic() + max(0.0, float(wait_s))
        best_delta_s: float | None = None

        while time.monotonic() <= deadline:
            if stop_requested is not None and stop_requested():
                raise StereoCaptureInterrupted("Stereo capture stopped")
            match = self._best_recent_pair(require_fresh=require_fresh)
            if match is None:
                time.sleep(0.005)
                continue
            left, right = match
            delta_s = abs(float(left.monotonic_ts) - float(right.monotonic_ts))
            best_delta_s = delta_s if best_delta_s is None else min(best_delta_s, delta_s)
            if delta_s <= self.pair.max_pair_delta_s:
                return self._save_pair(
                    left,
                    right,
                    delta_s,
                    flush_manifest=flush_manifest,
                )
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

    def _load_existing_manifest(self) -> dict[str, Any] | None:
        if not self.manifest_path.exists():
            return None
        try:
            manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise StereoCaptureError(f"Could not read existing stereo manifest: {exc}") from exc
        if manifest.get("schema") != "tritonpilot.stereo_capture_manifest":
            raise StereoCaptureError("Existing stereo manifest has an unexpected schema")

        existing_pair = manifest.get("pair") or {}
        mismatches = []
        for key in ("name", "left", "right", "rig_id"):
            if str(existing_pair.get(key, "")) != str(getattr(self.pair, key, "")):
                mismatches.append(key)
        if mismatches:
            raise StereoCaptureError(
                "Existing stereo session uses a different pair; start a new session before changing "
                + ", ".join(mismatches)
            )
        manifest.setdefault("frames", [])
        manifest.setdefault("started_wall_ts", self._started_wall_ts)
        return manifest

    def _resume_frame_state(self) -> None:
        frames = self._manifest.get("frames") or []
        self._frame_index = 0
        self._last_left_seq = None
        self._last_right_seq = None
        self._last_pair_key = None
        for frame in frames:
            try:
                self._frame_index = max(self._frame_index, int(frame.get("index", 0)))
            except Exception:
                pass
        if frames:
            last = frames[-1]
            left = last.get("left") or {}
            right = last.get("right") or {}
            try:
                self._last_left_seq = int(left.get("seq"))
                self._last_right_seq = int(right.get("seq"))
                self._last_pair_key = (self._last_left_seq, self._last_right_seq)
            except Exception:
                self._last_left_seq = None
                self._last_right_seq = None
                self._last_pair_key = None

    def _write_manifest(self) -> None:
        self.session_dir.mkdir(parents=True, exist_ok=True)
        with self.manifest_path.open("w", encoding="utf-8") as f:
            json.dump(self._manifest, f, indent=2, sort_keys=True)
            f.write("\n")
        self._manifest_dirty = False
        self._last_manifest_write_ts = time.monotonic()

    def _flush_manifest_if_needed(self, *, force: bool = False) -> None:
        if not self._manifest_dirty:
            return
        if force or self._last_manifest_write_ts <= 0.0:
            self._write_manifest()
            return
        if (time.monotonic() - self._last_manifest_write_ts) >= self._manifest_flush_interval_s:
            self._write_manifest()

    def _prepared_frame(self, packet: CameraFramePacket, stream_name: str) -> np.ndarray:
        frame = np.ascontiguousarray(packet.frame_bgr)
        if self.pair.apply_stream_rotation:
            rotation_deg = int(self._stream_def(stream_name).get("rotation_deg", 0) or 0)
            if rotation_deg:
                frame = rotate_frame(frame, rotation_deg)
        return frame

    def _save_images(self, left_frame: np.ndarray, left_path: Path, right_frame: np.ndarray, right_path: Path) -> None:
        errors: list[str] = []

        def _write_one(frame: np.ndarray, path: Path) -> None:
            try:
                save_snapshot(frame, path)
            except Exception as exc:
                errors.append(f"{path.name}: {exc}")

        threads = [
            threading.Thread(target=_write_one, args=(left_frame, left_path)),
            threading.Thread(target=_write_one, args=(right_frame, right_path)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        if errors:
            raise StereoCaptureError("Failed to write stereo images: " + "; ".join(errors))

    def _save_pair(
        self,
        left: CameraFramePacket,
        right: CameraFramePacket,
        delta_s: float,
        *,
        flush_manifest: bool = True,
    ) -> dict[str, Any]:
        self._frame_index += 1
        stem = f"pair_{self._frame_index:06d}"
        left_path = self.left_dir / f"{stem}_left.png"
        right_path = self.right_dir / f"{stem}_right.png"

        left_frame = self._prepared_frame(left, self.pair.left)
        right_frame = self._prepared_frame(right, self.pair.right)
        self._save_images(left_frame, left_path, right_frame, right_path)
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
        self._manifest_dirty = True
        self._flush_manifest_if_needed(force=flush_manifest)
        return record
