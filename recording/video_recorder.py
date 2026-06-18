"""Topside single-camera video recorder.

Records the exact H.264 (or MJPEG) RTP feed the ROV already produces into an
``.mp4`` on the pilot laptop -- no re-encode, no extra decode. It runs as its own
``gst-launch-1.0`` subprocess fed by a dedicated *mirror* UDP port, so it is fully
decoupled from the live Direct3D display (adding/removing the mirror is a live
``multiudpsink`` client update on the ROV, with zero display disruption).

Reliability choices:
- Fragmented MP4 (``mp4mux fragment-duration``): the file stays playable even if
  the app is killed without a clean shutdown (no lost moov atom).
- Started with ``-e`` so a Ctrl-Break still flushes EOS and finalizes normally.
"""
from __future__ import annotations

import logging
import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from video.gst_receiver import _suppress_gst_stderr_line
from video.gst_runtime import bootstrap_gstreamer_env


logger = logging.getLogger(__name__)


def _find_gst_launch() -> str:
    runtime = bootstrap_gstreamer_env()
    if runtime is None:
        raise FileNotFoundError(
            "Could not find gst-launch-1.0. Run setup_windows.ps1 or install GStreamer."
        )
    return str(runtime.gst_launch)


@dataclass(frozen=True)
class VideoRecorderConfig:
    name: str
    out_path: str
    codec: str = "h264"           # 'h264' or 'jpeg'
    port: int = 5200              # mirror UDP port the ROV duplicates RTP to
    bind_address: str = "0.0.0.0"
    # Recording favours completeness over latency: a generous jitter buffer that
    # never drops late packets gives the cleanest file the link allows.
    latency_ms: int = 200
    udp_buffer_size: int = 8 * 1024 * 1024
    fragment_ms: int = 1000
    extra: dict[str, Any] = field(default_factory=dict)


def build_video_recorder_cmd(gst_launch: str, cfg: VideoRecorderConfig) -> list[str]:
    """Build the gst-launch recorder pipeline (compressed passthrough -> mp4)."""

    # -e: forward EOS on Ctrl-Break so the muxer finalizes the file cleanly.
    base = [str(gst_launch), "-e", "--gst-disable-registry-fork", "-q"]
    udp_buffer_size = max(262144, int(cfg.udp_buffer_size))
    # gst-launch parses the pipeline string and processes backslash escapes, so a
    # Windows path like C:\Users\... gets mangled and filesink silently fails to
    # open. GStreamer accepts forward slashes on Windows, so normalize.
    out = str(cfg.out_path).replace("\\", "/")
    fragment = max(0, int(cfg.fragment_ms))
    mux = f"mp4mux fragment-duration={fragment}" if fragment > 0 else "mp4mux"

    if cfg.codec.lower() == "h264":
        caps = "application/x-rtp,media=video,encoding-name=H264,payload=96,clock-rate=90000"
        pipeline = [
            "udpsrc", f"address={cfg.bind_address}", "reuse=true", f"port={cfg.port}",
            f"buffer-size={udp_buffer_size}", f"caps={caps}",
            "!", "rtpjitterbuffer", f"latency={cfg.latency_ms}", "drop-on-latency=false",
            "!", "rtph264depay",
            "!", "h264parse", "config-interval=-1",
            "!", *mux.split(" "),
            "!", "filesink", f"location={out}", "sync=false",
        ]
    else:
        # MJPEG -> motion-jpeg in mp4 (rare; current rig is all H.264).
        caps = "application/x-rtp,media=video,encoding-name=JPEG,payload=26,clock-rate=90000"
        pipeline = [
            "udpsrc", f"address={cfg.bind_address}", "reuse=true", f"port={cfg.port}",
            f"buffer-size={udp_buffer_size}", f"caps={caps}",
            "!", "rtpjitterbuffer", f"latency={cfg.latency_ms}", "drop-on-latency=false",
            "!", "rtpjpegdepay",
            "!", "jpegparse",
            "!", *mux.split(" "),
            "!", "filesink", f"location={out}", "sync=false",
        ]
    return base + pipeline


class VideoRecorder:
    """Own and monitor one ``gst-launch-1.0`` recorder subprocess."""

    def __init__(self, cfg: VideoRecorderConfig):
        self.cfg = cfg
        self.proc: subprocess.Popen | None = None
        self._start_monotonic: float = 0.0
        self._gst = _find_gst_launch()

    @property
    def out_path(self) -> str:
        return str(self.cfg.out_path)

    def is_running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def elapsed_s(self) -> float:
        if self._start_monotonic <= 0.0:
            return 0.0
        return max(0.0, time.monotonic() - self._start_monotonic)

    def start(self) -> None:
        Path(self.cfg.out_path).parent.mkdir(parents=True, exist_ok=True)
        cmd = build_video_recorder_cmd(self._gst, self.cfg)
        env = dict(os.environ)
        bootstrap_gstreamer_env(env)
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        logger.info("Recording '%s' (mirror port %s) -> %s", self.cfg.name, self.cfg.port, self.cfg.out_path)
        self.proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
            creationflags=creationflags,
            bufsize=0,
        )
        self._start_monotonic = time.monotonic()
        threading.Thread(target=self._log_stream, name=f"rec-{self.cfg.name}", daemon=True).start()

    def stop(self, *, grace_s: float = 6.0) -> str:
        """Finalize the recording (EOS via -e) and return the output path."""
        proc = self.proc
        self.proc = None
        if proc is None:
            return self.out_path
        if proc.poll() is None:
            try:
                if os.name == "nt":
                    proc.send_signal(signal.CTRL_BREAK_EVENT)
                else:
                    proc.send_signal(signal.SIGINT)
            except Exception:
                pass
            try:
                proc.wait(timeout=max(0.5, float(grace_s)))
            except subprocess.TimeoutExpired:
                logger.warning("Recorder '%s' did not finalize in %.1fs; killing", self.cfg.name, grace_s)
                try:
                    proc.kill()
                except Exception:
                    pass
        return self.out_path

    def _log_stream(self) -> None:
        proc = self.proc
        if proc is None or proc.stdout is None:
            return
        for line in iter(proc.stdout.readline, b""):
            text = line.decode(errors="replace").rstrip()
            if not _suppress_gst_stderr_line(text):
                logger.info("[rec:%s] %s", self.cfg.name, text)
            if proc.poll() is not None:
                break
