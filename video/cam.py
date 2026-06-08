"""Remote camera wrappers for ROV RTP streams.

This module bridges the TritonOS video RPC service and the local GStreamer
receiver process. Callers get a small OpenCV-like camera object while the
implementation handles stream startup, local UDP binding, frame reads, and
cleanup.
"""

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
from video.gst_receiver import ReceiverProcess, RxConfig
from video.frame_rotation import normalize_rotation_deg
from video.rov_streams import ROVStreams

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CameraFramePacket:
    """Decoded camera frame with receiver-side timing metadata."""

    source_name: str
    frame_bgr: np.ndarray
    seq: int
    monotonic_ts: float
    wall_ts: float


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

        # Populated if the ROV had to perform recovery actions (e.g., USB rebind)
        self.start_messages: list[str] = []

        # Detect the best local IP to receive video if not provided.
        # IMPORTANT: the previous approach used 8.8.8.8, which tends to pick Wi-Fi.
        # Here we select the local IP that can reach the ROV video RPC host,
        # preferring wired/tether when possible.
        if windows_host is None:
            try:
                rov_host, rov_port = parse_zmq_endpoint(VIDEO_RPC_ENDPOINT)
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
                    rov_host, _rov_port = parse_zmq_endpoint(VIDEO_RPC_ENDPOINT)
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

    def _decode_packet(self, packet) -> CameraFramePacket:
        img = np.frombuffer(packet.data, dtype=np.uint8).reshape((self.height, self.width, 3))
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
        return [self._decode_packet(packet) for packet in packets]

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


class RemoteCaptureCamera:
    """Raw-frame receiver for a stream that is already being transmitted.

    Direct3D display streams are started by the display widget. Capture uses a
    mirrored UDP port so snapshots, recording, and stereo pairing can decode
    frames without owning or restarting the ROV-side sender.
    """

    def __init__(
        self,
        name: str,
        width: int,
        height: int,
        fps: int,
        video_format: str = "mjpeg",
        port: int = 5000,
        latency_ms: int = 60,
        channel_order: str = "BGR",
        windows_host: str | None = None,
        stream_opts: dict | None = None,
    ):
        self.name = name
        self.width = int(width)
        self.height = int(height)
        self.fps = int(fps)
        self.video_format = video_format
        self.port = int(port)
        self.latency_ms = int(latency_ms)
        self.channel_order = channel_order
        self.rotation_deg = normalize_rotation_deg(stream_opts.get("rotation_deg", 0) if stream_opts else 0)
        self.start_messages: list[str] = []

        stream_opts = stream_opts or {}
        if windows_host is None:
            try:
                rov_host, rov_port = parse_zmq_endpoint(VIDEO_RPC_ENDPOINT)
                prefer_wired = bool(stream_opts.get("tether_prefer_wired", True))
                windows_host = choose_video_receive_ip(
                    remote_host=rov_host,
                    remote_port=int(rov_port),
                    prefer_wired=prefer_wired,
                    require_private=True,
                )
            except Exception:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                try:
                    rov_host, _rov_port = parse_zmq_endpoint(VIDEO_RPC_ENDPOINT)
                    s.connect((rov_host, 9))
                    windows_host = s.getsockname()[0]
                finally:
                    s.close()
        self.windows_host = windows_host
        trace_event(
            "capture_camera_init",
            stream=self.name,
            port=self.port,
            windows_host=self.windows_host,
            width=self.width,
            height=self.height,
            fps=self.fps,
            video_format=self.video_format,
        )

        tx_is_h264 = (
            str(stream_opts.get("video_format", video_format)).lower() == "h264"
            or str(stream_opts.get("encode", "")).lower() == "h264"
        )

        bind_rx = bool(stream_opts.get("bind_receiver_to_host", True))
        rx_extra = {}
        configured_rx_extra = stream_opts.get("receiver_extra")
        if isinstance(configured_rx_extra, dict):
            rx_extra.update(configured_rx_extra)
        for key in (
            "frame_history_size",
            "receiver_h264_decoder",
            "h264_decoder",
            "receiver_capture_output_fps",
            "capture_output_fps",
            "receiver_output_fps",
            "output_fps",
        ):
            if key in stream_opts and stream_opts[key] is not None:
                rx_extra[key] = stream_opts[key]
        if "receiver_capture_output_fps" in rx_extra:
            rx_extra["receiver_output_fps"] = rx_extra["receiver_capture_output_fps"]
        elif "capture_output_fps" in rx_extra:
            rx_extra["receiver_output_fps"] = rx_extra["capture_output_fps"]
        rx_extra.setdefault("source_fps", self.fps)
        rx_extra["receiver_kill_port_users"] = False

        rx_cfg = RxConfig(
            name=f"{self.name} capture",
            codec="h264" if tx_is_h264 else "jpeg",
            port=self.port,
            bind_address=self.windows_host if bind_rx else "0.0.0.0",
            latency_ms=int(stream_opts.get("latency_ms", self.latency_ms)),
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
        trace_event(
            "capture_camera_started",
            stream=self.name,
            port=self.port,
            windows_host=self.windows_host,
            codec=rx_cfg.codec,
        )

    def read(self):
        packet = self.read_frame_packet()
        if packet is None:
            return False, None
        return True, packet.frame_bgr

    def _decode_packet(self, packet) -> CameraFramePacket:
        img = np.frombuffer(packet.data, dtype=np.uint8).reshape((self.height, self.width, 3))
        return CameraFramePacket(
            source_name=self.name,
            frame_bgr=img,
            seq=int(packet.seq),
            monotonic_ts=float(packet.monotonic_ts),
            wall_ts=float(packet.wall_ts),
        )

    def read_frame_packet(self) -> CameraFramePacket | None:
        packet = self.rx.read_frame_packet()
        if packet is None:
            return None
        return self._decode_packet(packet)

    def latest_frame_packet(self) -> CameraFramePacket | None:
        packet = self.rx.latest_frame_packet()
        if packet is None:
            return None
        return self._decode_packet(packet)

    def recent_frame_packets(self, *, max_age_s: float = 0.5) -> list[CameraFramePacket]:
        packets = self.rx.recent_frame_packets(max_age_s=max_age_s)
        return [self._decode_packet(packet) for packet in packets]

    def release(self, rx_grace_s: float = 0.15):
        trace_event("capture_camera_release", stream=self.name, port=self.port, rx_grace_s=rx_grace_s)
        try:
            self.rx.stop(grace_s=rx_grace_s)
        except Exception as e:
            logger.warning("Failed to stop capture receiver for '%s': %s", self.name, e)


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
        self._capture_opened: dict[str, RemoteCaptureCamera] = {}
        self._capture_refs: dict[str, int] = {}
        self._closing: dict[str, threading.Thread] = {}
        self._name_locks: dict[str, threading.Lock] = {}
        # Serialize open/close across background connect workers. This avoids
        # racing on shared bookkeeping and on the single ROV video RPC REQ socket.
        self._mgr_lock = threading.RLock()

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

    def _capture_port_for_stream(self, stream_opts: dict) -> int:
        for key in ("capture_port", "receiver_capture_port", "mirror_port"):
            value = stream_opts.get(key)
            if value is not None:
                return int(value)
        return int(stream_opts.get("port", 5000))

    def _stream_start_kwargs_for_capture(self, stream_opts: dict, capture_port: int) -> dict:
        host = self.windows_host
        if host is None:
            rov_host, rov_port = parse_zmq_endpoint(VIDEO_RPC_ENDPOINT)
            host = choose_video_receive_ip(
                remote_host=rov_host,
                remote_port=int(rov_port),
                prefer_wired=bool(stream_opts.get("tether_prefer_wired", True)),
                require_private=True,
            )
        kwargs = dict(
            name=stream_opts["name"],
            device=stream_opts["device"],
            width=int(stream_opts["width"]),
            height=int(stream_opts["height"]),
            fps=int(stream_opts["fps"]),
            video_format=stream_opts.get("video_format", "mjpeg"),
            host=host,
            port=int(stream_opts.get("port", 5000)),
        )
        for key in (
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
            if key in stream_opts and stream_opts[key] is not None:
                kwargs[key] = stream_opts[key]

        extra = dict(kwargs.get("extra") or {})
        raw_ports = extra.get("udp_mirror_ports", extra.get("mirror_udp_ports", []))
        if isinstance(raw_ports, (str, bytes)):
            ports = [int(p.strip()) for p in str(raw_ports).split(",") if p.strip()]
        elif isinstance(raw_ports, (list, tuple, set)):
            ports = [int(p) for p in raw_ports]
        elif raw_ports:
            ports = [int(raw_ports)]
        else:
            ports = []
        if int(capture_port) not in ports:
            ports.append(int(capture_port))
        extra["udp_mirror_ports"] = ports
        kwargs["extra"] = extra
        return kwargs

    def _capture_stream_needs_start(self, name: str, stream_opts: dict, capture_port: int) -> bool:
        try:
            status = self.rov.list_stream_status()
        except Exception as exc:
            trace_event("camera_manager_capture_status_failed", stream=name, error=str(exc))
            return False
        entry = (status or {}).get(name)
        if not entry or not bool((entry or {}).get("running", True)):
            trace_event("camera_manager_capture_stream_missing", stream=name, status=entry)
            return True
        config = dict((entry or {}).get("config") or entry or {})
        if int(config.get("port", stream_opts.get("port", 5000))) != int(stream_opts.get("port", 5000)):
            trace_event("camera_manager_capture_stream_port_mismatch", stream=name, status=config)
            return True
        extra = config.get("extra") or {}
        raw_ports = extra.get("udp_mirror_ports", extra.get("mirror_udp_ports", []))
        if isinstance(raw_ports, (str, bytes)):
            ports = [int(p.strip()) for p in str(raw_ports).split(",") if p.strip()]
        elif isinstance(raw_ports, (list, tuple, set)):
            ports = [int(p) for p in raw_ports]
        elif raw_ports:
            ports = [int(raw_ports)]
        else:
            ports = []
        if int(capture_port) not in ports:
            trace_event(
                "camera_manager_capture_stream_missing_mirror",
                stream=name,
                capture_port=capture_port,
                status_ports=ports,
            )
            return True
        return False

    def _start_rov_stream_for_capture(self, name: str, stream_opts: dict, capture_port: int) -> None:
        kwargs = self._stream_start_kwargs_for_capture(stream_opts, capture_port)
        trace_event(
            "camera_manager_capture_stream_start_request",
            stream=name,
            port=kwargs.get("port"),
            capture_port=capture_port,
            host=kwargs.get("host"),
        )
        resp = self.rov.start_stream(**kwargs)
        trace_event("camera_manager_capture_stream_started", stream=name, response=resp)

    def open_capture(self, name: str) -> RemoteCaptureCamera:
        """Open or share a capture-only receiver for a named stream."""

        trace_event("camera_manager_open_capture_request", stream=name)
        name_lock = self._lock_for_name(name)
        with name_lock:
            with self._mgr_lock:
                existing = self._capture_opened.get(name)
                if existing is not None:
                    self._capture_refs[name] = int(self._capture_refs.get(name, 0)) + 1
                    trace_event(
                        "camera_manager_open_capture_reused",
                        stream=name,
                        refs=self._capture_refs[name],
                    )
                    return existing
                stream_opts = self._merged_stream_options(name)

            capture_port = self._capture_port_for_stream(stream_opts)
            needs_rov_start = self._capture_stream_needs_start(name, stream_opts, capture_port)
            cam = RemoteCaptureCamera(
                name=stream_opts["name"],
                width=stream_opts["width"],
                height=stream_opts["height"],
                fps=stream_opts["fps"],
                video_format=stream_opts.get("video_format", "mjpeg"),
                port=capture_port,
                latency_ms=int(stream_opts.get("latency_ms", 60)),
                windows_host=self.windows_host,
                stream_opts=stream_opts,
            )
            if needs_rov_start:
                try:
                    self._start_rov_stream_for_capture(name, stream_opts, capture_port)
                except Exception:
                    try:
                        cam.release()
                    except Exception:
                        pass
                    raise
            with self._mgr_lock:
                existing = self._capture_opened.get(name)
                if existing is not None:
                    try:
                        cam.release()
                    except Exception:
                        pass
                    self._capture_refs[name] = int(self._capture_refs.get(name, 0)) + 1
                    trace_event(
                        "camera_manager_open_capture_race_reused",
                        stream=name,
                        refs=self._capture_refs[name],
                    )
                    return existing
                self._capture_opened[name] = cam
                self._capture_refs[name] = 1
                trace_event(
                    "camera_manager_open_capture_started",
                    stream=name,
                    port=capture_port,
                    refs=1,
                )
                return cam

    def latest_capture_packet(self, name: str) -> CameraFramePacket | None:
        with self._mgr_lock:
            cam = self._capture_opened.get(name)
        if cam is None:
            return None
        return cam.latest_frame_packet()

    def close_capture(self, name: str) -> bool:
        """Release one reference to a capture-only receiver."""

        trace_event("camera_manager_close_capture_request", stream=name)
        name_lock = self._lock_for_name(name)
        with name_lock:
            with self._mgr_lock:
                cam = self._capture_opened.get(name)
                if cam is None:
                    self._capture_refs.pop(name, None)
                    trace_event("camera_manager_close_capture_missing", stream=name)
                    return False
                refs = max(0, int(self._capture_refs.get(name, 1)) - 1)
                if refs > 0:
                    self._capture_refs[name] = refs
                    trace_event("camera_manager_close_capture_decremented", stream=name, refs=refs)
                    return False
                self._capture_opened.pop(name, None)
                self._capture_refs.pop(name, None)
            cam.release()
            trace_event("camera_manager_close_capture_closed", stream=name)
            return True

    def close_capture_async(self, name: str) -> bool:
        """Release one capture receiver reference on a background thread."""

        trace_event("camera_manager_close_capture_async_request", stream=name)
        name_lock = self._lock_for_name(name)
        with name_lock:
            with self._mgr_lock:
                cam = self._capture_opened.get(name)
                if cam is None:
                    self._capture_refs.pop(name, None)
                    trace_event("camera_manager_close_capture_async_missing", stream=name)
                    return False
                refs = max(0, int(self._capture_refs.get(name, 1)) - 1)
                if refs > 0:
                    self._capture_refs[name] = refs
                    trace_event("camera_manager_close_capture_async_decremented", stream=name, refs=refs)
                    return False
                self._capture_opened.pop(name, None)
                self._capture_refs.pop(name, None)

        def _release() -> None:
            try:
                cam.release()
            finally:
                trace_event("camera_manager_close_capture_async_closed", stream=name)

        try:
            threading.Thread(target=_release, name=f"video-capture-close-{name}", daemon=True).start()
        except Exception:
            _release()
        return True

    def close_all_capture(self) -> None:
        with self._mgr_lock:
            names = list(self._capture_opened.keys())
            self._capture_refs = {name: 1 for name in names}
        for name in names:
            try:
                self.close_capture(name)
            except Exception:
                pass

    def close_all_capture_async(self) -> None:
        with self._mgr_lock:
            items = list(self._capture_opened.items())
            self._capture_opened.clear()
            self._capture_refs.clear()

        for name, cam in items:
            def _release(camera=cam, stream_name=name) -> None:
                try:
                    camera.release()
                finally:
                    trace_event("camera_manager_close_all_capture_async_closed", stream=stream_name)

            try:
                threading.Thread(target=_release, name=f"video-capture-close-{name}", daemon=True).start()
            except Exception:
                _release()

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
