"""Direct GStreamer/Direct3D video pane for low-latency pilot viewing."""

from __future__ import annotations

import ctypes
import logging
import os
import signal
import socket
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Any

from PyQt6.QtCore import Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtWidgets import QApplication, QLabel, QSizePolicy, QVBoxLayout, QWidget

from config import VIDEO_RPC_ENDPOINT
from network.net_select import choose_video_receive_ip, parse_zmq_endpoint
from recording.capture_trace import trace_event
from recording.video_recorder import (
    RECORD_FANOUT_HOST,
    cv_fanout_port,
    liveness_fanout_port,
    record_fanout_port,
)
from video.gst_receiver import _suppress_gst_stderr_line, _win_kill_udp_port_users
from video.gst_runtime import bootstrap_gstreamer_env
from video.cam import RemoteCameraManager


logger = logging.getLogger(__name__)
_ORPHANED_CONNECT_WORKERS: set[QThread] = set()


@dataclass(frozen=True)
class DirectReceiverConfig:
    name: str
    codec: str
    port: int
    bind_address: str
    width: int = 0
    height: int = 0
    latency_ms: int = 50
    udp_buffer_size: int = 4 * 1024 * 1024
    drop_on_latency: bool = True
    h264_decoder: str = "openh264dec"
    sink: str = "d3d11videosink"
    square_crop: bool = False
    # When > 0, also fan the raw received RTP out to 127.0.0.1:<port> via a tee so
    # local consumers can read the exact stream the laptop already gets -- full
    # quality, no extra tether load. record_fanout_port feeds the mp4 recorder;
    # cv_fanout_port feeds the transect tracker's own decode. Each fan-out branch
    # is leaky so it can never back-pressure or stall the live display, and
    # sending to a loopback port with no listener is harmless on Windows
    # (verified: no WSAECONNRESET pipeline death).
    record_fanout_port: int = 0
    cv_fanout_port: int = 0
    # Dedicated loopback port the widget binds to count datagrams as a liveness
    # signal (see _LivenessProbe). Leaky like the others; no listener required.
    liveness_fanout_port: int = 0


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
    name = str(decoder or "").strip().lower() or "openh264dec"
    if name in {"auto", "hardware", "decodebin"}:
        return ["decodebin"]
    return [name]


# Decoders that output Direct3D11 GPU memory. Pairing them with d3d11convert +
# d3d11videosink keeps frames on the GPU (no per-frame GPU->CPU download), which
# is the whole point of hardware decode: it offloads the 4x 1080p30 decode from
# the CPU so a busy topside can't starve a decoder and freeze the display.
_D3D11_DECODERS = {"d3d11h264dec", "d3d11h264device1dec"}


def _decoder_outputs_d3d11(decoder: str) -> bool:
    return str(decoder or "").strip().lower() in _D3D11_DECODERS


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


def _render_output_chain(
    cfg: DirectReceiverConfig,
    crop_chain: list[str] | None = None,
) -> list[str]:
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
    output_chain = [*crop_chain, *_render_output_chain(cfg)]

    if cfg.codec.lower() == "h264":
        # Keep frames on the GPU when a Direct3D11 hardware decoder is selected
        # (d3d11convert), but fall back to CPU videoconvert for software decoders
        # or when a square crop is needed (videocrop is a CPU element).
        use_d3d11 = _decoder_outputs_d3d11(cfg.h264_decoder) and not crop_chain
        convert = "d3d11convert" if use_d3d11 else "videoconvert"
        caps = "application/x-rtp,media=video,encoding-name=H264,payload=96,clock-rate=90000"
        display_tail = [
            "rtpjitterbuffer", f"latency={cfg.latency_ms}",
            f"drop-on-latency={drop_on_latency}", "faststart-min-packets=1",
            "!", "rtph264depay",
            "!", "h264parse", "config-interval=-1", "disable-passthrough=true",
            "!", *_h264_decoder_chain(cfg.h264_decoder),
            "!", convert,
            *output_chain,
        ]
    else:
        caps = "application/x-rtp,media=video,encoding-name=JPEG,payload=26,clock-rate=90000"
        display_tail = [
            "rtpjitterbuffer", f"latency={cfg.latency_ms}",
            f"drop-on-latency={drop_on_latency}", "faststart-min-packets=1",
            "!", "rtpjpegdepay",
            "!", "jpegdec",
            "!", "videoconvert",
            *output_chain,
        ]

    src = [
        "udpsrc", f"address={cfg.bind_address}", "reuse=true", f"port={cfg.port}",
        f"buffer-size={udp_buffer_size}", f"caps={caps}",
    ]

    fanout_ports = [
        p for p in (
            int(getattr(cfg, "record_fanout_port", 0) or 0),
            int(getattr(cfg, "cv_fanout_port", 0) or 0),
            int(getattr(cfg, "liveness_fanout_port", 0) or 0),
        ) if p > 0
    ]
    if fanout_ports:
        # Tee the raw RTP: one branch feeds the live display exactly as before, the
        # rest forward the same UDP packets to loopback ports for local consumers
        # (recorder, transect CV). Each tee branch needs its own queue (separate
        # threads). The display queue is non-leaky so it never drops packets before
        # the jitter buffer (matching the old udpsrc->jitterbuffer back-pressure).
        # Each fan-out queue is leaky so a stalled/absent consumer can never back up
        # the tee and freeze the display.
        pipeline = [
            *src,
            "!", "tee", "name=rtptee",
            "rtptee.",
            "!", "queue", "max-size-buffers=512", "max-size-bytes=0", "max-size-time=0",
            "!", *display_tail,
        ]
        for fp in fanout_ports:
            pipeline += [
                "rtptee.",
                "!", "queue", "max-size-buffers=512", "max-size-bytes=0", "max-size-time=0",
                "leaky=downstream",
                "!", "udpsink", f"host={RECORD_FANOUT_HOST}", f"port={fp}",
                "sync=false", "async=false",
            ]
    else:
        pipeline = [*src, "!", *display_tail]
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

    # If a topside video recording is active for this stream, fold its mirror
    # UDP port into the sender config so the duplicate RTP feed survives any
    # display reconnect (start_stream rebuilds from this config). The recorder
    # also applies the mirror live via update_stream, but a reconnect would
    # otherwise wipe it; this keeps the recording robust.
    mirrors = getattr(manager, "recording_mirror_ports", {}).get(stream_name)
    if mirrors:
        extra = dict(options.get("extra") or {})
        existing = list(extra.get("udp_mirror_ports") or [])
        extra["udp_mirror_ports"] = existing + [p for p in mirrors if p not in existing]
        options["extra"] = extra
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
        parent=None,
    ):
        super().__init__(parent)
        self.manager = manager
        self.stream_name = stream_name
        self.host_hwnd = int(host_hwnd or 0)
        self.host_width = max(1, int(host_width or 1))
        self.host_height = max(1, int(host_height or 1))
        self.square_crop = bool(square_crop)
        self.proc: subprocess.Popen | None = None
        self.suppressor: "_RendererSuppressor | None" = None

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
                    receiver_extra.get("h264_decoder", "openh264dec"),
                )
            )
            sink = str(stream_opts.get("receiver_direct_sink", stream_opts.get("direct_sink", "d3d11videosink")))
            width = int(stream_opts.get("width", start_kwargs.get("width", 0)) or 0)
            height = int(stream_opts.get("height", start_kwargs.get("height", 0)) or 0)
            # Always provide the loopback fan-outs so a recording or the transect CV
            # can start (and survive display reconnects) with zero tether cost. Idle
            # fan-out to a port with no listener is harmless. Opt out per stream if
            # ever needed.
            enable_fanout = _truthy(stream_opts.get("enable_local_record", True), default=True)
            cfg = DirectReceiverConfig(
                name=self.stream_name,
                codec=codec,
                port=port,
                bind_address=host if bool(stream_opts.get("bind_receiver_to_host", True)) else "0.0.0.0",
                width=width,
                height=height,
                latency_ms=int(stream_opts.get("latency_ms", 50)),
                udp_buffer_size=int(stream_opts.get("receiver_udp_buffer_size", 4 * 1024 * 1024)),
                drop_on_latency=bool(stream_opts.get("receiver_drop_on_latency", True)),
                h264_decoder=h264_decoder,
                sink=sink,
                square_crop=bool(self.square_crop),
                record_fanout_port=record_fanout_port(port) if enable_fanout else 0,
                cv_fanout_port=cv_fanout_port(port) if enable_fanout else 0,
                liveness_fanout_port=liveness_fanout_port(port) if enable_fanout else 0,
            )
            cmd = build_direct_receiver_cmd(_find_gst_launch(), cfg)
            env = dict(os.environ)
            bootstrap_gstreamer_env(env)
            # Opt-in freeze diagnostics: surface decoder errors / sink QoS drops /
            # jitterbuffer warnings in the captured stderr without per-packet spam.
            # e.g. TRITON_VIDEO_GST_DEBUG=2  (warnings) or  rtpjitterbuffer:5  (packet loss).
            gst_debug = os.environ.get("TRITON_VIDEO_GST_DEBUG", "").strip()
            if gst_debug:
                env["GST_DEBUG"] = gst_debug
            creationflags = 0
            startupinfo = None
            if os.name == "nt":
                creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
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
            # Start suppressing the renderer's top-level window immediately, on
            # this worker thread, so it is hidden the instant it appears (before
            # any UI-thread signal round-trip can let it flash).
            try:
                self.suppressor = _RendererSuppressor(self.proc)
                self.suppressor.start()
            except Exception:
                self.suppressor = None
            receiver_info = {
                "codec": codec,
                "liveness_port": int(cfg.liveness_fanout_port or 0),
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


def _pid_top_level_windows(pid: int) -> list[int]:
    """All top-level window handles owned by ``pid`` (no title filter)."""
    if os.name != "nt" or not pid:
        return []
    matches: list[int] = []

    @_EnumWindowsProc
    def _callback(hwnd, _lparam):
        proc_id = ctypes.c_ulong()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(proc_id))
        if int(proc_id.value) == int(pid):
            matches.append(int(hwnd))
        return True

    _user32.EnumWindows(_callback, 0)
    return matches


class _RendererSuppressor:
    """Keep the renderer's top-level window hidden until it is embedded.

    The d3d11videosink subprocess creates a top-level "Direct3D11 renderer"
    window that we reparent into the Qt pane; between creation and reparent it
    would briefly flash on screen. This runs a tight poll on its OWN thread,
    started the instant the subprocess is spawned (off the UI thread, so a busy
    loading->pilot transition can't delay it), hiding any top-level (non-child)
    window the renderer process owns. Once the embed path reparents the window
    (it becomes WS_CHILD), the suppressor leaves it alone, so the reparent's
    re-show wins. Self-terminates when embedded, on process exit, or on timeout.
    """

    def __init__(self, proc: "subprocess.Popen", *, timeout_s: float = 15.0, interval_s: float = 0.004):
        self._proc = proc
        self._timeout_s = float(timeout_s)
        self._interval_s = float(interval_s)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if os.name != "nt" or self._proc is None:
            return
        pid = int(getattr(self._proc, "pid", 0) or 0)
        if not pid:
            return
        self._thread = threading.Thread(
            target=self._run, name=f"renderer-suppress-{pid}", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        pid = int(getattr(self._proc, "pid", 0) or 0)
        deadline = time.monotonic() + self._timeout_s
        while not self._stop.is_set() and time.monotonic() < deadline:
            try:
                if self._proc.poll() is not None:
                    return
            except Exception:
                return
            try:
                for hwnd in _pid_top_level_windows(pid):
                    try:
                        style = int(_get_window_long(hwnd, _GWL_STYLE))
                        if not (style & _WS_CHILD):
                            _user32.ShowWindow(hwnd, _SW_HIDE)
                    except Exception:
                        pass
            except Exception:
                pass
            self._stop.wait(self._interval_s)


class _LivenessProbe:
    """Counts RTP datagrams fanned out to a dedicated loopback port.

    The direct-render path never touches frames in Python (GStreamer renders
    straight to Direct3D), so a frozen pane whose renderer process is still alive
    is otherwise invisible to us. The pipeline tees the received RTP to a private
    loopback port; this binds that port and bumps ``last_ts`` on every datagram.
    A watchdog in the widget then treats a long gap as "stream silently died"
    and forces a reconnect. Fully passive and isolated: the port is unique per
    stream and nothing else ever binds it, so it cannot disturb the display.
    """

    def __init__(self, port: int):
        self.port = int(port)
        self.last_ts: float = 0.0
        self.packets: int = 0
        self._sock: socket.socket | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> bool:
        if self.port <= 0:
            return False
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((RECORD_FANOUT_HOST, self.port))
            sock.settimeout(0.5)
        except Exception:
            try:
                sock.close()  # type: ignore[union-attr]
            except Exception:
                pass
            return False
        self._sock = sock
        # Seed last_ts so the watchdog grace window starts now, not at epoch.
        self.last_ts = time.monotonic()
        self._thread = threading.Thread(
            target=self._run, name=f"video-liveness-{self.port}", daemon=True
        )
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        sock = self._sock
        self._sock = None
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass

    def _run(self) -> None:
        sock = self._sock
        if sock is None:
            return
        while not self._stop.is_set():
            try:
                data = sock.recv(65535)
            except socket.timeout:
                continue
            except OSError:
                return
            except Exception:
                return
            if data:
                self.packets += 1
                self.last_ts = time.monotonic()


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
        self._connect_started_ts = 0.0
        self._embedded_hwnd: int | None = None
        self._window_suppressor: "_RendererSuppressor | None" = None
        self._liveness: "_LivenessProbe | None" = None
        self._state = "waiting"
        self._last_error: str | None = None
        self._connected_ts = 0.0
        self._retry_backoff_s = 0.5
        self._next_retry_ts = 0.0
        # Self-healing watchdog windows. The direct renderer process staying
        # alive is NOT proof the pane is live: the ROV sender can stop, a camera
        # can drop, or the Direct3D window can fail to embed -- all leave a frozen
        # pane that the old code never recovered from. These force a reconnect.
        self._data_watchdog_s = 8.0   # no RTP datagrams while playing -> dead feed
        self._embed_watchdog_s = 9.0  # process alive but never showed a window
        self._connect_watchdog_s = 25.0  # a connect attempt that never returns
        self._display_fps = 30.0
        self._water_correction_enabled = False
        self._square_display_enabled = False
        self._rov_link_lost = False
        self._rov_link_wait_message = f"{self.stream_name}\nROV link lost.\nWaiting for heartbeat..."
        self.last_frame = None
        self.last_frame_ts: float = 0.0

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

        self._tick_timer = QTimer(self)
        self._tick_timer.setInterval(250)
        self._tick_timer.timeout.connect(self._tick)
        self._tick_timer.start()
        self._embed_timer = QTimer(self)
        self._embed_timer.setInterval(10)
        self._embed_timer.timeout.connect(self._try_embed)
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
        self._connect_started_ts = time.time()
        self._show_message(f"{self.stream_name}\nConnecting direct renderer...")
        self._connect_worker = _DirectConnectWorker(
            self.manager,
            self.stream_name,
            host_hwnd=int(self.winId()),
            host_width=self.width(),
            host_height=self.height(),
            square_crop=bool(self._square_display_enabled),
            parent=self,
        )
        self._connect_worker.receiver_started.connect(self._on_receiver_started)
        self._connect_worker.connected.connect(self._on_connected)
        self._connect_worker.failed.connect(self._on_connect_failed)
        self._connect_attempt_active = True
        self._connect_worker.start()

    def _start_liveness_probe(self, port: int) -> None:
        self._stop_liveness_probe()
        if port <= 0:
            return
        probe = _LivenessProbe(port)
        if probe.start():
            self._liveness = probe

    def _stop_liveness_probe(self) -> None:
        probe = self._liveness
        self._liveness = None
        if probe is not None:
            try:
                probe.stop()
            except Exception:
                pass

    def _data_age_s(self) -> float | None:
        """Seconds since the last RTP datagram, or None when no probe is active."""
        probe = self._liveness
        if probe is None or probe.last_ts <= 0:
            return None
        return max(0.0, time.monotonic() - probe.last_ts)

    def _remove_window_suppressor(self) -> None:
        suppressor = self._window_suppressor
        self._window_suppressor = None
        if suppressor is not None:
            try:
                suppressor.stop()
            except Exception:
                pass

    def _on_receiver_started(self, proc: subprocess.Popen, info: object = 0) -> None:
        self._proc = proc
        # Adopt the suppressor the worker already started at spawn time (it keeps
        # the renderer window hidden until we reparent it) so we can stop it once
        # embedded / torn down.
        worker = self._connect_worker
        if worker is not None and getattr(worker, "suppressor", None) is not None:
            self._remove_window_suppressor()
            self._window_suppressor = worker.suppressor
        embedded_hwnd = 0
        if isinstance(info, dict):
            self._start_liveness_probe(int(info.get("liveness_port", 0) or 0))
        else:
            try:
                embedded_hwnd = int(info or 0)
            except Exception:
                embedded_hwnd = 0
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
        threading.Thread(target=self._log_stream, args=(proc.stdout, "OUT"), daemon=True).start()
        threading.Thread(target=self._log_stream, args=(proc.stderr, "ERR"), daemon=True).start()

    def _on_connect_failed(self, error: str) -> None:
        self._connect_attempt_active = False
        self._connect_worker = None
        try:
            self._embed_timer.stop()
        except Exception:
            pass
        self._remove_window_suppressor()
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

    def _tick(self) -> None:
        now = time.time()
        proc = self._proc
        if proc is not None and proc.poll() is not None:
            self._proc = None
            self._embedded_hwnd = None
            self._remove_window_suppressor()
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

        # A connect attempt that never returns (e.g. the RPC wedged) would leave
        # the pane stuck "Connecting..." forever -- bound it and retry.
        if self._connect_attempt_active:
            if self._connect_started_ts > 0 and now - self._connect_started_ts > self._connect_watchdog_s:
                trace_event("direct_video_connect_watchdog", stream=self.stream_name)
                self._force_reconnect(
                    f"{self.stream_name}\nConnect timed out. Retrying...",
                    retry_delay_s=0.2,
                )
            return

        if self._state == "playing":
            # Watchdog 1: renderer process alive but no video data is arriving
            # (ROV sender stopped, camera dropped, start/stop raced). Self-heal.
            data_age = self._data_age_s()
            if data_age is not None and data_age > self._data_watchdog_s:
                trace_event(
                    "direct_video_data_watchdog",
                    stream=self.stream_name,
                    data_age_s=round(data_age, 2),
                )
                self._force_reconnect(
                    f"{self.stream_name}\nNo video data. Reconnecting...",
                    retry_delay_s=0.2,
                )
                return
            # Watchdog 2: connected but the Direct3D window never embedded.
            if self._embedded_hwnd is None:
                self._try_embed()
                if (
                    self._embedded_hwnd is None
                    and self._connected_ts > 0
                    and now - self._connected_ts > self._embed_watchdog_s
                ):
                    trace_event("direct_video_embed_watchdog", stream=self.stream_name)
                    self._force_reconnect(
                        f"{self.stream_name}\nRenderer window stuck. Reconnecting...",
                        retry_delay_s=0.2,
                    )
            return

        if now >= self._next_retry_ts:
            self._start_connect()

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
        self._stop_connect_worker(async_release=bool(async_release))
        self._stop_liveness_probe()
        self._remove_window_suppressor()
        proc = self._proc
        self._proc = None
        self._embedded_hwnd = None
        try:
            self._embed_timer.stop()
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

    def moveEvent(self, event) -> None:
        super().moveEvent(event)

    def showEvent(self, event) -> None:
        super().showEvent(event)

    def hideEvent(self, event) -> None:
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
