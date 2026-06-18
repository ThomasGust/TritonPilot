"""Remote camera wrappers for ROV RTP streams.

This module bridges the TritonOS video RPC service and the local GStreamer
receiver process. Callers get a small OpenCV-like camera object while the
implementation handles stream startup, local UDP binding, frame reads, and
cleanup.
"""

import base64
import json
import logging
import socket
import threading
import time
from dataclasses import dataclass

import numpy as np

from network.net_select import parse_zmq_endpoint, choose_video_receive_ip
from config import VIDEO_RPC_ENDPOINT
from recording.capture_trace import trace_event
from video.frame_quality import live_frame_rejection_reason
from video.gst_receiver import ReceiverProcess, RxConfig
from video.frame_rotation import normalize_rotation_deg
from video.rov_streams import ROVStreams

logger = logging.getLogger(__name__)


def _stream_int(stream_opts: dict, *names: str, default: int) -> int:
    for name in names:
        if name not in stream_opts or stream_opts.get(name) is None:
            continue
        try:
            return int(float(stream_opts.get(name)))
        except Exception:
            continue
    return int(default)


def _stream_bool(stream_opts: dict, *names: str, default: bool) -> bool:
    for name in names:
        if name not in stream_opts or stream_opts.get(name) is None:
            continue
        value = stream_opts.get(name)
        if isinstance(value, bool):
            return bool(value)
        text = str(value).strip().lower()
        if text in {"1", "true", "yes", "on"}:
            return True
        if text in {"0", "false", "no", "off"}:
            return False
    return bool(default)


def _stream_float(stream_opts: dict, *names: str, default: float) -> float:
    for name in names:
        if name not in stream_opts or stream_opts.get(name) is None:
            continue
        try:
            return float(stream_opts.get(name))
        except Exception:
            continue
    return float(default)


def _coerce_port_list(value) -> list[int]:
    if value is None:
        return []
    if isinstance(value, str):
        raw_values = [part.strip() for part in value.split(",") if part.strip()]
    elif isinstance(value, (list, tuple, set)):
        raw_values = list(value)
    else:
        raw_values = [value]
    ports: list[int] = []
    for raw in raw_values:
        try:
            port = int(float(raw))
        except Exception:
            continue
        if 0 < port <= 65535 and port not in ports:
            ports.append(port)
    return ports


def _endpoint_for_rov(rov: ROVStreams) -> str:
    return str(getattr(rov, "endpoint", VIDEO_RPC_ENDPOINT) or VIDEO_RPC_ENDPOINT)


@dataclass(frozen=True)
class CameraFramePacket:
    """Decoded camera frame with receiver-side timing metadata."""

    source_name: str
    frame_bgr: np.ndarray
    seq: int
    monotonic_ts: float
    wall_ts: float


@dataclass(frozen=True)
class SnapshotImagePacket:
    """Compressed still image captured by TritonOS."""

    source_name: str
    image_bytes: bytes
    mime_type: str
    extension: str
    wall_ts: float
    monotonic_ts: float
    byte_count: int
    caps: str = ""
    seq: int = 0
    shape: tuple[int, ...] = ()
    source_pts_ns: int | None = None
    source_dts_ns: int | None = None
    source_duration_ns: int | None = None
    source_monotonic_ts: float | None = None
    capture_source: str = ""


@dataclass(frozen=True)
class StereoImagePairPacket:
    """Compressed left/right still images captured together by TritonOS."""

    left: SnapshotImagePacket
    right: SnapshotImagePacket
    pair_delta_ms: float
    timestamp_source: str
    attempts: int = 1


class RemoteCv2Camera:
    """OpenCV-like reader for one ROV camera stream."""

    def __init__(
        self,
        rov: ROVStreams,
        name: str,
        device: str,
        width: int,
        height: int,
        fps: int,
        video_format: str = "mjpeg",
        port: int = 5000,
        codec: str = "jpeg",     # must match video_format or what you send
        latency_ms: int = 60,
        channel_order: str = "BGR",
        windows_host: str | None = None,
        stream_opts: dict | None = None,
    ):
        self.rov = rov
        self.name = name
        self.device = device
        self.width = width
        self.height = height
        self.fps = fps
        self.video_format = video_format
        self.port = port
        self.codec = codec
        self.latency_ms = latency_ms
        self.channel_order = channel_order
        self.rotation_deg = normalize_rotation_deg(stream_opts.get("rotation_deg", 0) if stream_opts else 0)
        self._last_rejected_artifact_seq: int | None = None

        # Populated if the ROV had to perform recovery actions (e.g., USB rebind)
        self.start_messages: list[str] = []

        # Detect the best local IP to receive video if not provided.
        # IMPORTANT: the previous approach used 8.8.8.8, which tends to pick Wi-Fi.
        # Here we select the local IP that can reach the ROV video RPC host,
        # preferring wired/tether when possible.
        if windows_host is None:
            try:
                rov_host, rov_port = parse_zmq_endpoint(_endpoint_for_rov(rov))
                prefer_wired = bool(stream_opts.get("tether_prefer_wired", True)) if stream_opts else True
                windows_host = choose_video_receive_ip(
                    remote_host=rov_host,
                    remote_port=int(rov_port),
                    prefer_wired=prefer_wired,
                    require_private=True,
                )
            except Exception:
                # fallback to the OS-chosen route (still better than 8.8.8.8)
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                try:
                    rov_host, _rov_port = parse_zmq_endpoint(_endpoint_for_rov(rov))
                    s.connect((rov_host, 9))
                    windows_host = s.getsockname()[0]
                finally:
                    s.close()
        self.windows_host = windows_host

        stream_opts = stream_opts or {}
        # Allow config to override receiver-side jitter buffer setting.
        self.latency_ms = int(stream_opts.get("latency_ms", self.latency_ms))

        start_kwargs = dict(
            name=self.name,
            device=self.device,
            width=self.width,
            height=self.height,
            fps=self.fps,
            video_format=self.video_format,
            host=self.windows_host,
            port=self.port,
        )

        # Pass through optional transcoding and transport knobs from config.
        for k in (
            "encode",
            "h264_bitrate",
            "h264_gop",
            "transport",
            "rtp_pt_jpeg",
            "rtp_pt_h264",
            "rtp_mtu",
            "latency_ms",
            "sync",
            "extra",
        ):
            if k in stream_opts and stream_opts[k] is not None:
                start_kwargs[k] = stream_opts[k]

        tx_is_h264 = (
            start_kwargs.get("video_format") == "h264"
            or str(start_kwargs.get("encode", "")).lower() == "h264"
        )

        # Start the local receiver before asking the ROV to transmit so every
        # visible stream has a UDP listener ready at nearly the same time.
        bind_rx = True
        if stream_opts and ("bind_receiver_to_host" in stream_opts):
            bind_rx = bool(stream_opts.get("bind_receiver_to_host"))

        rx_extra = {}
        configured_rx_extra = stream_opts.get("receiver_extra")
        if isinstance(configured_rx_extra, dict):
            rx_extra.update(configured_rx_extra)
        for key in (
            "frame_history_size",
            "receiver_h264_decoder",
            "h264_decoder",
            "receiver_output_fps",
            "output_fps",
        ):
            if key in stream_opts and stream_opts[key] is not None:
                rx_extra[key] = stream_opts[key]
        rx_extra.setdefault("source_fps", self.fps)

        rx_cfg = RxConfig(
            name=self.name,
            codec="h264" if tx_is_h264 else "jpeg",
            port=self.port,
            bind_address=self.windows_host if bind_rx else "0.0.0.0",
            latency_ms=self.latency_ms,
            mode="raw",
            width=self.width,
            height=self.height,
            channel_order=self.channel_order,
            udp_buffer_size=int(stream_opts.get("receiver_udp_buffer_size", 4 * 1024 * 1024)),
            drop_on_latency=bool(stream_opts.get("receiver_drop_on_latency", True)),
            extra=rx_extra,
        )
        self.rx = ReceiverProcess(rx_cfg)
        self.rx.start()

        try:
            resp = self.rov.start_stream(**start_kwargs)
        except Exception:
            try:
                self.rx.stop(grace_s=0.05)
            except Exception:
                pass
            raise

        if isinstance(resp, dict) and resp.get("messages"):
            try:
                self.start_messages = [str(m) for m in (resp.get("messages") or [])]
                for m in self.start_messages:
                    logger.warning("ROV video start notice (%s): %s", self.name, m)
            except Exception:
                self.start_messages = []

    def read(self):
        """Return ``(ok, frame)`` like ``cv2.VideoCapture.read``."""
        packet = self.read_frame_packet()
        if packet is None:
            return False, None
        return True, packet.frame_bgr

    def _decode_packet(self, packet) -> CameraFramePacket | None:
        img = np.frombuffer(packet.data, dtype=np.uint8).reshape((self.height, self.width, 3))
        rejection_reason = live_frame_rejection_reason(img)
        if rejection_reason is not None:
            try:
                seq = int(packet.seq)
            except Exception:
                seq = -1
            if getattr(self, "_last_rejected_artifact_seq", None) != seq:
                self._last_rejected_artifact_seq = seq
                trace_event(
                    "camera_frame_rejected",
                    stream=self.name,
                    seq=seq,
                    reason=rejection_reason,
                )
            return None
        return CameraFramePacket(
            source_name=self.name,
            frame_bgr=img,
            seq=int(packet.seq),
            monotonic_ts=float(packet.monotonic_ts),
            wall_ts=float(packet.wall_ts),
        )

    def read_frame_packet(self) -> CameraFramePacket | None:
        """Return the next unread decoded frame with timing metadata."""

        packet = self.rx.read_frame_packet()
        if packet is None:
            return None
        return self._decode_packet(packet)

    def latest_frame_packet(self) -> CameraFramePacket | None:
        """Return the latest decoded frame without consuming display delivery state."""

        packet = self.rx.latest_frame_packet()
        if packet is None:
            return None
        return self._decode_packet(packet)

    def recent_frame_packets(self, *, max_age_s: float = 0.5) -> list[CameraFramePacket]:
        """Return recent decoded frames without consuming display delivery state."""

        try:
            packets = self.rx.recent_frame_packets(max_age_s=max_age_s)
        except AttributeError:
            latest = self.latest_frame_packet()
            return [] if latest is None else [latest]
        decoded: list[CameraFramePacket] = []
        for packet in packets:
            decoded_packet = self._decode_packet(packet)
            if decoded_packet is not None:
                decoded.append(decoded_packet)
        return decoded

    def release(self, rx_grace_s: float = 0.15):
        """Stop the local receiver and ask TritonOS to stop transmitting."""
        # Stop local receiver first (tends to unblock quickly even if ROV is slow).
        try:
            self.rx.stop(grace_s=rx_grace_s)
        except Exception as e:
            logger.warning("Failed to stop local receiver for '%s': %s", self.name, e)

        # IMPORTANT: stop the ROV-side stream. The RPC expects a keyword arg "name".
        # A previous positional call here would throw a TypeError and get swallowed,
        # leaving streams running on the ROV and slowly overloading CPU/bandwidth.
        try:
            self.rov.stop_stream(name=self.name)
        except Exception as e:
            logger.warning("Failed to stop ROV stream '%s': %s", self.name, e)


class _SnapshotTap:
    """Warm decoded mirror for fast still snapshots from one stream."""

    def __init__(
        self,
        manager: "RemoteCameraManager",
        *,
        name: str,
        rx: ReceiverProcess,
        width: int,
        height: int,
        port: int,
        base_extra: dict,
        persistent: bool,
        idle_s: float,
        fresh_wait_s: float,
        reuse_max_age_s: float,
        mirror_check_interval_s: float,
    ):
        self.manager = manager
        self.name = str(name)
        self.rx = rx
        self.width = int(width)
        self.height = int(height)
        self.port = int(port)
        self.base_extra = dict(base_extra or {})
        self.persistent = bool(persistent)
        self.idle_s = max(0.0, float(idle_s))
        self.fresh_wait_s = max(0.0, float(fresh_wait_s))
        self.reuse_max_age_s = max(0.0, float(reuse_max_age_s))
        self.mirror_check_interval_s = max(0.0, float(mirror_check_interval_s))
        self._lock = threading.RLock()
        self._capture_lock = threading.Lock()
        self._started = False
        self._closed = False
        self._mirror_added = False
        self._last_mirror_check_mono = 0.0
        self._last_returned_seq = 0
        self._last_use_mono = time.monotonic()
        self._idle_timer: threading.Timer | None = None

    @property
    def closed(self) -> bool:
        with self._lock:
            return bool(self._closed)

    def make_persistent(self) -> None:
        with self._lock:
            self.persistent = True
            if self._idle_timer is not None:
                self._idle_timer.cancel()
                self._idle_timer = None

    def start(self) -> None:
        with self._lock:
            if self._closed:
                raise RuntimeError(f"snapshot tap for '{self.name}' is closed")
            if self._started:
                proc = getattr(self.rx, "proc", None)
                poll = getattr(proc, "poll", None)
                if proc is None or not callable(poll) or poll() is None:
                    try:
                        self._ensure_mirror_locked(force=False)
                    except Exception as exc:
                        logger.debug("Snapshot tap mirror check failed for '%s': %s", self.name, exc)
                    return
                logger.warning("Snapshot tap receiver for '%s' exited; restarting", self.name)
                self._started = False
            self.rx.start()
            try:
                self._ensure_mirror_locked(force=True)
            except Exception:
                try:
                    self.rx.stop(grace_s=0.05)
                except Exception:
                    pass
                raise
            self._started = True
            self._mirror_added = True
            trace_event("snapshot_tap_started", stream=self.name, port=self.port, persistent=self.persistent)
            self._touch_locked()

    def _mirror_port_present(self, extra: dict | None) -> bool:
        for key in ("udp_mirror_ports", "mirror_udp_ports"):
            if int(self.port) in _coerce_port_list((extra or {}).get(key)):
                return True
        return False

    def _ensure_mirror_locked(self, *, force: bool = False) -> None:
        now = time.monotonic()
        if not force and self.mirror_check_interval_s > 0.0:
            if now - float(self._last_mirror_check_mono) < self.mirror_check_interval_s:
                return
        self._last_mirror_check_mono = now

        latest_extra = self.manager._current_rov_stream_extra(self.name, self.base_extra)
        if self._mirror_port_present(latest_extra):
            self._mirror_added = True
            return

        updated_extra = self.manager._snapshot_extra_with_port(latest_extra, self.port)
        with self.manager._mgr_lock:
            self.manager.rov.update_stream(name=self.name, extra=updated_extra)
        self._mirror_added = True
        trace_event("snapshot_tap_mirror_added", stream=self.name, port=self.port, force=force)

    def _touch_locked(self) -> None:
        self._last_use_mono = time.monotonic()
        if self.persistent or self.idle_s <= 0.0 or self._closed:
            return
        if self._idle_timer is not None:
            self._idle_timer.cancel()
        timer = threading.Timer(self.idle_s, self.close_if_idle)
        timer.daemon = True
        self._idle_timer = timer
        timer.start()

    def close_if_idle(self) -> None:
        with self._lock:
            if self._closed or self.persistent:
                return
            idle_for = time.monotonic() - float(self._last_use_mono)
            if idle_for < self.idle_s:
                self._touch_locked()
                return
        self.close(reason="idle")

    def _packet_age_s(self, packet) -> float:
        try:
            return max(0.0, time.monotonic() - float(packet.monotonic_ts))
        except Exception:
            return 0.0

    def _usable_frame_from_packet(self, packet) -> tuple[np.ndarray | None, str | None]:
        img = np.frombuffer(packet.data, dtype=np.uint8).reshape((self.height, self.width, 3))
        rejection_reason = live_frame_rejection_reason(img)
        if rejection_reason is not None:
            return None, rejection_reason
        return img.copy(), None

    def _packet_to_camera_frame(self, packet, frame: np.ndarray, *, reused: bool) -> CameraFramePacket:
        seq = int(packet.seq)
        age_s = self._packet_age_s(packet)
        with self._lock:
            self._last_returned_seq = max(self._last_returned_seq, seq)
            self._touch_locked()
        trace_event(
            "snapshot_tap_frame_selected",
            stream=self.name,
            port=self.port,
            seq=seq,
            reused=bool(reused),
            age_s=age_s,
        )
        return CameraFramePacket(
            source_name=self.name,
            frame_bgr=frame,
            seq=seq,
            monotonic_ts=float(packet.monotonic_ts),
            wall_ts=float(packet.wall_ts),
        )

    def capture_frame(self, *, timeout_s: float) -> CameraFramePacket:
        self.start()
        mirror_warning = ""
        try:
            with self._lock:
                self._ensure_mirror_locked(force=True)
        except Exception as exc:
            mirror_warning = f"mirror check failed: {exc}"
            logger.warning("Snapshot mirror check failed for '%s': %s", self.name, exc)
            trace_event("snapshot_tap_mirror_check_failed", stream=self.name, port=self.port, error=str(exc))
        start_mono = time.monotonic()
        deadline = time.monotonic() + max(0.1, float(timeout_s))
        fresh_deadline = min(deadline, start_mono + self.fresh_wait_s)
        last_rejected = mirror_warning or "no frame"
        mirror_rechecked = False
        reusable_packet = None
        reusable_frame: np.ndarray | None = None

        # Serialize per-tap captures so repeated button presses produce a stream
        # of fresh frames when possible while still saving a recent decoded
        # packet if the raw pipe is temporarily starved.
        with self._capture_lock:
            min_seq = int(self._last_returned_seq)
            while time.monotonic() < deadline:
                with self._lock:
                    if self._closed:
                        raise RuntimeError(f"snapshot tap for '{self.name}' is closed")
                    rx = self.rx
                packet = rx.latest_frame_packet()
                if packet is not None:
                    seq = int(packet.seq)
                    frame, rejection_reason = self._usable_frame_from_packet(packet)
                    if rejection_reason is None and frame is not None:
                        if seq > min_seq:
                            return self._packet_to_camera_frame(packet, frame, reused=False)
                        last_rejected = "waiting for a fresh frame"
                        if self._packet_age_s(packet) <= self.reuse_max_age_s:
                            reusable_packet = packet
                            reusable_frame = frame
                    else:
                        last_rejected = rejection_reason
                else:
                    last_rejected = "no frame"

                now = time.monotonic()
                if reusable_packet is not None and reusable_frame is not None and now >= fresh_deadline:
                    return self._packet_to_camera_frame(reusable_packet, reusable_frame, reused=True)
                if not mirror_rechecked and now - start_mono >= 0.25:
                    mirror_rechecked = True
                    try:
                        with self._lock:
                            self._ensure_mirror_locked(force=True)
                    except Exception as exc:
                        last_rejected = f"mirror check failed: {exc}"
                time.sleep(0.01)
        if reusable_packet is not None and reusable_frame is not None:
            return self._packet_to_camera_frame(reusable_packet, reusable_frame, reused=True)
        with self._lock:
            self._touch_locked()
        raise TimeoutError(f"No usable snapshot frame received for '{self.name}' ({last_rejected})")

    def close(self, *, reason: str = "close") -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            mirror_added = bool(self._mirror_added)
            if self._idle_timer is not None:
                self._idle_timer.cancel()
                self._idle_timer = None

        if mirror_added:
            try:
                latest_extra = self.manager._current_rov_stream_extra(self.name, self.base_extra)
                with self.manager._mgr_lock:
                    self.manager.rov.update_stream(
                        name=self.name,
                        extra=self.manager._snapshot_extra_without_port(latest_extra, self.port),
                    )
            except Exception as exc:
                logger.warning("Failed to remove snapshot mirror for '%s' on UDP %s: %s", self.name, self.port, exc)
        try:
            logger.info("Closing snapshot tap '%s' port=%s reason=%s", self.name, self.port, reason)
            self.rx.stop(grace_s=0.05)
        except Exception:
            pass
        trace_event("snapshot_tap_closed", stream=self.name, port=self.port, reason=reason)
        self.manager._forget_snapshot_tap(self.name, self)


class RemoteCameraManager:
    """Load stream definitions and manage active ``RemoteCv2Camera`` objects."""

    def __init__(self, config_path: str):
        with open(config_path, "r") as f:
            cfg = json.load(f)

        # ROV RPC endpoint comes from config (single source of truth).
        self.rov = ROVStreams(endpoint=VIDEO_RPC_ENDPOINT)

        # Optional override for the topside host IP to receive UDP video.
        # If None, RemoteCv2Camera auto-detects the local IP.
        self.windows_host = cfg.get("windows_host")

        # Optional defaults that can be set at the top-level of streams.json.
        # Per-stream values can override these.
        self._defaults = {
            "tether_prefer_wired": bool(cfg.get("tether_prefer_wired", True)),
            "bind_receiver_to_host": bool(cfg.get("bind_receiver_to_host", True)),
        }
        for key in (
            "receiver_snapshot_output_fps",
            "snapshot_output_fps",
            "snapshot_fresh_wait_s",
            "snapshot_reuse_max_age_s",
            "snapshot_mirror_check_interval_s",
        ):
            if key in cfg and cfg[key] is not None:
                self._defaults[key] = cfg[key]
        self.snapshot_prewarm_count = max(0, _stream_int(cfg, "snapshot_prewarm_count", default=0))
        self.snapshot_tap_idle_s = _stream_float(cfg, "snapshot_tap_idle_s", default=8.0)
        try:
            self.default_layout_count = int(cfg.get("default_layout_count"))
        except Exception:
            self.default_layout_count = None
        self.stop_hidden_streams = cfg.get("stop_hidden_streams", None)

        self.stream_defs = {}
        for raw_stream in cfg.get("streams", []):
            stream = dict(raw_stream)
            stream["rotation_deg"] = normalize_rotation_deg(stream.get("rotation_deg", 0))
            self.stream_defs[stream["name"]] = stream
        requested_pane_order = cfg.get("default_pane_order", []) or []
        self.default_pane_order = [
            str(name).strip()
            for name in requested_pane_order
            if str(name).strip() in self.stream_defs
        ]
        # If a stream def omits "enabled", assume True.
        self._opened: dict[str, RemoteCv2Camera] = {}
        self._closing: dict[str, threading.Thread] = {}
        self._snapshot_taps: dict[str, _SnapshotTap] = {}
        self._name_locks: dict[str, threading.Lock] = {}
        # Serialize open/close across background connect workers. This avoids
        # racing on shared bookkeeping and on the single ROV video RPC REQ socket.
        self._mgr_lock = threading.RLock()

    def set_rpc_endpoint(self, endpoint: str, *, windows_host: str | None = None) -> bool:
        """Retarget future video RPC opens to a new ROV endpoint."""

        endpoint = str(endpoint or "").strip()
        if not endpoint:
            return False
        with self._mgr_lock:
            current_endpoint = str(getattr(self.rov, "endpoint", "") or "")
            current_host = str(self.windows_host or "")
            next_host = None if windows_host is None else str(windows_host).strip()
            if endpoint == current_endpoint and (next_host is None or next_host == current_host):
                return False

        self.close_snapshot_taps(reason="rpc_endpoint_changed")
        with self._mgr_lock:
            old_rov = self.rov
            self.rov = ROVStreams(endpoint=endpoint)
            if next_host:
                self.windows_host = next_host
        try:
            old_rov.close()
        except Exception:
            pass
        trace_event("camera_manager_rpc_endpoint_changed", endpoint=endpoint, windows_host=self.windows_host)
        return True

    def list_available(self):
        """Return enabled stream names in the configured default pane order."""
        enabled_names = [
            name
            for name, s in self.stream_defs.items()
            if s.get('enabled', True)
        ]
        ordered_names = [
            name
            for name in self.default_pane_order
            if name in enabled_names
        ]
        for name in enabled_names:
            if name not in ordered_names:
                ordered_names.append(name)
        return ordered_names

    def _lock_for_name(self, name: str) -> threading.Lock:
        with self._mgr_lock:
            lock = self._name_locks.get(name)
            if lock is None:
                lock = threading.Lock()
                self._name_locks[name] = lock
            return lock

    def _wait_for_pending_close(self, name: str, timeout_s: float = 5.0) -> None:
        deadline = time.monotonic() + max(0.0, float(timeout_s))
        while True:
            with self._mgr_lock:
                thread = self._closing.get(name)
            if thread is None:
                return
            remaining = max(0.0, deadline - time.monotonic())
            thread.join(timeout=remaining)
            with self._mgr_lock:
                if not thread.is_alive() and self._closing.get(name) is thread:
                    self._closing.pop(name, None)
                    return
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Timed out waiting for previous close of stream '{name}'")

    def open(self, name: str) -> RemoteCv2Camera:
        """Open a named stream or return the existing active camera."""
        name_lock = self._lock_for_name(name)
        with name_lock:
            self._wait_for_pending_close(name)
            with self._mgr_lock:
                if name in self._opened:
                    return self._opened[name]

                if name not in self.stream_defs:
                    raise KeyError(f"Unknown stream '{name}'")
                s = self.stream_defs[name]
                if not s.get('enabled', True):
                    raise ValueError(f"Stream '{name}' is disabled in config")

                # Merge stream options with top-level defaults.
                stream_opts = dict(self._defaults)
                stream_opts.update(s)

            cam = RemoteCv2Camera(
                rov=self.rov,
                name=s['name'],
                device=s['device'],
                width=s['width'],
                height=s['height'],
                fps=s['fps'],
                video_format=s.get('video_format', 'mjpeg'),
                port=s.get('port', 5000),
                windows_host=self.windows_host,
                stream_opts=stream_opts,
            )
            with self._mgr_lock:
                existing = self._opened.get(name)
                if existing is not None:
                    try:
                        cam.release()
                    except Exception:
                        pass
                    return existing
                self._opened[name] = cam
                return cam

    def _merged_stream_options(self, name: str) -> dict:
        if name not in self.stream_defs:
            raise KeyError(f"Unknown stream '{name}'")
        stream = self.stream_defs[name]
        if not stream.get("enabled", True):
            raise ValueError(f"Stream '{name}' is disabled in config")
        stream_opts = dict(self._defaults)
        stream_opts.update(stream)
        return stream_opts

    @staticmethod
    def _allocate_udp_port(bind_address: str | None = None) -> int:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            host = str(bind_address or "0.0.0.0").strip() or "0.0.0.0"
            sock.bind((host, 0))
            return int(sock.getsockname()[1])
        finally:
            sock.close()

    @staticmethod
    def _snapshot_extra_with_port(extra: dict | None, port: int) -> dict:
        updated = dict(extra or {})
        mirror_key = "udp_mirror_ports"
        if "udp_mirror_ports" not in updated and "mirror_udp_ports" in updated:
            mirror_key = "mirror_udp_ports"
        ports = _coerce_port_list(updated.get(mirror_key))
        if int(port) not in ports:
            ports.append(int(port))
        updated[mirror_key] = ports
        return updated

    @staticmethod
    def _snapshot_extra_without_port(extra: dict | None, port: int) -> dict:
        updated = dict(extra or {})
        for key in ("udp_mirror_ports", "mirror_udp_ports"):
            if key not in updated:
                continue
            ports = [p for p in _coerce_port_list(updated.get(key)) if p != int(port)]
            if ports:
                updated[key] = ports
            else:
                updated.pop(key, None)
        return updated

    def _current_rov_stream_extra(self, name: str, fallback: dict | None = None) -> dict:
        try:
            with self._mgr_lock:
                current = self.rov.list_status()
            if isinstance(current, dict):
                cfg = current.get(name)
                if isinstance(cfg, dict):
                    extra = cfg.get("extra")
                    if isinstance(extra, dict):
                        return dict(extra)
        except Exception:
            pass
        return dict(fallback or {})

    def _snapshot_windows_host(self, stream_opts: dict) -> str:
        windows_host = self.windows_host
        if windows_host is None:
            try:
                rov_host, rov_port = parse_zmq_endpoint(_endpoint_for_rov(self.rov))
                windows_host = choose_video_receive_ip(
                    remote_host=rov_host,
                    remote_port=int(rov_port),
                    prefer_wired=bool(stream_opts.get("tether_prefer_wired", True)),
                    require_private=True,
                )
            except Exception:
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                try:
                    rov_host, _rov_port = parse_zmq_endpoint(_endpoint_for_rov(self.rov))
                    sock.connect((rov_host, 9))
                    windows_host = sock.getsockname()[0]
                finally:
                    sock.close()
        return str(windows_host or "0.0.0.0")

    @staticmethod
    def _snapshot_bind_address(stream_opts: dict, windows_host: str) -> str:
        bind_rx = True
        if "bind_receiver_to_host" in stream_opts:
            bind_rx = bool(stream_opts.get("bind_receiver_to_host"))
        return windows_host if bind_rx else "0.0.0.0"

    @staticmethod
    def _snapshot_rx_config(name: str, stream_opts: dict, *, port: int, bind_address: str) -> RxConfig:
        tx_is_h264 = (
            str(stream_opts.get("video_format", "")).lower() == "h264"
            or str(stream_opts.get("encode", "")).lower() == "h264"
        )
        rx_extra: dict = {}
        configured_rx_extra = stream_opts.get("receiver_extra")
        if isinstance(configured_rx_extra, dict):
            rx_extra.update(configured_rx_extra)
        for key in (
            "frame_history_size",
            "receiver_h264_decoder",
            "h264_decoder",
        ):
            if key in stream_opts and stream_opts[key] is not None:
                rx_extra[key] = stream_opts[key]
        snapshot_output_fps = _stream_int(
            stream_opts,
            "receiver_snapshot_output_fps",
            "snapshot_output_fps",
            default=8,
        )
        if snapshot_output_fps > 0:
            rx_extra["receiver_output_fps"] = snapshot_output_fps
        rx_extra["source_fps"] = int(stream_opts.get("fps", 30) or 30)
        rx_extra["receiver_kill_port_users"] = False

        width = int(stream_opts.get("width", 0) or 0)
        height = int(stream_opts.get("height", 0) or 0)
        if width <= 0 or height <= 0:
            raise ValueError(f"Stream '{name}' has invalid snapshot dimensions {width}x{height}")

        return RxConfig(
            name=f"{name} Snapshot",
            codec="h264" if tx_is_h264 else "jpeg",
            port=int(port),
            bind_address=bind_address,
            latency_ms=int(stream_opts.get("receiver_snapshot_latency_ms", stream_opts.get("latency_ms", 25))),
            mode="raw",
            width=width,
            height=height,
            channel_order=str(stream_opts.get("channel_order", "BGR")),
            udp_buffer_size=int(
                stream_opts.get(
                    "receiver_snapshot_udp_buffer_size",
                    stream_opts.get("receiver_udp_buffer_size", 4 * 1024 * 1024),
                )
            ),
            drop_on_latency=bool(
                stream_opts.get(
                    "receiver_snapshot_drop_on_latency",
                    stream_opts.get("receiver_drop_on_latency", True),
                )
            ),
            extra=rx_extra,
        )

    def _new_snapshot_tap(self, name: str, stream_opts: dict, *, persistent: bool) -> _SnapshotTap:
        windows_host = self._snapshot_windows_host(stream_opts)
        bind_address = self._snapshot_bind_address(stream_opts, windows_host)
        capture_port = self._allocate_udp_port(bind_address)
        rx_cfg = self._snapshot_rx_config(name, stream_opts, port=capture_port, bind_address=bind_address)
        base_extra = self._current_rov_stream_extra(
            name,
            stream_opts.get("extra") if isinstance(stream_opts.get("extra"), dict) else {},
        )
        idle_s = _stream_float(stream_opts, "snapshot_tap_idle_s", default=self.snapshot_tap_idle_s)
        fresh_wait_s = _stream_float(stream_opts, "snapshot_fresh_wait_s", default=0.2)
        reuse_max_age_s = _stream_float(stream_opts, "snapshot_reuse_max_age_s", default=1.5)
        mirror_check_interval_s = _stream_float(stream_opts, "snapshot_mirror_check_interval_s", default=1.0)
        return _SnapshotTap(
            self,
            name=name,
            rx=ReceiverProcess(rx_cfg),
            width=rx_cfg.width,
            height=rx_cfg.height,
            port=capture_port,
            base_extra=base_extra,
            persistent=persistent,
            idle_s=idle_s,
            fresh_wait_s=fresh_wait_s,
            reuse_max_age_s=reuse_max_age_s,
            mirror_check_interval_s=mirror_check_interval_s,
        )

    def _get_snapshot_tap(self, name: str, stream_opts: dict, *, persistent: bool = False) -> _SnapshotTap:
        name_lock = self._lock_for_name(f"snapshot:{name}")
        with name_lock:
            with self._mgr_lock:
                existing = self._snapshot_taps.get(name)
                if existing is not None and not existing.closed:
                    if persistent:
                        existing.make_persistent()
                    return existing
                if existing is not None:
                    self._snapshot_taps.pop(name, None)

            tap = self._new_snapshot_tap(name, stream_opts, persistent=persistent)
            with self._mgr_lock:
                existing = self._snapshot_taps.get(name)
                if existing is not None and not existing.closed:
                    if persistent:
                        existing.make_persistent()
                    tap.close(reason="duplicate")
                    return existing
                self._snapshot_taps[name] = tap
                return tap

    def _forget_snapshot_tap(self, name: str, tap: _SnapshotTap) -> None:
        with self._mgr_lock:
            if self._snapshot_taps.get(name) is tap:
                self._snapshot_taps.pop(name, None)

    def close_snapshot_taps(self, *, reason: str = "close") -> None:
        with self._mgr_lock:
            taps = list(self._snapshot_taps.values())
        for tap in taps:
            try:
                tap.close(reason=reason)
            except Exception:
                pass

    def _snapshot_prewarm_names(self, names: list[str] | None = None, *, limit: int | None = None) -> list[str]:
        try:
            max_count = self.snapshot_prewarm_count if limit is None else int(limit)
        except Exception:
            max_count = 0
        if max_count <= 0:
            return []

        ordered: list[str] = []
        source_names = list(names or [])
        if not source_names:
            source_names = list(self.default_pane_order or []) + self.list_available()
        for raw_name in source_names:
            name = str(raw_name or "").strip()
            if not name or name in ordered or name not in self.stream_defs:
                continue
            ordered.append(name)
            if len(ordered) >= max_count:
                break
        return ordered

    def prewarm_snapshot_taps(self, names: list[str] | None = None, *, limit: int | None = None) -> list[str]:
        """Start persistent snapshot mirrors in the background for low-lag stills."""

        ordered = self._snapshot_prewarm_names(names, limit=limit)
        scheduled: list[str] = []
        for name in ordered:
            try:
                stream_opts = self._merged_stream_options(name)
                if not _stream_bool(stream_opts, "snapshot_prewarm", "snapshot_capture_enabled", default=True):
                    continue
                tap = self._get_snapshot_tap(name, stream_opts, persistent=True)
            except Exception as exc:
                logger.warning("Could not prepare snapshot tap for '%s': %s", name, exc)
                continue

            def _warm(tap=tap, name=name) -> None:
                try:
                    tap.start()
                except Exception as exc:
                    logger.warning("Snapshot tap prewarm failed for '%s': %s", name, exc)
                    try:
                        tap.close(reason="prewarm_failed")
                    except Exception:
                        pass

            threading.Thread(target=_warm, name=f"snapshot-prewarm-{name}", daemon=True).start()
            scheduled.append(name)
        if scheduled:
            trace_event("snapshot_taps_prewarm_scheduled", streams=scheduled, count=len(scheduled))
        return scheduled

    def _latest_opened_snapshot_frame(self, name: str, *, max_age_s: float) -> CameraFramePacket | None:
        with self._mgr_lock:
            camera = self._opened.get(name)
        if camera is None:
            return None
        try:
            packet = camera.latest_frame_packet()
        except Exception:
            return None
        if packet is None:
            return None
        try:
            age_s = time.monotonic() - float(packet.monotonic_ts)
            if max_age_s > 0.0 and age_s > max_age_s:
                return None
        except Exception:
            pass
        frame = getattr(packet, "frame_bgr", None)
        if frame is None:
            return None
        return CameraFramePacket(
            source_name=str(getattr(packet, "source_name", name) or name),
            frame_bgr=np.ascontiguousarray(frame).copy(),
            seq=int(getattr(packet, "seq", 0) or 0),
            monotonic_ts=float(getattr(packet, "monotonic_ts", time.monotonic()) or time.monotonic()),
            wall_ts=float(getattr(packet, "wall_ts", time.time()) or time.time()),
        )

    @staticmethod
    def _optional_int(value) -> int | None:
        if value is None:
            return None
        try:
            return int(value)
        except Exception:
            return None

    @staticmethod
    def _optional_float(value) -> float | None:
        if value is None:
            return None
        try:
            return float(value)
        except Exception:
            return None

    @staticmethod
    def _shape_tuple(value) -> tuple[int, ...]:
        if not isinstance(value, (list, tuple)):
            return ()
        shape: list[int] = []
        for item in value:
            try:
                shape.append(int(item))
            except Exception:
                return ()
        return tuple(shape)

    @classmethod
    def _snapshot_packet_from_payload(cls, data: dict, name: str) -> SnapshotImagePacket:
        if not isinstance(data, dict):
            raise RuntimeError("ROV snapshot returned no data")
        encoding = str(data.get("encoding") or "base64").strip().lower()
        if encoding != "base64":
            raise RuntimeError(f"Unsupported ROV snapshot encoding: {encoding}")
        payload = data.get("data_b64") or data.get("image_b64")
        if not isinstance(payload, str) or not payload:
            raise RuntimeError("ROV snapshot returned no image payload")
        try:
            image_bytes = base64.b64decode(payload.encode("ascii"), validate=True)
        except Exception as exc:
            raise RuntimeError(f"Could not decode ROV snapshot payload: {exc}") from exc
        if not image_bytes:
            raise RuntimeError("ROV snapshot payload was empty")

        mime_type = str(data.get("mime_type") or "image/jpeg").strip().lower()
        extension = str(data.get("extension") or ("jpg" if mime_type == "image/jpeg" else "img")).strip().lower()
        if extension.startswith("."):
            extension = extension[1:]
        if not extension or extension == "img":
            extension = "jpg" if mime_type == "image/jpeg" else "bin"
        try:
            wall_ts = float(data.get("wall_ts") or time.time())
        except Exception:
            wall_ts = time.time()
        try:
            monotonic_ts = float(data.get("monotonic_ts") or time.monotonic())
        except Exception:
            monotonic_ts = time.monotonic()
        try:
            byte_count = int(data.get("byte_count") or len(image_bytes))
        except Exception:
            byte_count = len(image_bytes)

        return SnapshotImagePacket(
            source_name=str(data.get("name") or data.get("stream") or name),
            image_bytes=image_bytes,
            mime_type=mime_type,
            extension=extension,
            wall_ts=wall_ts,
            monotonic_ts=monotonic_ts,
            byte_count=byte_count,
            caps=str(data.get("caps") or ""),
            seq=int(data.get("seq") or 0),
            shape=cls._shape_tuple(data.get("shape")),
            source_pts_ns=cls._optional_int(data.get("source_pts_ns")),
            source_dts_ns=cls._optional_int(data.get("source_dts_ns")),
            source_duration_ns=cls._optional_int(data.get("source_duration_ns")),
            source_monotonic_ts=cls._optional_float(data.get("source_monotonic_ts")),
            capture_source=str(data.get("capture_source") or ""),
        )

    def capture_onboard_snapshot(self, name: str, *, timeout_s: float = 2.0) -> SnapshotImagePacket:
        """Ask TritonOS to capture one still image on the ROV and return compressed bytes."""

        # Validate the stream name before asking the ROV for a capture.
        self._merged_stream_options(name)
        with self._mgr_lock:
            data = self.rov.capture_snapshot(name=name, timeout_s=float(timeout_s))
        return self._snapshot_packet_from_payload(data, name)

    def capture_onboard_stereo_pair(
        self,
        left: str,
        right: str,
        *,
        timeout_s: float = 2.0,
        max_pair_delta_ms: float = 50.0,
    ) -> StereoImagePairPacket:
        """Ask TritonOS to capture a fresh left/right still-image pair on the ROV."""

        self._merged_stream_options(left)
        self._merged_stream_options(right)
        with self._mgr_lock:
            data = self.rov.capture_stereo_pair(
                left=str(left),
                right=str(right),
                timeout_s=float(timeout_s),
                max_pair_delta_ms=float(max_pair_delta_ms),
            )
        if not isinstance(data, dict):
            raise RuntimeError("ROV stereo snapshot returned no data")
        left_packet = self._snapshot_packet_from_payload(dict(data.get("left") or {}), str(left))
        right_packet = self._snapshot_packet_from_payload(dict(data.get("right") or {}), str(right))
        try:
            pair_delta_ms = float(data.get("pair_delta_ms"))
        except Exception:
            pair_delta_ms = abs(float(left_packet.monotonic_ts) - float(right_packet.monotonic_ts)) * 1000.0
        try:
            attempts = int(data.get("attempts") or 1)
        except Exception:
            attempts = 1
        return StereoImagePairPacket(
            left=left_packet,
            right=right_packet,
            pair_delta_ms=pair_delta_ms,
            timestamp_source=str(data.get("timestamp_source") or "rov_snapshot_appsink_fresh_monotonic"),
            attempts=max(1, attempts),
        )

    def capture_snapshot_frame(self, name: str, *, timeout_s: float = 2.5) -> CameraFramePacket:
        """Return one source frame for a still snapshot without reading the GUI surface."""

        stream_opts = self._merged_stream_options(name)
        if _stream_bool(stream_opts, "snapshot_allow_display_frame", default=False):
            max_age_s = _stream_float(stream_opts, "snapshot_opened_frame_max_age_s", default=0.35)
            opened_packet = self._latest_opened_snapshot_frame(name, max_age_s=max_age_s)
            if opened_packet is not None:
                return opened_packet

        persistent_default = name in self._snapshot_prewarm_names()
        persistent = _stream_bool(stream_opts, "snapshot_persistent", default=persistent_default)
        tap = self._get_snapshot_tap(name, stream_opts, persistent=persistent)
        return tap.capture_frame(timeout_s=timeout_s)

    def close(self, name: str) -> bool:
        """Close a named stream if it is currently active."""
        name_lock = self._lock_for_name(name)
        with name_lock:
            with self._mgr_lock:
                cam = self._opened.pop(name, None)
            if cam:
                cam.release()
                return True
            self._wait_for_pending_close(name)
            return False

    def close_async(self, name: str) -> bool:
        """Close a stream on a background thread so Qt never waits on RPC/GStreamer cleanup."""

        with self._mgr_lock:
            cam = self._opened.pop(name, None)
            if cam is None:
                return False

            thread_ref: dict[str, threading.Thread] = {}

            def _release() -> None:
                try:
                    cam.release()
                finally:
                    thread = thread_ref.get("thread")
                    with self._mgr_lock:
                        if thread is not None and self._closing.get(name) is thread:
                            self._closing.pop(name, None)

            thread = threading.Thread(target=_release, name=f"video-close-{name}", daemon=True)
            thread_ref["thread"] = thread
            self._closing[name] = thread

        thread.start()
        return True
