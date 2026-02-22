"""
Windows GStreamer receiver using subprocesses only (no gi)
Now with optional per-camera channel order, but keeping the fast core logic.

channel_order can be:
    "BGR" (default, no extra work)
    "RGB", "BRG", "RBG", "GBR", "GRB"
"""
from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
import logging
from dataclasses import dataclass, asdict, field
from shutil import which
from typing import Dict, Optional, Any, List
import sys
import re

logger = logging.getLogger("gst_receiver_subproc")
if not logger.handlers:
    h = logging.StreamHandler()
    h.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s %(name)s: %(message)s"))
    logger.addHandler(h)
logger.setLevel(logging.INFO)


def _find_gst_launch() -> str:
    env_val = os.environ.get("GST_LAUNCH")
    if env_val and os.path.exists(env_val):
        return env_val

    p = which("gst-launch-1.0") or which("gst-launch-1.0.exe")
    if p:
        return p

    candidates = [
        r"C:\gstreamer\1.0\msvc_x86_64\bin\gst-launch-1.0.exe",
        r"C:\gstreamer\1.0\mingw_x86_64\bin\gst-launch-1.0.exe",
        r"C:\Program Files\GStreamer\1.0\msvc_x86_64\bin\gst-launch-1.0.exe",
        r"C:\Program Files\GStreamer\1.0\bin\gst-launch-1.0.exe",
    ]
    for c in candidates:
        if os.path.exists(c):
            return c

    raise FileNotFoundError(
        "Could not find 'gst-launch-1.0'. "
        "Install GStreamer (Complete) and either add its /bin to PATH or set GST_LAUNCH to the full exe path."
    )


def _win_list_udp_port_pids(port: int) -> list[tuple[int, str]]:
    """
    Returns [(pid, image_name), ...] for processes bound to UDP :port on Windows.
    We use `netstat -ano -p udp` + `tasklist /FI "PID eq ..."`.
    """
    results: list[tuple[int, str]] = []
    if os.name != "nt":
        return results

    try:
        # netstat -ano -p udp
        out = subprocess.check_output(
            ["netstat", "-ano", "-p", "udp"],
            text=True,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
        )
    except Exception:
        return results

    # lines look like:
    #  UDP    0.0.0.0:5000           *:*                                    1234
    #  UDP    [::]:5000              *:*                                    1234
    port_pat = f":{port} "
    pids: set[int] = set()

    for line in out.splitlines():
        if port_pat in line:
            parts = line.split()
            if parts:
                # PID is usually the last column
                try:
                    pid = int(parts[-1])
                    pids.add(pid)
                except ValueError:
                    pass

    # now resolve image name via tasklist
    for pid in pids:
        name = ""
        try:
            t_out = subprocess.check_output(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                text=True,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
            ).strip()
            # e.g. "gst-launch-1.0.exe","1234","Console","1","10,416 K"
            if t_out and not t_out.lower().startswith("info:"):
                name = t_out.split(",", 1)[0].strip().strip('"')
        except Exception:
            pass

        results.append((pid, name))
    return results


def _win_kill_udp_port_users(
    port: int,
    allowed_names: tuple[str, ...] = ("gst-launch-1.0.exe", "gst-launch-1.0", "python.exe", "python3.exe"),
):
    """
    Best-effort: kill any *likely* old receiver holding this UDP port.
    We only kill processes whose image name matches allowed_names.
    """
    if os.name != "nt":
        return

    conflicts = _win_list_udp_port_pids(port)
    for pid, name in conflicts:
        if name.lower() not in {n.lower() for n in allowed_names}:
            # don't kill random stuff
            continue
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
            )
        except Exception:
            pass

@dataclass
class RxConfig:
    name: str
    codec: str = "jpeg"       # 'jpeg' or 'h264'
    port: int = 5000
    # Bind the UDP receiver to a specific local interface address.
    # Default "0.0.0.0" listens on all interfaces.
    bind_address: str = "0.0.0.0"
    latency_ms: int = 60
    sink: str = "autovideosink"
    sync: bool = False
    record_path: Optional[str] = None
    mode: str = "window"      # "window" or "raw"
    width: int = 1280
    height: int = 720
    # NEW: we won’t apply this in the reader thread, only when the user asks for the frame
    channel_order: str = "BGR"
    extra: Dict[str, Any] = field(default_factory=dict)


class ReceiverProcess:
    _VALID_ORDERS = {"BGR", "RGB", "BRG", "RBG", "GBR", "GRB"}

    def __init__(self, cfg: RxConfig):
        if cfg.channel_order.upper() not in self._VALID_ORDERS:
            raise ValueError(f"channel_order must be one of {self._VALID_ORDERS}")
        self.cfg = cfg
        self.proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self._gst = _find_gst_launch()

        self._frame_size = self.cfg.width * self.cfg.height * 3
        self._raw_buffer_lock = threading.Lock()
        self._latest_frame: Optional[bytes] = None
        # Sequence bookkeeping so callers can tell whether a *new* frame arrived.
        # Without this, callers will keep re-reading the last frame forever when
        # the sender disappears (e.g. ROV reboot), making the UI think the stream
        # is still live.
        self._latest_seq: int = 0
        self._last_delivered_seq: int = 0
        self._latest_frame_ts: float = 0.0
        self._reader_thread: Optional[threading.Thread] = None
        self._stop_reader = threading.Event()

    def start(self):
        with self._lock:
            if self.proc and self.proc.poll() is None:
                logger.warning("Receiver '%s' already running", self.cfg.name)
                return

            # 👇 NEW: make sure no old receiver is sitting on this UDP port
            _win_kill_udp_port_users(self.cfg.port)

            # Reset frame bookkeeping so the first frame after (re)start is treated as new.
            with self._raw_buffer_lock:
                self._latest_frame = None
                self._latest_seq = 0
                self._last_delivered_seq = 0
                self._latest_frame_ts = 0.0

            cmd = self._build_cmd(self.cfg)
            logger.info("Starting receiver '%s': %s", self.cfg.name, " ".join(cmd))
            creationflags = 0
            if os.name == "nt":
                creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
            logger.info("Starting receiver '%s': %s", self.cfg.name, " ".join(cmd))
            creationflags = 0
            if os.name == "nt":
                creationflags = subprocess.CREATE_NEW_PROCESS_GROUP

            # >>> CHANGE 1: in raw mode we must NOT mix stderr into stdout
            if self.cfg.mode == "raw":
                self.proc = subprocess.Popen(
                    cmd,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,      # pure video bytes here
                    stderr=subprocess.PIPE,      # log separately
                    creationflags=creationflags,
                    bufsize=0,
                )
                self._stop_reader.clear()
                self._reader_thread = threading.Thread(
                    target=self._raw_reader_loop, name=f"raw-{self.cfg.name}", daemon=True
                )
                self._reader_thread.start()

                # start a small logger for stderr so we still see GStreamer errors
                threading.Thread(
                    target=self._log_stderr, name=f"gst-err-{self.cfg.name}", daemon=True
                ).start()
            else:
                # window mode: normal behavior
                self.proc = subprocess.Popen(
                    cmd,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    creationflags=creationflags,
                    bufsize=0,
                )
                threading.Thread(target=self._log_stdout, daemon=True).start()

    def stop(self, grace_s: float = 0.2):
        with self._lock:
            if not self.proc:
                return
            if self.proc.poll() is None:
                logger.info("Stopping receiver '%s'", self.cfg.name)
                try:
                    if os.name == "nt":
                        self.proc.send_signal(signal.CTRL_BREAK_EVENT)
                    else:
                        self.proc.terminate()
                    try:
                        self.proc.wait(max(0.0, float(grace_s)))
                    except subprocess.TimeoutExpired:
                        self.proc.kill()
                except Exception:
                    pass
            self.proc = None
            self._stop_reader.set()

    def restart(self):
        self.stop()
        time.sleep(0.1)
        self.start()

    def update(self, **updates):
        current = asdict(self.cfg)
        current.update(updates)
        new_cfg = RxConfig(**current)

        # channel_order-only change in raw mode can be applied live (no restart)
        if (
            self.cfg.mode == "raw"
            and new_cfg.channel_order.upper() in self._VALID_ORDERS
            and new_cfg.channel_order != self.cfg.channel_order
        ):
            logger.info(
                "Receiver '%s': live-update channel_order %s -> %s",
                self.cfg.name, self.cfg.channel_order, new_cfg.channel_order
            )
            self.cfg = new_cfg
            return

        # otherwise restart
        self.cfg = new_cfg
        self.restart()

    # ---------------- RAW READER (fast, old way) ---------------- #
    def _raw_reader_loop(self):
        assert self.proc is not None
        stream = self.proc.stdout
        if stream is None:
            logger.error("No stdout to read for raw mode")
            return

        def read_exact(n: int) -> Optional[bytes]:
            buf = b""
            while len(buf) < n and not self._stop_reader.is_set():
                chunk = stream.read(n - len(buf))
                if not chunk:
                    return None
                buf += chunk
            return buf

        while not self._stop_reader.is_set():
            frame = read_exact(self._frame_size)
            if frame is None:
                break
            # just store it — no per-byte work here
            with self._raw_buffer_lock:
                self._latest_frame = frame
                self._latest_seq += 1
                self._latest_frame_ts = time.time()

    def read_frame(self) -> Optional[bytes]:
        """
        Return the latest frame, applying channel order if needed.
        This keeps the inner reader fast but still lets us fix cameras like your GRB one.
        """
        if self.cfg.mode != "raw":
            raise RuntimeError("read_frame() only valid in mode='raw'")

        with self._raw_buffer_lock:
            frame = self._latest_frame
            seq = self._latest_seq

        # If we haven't received anything new since the last read, return None.
        # This allows upstream code to detect stalls and reconnect automatically.
        if frame is None or seq == self._last_delivered_seq:
            return None

        # Mark delivered *before* any expensive work.
        self._last_delivered_seq = seq

        if frame is None:
            return None

        order = self.cfg.channel_order.upper()
        if order == "BGR":
            return frame  # fast path, no copy

        # need numpy to reorder efficiently
        try:
            import numpy as np
        except ImportError as e:
            raise RuntimeError(
                f"channel_order='{order}' requires numpy installed"
            ) from e

        h, w = self.cfg.height, self.cfg.width
        arr = np.frombuffer(frame, dtype=np.uint8).reshape((h, w, 3))

        if order == "RGB":
            arr = arr[:, :, ::-1]          # BGR -> RGB
        elif order == "BRG":
            arr = arr[:, :, [0, 2, 1]]
        elif order == "RBG":
            arr = arr[:, :, [2, 0, 1]]
        elif order == "GBR":
            arr = arr[:, :, [1, 0, 2]]
        elif order == "GRB":
            arr = arr[:, :, [1, 2, 0]]
        else:
            # should not get here
            return frame

        return arr.tobytes()

    # ---------------- logging ---------------- #
    def _log_stdout(self):
        if not self.proc or not self.proc.stdout:
            return
        for line in iter(self.proc.stdout.readline, b""):
            logger.info("[gst:%s] %s", self.cfg.name, line.decode(errors="replace").rstrip())
            if self.proc.poll() is not None:
                break

    # >>> NEW: stderr logger for raw mode so it doesn't pollute stdout
    def _log_stderr(self):
        if not self.proc or not self.proc.stderr:
            return
        for line in iter(self.proc.stderr.readline, b""):
            logger.info("[gst:%s:ERR] %s", self.cfg.name, line.decode(errors="replace").rstrip())
            if self.proc.poll() is not None:
                break

    # ---------------- command builder ---------------- #
    def _build_cmd(self, cfg: RxConfig) -> List[str]:
        gst = self._gst

        # >>> CHANGE 2: raw = quiet, window = verbose
        if cfg.mode == "raw":
            base: List[str] = [gst, "-q"]   # quiet so stdout is clean
        else:
            base = [gst, "-v"]

        if cfg.mode == "raw":
            if cfg.codec.lower() == "jpeg":
                caps = "application/x-rtp,media=video,encoding-name=JPEG,payload=26,clock-rate=90000"
                pipeline = [
                    "udpsrc", f"address={cfg.bind_address}", "reuse=true", f"port={cfg.port}", f"caps={caps}",
                    "!", "rtpjitterbuffer", f"latency={cfg.latency_ms}",
                    "!", "rtpjpegdepay",
                    "!", "jpegdec",
                    "!", "videoconvert",
                    "!", (
                        f"video/x-raw,format=BGR,width={cfg.width},height={cfg.height},"
                        "colorimetry=1:4:0:0,range=full"
                    ),
                    "!", "fdsink", "fd=1",
                ]
            else:
                caps = "application/x-rtp,media=video,encoding-name=H264,payload=96,clock-rate=90000"
                pipeline = [
                    "udpsrc", f"address={cfg.bind_address}", "reuse=true", f"port={cfg.port}", f"caps={caps}",
                    "!", "rtpjitterbuffer", f"latency={cfg.latency_ms}",
                    "!", "rtph264depay",
                    "!", "h264parse",
                    "!", "avdec_h264",
                    "!", "videoconvert",
                    "!", (
                        f"video/x-raw,format=BGR,width={cfg.width},height={cfg.height},"
                        "colorimetry=1:4:0:0,range=full"
                    ),
                    "!", "fdsink", "fd=1",
                ]
            return base + pipeline

        # window mode
        if cfg.record_path:
            base.insert(1, "-e")

        if cfg.codec.lower() == "jpeg":
            caps = "application/x-rtp,media=video,encoding-name=JPEG,payload=26,clock-rate=90000"
            pipeline = [
                "udpsrc", f"address={cfg.bind_address}", "reuse=true", f"port={cfg.port}", f"caps={caps}",
                "!", "rtpjitterbuffer", f"latency={cfg.latency_ms}",
                "!", "rtpjpegdepay",
                "!", "jpegdec",
                "!", "videoconvert",
                "!", "video/x-raw,format=BGR,colorimetry=1:4:0:0,range=full",
                "!", cfg.sink, f"sync={'true' if cfg.sync else 'false'}",
            ]
        else:
            caps = "application/x-rtp,media=video,encoding-name=H264,payload=96,clock-rate=90000"
            pipeline = [
                "udpsrc", f"address={cfg.bind_address}", "reuse=true", f"port={cfg.port}", f"caps={caps}",
                "!", "rtpjitterbuffer", f"latency={cfg.latency_ms}",
                "!", "rtph264depay",
                "!", "h264parse",
                "!", "avdec_h264",
                "!", "videoconvert",
                "!", "video/x-raw,format=BGR,colorimetry=1:4:0:0,range=full",
                "!", cfg.sink, f"sync={'true' if cfg.sync else 'false'}",
            ]
        return base + pipeline


class ReceiverManager:
    def __init__(self):
        self._procs: Dict[str, ReceiverProcess] = {}
        self._lock = threading.Lock()

    def start_stream(self, cfg: RxConfig) -> ReceiverProcess:
        with self._lock:
            if cfg.name in self._procs:
                raise ValueError(f"Receiver '{cfg.name}' already exists")
            rp = ReceiverProcess(cfg)
            self._procs[cfg.name] = rp
            rp.start()
            return rp

    def stop_stream(self, name: str):
        with self._lock:
            rp = self._procs.pop(name, None)
        if rp:
            rp.stop()

    def stop_all(self):
        with self._lock:
            names = list(self._procs.keys())
        for n in names:
            self.stop_stream(n)

    def update_stream(self, name: str, **updates):
        with self._lock:
            rp = self._procs.get(name)
            if not rp:
                raise KeyError(f"No such receiver: {name}")
            rp.update(**updates)


if __name__ == "__main__":
    import argparse
    import numpy as np
    import cv2

    ap = argparse.ArgumentParser(description="Windows GStreamer receiver (subprocess, window/raw)")
    ap.add_argument("--name", default="cam0")
    ap.add_argument("--codec", choices=["jpeg", "h264"], default="jpeg")
    ap.add_argument("--port", type=int, default=5000)
    ap.add_argument("--latency-ms", type=int, default=60)
    ap.add_argument("--sink", default="autovideosink")
    ap.add_argument("--sync", action="store_true")
    ap.add_argument("--record-path", default=None)
    ap.add_argument("--mode", choices=["window", "raw"], default="raw")
    ap.add_argument("--width", type=int, default=1280)
    ap.add_argument("--height", type=int, default=720)
    ap.add_argument("--channel-order", default="BGR")
    args = ap.parse_args()

    cfg = RxConfig(
        name=args.name,
        codec=args.codec,
        port=args.port,
        latency_ms=args.latency_ms,
        sink=args.sink,
        sync=args.sync,
        record_path=args.record_path,
        mode=args.mode,
        width=args.width,
        height=args.height,
        channel_order=args.channel_order,
    )

    mgr = ReceiverManager()
    rp = mgr.start_stream(cfg)

    if args.mode == "window":
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            mgr.stop_all()
    else:
        try:
            while True:
                fr = rp.read_frame()
                if fr is not None:
                    img = np.frombuffer(fr, dtype=np.uint8).reshape((args.height, args.width, 3))
                    cv2.imshow(args.name, img)
                if cv2.waitKey(1) & 0xFF == 27:
                    break
                time.sleep(0.005)
        except KeyboardInterrupt:
            pass
        finally:
            mgr.stop_all()
            cv2.destroyAllWindows()
