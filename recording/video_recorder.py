"""Best-effort video and snapshot writers used by the pilot GUI."""

from __future__ import annotations

import os
import queue
import threading
import time
import logging
from pathlib import Path
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)


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

    Writes an .mp4 via imageio/ffmpeg when available, then OpenCV when needed;
    otherwise saves a PNG sequence.

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
        self._writer_backend: str | None = None  # "imageio" or "opencv"
        self._cv2 = None
        self._mode: str | None = None  # "mp4" or "frames"
        self._frame_dir: Path | None = None
        self._frame_idx = 0
        self._shape: tuple[int, int, int] | None = None

        # set during start()
        self._target: Path | None = None

    def _start_imageio_writer(self) -> Exception | None:
        """Start the preferred imageio/ffmpeg MP4 writer."""
        try:
            import imageio.v2 as imageio  # type: ignore
        except Exception as exc:
            return exc

        try:
            # Widest compatibility for common players:
            # - H.264 (libx264) + yuv420p pixel format
            # - macro_block_size=None avoids unexpected resizing
            self._writer = imageio.get_writer(
                str(self.out_path),
                fps=self.fps,
                codec="libx264",
                format="FFMPEG",
                pixelformat="yuv420p",
                macro_block_size=None,
            )
        except Exception as exc:
            self._writer = None
            return exc

        self._writer_backend = "imageio"
        self._mode = "mp4"
        self._target = self.out_path
        return None

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
        self._target = self.out_path
        return None

    def _prepare_frame_fallback(self) -> None:
        """Prepare the final PNG-frame fallback."""
        self._writer = None
        self._writer_backend = None
        self._mode = "frames"
        self._frame_dir = self.out_path.parent / f"{self.out_path.stem}_frames"
        self._frame_dir.mkdir(parents=True, exist_ok=True)
        self._target = self._frame_dir

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

        self.out_path.parent.mkdir(parents=True, exist_ok=True)

        imageio_error = self._start_imageio_writer()
        if imageio_error is not None:
            opencv_error = self._prepare_opencv_writer()
            if opencv_error is None:
                logger.warning(
                    "imageio/ffmpeg MP4 writer unavailable (%s). Using OpenCV MP4 writer.",
                    imageio_error,
                )
            else:
                logger.warning(
                    "MP4 writers unavailable (imageio/ffmpeg: %s; OpenCV: %s). "
                    "Falling back to PNG frame sequence.",
                    imageio_error,
                    opencv_error,
                )
                self._prepare_frame_fallback()

        self._started = True
        self._thread.start()
        return self._target or self.out_path

    def stop(self, timeout_s: float = 5.0) -> None:
        if not self._started:
            return

        self._stop_requested.set()
        # Try to wake the worker even if queue is full.
        try:
            self._q.put(None, timeout=0.5)
        except Exception:
            pass

        self._thread.join(timeout=timeout_s)
        if self._thread.is_alive():
            logger.warning("VideoRecorder thread did not stop within %.1fs; output may be incomplete.", timeout_s)

        self._started = False

    def add_frame(self, frame_bgr: np.ndarray) -> None:
        if not self._started or self._stop_requested.is_set():
            return
        try:
            frame = _normalize_bgr(frame_bgr)
        except Exception:
            # Bad frame; drop it
            return

        try:
            self._q.put_nowait(frame)
        except queue.Full:
            # Drop if overwhelmed (keeps UI responsive)
            pass

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
                    if self._mode == "mp4" and self._writer_backend == "imageio" and self._writer is not None:
                        rgb = frame[:, :, ::-1]
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
                except Exception:
                    # Don't crash the app mid-mission; keep recording best-effort.
                    continue
        finally:
            if self._writer is not None:
                try:
                    if self._writer_backend == "opencv":
                        self._writer.release()
                    else:
                        self._writer.close()
                except Exception:
                    pass
                self._writer = None


def save_snapshot(frame_bgr: np.ndarray, out_path: str | os.PathLike) -> None:
    """Save a single frame to disk (PNG/JPG based on extension)."""
    out_path = Path(out_path)
    if out_path.suffix == "":
        out_path = out_path.with_suffix(".png")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        frame = _normalize_bgr(frame_bgr)
    except Exception as e:
        logger.warning("Snapshot failed (bad frame): %s", e)
        return

    # Prefer Qt since it's already in the GUI stack.
    try:
        from PyQt6.QtGui import QImage  # type: ignore

        h, w, ch = frame.shape
        bytes_per_line = frame.strides[0]
        qimg = QImage(frame.data, w, h, bytes_per_line, QImage.Format.Format_BGR888)
        qimg.copy().save(str(out_path))
        return
    except Exception:
        pass

    # PIL fallback
    try:
        from PIL import Image  # type: ignore

        Image.fromarray(frame[:, :, ::-1]).save(out_path)
        return
    except Exception:
        pass

    # imageio fallback
    try:
        import imageio.v2 as imageio  # type: ignore

        imageio.imwrite(str(out_path), frame[:, :, ::-1])
    except Exception:
        pass
