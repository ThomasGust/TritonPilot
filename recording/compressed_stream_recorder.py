"""Compressed H.264/RTP recorder for direct pilot video streams."""

from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from recording.capture_trace import trace_event
from video.gst_receiver import _win_kill_udp_port_users


logger = logging.getLogger(__name__)


def _ffmpeg_path(path: str | os.PathLike) -> str:
    return str(Path(path).resolve()).replace("\\", "/")


@dataclass(frozen=True)
class H264RtpMp4RecordConfig:
    name: str
    port: int
    out_path: str | os.PathLike
    bind_address: str = "127.0.0.1"
    latency_ms: int = 250
    udp_buffer_size: int = 4 * 1024 * 1024
    drop_on_latency: bool = False
    sdp_path: str | os.PathLike | None = None


def _find_ffmpeg_exe() -> str:
    try:
        import imageio_ffmpeg  # type: ignore
    except Exception as exc:
        raise FileNotFoundError("imageio_ffmpeg is required for compressed H.264 recording") from exc
    return str(imageio_ffmpeg.get_ffmpeg_exe())


def build_h264_rtp_mp4_record_cmd(ffmpeg_exe: str, cfg: H264RtpMp4RecordConfig) -> list[str]:
    """Build an ffmpeg command that copies live H.264 RTP into MPEG-TS."""

    out_path = Path(cfg.out_path)
    sdp_path = Path(cfg.sdp_path) if cfg.sdp_path is not None else out_path.with_suffix(out_path.suffix + ".sdp")
    return [
        str(ffmpeg_exe),
        "-hide_banner",
        "-loglevel",
        os.environ.get("TRITON_COMPRESSED_RECORDER_FFMPEG_LOGLEVEL", "warning").strip() or "warning",
        "-protocol_whitelist",
        "file,udp,rtp",
        "-fflags",
        "+genpts",
        "-i",
        _ffmpeg_path(sdp_path),
        "-an",
        "-c:v",
        "copy",
        "-f",
        "mpegts",
        "-y",
        _ffmpeg_path(out_path),
    ]


class CompressedRtpRecorder:
    """Own an ffmpeg process that records local H.264 RTP into an MP4 file."""

    def __init__(
        self,
        out_path: str | os.PathLike,
        *,
        name: str,
        port: int,
        bind_address: str = "127.0.0.1",
        latency_ms: int = 250,
    ):
        p = Path(out_path)
        if p.suffix == "":
            p = p.with_suffix(".mp4")
        self.out_path = p
        self.name = str(name)
        self.port = int(port)
        self.bind_address = str(bind_address or "127.0.0.1")
        self.latency_ms = int(latency_ms)
        self.proc: subprocess.Popen | None = None
        self._active_path: Path | None = None
        self._sdp_path: Path | None = None
        self._target: Path | None = None
        self._started = False

    @property
    def target(self) -> Path | None:
        return self._target

    def queue_size(self) -> int:
        return 0

    def _transport_temp_path(self) -> Path:
        token = uuid.uuid4().hex[:8]
        return self.out_path.with_name(f".{self.out_path.name}.{token}.partial.ts")

    def _write_sdp(self) -> Path:
        token = uuid.uuid4().hex[:8]
        path = self.out_path.with_name(f".{self.out_path.name}.{token}.sdp")
        path.write_text(
            "\n".join(
                [
                    "v=0",
                    f"o=- 0 0 IN IP4 {self.bind_address}",
                    "s=TritonPilot H264 RTP recording",
                    f"c=IN IP4 {self.bind_address}",
                    "t=0 0",
                    f"m=video {int(self.port)} RTP/AVP 96",
                    "a=rtpmap:96 H264/90000",
                    "a=fmtp:96 packetization-mode=1",
                    "",
                ]
            ),
            encoding="ascii",
        )
        return path

    def start(self) -> Path:
        if self._started:
            return self._target or self.out_path
        self.out_path.parent.mkdir(parents=True, exist_ok=True)
        self._active_path = self._transport_temp_path()
        self._sdp_path = self._write_sdp()
        self._target = self.out_path
        trace_event(
            "compressed_rtp_recorder_start_request",
            name=self.name,
            port=self.port,
            bind_address=self.bind_address,
            path=self.out_path,
            active_path=self._active_path,
            sdp_path=self._sdp_path,
            latency_ms=self.latency_ms,
        )
        _win_kill_udp_port_users(self.port)
        cfg = H264RtpMp4RecordConfig(
            name=self.name,
            port=self.port,
            out_path=self._active_path,
            bind_address=self.bind_address,
            latency_ms=self.latency_ms,
            sdp_path=self._sdp_path,
        )
        cmd = build_h264_rtp_mp4_record_cmd(_find_ffmpeg_exe(), cfg)
        env = dict(os.environ)
        creationflags = 0
        startupinfo = None
        if os.name == "nt":
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = 0
        self.proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            creationflags=creationflags,
            startupinfo=startupinfo,
            bufsize=0,
        )
        self._started = True
        threading.Thread(target=self._log_stream, args=(self.proc.stdout, "OUT"), daemon=True).start()
        threading.Thread(target=self._log_stream, args=(self.proc.stderr, "ERR"), daemon=True).start()
        trace_event(
            "compressed_rtp_recorder_started",
            name=self.name,
            port=self.port,
            path=self.out_path,
            active_path=self._active_path,
            sdp_path=self._sdp_path,
            pid=getattr(self.proc, "pid", None),
        )
        return self._target or self.out_path

    def stop(self, timeout_s: float = 10.0, *, drain_pending: bool = True) -> None:
        if not self._started:
            return
        proc = self.proc
        stop_s = time.monotonic()
        trace_event(
            "compressed_rtp_recorder_stop_request",
            name=self.name,
            port=self.port,
            path=self.out_path,
            active_path=self._active_path,
            timeout_s=timeout_s,
            drain_pending=drain_pending,
        )
        if proc is not None and proc.poll() is None:
            try:
                if proc.stdin is not None:
                    proc.stdin.write(b"q\n")
                    proc.stdin.flush()
                proc.wait(timeout=min(2.0, max(0.5, float(timeout_s) * 0.25)))
            except Exception:
                try:
                    proc.terminate()
                    proc.wait(timeout=1.0)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
        self.proc = None
        self._started = False
        self._finalize(timeout_s=max(1.0, float(timeout_s)))
        self._remove_sdp()
        trace_event(
            "compressed_rtp_recorder_stopped",
            name=self.name,
            port=self.port,
            path=self.out_path,
            active_path=self._active_path,
            dt_ms=(time.monotonic() - stop_s) * 1000.0,
        )

    def _finalize(self, *, timeout_s: float = 10.0) -> None:
        active = self._active_path
        final = self.out_path
        if active is None:
            return
        try:
            size = active.stat().st_size if active.exists() else 0
        except Exception:
            size = 0
        if size <= 0:
            try:
                if active.exists():
                    active.unlink()
            except Exception:
                pass
            trace_event(
                "compressed_rtp_recorder_discarded_empty_mp4",
                name=self.name,
                path=final,
                active_path=active,
            )
            return
        remux_path = final.with_name(f".{final.name}.{uuid.uuid4().hex[:8]}.remux.mp4")
        try:
            cmd = [
                _find_ffmpeg_exe(),
                "-hide_banner",
                "-loglevel",
                os.environ.get("TRITON_COMPRESSED_RECORDER_FFMPEG_LOGLEVEL", "warning").strip() or "warning",
                "-fflags",
                "+genpts",
                "-i",
                _ffmpeg_path(active),
                "-c",
                "copy",
                "-movflags",
                "+faststart",
                "-y",
                _ffmpeg_path(remux_path),
            ]
            result = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=max(1.0, float(timeout_s)),
            )
            if result.returncode != 0:
                stderr = result.stderr.decode(errors="replace") if isinstance(result.stderr, bytes) else str(result.stderr)
                raise RuntimeError(stderr.strip() or f"ffmpeg remux exited {result.returncode}")
            if final.exists():
                final.unlink()
            remux_path.replace(final)
            try:
                active.unlink()
            except Exception:
                pass
            trace_event(
                "compressed_rtp_recorder_finalized_mp4",
                name=self.name,
                path=final,
                active_path=active,
                size=size,
            )
        except Exception as exc:
            logger.warning("Could not finalize compressed recording %s -> %s: %s", active, final, exc)
            try:
                if remux_path.exists():
                    remux_path.unlink()
            except Exception:
                pass
            trace_event(
                "compressed_rtp_recorder_finalize_failed",
                name=self.name,
                path=final,
                active_path=active,
                size=size,
                error=str(exc),
            )

    def _remove_sdp(self) -> None:
        path = self._sdp_path
        self._sdp_path = None
        if path is None:
            return
        try:
            if path.exists():
                path.unlink()
        except Exception:
            pass

    def _log_stream(self, stream, label: str) -> None:
        if stream is None:
            return
        for line in iter(stream.readline, b""):
            text = line.decode(errors="replace").rstrip()
            if text:
                logger.info("[compressed-rec:%s:%s] %s", self.name, label, text)
