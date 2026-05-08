from __future__ import annotations

import re
import time
from pathlib import Path


_UNSAFE_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')
_SEPARATOR_CHARS = re.compile(r"[\s_]+")


def safe_filename_component(value: object, fallback: str = "capture") -> str:
    text = str(value or "").strip()
    text = _UNSAFE_FILENAME_CHARS.sub("_", text)
    text = _SEPARATOR_CHARS.sub("_", text)
    text = text.strip(" ._")
    return text or fallback


def timestamped_camera_stem(camera_name: object, purpose: str | None = None) -> str:
    parts = [
        time.strftime("%Y%m%d-%H%M%S"),
        safe_filename_component(camera_name, fallback="camera"),
    ]
    if purpose:
        parts.append(safe_filename_component(purpose, fallback="capture"))
    return "_".join(parts)


def unique_capture_path(directory: str | Path, stem: str, suffix: str) -> Path:
    output_dir = Path(directory)
    ext = suffix if str(suffix).startswith(".") else f".{suffix}"
    candidate = output_dir / f"{stem}{ext}"
    counter = 2
    while candidate.exists():
        candidate = output_dir / f"{stem}-{counter:02d}{ext}"
        counter += 1
    return candidate
