"""Direct GStreamer/Direct3D video pane for low-latency pilot viewing."""

from __future__ import annotations

import ctypes
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
from PyQt6.QtWidgets import QLabel, QSizePolicy, QVBoxLayout, QWidget

from config import VIDEO_RPC_ENDPOINT
from network.net_select import choose_video_receive_ip, parse_zmq_endpoint
from recording.capture_paths import timestamped_camera_stem, unique_capture_path
from recording.save_location import DEFAULT_RECORDINGS_DIR
from recording.video_recorder import VideoRecorder, save_snapshot
from video.gst_receiver import _suppress_gst_stderr_line, _win_kill_udp_port_users
from video.gst_runtime import bootstrap_gstreamer_env
from video.cam import RemoteCameraManager


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DirectReceiverConfig:
    name: str
    codec: str
    port: int
    bind_address: str
    latency_ms: int = 5
    udp_buffer_size: int = 4 * 1024 * 1024
    drop_on_latency: bool = True
    h264_decoder: str = "decodebin"
    sink: str = "d3d11videosink"


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


def build_direct_receiver_cmd(gst_launch: str, cfg: DirectReceiverConfig) -> list[str]:
    """Build a direct-render RTP receiver pipeline.

    Unlike the legacy pilot widget, this keeps frames inside GStreamer and lets
    the video sink render through Direct3D. No raw 1080p BGR frames are copied
    through Python.
    """

    base = [str(gst_launch), "--gst-disable-registry-fork", "-q"]
    udp_buffer_size = max(262144, int(cfg.udp_buffer_size))
    drop_on_latency = "true" if cfg.drop_on_latency else "false"
    sink = str(cfg.sink or "d3d11videosink").strip() or "d3d11videosink"
    sink_props = ["sync=false", "async=false", "force-aspect-ratio=true"]

    if cfg.codec.lower() == "h264":
        caps = "application/x-rtp,media=video,encoding-name=H264,payload=96,clock-rate=90000"
        pipeline = [
            "udpsrc", f"address={cfg.bind_address}", "reuse=true", f"port={cfg.port}",
            f"buffer-size={udp_buffer_size}", f"caps={caps}",
            "!", "rtpjitterbuffer", f"latency={cfg.latency_ms}",
            f"drop-on-latency={drop_on_latency}", "faststart-min-packets=1",
            "!", "rtph264depay",
            "!", "h264parse", "config-interval=-1", "disable-passthrough=true",
            "!", *_h264_decoder_chain(cfg.h264_decoder),
            "!", "videoconvert",
            "!", "queue", "max-size-buffers=1", "max-size-bytes=0",
            "max-size-time=0", "leaky=downstream",
            "!", sink, *sink_props,
        ]
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
            "!", "queue", "max-size-buffers=1", "max-size-bytes=0",
            "max-size-time=0", "leaky=downstream",
            "!", sink, *sink_props,
        ]
    return base + pipeline


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
    rov_host, rov_port = parse_zmq_endpoint(VIDEO_RPC_ENDPOINT)
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
    receiver_started = pyqtSignal(object)
    connected = pyqtSignal(object, str)
    failed = pyqtSignal(str)

    def __init__(self, manager: RemoteCameraManager, stream_name: str, parent=None):
        super().__init__(parent)
        self.manager = manager
        self.stream_name = stream_name
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

            cfg = DirectReceiverConfig(
                name=self.stream_name,
                codec=codec,
                port=port,
                bind_address=host if bool(stream_opts.get("bind_receiver_to_host", True)) else "0.0.0.0",
                latency_ms=int(stream_opts.get("latency_ms", 5)),
                udp_buffer_size=int(stream_opts.get("receiver_udp_buffer_size", 4 * 1024 * 1024)),
                drop_on_latency=bool(stream_opts.get("receiver_drop_on_latency", True)),
                h264_decoder=h264_decoder,
                sink=sink,
            )
            cmd = build_direct_receiver_cmd(_find_gst_launch(), cfg)
            env = dict(os.environ)
            bootstrap_gstreamer_env(env)
            creationflags = 0
            if os.name == "nt":
                creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
            self.proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                creationflags=creationflags,
                bufsize=0,
            )
            self.receiver_started.emit(self.proc)

            # Start the sender after the UDP listener exists.
            resp = self.manager.rov.start_stream(**start_kwargs)
            notice = ""
            try:
                messages = list((resp or {}).get("messages") or [])
                if messages:
                    notice = "\n".join(str(m) for m in messages[-3:])
            except Exception:
                notice = ""
            self.connected.emit(self.proc, notice)
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
        if int(proc_id.value) == int(pid) and _user32.IsWindowVisible(hwnd) and _title(hwnd) == "Direct3D11 renderer":
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


class _CaptureBadgeOverlay(QWidget):
    """Transparent top-level badge layer that can sit above a native video child."""

    def __init__(self, owner: QWidget):
        flags = (
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowDoesNotAcceptFocus
            | Qt.WindowType.WindowStaysOnTopHint
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

    def __init__(self, manager: RemoteCameraManager, stream_name: str, parent=None):
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
        self._capture_camera = None
        self._capture_lock = threading.RLock()
        self._rec: VideoRecorder | None = None
        self._record_thread: threading.Thread | None = None
        self._record_stop = threading.Event()
        self._record_started_ts: float | None = None
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
        self._embed_timer.setInterval(50)
        self._embed_timer.timeout.connect(self._try_embed)
        self._start_connect()

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

    def _refresh_capture_indicators(self) -> None:
        now = time.time()
        if self._record_started_ts is not None and self._rec is not None:
            text = f"REC {self._format_elapsed(now - self._record_started_ts)}"
            if self._record_badge.text() != text:
                self._record_badge.setText(text)
            self._record_badge.show()
        else:
            self._record_badge.hide()

        if self._snapshot_indicator_until_ts > now:
            if self._snapshot_badge.text() != self._snapshot_indicator_text:
                self._snapshot_badge.setText(self._snapshot_indicator_text)
            self._snapshot_badge.show()
        else:
            self._snapshot_badge.hide()
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

    def _release_capture_camera(self) -> None:
        with self._capture_lock:
            camera = self._capture_camera
            self._capture_camera = None
        if camera is None:
            return
        closer = getattr(self.manager, "close_capture", None)
        try:
            if callable(closer):
                closer(self.stream_name)
            else:
                self.manager.close(self.stream_name)
        except Exception:
            pass

    def _capture_packet(self, *, wait_s: float = 0.0, consume: bool = False):
        camera = self._ensure_capture_camera()
        deadline = time.monotonic() + max(0.0, float(wait_s))
        while True:
            packet = None
            if not consume:
                latest = getattr(camera, "latest_frame_packet", None)
                if callable(latest):
                    try:
                        packet = latest()
                    except Exception:
                        packet = None
            if packet is None:
                reader = getattr(camera, "read_frame_packet", None)
                if callable(reader):
                    try:
                        packet = reader()
                    except Exception:
                        packet = None
            if packet is None and consume:
                latest = getattr(camera, "latest_frame_packet", None)
                if callable(latest) and time.monotonic() >= deadline:
                    # Last resort for very short clips: return something rather
                    # than failing to start a file before the next keyframe.
                    try:
                        packet = latest()
                    except Exception:
                        packet = None
            if packet is not None:
                self.last_frame = packet.frame_bgr
                self.last_frame_ts = time.time()
                try:
                    self.frame_buffer.append(packet.frame_bgr)
                except Exception:
                    pass
                return packet
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.02)

    def latest_frame_packet(self):
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
        if self._connect_attempt_active:
            return
        if self._proc is not None and self._proc.poll() is None:
            return
        self._state = "connecting"
        self._last_error = None
        self._embedded_hwnd = None
        self._show_message(f"{self.stream_name}\nConnecting direct renderer...")
        self._connect_worker = _DirectConnectWorker(self.manager, self.stream_name, parent=self)
        self._connect_worker.receiver_started.connect(self._on_receiver_started)
        self._connect_worker.connected.connect(self._on_connected)
        self._connect_worker.failed.connect(self._on_connect_failed)
        self._connect_attempt_active = True
        self._connect_worker.start()

    def _on_receiver_started(self, proc: subprocess.Popen) -> None:
        self._proc = proc
        self._show_message(f"{self.stream_name}\nWaiting for Direct3D window...")
        self._try_embed()
        if not self._embedded_hwnd and not self._embed_timer.isActive():
            self._embed_timer.start()

    def _on_connected(self, proc: subprocess.Popen, notice: str) -> None:
        self._connect_attempt_active = False
        self._connect_worker = None
        self._proc = proc
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
        _user32.MoveWindow(hwnd, 0, 0, native_width, native_height, True)

    def _tick(self) -> None:
        now = time.time()
        proc = self._proc
        if proc is not None and proc.poll() is not None:
            self._proc = None
            self._embedded_hwnd = None
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

        if self._state == "playing":
            self._try_embed()
        elif self._state == "connecting":
            return
        elif now >= self._next_retry_ts:
            self._start_connect()
        self._refresh_capture_indicators()

    def status(self) -> dict:
        age = max(0.0, time.time() - self._connected_ts) if self._connected_ts > 0 else None
        return {
            "state": self._state,
            "age_s": age,
            "last_error": self._last_error,
            "render_mode": "direct3d",
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

        try:
            self._ensure_capture_camera()
        except Exception as exc:
            logger.warning("Could not open capture receiver for '%s': %s", self.stream_name, exc)
            return None

        rec = VideoRecorder(out_file, fps=fps)
        try:
            target = rec.start()
        except Exception as exc:
            logger.warning("Could not start recorder for '%s': %s", self.stream_name, exc)
            return None

        self._rec = rec
        self._record_stop.clear()
        self._record_started_ts = time.time()

        def _record_loop() -> None:
            period_s = 1.0 / max(1.0, float(fps or 30.0))
            next_ts = time.monotonic()
            while not self._record_stop.is_set():
                packet = None
                try:
                    packet = self._capture_packet(wait_s=min(0.25, period_s), consume=True)
                except Exception as exc:
                    logger.warning("Capture read failed for '%s': %s", self.stream_name, exc)
                    time.sleep(0.05)
                if packet is not None:
                    try:
                        rec.add_frame(packet.frame_bgr)
                    except Exception:
                        pass
                next_ts += period_s
                sleep_s = next_ts - time.monotonic()
                if sleep_s > 0:
                    self._record_stop.wait(min(sleep_s, 0.1))
                else:
                    next_ts = time.monotonic()

        self._record_thread = threading.Thread(
            target=_record_loop,
            name=f"direct-video-rec-{self.stream_name}",
            daemon=True,
        )
        self._record_thread.start()
        self._refresh_capture_indicators()
        return str(target)

    def stop_recording(self) -> None:
        rec = self._rec
        thread = self._record_thread
        if rec is None:
            return
        self._rec = None
        self._record_thread = None
        self._record_started_ts = None
        self._record_stop.set()
        self._refresh_capture_indicators()

        def _finish_recording() -> None:
            try:
                if thread is not None:
                    thread.join(timeout=1.5)
            except Exception:
                pass
            try:
                rec.stop()
            except Exception as exc:
                logger.warning("Video recording finalization failed for '%s': %s", self.stream_name, exc)

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

        try:
            self._ensure_capture_camera()
        except Exception as exc:
            logger.warning("Could not open capture receiver for snapshot '%s': %s", self.stream_name, exc)
            return None

        def _write_snapshot() -> None:
            try:
                packet = self._capture_packet(wait_s=2.0)
                if packet is None:
                    logger.warning("Snapshot skipped for '%s': no capture frame available", self.stream_name)
                    return
                frame = np.array(packet.frame_bgr, copy=True)
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

    def _stop_connect_worker(self) -> None:
        if self._connect_worker is None:
            return
        worker = self._connect_worker
        try:
            worker.quit()
            worker.wait(5000)
        except Exception:
            pass
        self._connect_attempt_active = False
        self._connect_worker = None

    def shutdown(self, release_only: bool = True, *, async_release: bool = True) -> None:
        try:
            self.stop_recording()
        except Exception:
            pass
        self._release_capture_camera()
        self._stop_connect_worker()
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
            _stop_process(proc)
        try:
            self.manager.rov.stop_stream(name=self.stream_name)
        except Exception:
            pass
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
            self.shutdown(release_only=True)
            self._schedule_retry(0.1)
        except Exception:
            pass
        super().mouseDoubleClickEvent(event)
