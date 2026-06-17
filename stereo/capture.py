"""ROV-side still-image stereo capture sessions."""

from __future__ import annotations

import json
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from recording.capture_trace import trace_event
from recording.save_location import DEFAULT_RECORDINGS_DIR
from stereo.pairs import StereoPairConfig
from video.cam import RemoteCameraManager, SnapshotImagePacket, StereoImagePairPacket


class StereoCaptureError(RuntimeError):
    """Raised when a stereo still capture cannot be completed."""


def safe_filename_component(value: object, fallback: str = "session") -> str:
    text = str(value or "").strip()
    chars: list[str] = []
    last_was_sep = False
    for ch in text:
        if ("A" <= ch <= "Z") or ("a" <= ch <= "z") or ("0" <= ch <= "9") or ch in {".", "-"}:
            chars.append(ch)
            last_was_sep = False
        else:
            if not last_was_sep:
                chars.append("_")
                last_was_sep = True
    safe = "".join(chars).strip("._-")
    return safe or fallback


def default_stereo_session_name(*, now: float | None = None) -> str:
    ts = time.time() if now is None else float(now)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(ts))
    millis = int(max(0.0, min(0.999, ts - int(ts))) * 1000.0)
    return f"{stamp}-{millis:03d}"


class StereoCaptureSession:
    """Save one-at-a-time left/right still pairs under a stereo session folder."""

    MANIFEST_VERSION = 1

    def __init__(
        self,
        manager: RemoteCameraManager,
        pair: StereoPairConfig,
        *,
        output_root: str | Path | None = None,
        session_name: str | None = None,
    ):
        self.manager = manager
        self.pair = pair
        self.output_root = Path(output_root or DEFAULT_RECORDINGS_DIR)
        self.session_name = safe_filename_component(session_name or default_stereo_session_name(), "session")
        self.session_dir = self.output_root / "stereo_sessions" / self.session_name
        self.left_dir = self.session_dir / "left"
        self.right_dir = self.session_dir / "right"
        self._started_wall_ts: float | None = None
        self._frame_index = 0
        self._manifest: dict[str, Any] = {}
        self._manifest_dirty = False

    @property
    def manifest_path(self) -> Path:
        return self.session_dir / "manifest.json"

    def start(self) -> Path:
        self.left_dir.mkdir(parents=True, exist_ok=True)
        self.right_dir.mkdir(parents=True, exist_ok=True)
        self._started_wall_ts = time.time()
        self._manifest = self._load_existing_manifest() or self._base_manifest()
        self._resume_frame_index()
        self._write_manifest()
        trace_event(
            "stereo_session_started",
            pair=self.pair.name,
            session_dir=str(self.session_dir),
            frame_index=self._frame_index,
        )
        return self.session_dir

    def stop(self) -> None:
        if not self._manifest:
            return
        self._manifest["ended_wall_ts"] = time.time()
        self._manifest_dirty = True
        self._write_manifest()
        trace_event(
            "stereo_session_stopped",
            pair=self.pair.name,
            session_dir=str(self.session_dir),
            frame_index=self._frame_index,
        )

    def capture_once(self, *, wait_s: float = 2.0, flush_manifest: bool = True) -> dict[str, Any]:
        if not self._manifest:
            raise StereoCaptureError("StereoCaptureSession.start() must be called before capture")
        capture_start_s = time.monotonic()
        trace_event(
            "stereo_capture_once_start",
            pair=self.pair.name,
            next_index=self._frame_index + 1,
            wait_s=wait_s,
            max_pair_delta_ms=self.pair.max_pair_delta_ms,
        )
        capture = getattr(self.manager, "capture_onboard_stereo_pair", None)
        if not callable(capture):
            raise StereoCaptureError("ROV onboard stereo capture is not available")
        try:
            packet = capture(
                self.pair.left,
                self.pair.right,
                timeout_s=float(wait_s),
                max_pair_delta_ms=float(self.pair.max_pair_delta_ms),
            )
        except Exception as exc:
            raise StereoCaptureError(f"ROV onboard stereo capture failed: {exc}") from exc
        record = self._save_packet_pair(packet)
        if flush_manifest:
            self._write_manifest()
        trace_event(
            "stereo_capture_once_done",
            pair=self.pair.name,
            index=record.get("index"),
            pair_delta_ms=record.get("pair_delta_ms"),
            dt_ms=(time.monotonic() - capture_start_s) * 1000.0,
        )
        return record

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
                "timestamp_source": "rov_snapshot_appsink_fresh_monotonic",
                "sync_quality": "best effort; paired ROV-side fresh snapshot pulls without external camera trigger",
                "quality_gate": "captures from TritonOS onboard decoded JPEG snapshot branches, not from the display widget",
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
            raise StereoCaptureError("Existing stereo session uses a different pair: " + ", ".join(mismatches))
        manifest.setdefault("frames", [])
        manifest.setdefault("started_wall_ts", self._started_wall_ts)
        manifest.setdefault("capture_notes", {})["timestamp_source"] = "rov_snapshot_appsink_fresh_monotonic"
        return manifest

    def _resume_frame_index(self) -> None:
        self._frame_index = 0
        for frame in self._manifest.get("frames") or []:
            try:
                self._frame_index = max(self._frame_index, int(frame.get("index", 0)))
            except Exception:
                pass

    def _write_manifest(self) -> None:
        self.session_dir.mkdir(parents=True, exist_ok=True)
        with self.manifest_path.open("w", encoding="utf-8") as f:
            json.dump(self._manifest, f, indent=2, sort_keys=True)
            f.write("\n")
        self._manifest_dirty = False

    @staticmethod
    def _extension_for_packet(packet: SnapshotImagePacket) -> str:
        extension = str(packet.extension or "").strip().lower().lstrip(".")
        if not extension:
            extension = "jpg" if str(packet.mime_type).lower() == "image/jpeg" else "bin"
        extension = "".join(ch for ch in extension if ("a" <= ch <= "z") or ("0" <= ch <= "9"))
        return extension or "jpg"

    @staticmethod
    def _manifest_path(path: Path, session_dir: Path) -> str:
        return str(path.relative_to(session_dir)).replace("/", "\\")

    def _packet_shape(self, packet: SnapshotImagePacket, stream_name: str) -> list[int]:
        if packet.shape:
            return [int(v) for v in packet.shape]
        stream = self._stream_def(stream_name)
        try:
            height = int(stream.get("height") or 0)
            width = int(stream.get("width") or 0)
        except Exception:
            return []
        if height > 0 and width > 0:
            return [height, width, 3]
        return []

    def _packet_metadata(self, packet: SnapshotImagePacket, stream_name: str) -> dict[str, Any]:
        meta: dict[str, Any] = {
            "stream": stream_name,
            "seq": int(packet.seq or 0),
            "shape": self._packet_shape(packet, stream_name),
            "wall_ts": float(packet.wall_ts),
            "monotonic_ts": float(packet.monotonic_ts),
        }
        if packet.source_pts_ns is not None:
            meta["source_pts_ns"] = int(packet.source_pts_ns)
        if packet.source_dts_ns is not None:
            meta["source_dts_ns"] = int(packet.source_dts_ns)
        if packet.source_duration_ns is not None:
            meta["source_duration_ns"] = int(packet.source_duration_ns)
        return meta

    def _save_packet_pair(self, packet: StereoImagePairPacket) -> dict[str, Any]:
        self._frame_index += 1
        stem = f"pair_{self._frame_index:06d}"
        left_ext = self._extension_for_packet(packet.left)
        right_ext = self._extension_for_packet(packet.right)
        left_path = self.left_dir / f"{stem}_left.{left_ext}"
        right_path = self.right_dir / f"{stem}_right.{right_ext}"
        left_path.write_bytes(packet.left.image_bytes)
        right_path.write_bytes(packet.right.image_bytes)
        record = {
            "index": self._frame_index,
            "stem": stem,
            "left_path": self._manifest_path(left_path, self.session_dir),
            "right_path": self._manifest_path(right_path, self.session_dir),
            "pair_delta_ms": float(packet.pair_delta_ms),
            "save_pending": False,
            "left": self._packet_metadata(packet.left, self.pair.left),
            "right": self._packet_metadata(packet.right, self.pair.right),
        }
        self._manifest.setdefault("frames", []).append(record)
        self._manifest.setdefault("capture_notes", {})["timestamp_source"] = str(
            packet.timestamp_source or "rov_snapshot_appsink_fresh_monotonic"
        )
        self._manifest_dirty = True
        return record
