"""Low-overhead JSONL timing trace for camera capture/recording diagnostics."""

from __future__ import annotations

import atexit
import json
import os
import queue
import threading
import time
from pathlib import Path
from typing import Any


_TRUTHY = {"1", "true", "yes", "on", "debug"}
_queue: "queue.SimpleQueue[dict[str, Any] | None]" = queue.SimpleQueue()
_thread: threading.Thread | None = None
_lock = threading.Lock()
_path: Path | None = None


def enabled() -> bool:
    return str(os.environ.get("TRITON_CAPTURE_TRACE", "")).strip().lower() in _TRUTHY


def trace_path() -> Path | None:
    if not enabled():
        return None
    return _ensure_path()


def _ensure_path() -> Path:
    global _path
    if _path is None:
        root = Path(os.environ.get("TRITON_CAPTURE_TRACE_DIR", ".runtime-logs"))
        root.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        _path = root / f"capture_trace_{stamp}_pid{os.getpid()}.jsonl"
    return _path


def _jsonable(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    shape = getattr(value, "shape", None)
    if shape is not None:
        return {"type": type(value).__name__, "shape": list(shape)}
    return str(value)


def _writer() -> None:
    path = _ensure_path()
    with path.open("a", encoding="utf-8", buffering=1) as f:
        while True:
            item = _queue.get()
            if item is None:
                return
            f.write(json.dumps(item, separators=(",", ":"), sort_keys=True) + "\n")


def _ensure_thread() -> None:
    global _thread
    if not enabled():
        return
    with _lock:
        if _thread is not None and _thread.is_alive():
            return
        _thread = threading.Thread(target=_writer, name="capture-trace-writer", daemon=True)
        _thread.start()


def trace_event(stage: str, **fields: Any) -> None:
    if not enabled():
        return
    try:
        _ensure_thread()
        _queue.put(
            {
                "stage": str(stage),
                "mono_s": time.monotonic(),
                "mono_ns": time.monotonic_ns(),
                "wall_s": time.time(),
                "pid": os.getpid(),
                "thread": threading.current_thread().name,
                **{str(k): _jsonable(v) for k, v in fields.items()},
            }
        )
    except Exception:
        pass


def flush(timeout_s: float = 1.0) -> None:
    if not enabled():
        return
    thread = _thread
    if thread is None:
        return
    try:
        _queue.put(None)
        thread.join(timeout=max(0.0, float(timeout_s)))
    except Exception:
        pass


atexit.register(flush)
