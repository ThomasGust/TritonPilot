"""Best-effort video and snapshot writers used by the pilot GUI."""

from __future__ import annotations

import os
import queue
import threading
import time
import logging
import uuid
from pathlib import Path
from typing import Optional

import numpy as np

from recording.capture_trace import trace_event

logger = logging.getLogger(__name__)


def _env_int(name: str, default: int, *, min_value: int, max_value: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return int(default)
    try:
        value = int(raw)
    except Exception:
        return int(default)
    return max(int(min_value), min(int(max_value), value))


def _ffmpeg_crf() -> int:
    # CRF 15 is a high-quality analysis default: much cleaner than OpenCV mp4v
    # defaults while still smaller than visually lossless CRF 0 output.
    return _env_int("TRITON_VIDEO_RECORDER_CRF", 15, min_value=0, max_value=35)


def _ffmpeg_preset() -> str:
    preset = os.environ.get("TRITON_VIDEO_RECORDER_PRESET", "veryfast").strip().lower()
    allowed = {"ultrafast", "superfast", "veryfast", "faster", "fast", "medium"}
    return preset if preset in allowed else "veryfast"


def _ffmpeg_bitrate() -> str | None:
    value = os.environ.get("TRITON_VIDEO_RECORDER_BITRATE", "").strip()
    return value or None


def _ffmpeg_output_params() -> list[str]:
    params = ["-preset", _ffmpeg_preset(), "-movflags", "+faststart"]
    if _ffmpeg_bitrate() is None:
        params = ["-crf", str(_ffmpeg_crf())] + params
    return params


def _preferred_mp4_backends() -> tuple[str, ...]:
    """Return MP4 writer backends in the order this runtime should try them."""
    forced = os.environ.get("TRITON_VIDEO_RECORDER_BACKEND", "").strip().lower()
    if forced in {"ffmpeg", "x264", "libx264", "imageio_ffmpeg"}:
        return ("ffmpeg", "imageio", "opencv")
    if forced in {"opencv", "cv2"}:
        return ("opencv", "ffmpeg", "imageio")
    if forced == "imageio":
        return ("imageio", "ffmpeg", "opencv")

    return ("ffmpeg", "imageio", "opencv")


def _normalize_bgr(frame: np.ndarray) -> np.ndarray:
    """Return a contiguous uint8 BGR (H,W,3) frame."""
    arr = np.asarray(frame)
    if arr.ndim == 2:
        arr = np.stack([arr, arr, arr], axis=-1)
    if arr.ndim != 3:
        raise ValueError(f"Expected 2D/3D image array, got shape={arr.shape}")
    if arr.shape[2] == 4:
        arr = arr[:, :, :3]
    if arr.shape[2] != 3:
        raise ValueError(f"Expected 3-channel BGR image, got shape={arr.shape}")
    if arr.dtype != np.uint8:
        arr = np.clip(arr, 0, 255).astype(np.uint8)
    return np.ascontiguousarray(arr)


class VideoRecorder:
    """Frame-based video recorder.

    Writes a high-quality H.264 .mp4 through ffmpeg when available, then
    imageio/OpenCV when needed; otherwise saves a PNG sequence.

    Notes:
      - `out_path` is a FILE path (e.g. recordings/session/main_camera.mp4).
      - Frames are expected as BGR uint8 (as produced by the existing pipeline).
    """

    def __init__(self, out_path: str | os.PathLike, fps: float = 30.0):
        p = Path(out_path)
        # If user passes a directory or a path without suffix, default to .mp4
        if p.suffix == "":
            p = p.with_suffix(".mp4")
        self.out_path = p
        self.fps = float(fps)

        self._q: "queue.Queue[Optional[np.ndarray]]" = queue.Queue(maxsize=500)
        self._stop_requested = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._started = False

        self._writer = None
        self._writer_backend: str | None = None  # "ffmpeg", "imageio", or "opencv"
        self._cv2 = None
        self._ffmpeg_write_frames = None
        self._mode: str | None = None  # "mp4" or "frames"
        self._frame_dir: Path | None = None
        self._active_out_path: Path | None = None
        self._frame_idx = 0
        self._written_frames = 0
        self._shape: tuple[int, int, int] | None = None

        # set during start()
        self._target: Path | None = None

    def _mp4_temp_path(self) -> Path:
        token = uuid.uuid4().hex[:8]
        return self.out_path.with_name(f".{self.out_path.name}.{token}.partial")

    def _mp4_output_params(self, *, force_mp4_format: bool = False) -> list[str]:
        params = list(_ffmpeg_output_params())
        if force_mp4_format:
            params.extend(["-f", "mp4"])
        return params

    def _prepare_ffmpeg_writer(self) -> Exception | None:
        """Prepare the high-quality ffmpeg MP4 writer.

        The process is created lazily on the first frame because ffmpeg needs
        the frame dimensions up front.
        """
        try:
            import imageio_ffmpeg  # type: ignore
        except Exception as exc:
            return exc

        try:
            imageio_ffmpeg.get_ffmpeg_exe()
        except Exception as exc:
            return exc

        self._ffmpeg_write_frames = imageio_ffmpeg.write_frames
        self._writer = None
        self._writer_backend = "ffmpeg"
        self._mode = "mp4"
        self._active_out_path = self._mp4_temp_path()
        self._target = self.out_path
        return None

    def _start_imageio_writer(self) -> Exception | None:
        """Start the imageio/ffmpeg MP4 fallback."""
        active_out_path = self._mp4_temp_path()
        try:
            import imageio.v2 as imageio  # type: ignore
        except Exception as exc:
            return exc

        try:
            # Widest compatibility for common players:
            # - H.264 (libx264) + yuv420p pixel format
            # - macro_block_size=None avoids unexpected resizing
            self._writer = imageio.get_writer(
                str(active_out_path),
                fps=self.fps,
                codec="libx264",
                format="FFMPEG",
                pixelformat="yuv420p",
                macro_block_size=None,
                quality=None,
                bitrate=_ffmpeg_bitrate(),
                output_params=self._mp4_output_params(force_mp4_format=True),
            )
        except Exception as exc:
            self._writer = None
            return exc

        self._writer_backend = "imageio"
        self._mode = "mp4"
        self._active_out_path = active_out_path
        self._target = self.out_path
        return None

    def _ensure_ffmpeg_writer(self, frame_shape: tuple[int, ...]) -> bool:
        """Open the ffmpeg pipe once frame geometry is known."""
        if self._writer is not None:
            return True
        if self._ffmpeg_write_frames is None or len(frame_shape) < 2:
            return False

        h, w = int(frame_shape[0]), int(frame_shape[1])
        active_out_path = self._active_out_path or self.out_path
        try:
            writer = self._ffmpeg_write_frames(
                str(active_out_path),
                size=(w, h),
                pix_fmt_in="rgb24",
                pix_fmt_out="yuv420p",
                fps=self.fps,
                codec="libx264",
                quality=None,
                bitrate=_ffmpeg_bitrate(),
                macro_block_size=1,
                ffmpeg_log_level=os.environ.get("TRITON_VIDEO_RECORDER_FFMPEG_LOGLEVEL", "warning").strip()
                or "warning",
                ffmpeg_timeout=0,
                output_params=self._mp4_output_params(force_mp4_format=active_out_path != self.out_path),
            )
            writer.send(None)
        except Exception as exc:
            self._writer = None
            logger.warning(
                "High-quality ffmpeg MP4 writer could not open %s (%s); falling back to PNG frame sequence.",
                self.out_path,
                exc,
            )
            self._prepare_frame_fallback()
            return False

        self._writer = writer
        return True

    def _prepare_opencv_writer(self) -> Exception | None:
        """Prepare the OpenCV MP4 fallback.

        OpenCV needs the frame size before opening the writer, so the actual
        VideoWriter is created lazily when the first frame arrives.
        """
        try:
            import cv2  # type: ignore
        except Exception as exc:
            return exc

        self._cv2 = cv2
        self._writer = None
        self._writer_backend = "opencv"
        self._mode = "mp4"
        self._active_out_path = self.out_path
        self._target = self.out_path
        return None

    def _prepare_frame_fallback(self) -> None:
        """Prepare the final PNG-frame fallback."""
        self._writer = None
        self._writer_backend = None
        self._mode = "frames"
        self._active_out_path = None
        self._frame_dir = self.out_path.parent / f"{self.out_path.stem}_frames"
        self._frame_dir.mkdir(parents=True, exist_ok=True)
        self._target = self._frame_dir

    def _remove_path_quietly(self, path: Path | None) -> None:
        if path is None:
            return
        try:
            if path.exists() and path.is_file():
                path.unlink()
        except Exception:
            logger.warning("Could not remove incomplete recording %s", path)

    def _finalize_mp4_path(self) -> None:
        if self._mode != "mp4":
            return

        active = self._active_out_path or self.out_path
        final = self.out_path
        if self._written_frames <= 0:
            self._remove_path_quietly(active)
            if active != final:
                self._remove_path_quietly(final)
            trace_event(
                "video_recorder_discarded_empty_mp4",
                path=final,
                active_path=active,
                backend=self._writer_backend,
            )
            return

        if active == final:
            return
        try:
            if final.exists():
                final.unlink()
            active.replace(final)
            trace_event(
                "video_recorder_finalized_mp4",
                path=final,
                active_path=active,
                backend=self._writer_backend,
                written_frames=self._written_frames,
            )
        except Exception as exc:
            logger.warning("Could not finalize recording %s -> %s: %s", active, final, exc)
            trace_event(
                "video_recorder_finalize_failed",
                path=final,
                active_path=active,
                backend=self._writer_backend,
                written_frames=self._written_frames,
                error=str(exc),
            )

    def _ensure_opencv_writer(self, frame_shape: tuple[int, ...]) -> bool:
        """Open an OpenCV MP4 writer once the frame geometry is known."""
        if self._writer is not None:
            return True
        if self._cv2 is None or len(frame_shape) < 2:
            return False

        h, w = int(frame_shape[0]), int(frame_shape[1])
        last_error: Exception | None = None
        for codec in ("mp4v", "avc1", "H264"):
            try:
                fourcc = self._cv2.VideoWriter_fourcc(*codec)
                writer = self._cv2.VideoWriter(str(self.out_path), fourcc, self.fps, (w, h))
                if writer is not None and writer.isOpened():
                    self._writer = writer
                    return True
                try:
                    writer.release()
                except Exception:
                    pass
            except Exception as exc:
                last_error = exc

        logger.warning(
            "OpenCV MP4 writer could not open %s%s; falling back to PNG frame sequence.",
            self.out_path,
            f" ({last_error})" if last_error is not None else "",
        )
        self._prepare_frame_fallback()
        return False

    @property
    def target(self) -> Path | None:
        """Path of the active output: mp4 file (preferred) or frames directory."""
        return self._target

    def start(self) -> Path:
        if self._started:
            return self._target or self.out_path

        trace_event("video_recorder_start_request", path=self.out_path, fps=self.fps)
        self.out_path.parent.mkdir(parents=True, exist_ok=True)

        errors: dict[str, Exception] = {}
        for backend in _preferred_mp4_backends():
            if backend == "ffmpeg":
                err = self._prepare_ffmpeg_writer()
            elif backend == "opencv":
                err = self._prepare_opencv_writer()
            else:
                err = self._start_imageio_writer()
            if err is None:
                if errors:
                    logger.warning(
                        "%s MP4 writer unavailable (%s). Using %s MP4 writer.",
                        ", ".join(errors.keys()),
                        "; ".join(f"{name}: {exc}" for name, exc in errors.items()),
                        backend,
                    )
                break
            errors[backend] = err
        else:
            logger.warning(
                "MP4 writers unavailable (%s). Falling back to PNG frame sequence.",
                "; ".join(f"{name}: {exc}" for name, exc in errors.items()),
            )
            self._prepare_frame_fallback()

        self._started = True
        self._thread.start()
        trace_event(
            "video_recorder_started",
            path=self.out_path,
            target=self._target or self.out_path,
            backend=self._writer_backend,
            mode=self._mode,
            fps=self.fps,
        )
        return self._target or self.out_path

    def stop(self, timeout_s: float = 5.0, *, drain_pending: bool = True) -> None:
        if not self._started:
            return

        trace_event(
            "video_recorder_stop_request",
            path=self.out_path,
            timeout_s=timeout_s,
            drain_pending=drain_pending,
            queue_size=self.queue_size(),
            written_frames=self._written_frames,
        )
        self._stop_requested.set()
        if not drain_pending:
            while True:
                try:
                    self._q.get_nowait()
                except queue.Empty:
                    break
        # Try to wake the worker even if queue is full.
        try:
            self._q.put(None, timeout=0.5)
        except Exception:
            pass

        self._thread.join(timeout=timeout_s)
        if self._thread.is_alive():
            logger.warning("VideoRecorder thread did not stop within %.1fs; output may be incomplete.", timeout_s)

        self._started = False
        trace_event(
            "video_recorder_stopped",
            path=self.out_path,
            alive=self._thread.is_alive(),
            queue_size=self.queue_size(),
            written_frames=self._written_frames,
        )

    def queue_size(self) -> int:
        try:
            return int(self._q.qsize())
        except Exception:
            return -1

    def add_frame(self, frame_bgr: np.ndarray) -> bool:
        if not self._started or self._stop_requested.is_set():
            trace_event("video_recorder_add_frame_skipped", path=self.out_path, reason="not_started_or_stopping")
            return False
        try:
            frame = _normalize_bgr(frame_bgr)
        except Exception:
            # Bad frame; drop it
            trace_event("video_recorder_add_frame_skipped", path=self.out_path, reason="bad_frame")
            return False

        try:
            self._q.put_nowait(frame)
            trace_event(
                "video_recorder_frame_queued",
                path=self.out_path,
                queue_size=self.queue_size(),
                shape=list(frame.shape),
            )
            return True
        except queue.Full:
            # Drop if overwhelmed (keeps UI responsive)
            trace_event("video_recorder_add_frame_skipped", path=self.out_path, reason="queue_full")
            return False

    def _save_png(self, frame_bgr: np.ndarray, path: Path) -> None:
        """Best-effort PNG write without pulling in OpenCV."""
        # Prefer Qt since it's already in the GUI stack.
        try:
            from PyQt6.QtGui import QImage  # type: ignore

            h, w, ch = frame_bgr.shape
            bytes_per_line = frame_bgr.strides[0]
            qimg = QImage(frame_bgr.data, w, h, bytes_per_line, QImage.Format.Format_BGR888)
            qimg.copy().save(str(path))
            return
        except Exception:
            pass

        # PIL fallback
        try:
            from PIL import Image  # type: ignore

            Image.fromarray(frame_bgr[:, :, ::-1]).save(path)
            return
        except Exception:
            pass

        # imageio fallback
        try:
            import imageio.v2 as imageio  # type: ignore

            imageio.imwrite(str(path), frame_bgr[:, :, ::-1])
        except Exception:
            pass

    def _run(self) -> None:
        from queue import Empty

        try:
            while True:
                try:
                    item = self._q.get(timeout=0.25)
                except Empty:
                    if self._stop_requested.is_set():
                        break
                    continue

                if item is None:
                    break

                frame = item
                write_start_s = time.monotonic()
                try:
                    # Keep a fixed geometry; if it changes, resize to the first frame.
                    if self._shape is None:
                        self._shape = frame.shape
                    elif frame.shape != self._shape:
                        # Lightweight resize via PIL if available; otherwise drop.
                        try:
                            from PIL import Image  # type: ignore

                            h, w, _ = self._shape
                            rgb = frame[:, :, ::-1]
                            rgb = np.asarray(Image.fromarray(rgb).resize((w, h)))
                            frame = rgb[:, :, ::-1].astype(np.uint8)
                        except Exception:
                            continue

                    wrote = False
                    if self._mode == "mp4" and self._writer_backend == "ffmpeg":
                        if self._ensure_ffmpeg_writer(frame.shape) and self._writer is not None:
                            rgb = np.ascontiguousarray(frame[:, :, ::-1])
                            self._writer.send(rgb)
                            wrote = True
                    elif self._mode == "mp4" and self._writer_backend == "imageio" and self._writer is not None:
                        rgb = np.ascontiguousarray(frame[:, :, ::-1])
                        self._writer.append_data(rgb)
                        wrote = True
                    elif self._mode == "mp4" and self._writer_backend == "opencv":
                        if self._ensure_opencv_writer(frame.shape) and self._writer is not None:
                            self._writer.write(frame)
                            wrote = True

                    if not wrote and self._mode == "frames" and self._frame_dir is not None:
                        p = self._frame_dir / f"{self._frame_idx:06d}.png"
                        self._frame_idx += 1
                        self._save_png(frame, p)
                        wrote = True
                    if wrote:
                        self._written_frames += 1
                    trace_event(
                        "video_recorder_frame_written",
                        path=self.out_path,
                        backend=self._writer_backend,
                        mode=self._mode,
                        wrote=wrote,
                        written_frames=self._written_frames,
                        queue_size=self.queue_size(),
                        dt_ms=(time.monotonic() - write_start_s) * 1000.0,
                    )
                except Exception as exc:
                    # Don't crash the app mid-mission; keep recording best-effort.
                    logger.warning("Video frame write failed for %s: %s", self.out_path, exc)
                    trace_event(
                        "video_recorder_frame_write_failed",
                        path=self.out_path,
                        backend=self._writer_backend,
                        mode=self._mode,
                        queue_size=self.queue_size(),
                        error=str(exc),
                    )
                    continue
        finally:
            if self._writer is not None:
                try:
                    if self._writer_backend == "opencv":
                        self._writer.release()
                    elif self._writer_backend == "ffmpeg":
                        self._writer.close()
                    else:
                        self._writer.close()
                except Exception:
                    pass
                self._writer = None
            self._finalize_mp4_path()


def save_snapshot(frame_bgr: np.ndarray, out_path: str | os.PathLike) -> None:
    """Save a single frame to disk (PNG/JPG based on extension)."""
    out_path = Path(out_path)
    if out_path.suffix == "":
        out_path = out_path.with_suffix(".png")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = out_path.with_name(f".{out_path.stem}.{uuid.uuid4().hex[:8]}.partial{out_path.suffix}")

    try:
        frame = _normalize_bgr(frame_bgr)
    except Exception as e:
        logger.warning("Snapshot failed (bad frame): %s", e)
        return

    try:
        # Prefer Qt since it's already in the GUI stack.
        try:
            from PyQt6.QtGui import QImage  # type: ignore

            h, w, ch = frame.shape
            bytes_per_line = frame.strides[0]
            qimg = QImage(frame.data, w, h, bytes_per_line, QImage.Format.Format_BGR888)
            if qimg.copy().save(str(temp_path)) and temp_path.exists() and temp_path.stat().st_size > 0:
                temp_path.replace(out_path)
                return
        except Exception:
            pass

        # PIL fallback
        try:
            from PIL import Image  # type: ignore

            Image.fromarray(frame[:, :, ::-1]).save(temp_path)
            if temp_path.exists() and temp_path.stat().st_size > 0:
                temp_path.replace(out_path)
                return
        except Exception:
            pass

        # imageio fallback
        try:
            import imageio.v2 as imageio  # type: ignore

            imageio.imwrite(str(temp_path), frame[:, :, ::-1])
            if temp_path.exists() and temp_path.stat().st_size > 0:
                temp_path.replace(out_path)
                return
        except Exception:
            pass
        logger.warning("Snapshot failed: no image writer could save %s", out_path)
    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except Exception:
            pass
