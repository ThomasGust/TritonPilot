# recording/video_recorder.py
from __future__ import annotations

import os
import queue
import threading
import time
from pathlib import Path
from typing import Optional

import numpy as np


class VideoRecorder:
    """
    Frame-based video recorder.

    Tries to write an .mp4 using imageio/ffmpeg if available.
    Falls back to saving a PNG frame sequence if not.
    """

    def __init__(self, out_dir: str | os.PathLike, basename: str = "video", fps: float = 30.0):
        self.out_dir = Path(out_dir)
        self.basename = basename
        self.fps = float(fps)

        self._q: "queue.Queue[Optional[np.ndarray]]" = queue.Queue(maxsize=500)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._started = False

        self._writer = None
        self._mode = None  # "mp4" or "frames"
        self._frame_dir = None
        self._frame_idx = 0

    def start(self) -> Path:
        if self._started:
            return self.out_dir
        self.out_dir.mkdir(parents=True, exist_ok=True)
        # try mp4 writer first
        try:
            import imageio.v2 as imageio  # type: ignore
            out_path = self.out_dir / f"{self.basename}.mp4"
            self._writer = imageio.get_writer(str(out_path), fps=self.fps)
            self._mode = "mp4"
        except Exception:
            # fall back to frame sequence
            self._frame_dir = self.out_dir / f"{self.basename}_frames"
            self._frame_dir.mkdir(parents=True, exist_ok=True)
            self._mode = "frames"

        self._started = True
        self._thread.start()
        return self.out_dir

    def stop(self, timeout_s: float = 3.0) -> None:
        if not self._started:
            return
        try:
            self._q.put_nowait(None)
        except Exception:
            pass
        self._stop.set()
        self._thread.join(timeout=timeout_s)

        if self._writer is not None:
            try:
                self._writer.close()
            except Exception:
                pass
            self._writer = None

        self._started = False

    def add_frame(self, frame_bgr: np.ndarray) -> None:
        """
        Accepts a frame in BGR uint8 format (as used by the existing pipeline).
        """
        if not self._started or self._stop.is_set():
            return
        try:
            self._q.put_nowait(frame_bgr.copy())
        except queue.Full:
            pass

    def _run(self) -> None:
        while not self._stop.is_set():
            item = self._q.get()
            if item is None:
                break
            frame = item
            try:
                if self._mode == "mp4" and self._writer is not None:
                    # imageio expects RGB
                    rgb = frame[:, :, ::-1]
                    self._writer.append_data(rgb)
                elif self._mode == "frames" and self._frame_dir is not None:
                    # write PNG frames using Qt (no extra deps)
                    path = self._frame_dir / f"{self._frame_idx:06d}.png"
                    self._frame_idx += 1
                    try:
                        from PyQt6.QtGui import QImage
                        h, w, ch = frame.shape
                        qimg = QImage(frame.data, w, h, ch * w, QImage.Format.Format_BGR888)
                        # copy to detach from numpy buffer
                        qimg.copy().save(str(path))
                    except Exception:
                        pass
            except Exception:
                pass


def save_snapshot(frame_bgr: np.ndarray, out_path: str | os.PathLike) -> None:
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        from PyQt6.QtGui import QImage
        h, w, ch = frame_bgr.shape
        qimg = QImage(frame_bgr.data, w, h, ch * w, QImage.Format.Format_BGR888)
        qimg.copy().save(str(out_path))
    except Exception:
        # best-effort fallback (requires extra deps)
        try:
            from PIL import Image  # type: ignore
            Image.fromarray(frame_bgr[:, :, ::-1]).save(out_path)
        except Exception:
            pass
