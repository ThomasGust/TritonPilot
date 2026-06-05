"""Threaded JSONL recorder for pilot and telemetry message streams."""

from __future__ import annotations

import json
import os
import queue
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from recording.save_location import DEFAULT_RECORDINGS_DIR


@dataclass
class RecordEvent:
    """One queued message waiting to be written to the stream log."""

    t: float
    stream: str
    msg: Dict[str, Any]


class StreamRecorder:
    """
    Thread-safe recorder for JSON-ish message streams.
    Writes newline-delimited JSON (jsonl) with an envelope: {t, stream, msg}.
    """

    def __init__(self, out_path: Path):
        self.out_path = Path(out_path)
        self._q: "queue.Queue[Optional[RecordEvent]]" = queue.Queue(maxsize=10_000)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._fh = None

    @staticmethod
    def make_session_dir(base_dir: str | os.PathLike = DEFAULT_RECORDINGS_DIR) -> Path:
        ts = time.strftime("%Y%m%d-%H%M%S")
        root = Path(base_dir)
        for suffix in range(0, 1000):
            name = ts if suffix == 0 else f"{ts}-{suffix:02d}"
            p = root / name
            try:
                p.mkdir(parents=True, exist_ok=False)
                return p
            except FileExistsError:
                continue
        raise RuntimeError(f"Could not create a unique recording session under {root}")

    def start(self) -> None:
        self.out_path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.out_path, "a", buffering=1)  # line-buffered
        self._thread.start()

    def stop(self, timeout_s: float = 2.0) -> None:
        try:
            self._q.put_nowait(None)
        except Exception:
            pass
        self._stop.set()
        self._thread.join(timeout=timeout_s)
        if self._fh:
            try:
                self._fh.flush()
                self._fh.close()
            finally:
                self._fh = None

    def record(self, stream: str, msg: Dict[str, Any]) -> None:
        if self._stop.is_set():
            return
        ev = RecordEvent(t=time.time(), stream=str(stream), msg=msg)
        try:
            self._q.put_nowait(ev)
        except queue.Full:
            # drop if overwhelmed (keeps UI/control responsive)
            pass

    def _run(self) -> None:
        assert self._fh is not None
        while True:
            ev = self._q.get()
            if ev is None:
                break
            try:
                self._fh.write(json.dumps({"t": ev.t, "stream": ev.stream, "msg": ev.msg}) + "\n")
            except Exception:
                # ignore write errors to avoid crashing the app mid-mission
                pass
