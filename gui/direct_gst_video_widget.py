"""Direct GStreamer/Direct3D video pane for low-latency pilot viewing."""

from __future__ import annotations

import ctypes
import base64
import logging
import os
import signal
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from PyQt6.QtCore import Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtGui import QImage
from PyQt6.QtWidgets import QApplication, QLabel, QSizePolicy, QVBoxLayout, QWidget

from config import VIDEO_RPC_ENDPOINT
from network.net_select import choose_video_receive_ip, parse_zmq_endpoint
from recording.capture_trace import trace_event
from recording.capture_paths import timestamped_camera_stem, unique_capture_path
from recording.save_location import DEFAULT_RECORDINGS_DIR
from recording.compressed_stream_recorder import CompressedRtpRecorder
from recording.frame_quality import (
    capture_frame_rejection_reason,
    looks_like_green_startup_artifact as _looks_like_green_startup_artifact,
)
from recording.video_recorder import VideoRecorder, save_snapshot
from video.gst_receiver import _suppress_gst_stderr_line, _win_kill_udp_port_users
from video.gst_runtime import bootstrap_gstreamer_env
from video.cam import CameraFramePacket, RemoteCameraManager


logger = logging.getLogger(__name__)
_ORPHANED_CONNECT_WORKERS: set[QThread] = set()
_STARTUP_ARTIFACT_MAX_SKIP_S = 2.5


@dataclass(frozen=True)
class DirectReceiverConfig:
    name: str
    codec: str
    port: int
    bind_address: str
    width: int = 0
    height: int = 0
    latency_ms: int = 5
    udp_buffer_size: int = 4 * 1024 * 1024
    drop_on_latency: bool = True
    h264_decoder: str = "decodebin"
    sink: str = "d3d11videosink"
    square_crop: bool = False
    frame_pipe: bool = False
    frame_pipe_fps: float = 0.0
    record_rtp_port: int = 0
    record_rtp_host: str = "127.0.0.1"


def _find_gst_launch() -> str:
    runtime = bootstrap_gstreamer_env()
    if runtime is None:
        raise FileNotFoundError(
            "Could not find gst-launch-1.0. Run setup_windows.ps1 or install GStreamer."
        )
    return str(runtime.gst_launch)


def _truthy(value: Any, default: bool = False) -> bool:
    if value is None:
        return bool(default)
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return bool(value)


def _h264_decoder_chain(decoder: str) -> list[str]:
    name = str(decoder or "decodebin").strip().lower()
    if name in {"", "auto", "hardware", "decodebin"}:
        return ["decodebin"]
    return [name]


def _square_crop_chain(cfg: DirectReceiverConfig) -> list[str]:
    if not bool(cfg.square_crop):
        return []
    width = max(0, int(cfg.width or 0))
    height = max(0, int(cfg.height or 0))
    if width <= 0 or height <= 0 or width == height:
        return []
    if width > height:
        extra = width - height
        left = extra // 2
        right = extra - left
        return ["!", "videocrop", f"left={left}", f"right={right}", "top=0", "bottom=0"]
    extra = height - width
    top = extra // 2
    bottom = extra - top
    return ["!", "videocrop", "left=0", "right=0", f"top={top}", f"bottom={bottom}"]


def _receiver_output_dimensions(cfg: DirectReceiverConfig) -> tuple[int, int]:
    width = max(0, int(cfg.width or 0))
    height = max(0, int(cfg.height or 0))
    if width <= 0 or height <= 0:
        return 0, 0
    if bool(cfg.square_crop) and width != height:
        side = min(width, height)
        return side, side
    return width, height


def _raw_frame_caps(width: int, height: int) -> str:
    return (
        f"video/x-raw,format=BGR,width={int(width)},height={int(height)},"
        "colorimetry=1:4:0:0,range=full"
    )


def _render_output_chain(cfg: DirectReceiverConfig, crop_chain: list[str] | None = None) -> list[str]:
    sink = str(cfg.sink or "d3d11videosink").strip() or "d3d11videosink"
    sink_props = ["sync=false", "async=false"]
    if sink.lower() not in {"fakesink", "appsink", "filesink"}:
        sink_props.append("force-aspect-ratio=true")
    return [
        "!", "queue", "max-size-buffers=1", "max-size-bytes=0",
        "max-size-time=0", "leaky=downstream",
        *(crop_chain or []),
        "!", sink, *sink_props,
    ]


def _frame_pipe_output_chain(cfg: DirectReceiverConfig) -> list[str]:
    width = max(0, int(cfg.width or 0))
    height = max(0, int(cfg.height or 0))
    if width <= 0 or height <= 0:
        return []
    chain = [
        "frame_t.", "!", "queue", "max-size-buffers=1", "max-size-bytes=0",
        "max-size-time=0", "leaky=downstream",
    ]
    try:
        fps = float(cfg.frame_pipe_fps or 0.0)
    except Exception:
        fps = 0.0
    if fps > 0.0:
        rate = max(1, int(round(fps)))
        chain.extend(
            [
                "!", "videoconvert",
                "!", "videorate", "drop-only=true", f"max-rate={rate}",
                "!", f"{_raw_frame_caps(width, height)},framerate={rate}/1",
            ]
        )
    else:
        chain.extend(["!", "videoconvert", "!", _raw_frame_caps(width, height)])
    chain.extend(["!", "fdsink", "fd=1", "sync=false", "async=false"])
    return chain


def _rtp_record_forward_chain(cfg: DirectReceiverConfig) -> list[str]:
    port = int(cfg.record_rtp_port or 0)
    if port <= 0:
        return []
    host = str(cfg.record_rtp_host or "127.0.0.1").strip() or "127.0.0.1"
    return [
        "rtp_t.", "!", "queue", "max-size-buffers=0", "max-size-bytes=0",
        "max-size-time=0",
        "!", "udpsink", f"host={host}", f"port={port}", "sync=false", "async=false",
    ]


def build_direct_receiver_cmd(gst_launch: str, cfg: DirectReceiverConfig) -> list[str]:
    """Build a direct-render RTP receiver pipeline.

    Unlike the legacy pilot widget, this keeps frames inside GStreamer and lets
    the video sink render through Direct3D. No raw 1080p BGR frames are copied
    through Python.
    """

    base = [str(gst_launch), "--gst-disable-registry-fork", "-q"]
    udp_buffer_size = max(262144, int(cfg.udp_buffer_size))
    drop_on_latency = "true" if cfg.drop_on_latency else "false"
    crop_chain = _square_crop_chain(cfg)
    output_chain: list[str]
    if bool(cfg.frame_pipe):
        output_chain = [
            "!", "tee", "name=frame_t",
            *_render_output_chain(cfg, crop_chain),
            *_frame_pipe_output_chain(cfg),
        ]
    else:
        output_chain = [*crop_chain, *_render_output_chain(cfg)]

    if cfg.codec.lower() == "h264":
        caps = "application/x-rtp,media=video,encoding-name=H264,payload=96,clock-rate=90000"
        source_chain = [
            "udpsrc", f"address={cfg.bind_address}", "reuse=true", f"port={cfg.port}",
            f"buffer-size={udp_buffer_size}", f"caps={caps}",
        ]
        h264_display_chain = [
            "!", "rtpjitterbuffer", f"latency={cfg.latency_ms}",
            f"drop-on-latency={drop_on_latency}", "faststart-min-packets=1",
            "!", "rtph264depay",
            "!", "h264parse", "config-interval=-1", "disable-passthrough=true",
            "!", *_h264_decoder_chain(cfg.h264_decoder),
            "!", "videoconvert",
            *output_chain,
        ]
        if int(cfg.record_rtp_port or 0) > 0:
            pipeline = [
                *source_chain,
                "!", "tee", "name=rtp_t",
                "rtp_t.", "!", "queue", "max-size-buffers=1", "max-size-bytes=0",
                "max-size-time=0", "leaky=downstream",
                *h264_display_chain,
                *_rtp_record_forward_chain(cfg),
            ]
        else:
            pipeline = [*source_chain, *h264_display_chain]
    else:
        caps = "application/x-rtp,media=video,encoding-name=JPEG,payload=26,clock-rate=90000"
        pipeline = [
            "udpsrc", f"address={cfg.bind_address}", "reuse=true", f"port={cfg.port}",
            f"buffer-size={udp_buffer_size}", f"caps={caps}",
            "!", "rtpjitterbuffer", f"latency={cfg.latency_ms}",
            f"drop-on-latency={drop_on_latency}", "faststart-min-packets=1",
            "!", "rtpjpegdepay",
            "!", "jpegdec",
            "!", "videoconvert",
            *output_chain,
        ]
    return base + pipeline


def _env_truthy(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return bool(default)
    return _truthy(value, default=default)


def _viewport_frame_pipe_requested(stream_opts: dict[str, Any]) -> bool:
    requested = False
    for key in (
        "receiver_viewport_frame_pipe",
        "viewport_frame_pipe",
        "direct_viewport_frame_pipe",
        "direct_viewport_frames",
    ):
        if key in stream_opts:
            requested = _truthy(stream_opts.get(key))
            break
    return _env_truthy("TRITON_DIRECT_VIEWPORT_FRAMES", requested)


def _compressed_recording_requested(stream_opts: dict[str, Any]) -> bool:
    requested = True
    for key in (
        "receiver_compressed_recording",
        "compressed_recording",
        "direct_compressed_recording",
    ):
        if key in stream_opts:
            requested = _truthy(stream_opts.get(key), default=True)
            break
    return _env_truthy("TRITON_DIRECT_COMPRESSED_RECORDING", requested)


def _snapshot_frame_pipe_requested(stream_opts: dict[str, Any]) -> bool:
    requested = True
    for key in (
        "receiver_snapshot_frame_pipe",
        "snapshot_frame_pipe",
        "direct_snapshot_frame_pipe",
        "direct_snapshot_frames",
    ):
        if key in stream_opts:
            requested = _truthy(stream_opts.get(key), default=True)
            break
    return _env_truthy("TRITON_DIRECT_SNAPSHOT_FRAMES", requested)


def _snapshot_frame_pipe_fps(stream_opts: dict[str, Any]) -> float:
    for key in (
        "receiver_snapshot_frame_pipe_fps",
        "snapshot_frame_pipe_fps",
        "direct_snapshot_frame_pipe_fps",
    ):
        if key not in stream_opts or stream_opts.get(key) is None:
            continue
        try:
            return max(0.25, min(30.0, float(stream_opts.get(key))))
        except Exception:
            return 2.0
    raw = os.environ.get("TRITON_DIRECT_SNAPSHOT_FPS", "").strip()
    if raw:
        try:
            return max(0.25, min(30.0, float(raw)))
        except Exception:
            return 2.0
    return 2.0


def _compressed_recording_port(stream_opts: dict[str, Any], display_port: int) -> int:
    for key in (
        "receiver_compressed_record_port",
        "compressed_record_port",
        "direct_record_port",
    ):
        if key not in stream_opts or stream_opts.get(key) is None:
            continue
        try:
            return int(stream_opts.get(key))
        except Exception:
            return 0
    return 7000 + (int(display_port) % 1000)


def _compressed_recording_latency_ms(stream_opts: dict[str, Any]) -> int:
    for key in (
        "receiver_compressed_record_latency_ms",
        "compressed_record_latency_ms",
        "record_latency_ms",
    ):
        if key not in stream_opts or stream_opts.get(key) is None:
            continue
        try:
            return max(0, int(stream_opts.get(key)))
        except Exception:
            return 250
    return 250


def _snapshot_wait_s() -> float:
    raw = os.environ.get("TRITON_SNAPSHOT_WAIT_S", "2.0").strip()
    try:
        return max(0.1, min(10.0, float(raw)))
    except Exception:
        return 2.0


@dataclass(frozen=True)
class _StoredViewportFrame:
    data: bytes
    seq: int
    monotonic_ts: float
    wall_ts: float


class _ViewportFramePipe:
    """Reads the direct renderer tee and exposes viewport frames as camera packets."""

    def __init__(self, source_name: str, width: int, height: int, *, history_size: int = 12):
        self.source_name = str(source_name)
        self.width = max(1, int(width))
        self.height = max(1, int(height))
        self._frame_size = self.width * self.height * 3
        self._history: deque[_StoredViewportFrame] = deque(maxlen=max(1, int(history_size)))
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._latest_seq = 0
        self._last_delivered_seq = 0

    def start(self, stream) -> None:
        self.stop()
        if stream is None:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._reader_loop,
            args=(stream,),
            name=f"direct-video-viewport-pipe-{self.source_name}",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        self._thread = None
        if thread is not None and thread.is_alive():
            try:
                thread.join(timeout=0.1)
            except Exception:
                pass
        with self._lock:
            self._last_delivered_seq = self._latest_seq

    def _reader_loop(self, stream) -> None:
        trace_event(
            "direct_viewport_frame_pipe_started",
            stream=self.source_name,
            width=self.width,
            height=self.height,
            frame_size=self._frame_size,
        )

        def _read_exact(size: int) -> bytes | None:
            chunks: list[bytes] = []
            remaining = int(size)
            while remaining > 0 and not self._stop.is_set():
                chunk = stream.read(remaining)
                if not chunk:
                    return None
                chunks.append(chunk)
                remaining -= len(chunk)
            if remaining > 0:
                return None
            if len(chunks) == 1:
                return chunks[0]
            return b"".join(chunks)

        while not self._stop.is_set():
            data = _read_exact(self._frame_size)
            if data is None:
                break
            now_wall = time.time()
            now_mono = time.monotonic()
            with self._lock:
                self._latest_seq += 1
                stored = _StoredViewportFrame(
                    data=data,
                    seq=self._latest_seq,
                    monotonic_ts=now_mono,
                    wall_ts=now_wall,
                )
                self._history.append(stored)
                history_len = len(self._history)
            trace_event(
                "direct_viewport_frame_arrived",
                stream=self.source_name,
                seq=stored.seq,
                history_len=history_len,
                monotonic_ts=now_mono,
                wall_ts=now_wall,
            )
        trace_event(
            "direct_viewport_frame_pipe_stopped",
            stream=self.source_name,
            latest_seq=self._latest_seq,
        )

    def _packet_from_stored(self, stored: _StoredViewportFrame) -> CameraFramePacket:
        frame = np.frombuffer(stored.data, dtype=np.uint8).reshape((self.height, self.width, 3)).copy()
        return CameraFramePacket(
            source_name=self.source_name,
            frame_bgr=frame,
            seq=stored.seq,
            monotonic_ts=stored.monotonic_ts,
            wall_ts=stored.wall_ts,
        )

    def _snapshot_packet(self, *, consume: bool) -> CameraFramePacket | None:
        with self._lock:
            stored = self._history[-1] if self._history else None
            if stored is None:
                return None
            if consume:
                if stored.seq == self._last_delivered_seq:
                    return None
                self._last_delivered_seq = stored.seq
        return self._packet_from_stored(stored)

    def read_frame_packet(self) -> CameraFramePacket | None:
        return self._snapshot_packet(consume=True)

    def latest_frame_packet(self) -> CameraFramePacket | None:
        return self._snapshot_packet(consume=False)

    def recent_frame_packets(self, *, max_age_s: float = 0.5) -> list[CameraFramePacket]:
        now = time.monotonic()
        max_age_s = max(0.0, float(max_age_s))
        with self._lock:
            frames = [
                stored
                for stored in self._history
                if max_age_s <= 0.0 or (now - float(stored.monotonic_ts)) <= max_age_s
            ]
        return [self._packet_from_stored(stored) for stored in frames]


def _stream_options(manager: RemoteCameraManager, stream_name: str) -> dict[str, Any]:
    options: dict[str, Any] = {}
    defaults = getattr(manager, "_defaults", {})
    if isinstance(defaults, dict):
        options.update(defaults)
    stream_defs = getattr(manager, "stream_defs", {})
    if stream_name not in stream_defs:
        raise KeyError(f"Unknown stream '{stream_name}'")
    options.update(dict(stream_defs[stream_name]))
    return options


def _resolve_windows_host(manager: RemoteCameraManager, stream_opts: dict[str, Any]) -> str:
    configured = getattr(manager, "windows_host", None)
    if configured:
        return str(configured)
    endpoint = str(getattr(getattr(manager, "rov", None), "endpoint", VIDEO_RPC_ENDPOINT) or VIDEO_RPC_ENDPOINT)
    rov_host, rov_port = parse_zmq_endpoint(endpoint)
    return choose_video_receive_ip(
        remote_host=rov_host,
        remote_port=int(rov_port),
        prefer_wired=bool(stream_opts.get("tether_prefer_wired", True)),
        require_private=True,
    )


def _start_kwargs(stream_opts: dict[str, Any], *, host: str) -> dict[str, Any]:
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
    capture_port = stream_opts.get("capture_port", stream_opts.get("receiver_capture_port"))
    if capture_port is not None:
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

class _DirectConnectWorker(QThread):
    receiver_started = pyqtSignal(object, object)
    connected = pyqtSignal(object, str, object)
    failed = pyqtSignal(str)

    def __init__(
        self,
        manager: RemoteCameraManager,
        stream_name: str,
        *,
        host_hwnd: int = 0,
        host_width: int = 1,
        host_height: int = 1,
        square_crop: bool = False,
        viewport_frame_pipe_requested: bool = False,
        parent=None,
    ):
        super().__init__(parent)
        self.manager = manager
        self.stream_name = stream_name
        self.host_hwnd = int(host_hwnd or 0)
        self.host_width = max(1, int(host_width or 1))
        self.host_height = max(1, int(host_height or 1))
        self.square_crop = bool(square_crop)
        self.viewport_frame_pipe_requested = bool(viewport_frame_pipe_requested)
        self.proc: subprocess.Popen | None = None

    def run(self) -> None:
        try:
            stream_opts = _stream_options(self.manager, self.stream_name)
            host = _resolve_windows_host(self.manager, stream_opts)
            start_kwargs = _start_kwargs(stream_opts, host=host)
            port = int(start_kwargs.get("port", 5000))
            tx_is_h264 = (
                str(start_kwargs.get("video_format", "")).lower() == "h264"
                or str(start_kwargs.get("encode", "")).lower() == "h264"
            )
            codec = "h264" if tx_is_h264 else "jpeg"

            _win_kill_udp_port_users(port)

            extra = stream_opts.get("receiver_extra")
            receiver_extra = dict(extra) if isinstance(extra, dict) else {}
            for key in ("receiver_h264_decoder", "h264_decoder"):
                if key in stream_opts and stream_opts[key] is not None:
                    receiver_extra[key] = stream_opts[key]
            h264_decoder = str(
                receiver_extra.get(
                    "receiver_h264_decoder",
                    receiver_extra.get("h264_decoder", "decodebin"),
                )
            )
            sink = str(stream_opts.get("receiver_direct_sink", stream_opts.get("direct_sink", "d3d11videosink")))
            width = int(stream_opts.get("width", start_kwargs.get("width", 0)) or 0)
            height = int(stream_opts.get("height", start_kwargs.get("height", 0)) or 0)
            requested_frame_pipe = self.viewport_frame_pipe_requested or _viewport_frame_pipe_requested(stream_opts)
            requested_snapshot_pipe = _snapshot_frame_pipe_requested(stream_opts)
            frame_pipe = bool((requested_frame_pipe or requested_snapshot_pipe) and width > 0 and height > 0)
            frame_pipe_mode = "capture" if requested_frame_pipe else ("snapshot" if requested_snapshot_pipe else "")
            frame_pipe_fps = 0.0 if requested_frame_pipe else _snapshot_frame_pipe_fps(stream_opts)
            record_rtp_host = "127.0.0.1"
            record_rtp_port = 0
            record_latency_ms = _compressed_recording_latency_ms(stream_opts)
            if codec == "h264" and _compressed_recording_requested(stream_opts):
                record_rtp_port = _compressed_recording_port(stream_opts, port)

            cfg = DirectReceiverConfig(
                name=self.stream_name,
                codec=codec,
                port=port,
                bind_address=host if bool(stream_opts.get("bind_receiver_to_host", True)) else "0.0.0.0",
                width=width,
                height=height,
                latency_ms=int(stream_opts.get("latency_ms", 5)),
                udp_buffer_size=int(stream_opts.get("receiver_udp_buffer_size", 4 * 1024 * 1024)),
                drop_on_latency=bool(stream_opts.get("receiver_drop_on_latency", True)),
                h264_decoder=h264_decoder,
                sink=sink,
                square_crop=bool(self.square_crop),
                frame_pipe=frame_pipe,
                frame_pipe_fps=frame_pipe_fps,
                record_rtp_port=record_rtp_port,
                record_rtp_host=record_rtp_host,
            )
            frame_width = int(width if frame_pipe else 0)
            frame_height = int(height if frame_pipe else 0)
            cmd = build_direct_receiver_cmd(_find_gst_launch(), cfg)
            env = dict(os.environ)
            bootstrap_gstreamer_env(env)
            creationflags = 0
            startupinfo = None
            if os.name == "nt":
                creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                startupinfo.wShowWindow = 0
            self.proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                creationflags=creationflags,
                startupinfo=startupinfo,
                bufsize=0,
            )
            receiver_info = {
                "frame_pipe": bool(frame_pipe),
                "frame_pipe_mode": frame_pipe_mode,
                "frame_pipe_fps": float(frame_pipe_fps),
                "frame_width": int(frame_width),
                "frame_height": int(frame_height),
                "codec": codec,
                "record_rtp_host": record_rtp_host,
                "record_rtp_port": int(record_rtp_port),
                "record_latency_ms": int(record_latency_ms),
            }
            self.receiver_started.emit(self.proc, receiver_info)

            # Start the sender after the UDP listener exists.
            resp = self.manager.rov.start_stream(**start_kwargs)
            embedded_hwnd = 0
            if self.host_hwnd:
                embedded_hwnd = _wait_and_embed_window(
                    self.proc.pid,
                    self.host_hwnd,
                    self.host_width,
                    self.host_height,
                    timeout_s=1.0,
                )
            notice = ""
            try:
                messages = list((resp or {}).get("messages") or [])
                if messages:
                    notice = "\n".join(str(m) for m in messages[-3:])
            except Exception:
                notice = ""
            self.connected.emit(self.proc, notice, embedded_hwnd)
        except Exception as exc:
            proc = self.proc
            self.proc = None
            if proc is not None:
                _stop_process(proc, grace_s=0.05)
            self.failed.emit(str(exc))


def _stop_process(proc: subprocess.Popen, *, grace_s: float = 0.25) -> None:
    if proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            proc.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            proc.terminate()
        proc.wait(timeout=max(0.0, float(grace_s)))
    except Exception:
        try:
            proc.terminate()
            proc.wait(timeout=0.1)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


def _stop_process_async(proc: subprocess.Popen, *, grace_s: float = 0.05) -> None:
    try:
        if proc.poll() is not None:
            return
    except Exception:
        return

    try:
        threading.Thread(
            target=lambda: _stop_process(proc, grace_s=grace_s),
            name=f"direct-video-proc-stop-{getattr(proc, 'pid', 'unknown')}",
            daemon=True,
        ).start()
    except Exception:
        _stop_process(proc, grace_s=0.0)


def _disconnect_direct_worker(worker: QThread) -> None:
    for signal_name in ("receiver_started", "connected", "failed"):
        signal = getattr(worker, signal_name, None)
        disconnect = getattr(signal, "disconnect", None)
        if callable(disconnect):
            try:
                disconnect()
            except Exception:
                pass


def _abandon_direct_connect_worker(worker: QThread) -> None:
    _disconnect_direct_worker(worker)
    try:
        worker.setParent(None)
    except Exception:
        pass
    _ORPHANED_CONNECT_WORKERS.add(worker)

    def _finished() -> None:
        try:
            proc = getattr(worker, "proc", None)
            if proc is not None:
                _stop_process(proc, grace_s=0.02)
        except Exception:
            pass
        _ORPHANED_CONNECT_WORKERS.discard(worker)

    try:
        worker.finished.connect(_finished)
    except Exception:
        pass
    try:
        worker.quit()
    except Exception:
        pass


if os.name == "nt":
    _user32 = ctypes.windll.user32
    _GWL_STYLE = -16
    _WS_CHILD = 0x40000000
    _WS_VISIBLE = 0x10000000
    _WS_POPUP = 0x80000000
    _WS_CAPTION = 0x00C00000
    _WS_THICKFRAME = 0x00040000
    _WS_MINIMIZEBOX = 0x00020000
    _WS_MAXIMIZEBOX = 0x00010000
    _WS_SYSMENU = 0x00080000
    _WS_DISABLED = 0x08000000
    _SWP_NOZORDER = 0x0004
    _SWP_NOACTIVATE = 0x0010
    _SWP_FRAMECHANGED = 0x0020
    _SWP_SHOWWINDOW = 0x0040
    _SW_HIDE = 0

    _EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    class _RECT(ctypes.Structure):
        _fields_ = [
            ("left", ctypes.c_long),
            ("top", ctypes.c_long),
            ("right", ctypes.c_long),
            ("bottom", ctypes.c_long),
        ]

    if ctypes.sizeof(ctypes.c_void_p) == 8:
        _get_window_long = _user32.GetWindowLongPtrW
        _set_window_long = _user32.SetWindowLongPtrW
    else:
        _get_window_long = _user32.GetWindowLongW
        _set_window_long = _user32.SetWindowLongW


def _top_level_windows_for_pid(pid: int) -> list[int]:
    if os.name != "nt":
        return []
    matches: list[int] = []

    def _title(hwnd) -> str:
        length = _user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(length + 1)
        _user32.GetWindowTextW(hwnd, buf, length + 1)
        return str(buf.value or "")

    def _maybe_add(hwnd) -> None:
        proc_id = ctypes.c_ulong()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(proc_id))
        if int(proc_id.value) == int(pid) and _title(hwnd) == "Direct3D11 renderer":
            matches.append(int(hwnd))

    _EnumChildProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    @_EnumChildProc
    def _child_callback(hwnd, _lparam):
        _maybe_add(hwnd)
        return True

    @_EnumWindowsProc
    def _callback(hwnd, _lparam):
        _maybe_add(hwnd)
        _user32.EnumChildWindows(hwnd, _child_callback, 0)
        return True

    _user32.EnumWindows(_callback, 0)
    return matches


def _window_client_size(hwnd: int, fallback_width: int, fallback_height: int) -> tuple[int, int]:
    if os.name != "nt" or not hwnd:
        return max(1, int(fallback_width)), max(1, int(fallback_height))
    rect = _RECT()
    if _user32.GetClientRect(int(hwnd), ctypes.byref(rect)):
        width = int(rect.right - rect.left)
        height = int(rect.bottom - rect.top)
        if width > 0 and height > 0:
            return width, height
    return max(1, int(fallback_width)), max(1, int(fallback_height))


def _embed_window(child_hwnd: int, parent_hwnd: int, width: int, height: int) -> bool:
    if os.name != "nt" or not child_hwnd or not parent_hwnd:
        return False
    _user32.ShowWindow(child_hwnd, _SW_HIDE)
    style = int(_get_window_long(child_hwnd, _GWL_STYLE))
    # Keep the renderer visible, but do not let its foreign HWND eat pane
    # clicks that the Qt UI uses for active-camera selection.
    style |= _WS_CHILD | _WS_VISIBLE | _WS_DISABLED
    style &= ~(_WS_POPUP | _WS_CAPTION | _WS_THICKFRAME | _WS_MINIMIZEBOX | _WS_MAXIMIZEBOX | _WS_SYSMENU)
    _set_window_long(child_hwnd, _GWL_STYLE, style)
    _user32.SetParent(child_hwnd, int(parent_hwnd))
    native_width, native_height = _window_client_size(parent_hwnd, width, height)
    return bool(
        _user32.SetWindowPos(
            child_hwnd,
            0,
            0,
            0,
            native_width,
            native_height,
            _SWP_NOZORDER | _SWP_NOACTIVATE | _SWP_FRAMECHANGED | _SWP_SHOWWINDOW,
        )
    )


def _wait_and_embed_window(pid: int, parent_hwnd: int, width: int, height: int, *, timeout_s: float) -> int:
    if os.name != "nt" or not pid or not parent_hwnd:
        return 0
    deadline = time.monotonic() + max(0.0, float(timeout_s))
    while time.monotonic() <= deadline:
        for hwnd in _top_level_windows_for_pid(int(pid)):
            if _embed_window(hwnd, parent_hwnd, width, height):
                return int(hwnd)
        time.sleep(0.005)
    return 0


class _CaptureBadgeOverlay(QWidget):
    """Transparent top-level badge layer that can sit above a native video child."""

    def __init__(self, owner: QWidget):
        flags = (
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        super().__init__(owner, flags)
        self._owner = owner
        self.setObjectName("videoCaptureOverlay")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        self.record_badge = QLabel("REC 00:00", self)
        self.record_badge.setObjectName("videoRecordBadge")
        self.record_badge.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.record_badge.hide()

        self.snapshot_badge = QLabel("SNAP", self)
        self.snapshot_badge.setObjectName("videoSnapshotBadge")
        self.snapshot_badge.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.snapshot_badge.hide()
        self.hide()

    def sync(self) -> None:
        owner = self._owner
        if not owner.isVisible() or owner.width() <= 1 or owner.height() <= 1:
            self.hide()
            return
        app = QApplication.instance()
        if app is not None and app.activePopupWidget() is not None:
            self.hide()
            return
        window = owner.window()
        if window is not None and (window.isMinimized() or not window.isVisible()):
            self.hide()
            return

        self.resize(owner.size())
        self.move(owner.mapToGlobal(owner.rect().topLeft()))

        margin = 10
        visible_badges = []
        for badge, x_mode in (
            (self.record_badge, "left"),
            (self.snapshot_badge, "right"),
        ):
            if badge.isHidden():
                continue
            badge.adjustSize()
            y = margin
            if x_mode == "left":
                x = margin
            else:
                x = max(margin, self.width() - badge.width() - margin)
            badge.move(x, y)
            badge.raise_()
            visible_badges.append(badge)

        if visible_badges:
            self.show()
            self.raise_()
        else:
            self.hide()


class DirectGstVideoWidget(QWidget):
    """Low-latency video widget that lets GStreamer render directly to Direct3D."""

    activated = pyqtSignal()

    def __init__(self, manager: RemoteCameraManager, stream_name: str, parent=None, *, autostart: bool = True):
        super().__init__(parent)
        self.manager = manager
        self.stream_name = stream_name
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

        self._proc: subprocess.Popen | None = None
        self._connect_worker: _DirectConnectWorker | None = None
        self._connect_attempt_active = False
        self._embedded_hwnd: int | None = None
        self._state = "waiting"
        self._last_error: str | None = None
        self._connected_ts = 0.0
        self._retry_backoff_s = 0.5
        self._next_retry_ts = 0.0
        self._display_fps = 30.0
        self._water_correction_enabled = False
        self._square_display_enabled = False
        self._rov_link_lost = False
        self._rov_link_wait_message = f"{self.stream_name}\nROV link lost.\nWaiting for heartbeat..."
        self._capture_camera = None
        self._capture_lock = threading.RLock()
        self._viewport_reader: _ViewportFramePipe | None = None
        self._viewport_frame_pipe_enabled = False
        self._snapshot_frame_pipe_enabled = False
        self._capture_frame_pipe_requested = False
        self._last_rejected_capture_artifact_seq: int | None = None
        self._compressed_recording_available = False
        self._compressed_record_host = "127.0.0.1"
        self._compressed_record_port = 0
        self._compressed_record_latency_ms = 250
        self._rec: object | None = None
        self._record_thread: threading.Thread | None = None
        self._record_stop = threading.Event()
        self._record_started_ts: float | None = None
        self._record_started_monotonic_s: float | None = None
        self._record_elapsed_s: int = 0
        self._snapshot_indicator_until_ts: float = 0.0
        self._snapshot_indicator_text: str = "SNAP"
        self._snapshot_indicator_duration_s: float = 1.2
        self.last_frame = None
        self.last_frame_ts: float = 0.0
        self.frame_buffer = deque(maxlen=1)

        self.setAttribute(Qt.WidgetAttribute.WA_NativeWindow, True)
        self.setAttribute(Qt.WidgetAttribute.WA_DontCreateNativeAncestors, False)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMinimumSize(160, 90)

        self._message = QLabel(f"{self.stream_name}\nConnecting direct renderer...", self)
        self._message.setObjectName("videoPanePlaceholder")
        self._message.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._message.setWordWrap(True)
        self._message.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        self._capture_overlay = _CaptureBadgeOverlay(self)
        self._record_badge = self._capture_overlay.record_badge
        self._snapshot_badge = self._capture_overlay.snapshot_badge

        self._tick_timer = QTimer(self)
        self._tick_timer.setInterval(250)
        self._tick_timer.timeout.connect(self._tick)
        self._tick_timer.start()
        self._embed_timer = QTimer(self)
        self._embed_timer.setInterval(10)
        self._embed_timer.timeout.connect(self._try_embed)
        self._record_label_timer = QTimer(self)
        self._record_label_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._record_label_timer.setSingleShot(True)
        self._record_label_timer.setInterval(1000)
        self._record_label_timer.timeout.connect(self._on_record_label_tick)
        if autostart:
            self._start_connect()
        else:
            self._rov_link_lost = True
            self._rov_link_wait_message = f"{self.stream_name}\nWaiting for ROV heartbeat..."
            self._schedule_retry(1.0)
            self._show_message(self._rov_link_wait_message)

    def _show_message(self, text: str) -> None:
        self._message.setText(text)
        self._message.show()
        self._message.raise_()

    def _hide_message(self) -> None:
        self._message.hide()

    def _format_elapsed(self, elapsed_s: float) -> str:
        elapsed_s = max(0, int(elapsed_s))
        minutes, seconds = divmod(elapsed_s, 60)
        hours, minutes = divmod(minutes, 60)
        if hours > 0:
            return f"{hours:d}:{minutes:02d}:{seconds:02d}"
        return f"{minutes:02d}:{seconds:02d}"

    def _layout_capture_badges(self) -> None:
        self._capture_overlay.sync()

    def _record_elapsed_from_clock(self) -> int:
        started = self._record_started_monotonic_s
        if started is None:
            return 0
        elapsed = int(max(0.0, time.monotonic() - float(started)))
        if elapsed < self._record_elapsed_s:
            return int(self._record_elapsed_s)
        self._record_elapsed_s = elapsed
        return elapsed

    def _schedule_record_label_tick(self) -> None:
        if self._record_started_monotonic_s is None or self._rec is None:
            self._record_label_timer.stop()
            return
        elapsed = max(0.0, time.monotonic() - float(self._record_started_monotonic_s))
        next_second = int(elapsed) + 1
        delay_ms = int(max(25.0, min(1000.0, (next_second - elapsed) * 1000.0)))
        self._record_label_timer.start(delay_ms)

    def _on_record_label_tick(self) -> None:
        if self._record_started_ts is None or self._rec is None:
            self._record_label_timer.stop()
            return
        self._set_record_badge_elapsed(self._record_elapsed_from_clock())
        self._layout_capture_badges()
        self._schedule_record_label_tick()

    def _set_record_badge_elapsed(self, elapsed_s: int) -> None:
        text = f"REC {self._format_elapsed(elapsed_s)}"
        if self._record_badge.text() != text:
            self._record_badge.setText(text)
            trace_event(
                "mono_record_badge_update",
                stream=self.stream_name,
                elapsed_s=int(elapsed_s),
                text=text,
            )
        self._record_badge.show()

    def _refresh_capture_indicators(self) -> None:
        now = time.time()
        layout_needed = bool(self._capture_overlay.isVisible())
        record_was_visible = not self._record_badge.isHidden()
        snapshot_was_visible = not self._snapshot_badge.isHidden()
        if self._record_started_ts is not None and self._rec is not None:
            self._set_record_badge_elapsed(self._record_elapsed_from_clock())
            layout_needed = True
        else:
            if record_was_visible:
                self._record_badge.hide()
                layout_needed = True

        if self._snapshot_indicator_until_ts > now:
            if self._snapshot_badge.text() != self._snapshot_indicator_text:
                self._snapshot_badge.setText(self._snapshot_indicator_text)
            self._snapshot_badge.show()
            layout_needed = True
        else:
            if snapshot_was_visible:
                self._snapshot_badge.hide()
                layout_needed = True
        if layout_needed:
            self._layout_capture_badges()

    def _flash_snapshot_indicator(self, text: str = "SNAP") -> None:
        self._snapshot_indicator_text = str(text or "SNAP")
        self._snapshot_indicator_until_ts = time.time() + self._snapshot_indicator_duration_s
        self._refresh_capture_indicators()

    def _ensure_capture_camera(self):
        with self._capture_lock:
            if self._capture_camera is not None:
                return self._capture_camera
            opener = getattr(self.manager, "open_capture", None)
            if callable(opener):
                self._capture_camera = opener(self.stream_name)
            else:
                self._capture_camera = self.manager.open(self.stream_name)
            return self._capture_camera

    def _release_capture_camera(self, *, async_release: bool = False) -> None:
        with self._capture_lock:
            camera = self._capture_camera
            self._capture_camera = None
        if camera is None:
            return

        def _close() -> None:
            closer = getattr(self.manager, "close_capture", None)
            try:
                if callable(closer):
                    closer(self.stream_name)
                else:
                    self.manager.close(self.stream_name)
            except Exception:
                try:
                    camera.release()
                except Exception:
                    pass

        if async_release:
            async_closer = getattr(self.manager, "close_capture_async", None)
            try:
                if callable(async_closer) and bool(async_closer(self.stream_name)):
                    return
            except Exception:
                pass
            try:
                threading.Thread(
                    target=_close,
                    name=f"direct-video-capture-close-{self.stream_name}",
                    daemon=True,
                ).start()
                return
            except Exception:
                pass

        _close()

    def _stop_viewport_reader(self) -> None:
        reader = self._viewport_reader
        self._viewport_reader = None
        self._viewport_frame_pipe_enabled = False
        self._snapshot_frame_pipe_enabled = False
        if reader is not None:
            try:
                reader.stop()
            except Exception:
                pass

    def _clear_compressed_recording_route(self) -> None:
        self._compressed_recording_available = False
        self._compressed_record_host = "127.0.0.1"
        self._compressed_record_port = 0
        self._compressed_record_latency_ms = 250

    def _source_packet(self, source, *, consume: bool, allow_stale_latest: bool):
        packet = None
        if not consume:
            latest = getattr(source, "latest_frame_packet", None)
            if callable(latest):
                try:
                    packet = latest()
                except Exception:
                    packet = None
        if packet is None:
            reader = getattr(source, "read_frame_packet", None)
            if callable(reader):
                try:
                    packet = reader()
                except Exception:
                    packet = None
        if packet is None and consume and allow_stale_latest:
            latest = getattr(source, "latest_frame_packet", None)
            if callable(latest):
                try:
                    packet = latest()
                except Exception:
                    packet = None
        return packet

    def _store_packet(self, packet) -> None:
        self.last_frame = packet.frame_bgr
        self.last_frame_ts = time.time()
        try:
            self.frame_buffer.append(packet.frame_bgr)
        except Exception:
            pass

    def _capture_packet(
        self,
        *,
        wait_s: float = 0.0,
        consume: bool = False,
        allow_stale_latest: bool = True,
        allow_capture_fallback: bool = True,
        reject_unusable: bool = False,
    ):
        deadline = time.monotonic() + max(0.0, float(wait_s))
        fallback_opened = self._capture_camera is not None or self._viewport_reader is None
        if fallback_opened and self._capture_camera is None and bool(allow_capture_fallback):
            self._ensure_capture_camera()
        while True:
            packet = self._source_packet(
                self._viewport_reader,
                consume=consume,
                allow_stale_latest=allow_stale_latest and time.monotonic() >= deadline,
            ) if self._viewport_reader is not None and self._viewport_frame_pipe_enabled else None
            if packet is None and fallback_opened and self._capture_camera is not None:
                packet = self._source_packet(
                    self._capture_camera,
                    consume=consume,
                    allow_stale_latest=allow_stale_latest and time.monotonic() >= deadline,
                )
            if packet is not None:
                rejection_reason = capture_frame_rejection_reason(packet.frame_bgr) if reject_unusable else None
                if rejection_reason is not None:
                    try:
                        seq = int(getattr(packet, "seq", -1))
                    except Exception:
                        seq = -1
                    if seq != self._last_rejected_capture_artifact_seq:
                        self._last_rejected_capture_artifact_seq = seq
                        trace_event(
                            "direct_capture_frame_rejected",
                            stream=self.stream_name,
                            seq=seq,
                            reason=rejection_reason,
                        )
                    packet = None
                else:
                    self._last_rejected_capture_artifact_seq = None
            if packet is not None:
                self._store_packet(packet)
                return packet
            if time.monotonic() >= deadline:
                if bool(allow_capture_fallback) and not fallback_opened:
                    try:
                        self._ensure_capture_camera()
                        fallback_opened = True
                        deadline = time.monotonic() + max(0.25, min(0.5, float(wait_s) if wait_s else 0.25))
                        continue
                    except Exception:
                        return None
                return None
            time.sleep(0.02)

    def _ensure_capture_source(self, *, wait_s: float = 0.25) -> str:
        if self._viewport_reader is not None and self._viewport_frame_pipe_enabled:
            packet = self._capture_packet(
                wait_s=max(0.0, float(wait_s)),
                consume=False,
                allow_stale_latest=True,
                allow_capture_fallback=False,
            )
            if packet is not None:
                return "viewport"
        self._ensure_capture_camera()
        return "capture"

    def _restart_capture_stream_for_snapshot(self) -> bool:
        restart = getattr(self.manager, "restart_capture_stream", None)
        if not callable(restart):
            return False
        try:
            trace_event("direct_snapshot_capture_restart_request", stream=self.stream_name)
            restart(self.stream_name)
            trace_event("direct_snapshot_capture_restart_done", stream=self.stream_name)
            return True
        except Exception as exc:
            logger.warning("Snapshot capture restart failed for '%s': %s", self.stream_name, exc)
            trace_event("direct_snapshot_capture_restart_failed", stream=self.stream_name, error=str(exc))
            return False

    def _direct_render_snapshot_frame(self) -> tuple[np.ndarray, str] | None:
        """Grab a still frame without opening the separate capture receiver."""

        if self._viewport_reader is not None and (
            self._snapshot_frame_pipe_enabled or self._viewport_frame_pipe_enabled
        ):
            packet = self._source_packet(
                self._viewport_reader,
                consume=False,
                allow_stale_latest=True,
            )
            if packet is not None:
                rejection_reason = capture_frame_rejection_reason(packet.frame_bgr)
                if rejection_reason is None:
                    self._store_packet(packet)
                    source = "direct_snapshot_pipe"
                    if self._viewport_frame_pipe_enabled:
                        source = "direct_frame_pipe"
                    return np.array(packet.frame_bgr, copy=True), source
                trace_event(
                    "direct_snapshot_pipe_rejected",
                    stream=self.stream_name,
                    seq=int(getattr(packet, "seq", -1)),
                    reason=rejection_reason,
                )

        if not _env_truthy("TRITON_DIRECT_SCREEN_SNAPSHOT", False):
            return None

        hwnd = int(self._embedded_hwnd or 0)
        if hwnd <= 0:
            return None
        try:
            center = self.mapToGlobal(self.rect().center())
            screen = QApplication.screenAt(center) or QApplication.primaryScreen()
        except Exception:
            screen = QApplication.primaryScreen()
        if screen is None:
            return None
        try:
            pixmap = screen.grabWindow(hwnd)
        except Exception as exc:
            trace_event("direct_screen_snapshot_failed", stream=self.stream_name, error=str(exc))
            return None
        if pixmap.isNull():
            trace_event("direct_screen_snapshot_failed", stream=self.stream_name, error="null_pixmap")
            return None
        image = pixmap.toImage()
        if image.isNull() or image.width() <= 0 or image.height() <= 0:
            trace_event("direct_screen_snapshot_failed", stream=self.stream_name, error="null_image")
            return None
        image = image.convertToFormat(QImage.Format.Format_BGR888)
        width = int(image.width())
        height = int(image.height())
        bytes_per_line = int(image.bytesPerLine())
        try:
            bits = image.bits()
            bits.setsize(int(image.sizeInBytes()))
            arr = np.frombuffer(bits, dtype=np.uint8).reshape((height, bytes_per_line))
            frame = arr[:, : width * 3].reshape((height, width, 3)).copy()
        except Exception as exc:
            trace_event("direct_screen_snapshot_failed", stream=self.stream_name, error=str(exc))
            return None
        rejection_reason = capture_frame_rejection_reason(frame)
        if rejection_reason is not None:
            trace_event("direct_screen_snapshot_rejected", stream=self.stream_name, reason=rejection_reason)
            return None
        trace_event(
            "direct_screen_snapshot_grabbed",
            stream=self.stream_name,
            width=width,
            height=height,
        )
        return frame, "direct3d_screen"

    def _rov_snapshot_png_payload(self) -> bytes | None:
        """Capture one PNG from TritonOS without opening the topside mirror receiver."""

        rov = getattr(self.manager, "rov", None)
        capture = getattr(rov, "capture_frame", None)
        if not callable(capture):
            return None
        capture_s = time.monotonic()
        try:
            response = capture(stream=self.stream_name, wait_s=_snapshot_wait_s(), format="png")
        except Exception as exc:
            trace_event(
                "direct_snapshot_rov_capture_failed",
                stream=self.stream_name,
                dt_ms=(time.monotonic() - capture_s) * 1000.0,
                error=str(exc),
            )
            return None
        if not isinstance(response, dict):
            trace_event(
                "direct_snapshot_rov_capture_failed",
                stream=self.stream_name,
                dt_ms=(time.monotonic() - capture_s) * 1000.0,
                error="invalid_response",
            )
            return None
        fmt = str(response.get("format") or response.get("image_format") or "png").strip().lower()
        if fmt not in {"png", "image/png"}:
            trace_event(
                "direct_snapshot_rov_capture_failed",
                stream=self.stream_name,
                dt_ms=(time.monotonic() - capture_s) * 1000.0,
                error=f"unsupported_format:{fmt}",
            )
            return None
        raw = response.get("image_b64", response.get("payload_b64"))
        if not raw:
            trace_event(
                "direct_snapshot_rov_capture_failed",
                stream=self.stream_name,
                dt_ms=(time.monotonic() - capture_s) * 1000.0,
                error="missing_image_b64",
            )
            return None
        try:
            payload = base64.b64decode(str(raw), validate=True)
        except Exception as exc:
            trace_event(
                "direct_snapshot_rov_capture_failed",
                stream=self.stream_name,
                dt_ms=(time.monotonic() - capture_s) * 1000.0,
                error=str(exc),
            )
            return None
        if not payload:
            return None
        trace_event(
            "direct_snapshot_rov_capture_completed",
            stream=self.stream_name,
            seq=response.get("seq"),
            dt_ms=(time.monotonic() - capture_s) * 1000.0,
            shape=response.get("shape"),
        )
        return payload

    def latest_frame_packet(self):
        if self._viewport_reader is not None and self._viewport_frame_pipe_enabled:
            latest = getattr(self._viewport_reader, "latest_frame_packet", None)
            if callable(latest):
                try:
                    packet = latest()
                    if packet is not None:
                        return packet
                except Exception:
                    pass
        camera = self._capture_camera
        if camera is None:
            return None
        latest = getattr(camera, "latest_frame_packet", None)
        if not callable(latest):
            return None
        try:
            return latest()
        except Exception:
            return None

    def recent_frame_packets(self, *, max_age_s: float = 0.5):
        if self._viewport_reader is not None and self._viewport_frame_pipe_enabled:
            recent = getattr(self._viewport_reader, "recent_frame_packets", None)
            if callable(recent):
                try:
                    packets = list(recent(max_age_s=max_age_s))
                    if packets:
                        return packets
                except Exception:
                    pass
        camera = self._capture_camera
        if camera is None:
            return []
        recent = getattr(camera, "recent_frame_packets", None)
        if not callable(recent):
            packet = self.latest_frame_packet()
            return [] if packet is None else [packet]
        try:
            return list(recent(max_age_s=max_age_s))
        except Exception:
            return []

    def _schedule_retry(self, delay_s: float) -> None:
        self._next_retry_ts = time.time() + max(0.0, float(delay_s))

    def _start_connect(self) -> None:
        if self._rov_link_lost:
            self._show_message(self._rov_link_wait_message)
            self._schedule_retry(1.0)
            return
        if self._connect_attempt_active:
            return
        if self._proc is not None and self._proc.poll() is None:
            return
        self._state = "connecting"
        self._last_error = None
        self._embedded_hwnd = None
        self._show_message(f"{self.stream_name}\nConnecting direct renderer...")
        self._connect_worker = _DirectConnectWorker(
            self.manager,
            self.stream_name,
            host_hwnd=int(self.winId()),
            host_width=self.width(),
            host_height=self.height(),
            square_crop=bool(self._square_display_enabled),
            viewport_frame_pipe_requested=bool(self._capture_frame_pipe_requested),
            parent=self,
        )
        self._connect_worker.receiver_started.connect(self._on_receiver_started)
        self._connect_worker.connected.connect(self._on_connected)
        self._connect_worker.failed.connect(self._on_connect_failed)
        self._connect_attempt_active = True
        self._connect_worker.start()

    def _on_receiver_started(self, proc: subprocess.Popen, info: object = 0) -> None:
        self._proc = proc
        embedded_hwnd = 0
        frame_pipe = False
        frame_pipe_mode = ""
        frame_width = 0
        frame_height = 0
        record_rtp_port = 0
        record_rtp_host = "127.0.0.1"
        record_latency_ms = 250
        codec = ""
        if isinstance(info, dict):
            frame_pipe = bool(info.get("frame_pipe"))
            frame_pipe_mode = str(info.get("frame_pipe_mode") or "")
            try:
                frame_width = int(info.get("frame_width") or 0)
                frame_height = int(info.get("frame_height") or 0)
            except Exception:
                frame_width = 0
                frame_height = 0
            try:
                record_rtp_port = int(info.get("record_rtp_port") or 0)
                record_latency_ms = int(info.get("record_latency_ms") or 250)
            except Exception:
                record_rtp_port = 0
                record_latency_ms = 250
            record_rtp_host = str(info.get("record_rtp_host") or "127.0.0.1")
            codec = str(info.get("codec") or "")
        else:
            try:
                embedded_hwnd = int(info or 0)
            except Exception:
                embedded_hwnd = 0
        self._stop_viewport_reader()
        self._compressed_recording_available = codec.lower() == "h264" and record_rtp_port > 0
        self._compressed_record_host = record_rtp_host or "127.0.0.1"
        self._compressed_record_port = max(0, int(record_rtp_port))
        self._compressed_record_latency_ms = max(0, int(record_latency_ms))
        if frame_pipe and frame_width > 0 and frame_height > 0:
            self._viewport_reader = _ViewportFramePipe(self.stream_name, frame_width, frame_height)
            self._viewport_frame_pipe_enabled = frame_pipe_mode == "capture"
            self._snapshot_frame_pipe_enabled = frame_pipe_mode in {"snapshot", "capture"}
            try:
                self._viewport_reader.start(proc.stdout)
            except Exception as exc:
                self._stop_viewport_reader()
                logger.warning("Could not start viewport frame pipe for '%s': %s", self.stream_name, exc)
        if embedded_hwnd:
            self._embedded_hwnd = int(embedded_hwnd)
            self._hide_message()
        else:
            self._show_message(f"{self.stream_name}\nWaiting for Direct3D window...")
            self._try_embed()
        if not self._embedded_hwnd and not self._embed_timer.isActive():
            self._embed_timer.start()

    def _on_connected(self, proc: subprocess.Popen, notice: str, embedded_hwnd: int = 0) -> None:
        self._connect_attempt_active = False
        self._connect_worker = None
        self._proc = proc
        self._rov_link_lost = False
        if embedded_hwnd:
            self._embedded_hwnd = int(embedded_hwnd)
        self._state = "playing"
        self._connected_ts = time.time()
        self._retry_backoff_s = 0.5
        if self._embedded_hwnd:
            self._hide_message()
        elif notice:
            self._show_message(f"{self.stream_name}\nConnected:\n{notice}")
        else:
            self._show_message(f"{self.stream_name}\nWaiting for Direct3D window...")
        if not self._embedded_hwnd and not self._embed_timer.isActive():
            self._embed_timer.start()
        if self._viewport_reader is None:
            threading.Thread(target=self._log_stream, args=(proc.stdout, "OUT"), daemon=True).start()
        threading.Thread(target=self._log_stream, args=(proc.stderr, "ERR"), daemon=True).start()

    def _on_connect_failed(self, error: str) -> None:
        self._connect_attempt_active = False
        self._connect_worker = None
        try:
            self._embed_timer.stop()
        except Exception:
            pass
        self._proc = None
        self._embedded_hwnd = None
        self._stop_viewport_reader()
        self._clear_compressed_recording_route()
        self._last_error = error
        self._state = "waiting"
        self._retry_backoff_s = min(self._retry_backoff_s * 1.5, 5.0)
        self._schedule_retry(self._retry_backoff_s)
        self._show_message(f"{self.stream_name}\nDirect renderer unavailable. Retrying...\n\n{error}")

    def _log_stream(self, stream, label: str) -> None:
        if stream is None:
            return
        for line in iter(stream.readline, b""):
            text = line.decode(errors="replace").rstrip()
            if not _suppress_gst_stderr_line(text):
                logger.info("[direct-gst:%s:%s] %s", self.stream_name, label, text)

    def _try_embed(self) -> None:
        proc = self._proc
        if proc is None or proc.poll() is not None or self._embedded_hwnd:
            try:
                self._embed_timer.stop()
            except Exception:
                pass
            return
        host_hwnd = int(self.winId())
        for hwnd in _top_level_windows_for_pid(proc.pid):
            if _embed_window(hwnd, host_hwnd, self.width(), self.height()):
                self._embedded_hwnd = hwnd
                self._hide_message()
                try:
                    self._embed_timer.stop()
                except Exception:
                    pass
                return

    def _resize_embedded(self) -> None:
        hwnd = self._embedded_hwnd
        if not hwnd or os.name != "nt":
            return
        host_hwnd = int(self.winId())
        native_width, native_height = _window_client_size(host_hwnd, self.width(), self.height())
        _user32.SetWindowPos(
            int(hwnd),
            0,
            0,
            0,
            native_width,
            native_height,
            _SWP_NOZORDER | _SWP_NOACTIVATE | _SWP_FRAMECHANGED | _SWP_SHOWWINDOW,
        )
        try:
            _user32.InvalidateRect(int(hwnd), None, True)
            _user32.UpdateWindow(int(hwnd))
        except Exception:
            pass

    def refresh_layout_geometry(self) -> None:
        self._message.setGeometry(0, 0, self.width(), self.height())
        if self._embedded_hwnd and os.name == "nt":
            if not _embed_window(int(self._embedded_hwnd), int(self.winId()), self.width(), self.height()):
                self._embedded_hwnd = None
                self._try_embed()
        else:
            self._try_embed()
        self._resize_embedded()
        self._layout_capture_badges()

    def _tick(self) -> None:
        now = time.time()
        proc = self._proc
        if proc is not None and proc.poll() is not None:
            self._proc = None
            self._embedded_hwnd = None
            self._stop_viewport_reader()
            self._clear_compressed_recording_route()
            try:
                self._embed_timer.stop()
            except Exception:
                pass
            self._state = "waiting"
            self._last_error = f"GStreamer renderer exited with code {proc.returncode}"
            try:
                self.manager.rov.stop_stream(name=self.stream_name)
            except Exception:
                pass
            self._retry_backoff_s = min(self._retry_backoff_s * 1.5, 5.0)
            self._schedule_retry(self._retry_backoff_s)
            self._show_message(f"{self.stream_name}\nRenderer stopped. Reconnecting...")
            return

        if self._state == "playing" and self._embedded_hwnd is None:
            self._try_embed()
        elif self._state == "connecting":
            return
        elif now >= self._next_retry_ts:
            self._start_connect()
        self._refresh_capture_indicators()

    def _force_reconnect(self, message: str, *, retry_delay_s: float = 0.2) -> None:
        try:
            self.shutdown(release_only=True, async_release=True)
        except Exception:
            pass
        self._state = "waiting"
        self._last_error = message.replace("\n", " ")
        self._show_message(message)
        self._retry_backoff_s = 0.5
        if not self._rov_link_lost:
            self._schedule_retry(retry_delay_s)

    def set_capture_frame_pipe_enabled(self, enabled: bool) -> None:
        """Temporarily capture frames from the direct renderer pipeline."""

        enabled = bool(enabled)
        if enabled == self._capture_frame_pipe_requested:
            return
        self._capture_frame_pipe_requested = enabled
        trace_event(
            "direct_capture_frame_pipe_requested",
            stream=self.stream_name,
            enabled=enabled,
            active=self._proc is not None and self._proc.poll() is None,
            active_frame_pipe=self._viewport_frame_pipe_enabled,
        )

    def set_rov_link_status(self, status: str) -> None:
        status_key = str(status or "").strip().upper()
        if status_key in {"LOST", "NO DATA", "TETHER", "TETHER LOST", "TETHER UNREACHABLE"}:
            if status_key == "NO DATA":
                self._rov_link_wait_message = f"{self.stream_name}\nWaiting for ROV heartbeat..."
            elif status_key.startswith("TETHER"):
                self._rov_link_wait_message = f"{self.stream_name}\nTETHER NETWORK UNREACHABLE\nWaiting for tether..."
            else:
                self._rov_link_wait_message = f"{self.stream_name}\nROV link lost.\nWaiting for heartbeat..."
            if self._rov_link_lost:
                self._show_message(self._rov_link_wait_message)
                return
            self._rov_link_lost = True
            trace_event("direct_video_link_lost", stream=self.stream_name)
            self._force_reconnect(
                self._rov_link_wait_message,
                retry_delay_s=0.0,
            )
            return
        if status_key == "OK" and self._rov_link_lost:
            self._rov_link_lost = False
            self._rov_link_wait_message = f"{self.stream_name}\nROV link lost.\nWaiting for heartbeat..."
            trace_event("direct_video_link_recovered", stream=self.stream_name)
            self._force_reconnect(
                f"{self.stream_name}\nROV heartbeat recovered.\nReconnecting video...",
                retry_delay_s=0.1,
            )

    def status(self) -> dict:
        age = max(0.0, time.time() - self._connected_ts) if self._connected_ts > 0 else None
        return {
            "state": self._state,
            "age_s": age,
            "last_error": self._last_error,
            "render_mode": "direct3d",
            "rov_link_lost": bool(self._rov_link_lost),
        }

    def water_correction_enabled(self) -> bool:
        return bool(self._water_correction_enabled)

    def set_water_correction(self, enabled: bool) -> None:
        # Direct-render mode intentionally bypasses CPU frame transforms.
        self._water_correction_enabled = bool(enabled)

    def is_recording(self) -> bool:
        return self._rec is not None

    def display_fps(self) -> float:
        return float(self._display_fps)

    def set_display_fps(self, fps: float) -> None:
        try:
            self._display_fps = float(fps)
        except Exception:
            self._display_fps = 30.0

    def set_square_display_enabled(self, enabled: bool) -> None:
        enabled = bool(enabled)
        if enabled == self._square_display_enabled:
            return
        self._square_display_enabled = enabled
        if self._rec is not None:
            trace_event(
                "direct_video_square_display_deferred_while_recording",
                stream=self.stream_name,
                enabled=enabled,
            )
            return
        if self._proc is not None or self._connect_attempt_active:
            trace_event(
                "direct_video_square_display_changed",
                stream=self.stream_name,
                enabled=enabled,
            )
            self._force_reconnect(
                f"{self.stream_name}\nUpdating square transect view...",
                retry_delay_s=0.05,
            )

    def _start_compressed_recording(self, out_file: str, *, fps: float) -> str | None:
        proc = self._proc
        if (
            not self._compressed_recording_available
            or self._compressed_record_port <= 0
            or proc is None
            or proc.poll() is not None
        ):
            return None
        recorder_start_s = time.monotonic()
        rec = CompressedRtpRecorder(
            out_file,
            name=self.stream_name,
            port=int(self._compressed_record_port),
            bind_address=self._compressed_record_host,
            latency_ms=int(self._compressed_record_latency_ms),
        )
        try:
            target = rec.start()
        except Exception as exc:
            logger.warning("Could not start compressed recorder for '%s': %s", self.stream_name, exc)
            trace_event(
                "mono_compressed_recorder_start_failed",
                stream=self.stream_name,
                out_file=out_file,
                port=int(self._compressed_record_port),
                dt_ms=(time.monotonic() - recorder_start_s) * 1000.0,
                error=str(exc),
            )
            return None
        self._rec = rec
        self._record_thread = None
        self._record_stop.clear()
        trace_event(
            "mono_recorder_started",
            stream=self.stream_name,
            out_file=out_file,
            target=target,
            backend="compressed_h264_rtp",
            port=int(self._compressed_record_port),
            fps=fps,
            dt_ms=(time.monotonic() - recorder_start_s) * 1000.0,
        )
        self._refresh_capture_indicators()
        self._schedule_record_label_tick()
        return str(target)

    def start_recording(self, out_dir: str | None = None, basename: str | None = None, fps: float = 30.0) -> str | None:
        if self._rec is not None:
            target = self._rec.target
            return str(target) if target is not None else None

        if out_dir is None:
            out_dir = str(DEFAULT_RECORDINGS_DIR)
        Path(out_dir).mkdir(parents=True, exist_ok=True)

        if basename is None:
            base = timestamped_camera_stem(self.stream_name, "video")
        else:
            base = Path(basename).stem or self.stream_name
        out_file = unique_capture_path(out_dir, base, ".mp4")

        record_started_wall = time.time()
        record_started_mono = time.monotonic()
        trace_event(
            "mono_record_start_request",
            stream=self.stream_name,
            out_file=out_file,
            fps=fps,
            record_started_wall_s=record_started_wall,
            record_started_mono_s=record_started_mono,
        )
        self._record_started_ts = record_started_wall
        self._record_started_monotonic_s = record_started_mono
        self._record_elapsed_s = 0
        self._set_record_badge_elapsed(0)
        self._schedule_record_label_tick()

        compressed_target = self._start_compressed_recording(out_file, fps=fps)
        if compressed_target is not None:
            return compressed_target

        capture_open_s = time.monotonic()
        source = "capture"
        try:
            source = self._ensure_capture_source(wait_s=0.35)
        except Exception as exc:
            logger.warning("Could not prepare capture source for '%s': %s", self.stream_name, exc)
            trace_event(
                "mono_capture_receiver_failed",
                stream=self.stream_name,
                out_file=out_file,
                dt_ms=(time.monotonic() - capture_open_s) * 1000.0,
                error=str(exc),
            )
            self._record_started_ts = None
            self._record_started_monotonic_s = None
            self._record_elapsed_s = 0
            self._record_label_timer.stop()
            self._refresh_capture_indicators()
            return None
        trace_event(
            "mono_capture_receiver_ready",
            stream=self.stream_name,
            out_file=out_file,
            source=source,
            dt_ms=(time.monotonic() - capture_open_s) * 1000.0,
        )

        rec = VideoRecorder(out_file, fps=fps)
        recorder_start_s = time.monotonic()
        try:
            target = rec.start()
        except Exception as exc:
            logger.warning("Could not start recorder for '%s': %s", self.stream_name, exc)
            trace_event(
                "mono_recorder_start_failed",
                stream=self.stream_name,
                out_file=out_file,
                dt_ms=(time.monotonic() - recorder_start_s) * 1000.0,
                error=str(exc),
            )
            self._record_started_ts = None
            self._record_started_monotonic_s = None
            self._record_elapsed_s = 0
            self._record_label_timer.stop()
            self._refresh_capture_indicators()
            return None
        trace_event(
            "mono_recorder_started",
            stream=self.stream_name,
            out_file=out_file,
            target=target,
            dt_ms=(time.monotonic() - recorder_start_s) * 1000.0,
        )

        self._rec = rec
        self._record_stop.clear()

        def _record_loop() -> None:
            period_s = 1.0 / max(1.0, float(fps or 30.0))
            next_ts = float(record_started_mono)
            last_frame = None
            last_seq = None
            last_frame_rx_s = record_started_mono
            frame_index = 0
            skipped_startup_artifacts = 0
            startup_artifact_deadline_s = record_started_mono + float(_STARTUP_ARTIFACT_MAX_SKIP_S)
            stale_reuse_limit_s = max(0.5, period_s * 4.0)
            trace_event(
                "mono_record_loop_started",
                stream=self.stream_name,
                out_file=out_file,
                fps=fps,
                period_ms=period_s * 1000.0,
            )
            while not self._record_stop.is_set():
                if last_frame is None:
                    try:
                        packet = self._capture_packet(
                            wait_s=min(0.25, max(0.02, period_s)),
                            consume=True,
                            allow_stale_latest=True,
                        )
                    except Exception as exc:
                        logger.warning("Capture read failed for '%s': %s", self.stream_name, exc)
                        packet = None
                    if packet is None:
                        trace_event(
                            "mono_record_waiting_first_frame",
                            stream=self.stream_name,
                            out_file=out_file,
                            elapsed_ms=(time.monotonic() - record_started_mono) * 1000.0,
                        )
                        self._record_stop.wait(0.02)
                        continue
                    if (
                        time.monotonic() < startup_artifact_deadline_s
                        and _looks_like_green_startup_artifact(packet.frame_bgr)
                    ):
                        skipped_startup_artifacts += 1
                        trace_event(
                            "mono_record_startup_artifact_skipped",
                            stream=self.stream_name,
                            out_file=out_file,
                            seq=int(getattr(packet, "seq", -1)),
                            skipped=skipped_startup_artifacts,
                            elapsed_ms=(time.monotonic() - record_started_mono) * 1000.0,
                        )
                        self._record_stop.wait(0.02)
                        continue
                    last_frame = packet.frame_bgr
                    last_seq = int(getattr(packet, "seq", -1))
                    last_frame_rx_s = time.monotonic()
                    if skipped_startup_artifacts:
                        next_ts = time.monotonic()
                    trace_event(
                        "mono_record_first_frame",
                        stream=self.stream_name,
                        out_file=out_file,
                        seq=last_seq,
                        skipped_startup_artifacts=skipped_startup_artifacts,
                        frame_age_ms=(time.monotonic() - float(getattr(packet, "monotonic_ts", time.monotonic()))) * 1000.0,
                        elapsed_ms=(time.monotonic() - record_started_mono) * 1000.0,
                    )

                sleep_s = next_ts - time.monotonic()
                if sleep_s > 0:
                    self._record_stop.wait(min(sleep_s, 0.1))
                    continue
                due_s = next_ts
                now_s = time.monotonic()
                packet = None
                reused = True
                try:
                    packet = self._capture_packet(
                        wait_s=min(0.05, max(0.0, period_s * 0.75)),
                        consume=True,
                        allow_stale_latest=False,
                    )
                    if packet is not None:
                        last_frame = packet.frame_bgr
                        seq = int(getattr(packet, "seq", -1))
                        reused = seq == last_seq
                        last_seq = seq
                        last_frame_rx_s = time.monotonic()
                except Exception as exc:
                    logger.warning("Capture read failed for '%s': %s", self.stream_name, exc)
                    trace_event(
                        "mono_record_capture_read_failed",
                        stream=self.stream_name,
                        out_file=out_file,
                        frame_index=frame_index,
                        error=str(exc),
                    )
                seq = last_seq
                frame_age_ms = None
                if packet is not None:
                    try:
                        frame_age_ms = (time.monotonic() - float(packet.monotonic_ts)) * 1000.0
                    except Exception:
                        frame_age_ms = None
                if packet is None and (time.monotonic() - float(last_frame_rx_s)) > stale_reuse_limit_s:
                    frame_index += 1
                    trace_event(
                        "mono_record_tick_skipped_stale",
                        stream=self.stream_name,
                        out_file=out_file,
                        frame_index=frame_index,
                        seq=last_seq,
                        queue_size=getattr(rec, "queue_size", lambda: -1)(),
                        stale_ms=(time.monotonic() - float(last_frame_rx_s)) * 1000.0,
                    )
                    next_ts += period_s
                    continue
                try:
                    accepted = bool(rec.add_frame(last_frame))
                except Exception:
                    accepted = False
                frame_index += 1
                trace_event(
                    "mono_record_tick",
                    stream=self.stream_name,
                    out_file=out_file,
                    frame_index=frame_index,
                    seq=seq,
                    reused=reused,
                    accepted=accepted,
                    queue_size=getattr(rec, "queue_size", lambda: -1)(),
                    due_elapsed_ms=(due_s - record_started_mono) * 1000.0,
                    lag_ms=(now_s - due_s) * 1000.0,
                    frame_age_ms=frame_age_ms,
                    label_elapsed_s=self._record_elapsed_s,
                )
                next_ts += period_s
            trace_event(
                "mono_record_loop_stopped",
                stream=self.stream_name,
                out_file=out_file,
                frame_index=frame_index,
                queue_size=getattr(rec, "queue_size", lambda: -1)(),
            )

        self._record_thread = threading.Thread(
            target=_record_loop,
            name=f"direct-video-rec-{self.stream_name}",
            daemon=True,
        )
        self._record_thread.start()
        trace_event("mono_record_thread_started", stream=self.stream_name, out_file=out_file, target=target)
        self._refresh_capture_indicators()
        self._schedule_record_label_tick()
        return str(target)

    def stop_recording(self) -> None:
        rec = self._rec
        thread = self._record_thread
        if rec is None:
            return
        trace_event(
            "mono_record_stop_request",
            stream=self.stream_name,
            target=getattr(rec, "target", None),
            queue_size=getattr(rec, "queue_size", lambda: -1)(),
            elapsed_s=self._record_elapsed_s,
        )
        self._rec = None
        self._record_thread = None
        self._record_started_ts = None
        self._record_started_monotonic_s = None
        self._record_elapsed_s = 0
        self._record_stop.set()
        self._record_label_timer.stop()
        self._refresh_capture_indicators()

        def _finish_recording() -> None:
            finish_s = time.monotonic()
            trace_event(
                "mono_record_finish_started",
                stream=self.stream_name,
                target=getattr(rec, "target", None),
                queue_size=getattr(rec, "queue_size", lambda: -1)(),
            )
            try:
                if thread is not None:
                    thread.join(timeout=1.5)
            except Exception:
                pass
            try:
                try:
                    rec.stop(timeout_s=10.0)
                except TypeError:
                    rec.stop()
            except Exception as exc:
                logger.warning("Video recording finalization failed for '%s': %s", self.stream_name, exc)
                trace_event(
                    "mono_record_finish_failed",
                    stream=self.stream_name,
                    target=getattr(rec, "target", None),
                    dt_ms=(time.monotonic() - finish_s) * 1000.0,
                    error=str(exc),
                )
                return
            trace_event(
                "mono_record_finished",
                stream=self.stream_name,
                target=getattr(rec, "target", None),
                dt_ms=(time.monotonic() - finish_s) * 1000.0,
                queue_size=getattr(rec, "queue_size", lambda: -1)(),
            )

        try:
            threading.Thread(
                target=_finish_recording,
                name=f"direct-video-rec-stop-{self.stream_name}",
                daemon=True,
            ).start()
        except Exception:
            _finish_recording()

    def save_snapshot(self, out_dir: str | None = None, basename: str | None = None) -> str | None:
        if out_dir is None:
            out_dir = str(DEFAULT_RECORDINGS_DIR)
        Path(out_dir).mkdir(parents=True, exist_ok=True)

        if basename is None:
            base = timestamped_camera_stem(self.stream_name, "snapshot")
        else:
            base = Path(basename).stem or self.stream_name
        out_path = unique_capture_path(out_dir, base, ".png")

        direct_snapshot = self._direct_render_snapshot_frame()
        direct_frame = None
        rov_png_payload = None
        snapshot_source = "capture"
        if direct_snapshot is not None:
            direct_frame, snapshot_source = direct_snapshot
        else:
            rov_png_payload = self._rov_snapshot_png_payload()
            if rov_png_payload is not None:
                snapshot_source = "rov_capture_rpc"
            else:
                try:
                    snapshot_source = self._ensure_capture_source(wait_s=0.25)
                except Exception as exc:
                    logger.warning("Could not prepare capture source for snapshot '%s': %s", self.stream_name, exc)
                    return None
        trace_event("direct_snapshot_source_selected", stream=self.stream_name, source=snapshot_source)
        if direct_frame is not None:
            frame = np.array(direct_frame, copy=True)
        elif rov_png_payload is not None:
            frame = None
        else:
            packet = self._capture_packet(
                wait_s=_snapshot_wait_s(),
                consume=True,
                allow_stale_latest=False,
                reject_unusable=True,
            )
            if packet is None and self._restart_capture_stream_for_snapshot():
                packet = self._capture_packet(
                    wait_s=max(_snapshot_wait_s(), 3.0),
                    consume=True,
                    allow_stale_latest=False,
                    reject_unusable=True,
                )
            if packet is None:
                logger.warning("Snapshot skipped for '%s': no fresh usable capture frame available", self.stream_name)
                return None
            frame = np.array(packet.frame_bgr, copy=True)

        def _write_snapshot() -> None:
            try:
                if rov_png_payload is not None:
                    Path(out_path).write_bytes(rov_png_payload)
                else:
                    save_snapshot(frame, out_path)
            except Exception as exc:
                logger.warning("Snapshot write failed for '%s' -> %s: %s", self.stream_name, out_path, exc)

        try:
            threading.Thread(
                target=_write_snapshot,
                name=f"direct-video-snapshot-{self.stream_name}",
                daemon=True,
            ).start()
        except Exception:
            try:
                _write_snapshot()
            except Exception:
                return None

        self._flash_snapshot_indicator("SNAP")
        return str(out_path)

    def _stop_connect_worker(self, *, async_release: bool = False) -> None:
        if self._connect_worker is None:
            return
        worker = self._connect_worker
        self._connect_attempt_active = False
        self._connect_worker = None
        if async_release:
            _abandon_direct_connect_worker(worker)
            return
        try:
            worker.quit()
            worker.wait(5000)
        except Exception:
            pass

    def shutdown(self, release_only: bool = True, *, async_release: bool = True) -> None:
        try:
            self.stop_recording()
        except Exception:
            pass
        self._release_capture_camera(async_release=bool(async_release))
        self._stop_viewport_reader()
        self._clear_compressed_recording_route()
        self._stop_connect_worker(async_release=bool(async_release))
        proc = self._proc
        self._proc = None
        self._embedded_hwnd = None
        try:
            self._embed_timer.stop()
        except Exception:
            pass
        try:
            self._capture_overlay.hide()
        except Exception:
            pass
        if proc is not None:
            if async_release:
                _stop_process_async(proc, grace_s=0.05)
            else:
                _stop_process(proc)

        def _stop_remote_stream() -> None:
            try:
                self.manager.rov.stop_stream(name=self.stream_name)
            except Exception:
                pass

        if async_release:
            try:
                threading.Thread(
                    target=_stop_remote_stream,
                    name=f"direct-video-remote-stop-{self.stream_name}",
                    daemon=True,
                ).start()
            except Exception:
                pass
        else:
            _stop_remote_stream()
        self._state = "waiting"

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._message.setGeometry(0, 0, self.width(), self.height())
        self._resize_embedded()
        self._layout_capture_badges()

    def moveEvent(self, event) -> None:
        super().moveEvent(event)
        self._layout_capture_badges()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._layout_capture_badges()

    def hideEvent(self, event) -> None:
        try:
            self._capture_overlay.hide()
        except Exception:
            pass
        super().hideEvent(event)

    def closeEvent(self, event) -> None:
        try:
            self._tick_timer.stop()
        except Exception:
            pass
        try:
            self._embed_timer.stop()
        except Exception:
            pass
        try:
            self._record_label_timer.stop()
        except Exception:
            pass
        self.shutdown(release_only=True)
        try:
            self._capture_overlay.close()
        except Exception:
            pass
        super().closeEvent(event)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.activated.emit()
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        try:
            self._rov_link_lost = False
            self._force_reconnect(
                f"{self.stream_name}\nManual reconnect requested...",
                retry_delay_s=0.1,
            )
        except Exception:
            pass
        super().mouseDoubleClickEvent(event)
