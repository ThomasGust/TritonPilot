"""Save-location validation and fallback handling for pilot recordings."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from uuid import uuid4


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RECORDINGS_DIR = REPO_ROOT / "recordings"


@dataclass(frozen=True)
class SaveLocation:
    """Resolved recording directory plus any fallback explanation."""

    path: Path
    used_fallback: bool = False
    reason: str = ""


def _display_path(path: Path) -> str:
    try:
        return str(path.resolve())
    except Exception:
        return str(path)


def is_available_directory(path: str | Path | None) -> bool:
    """Return True when path is an existing writable directory."""
    if path is None:
        return False

    try:
        candidate = Path(path).expanduser()
    except Exception:
        return False

    try:
        if not candidate.exists() or not candidate.is_dir():
            return False
    except Exception:
        return False

    probe: Optional[Path] = None
    try:
        probe = candidate / f".tritonpilot_write_test_{uuid4().hex}"
        with probe.open("x", encoding="utf-8"):
            pass
        return True
    except Exception:
        return False
    finally:
        if probe is not None:
            try:
                probe.unlink()
            except FileNotFoundError:
                pass
            except Exception:
                pass


def resolve_recordings_dir(preferred: str | Path | None, fallback: str | Path = DEFAULT_RECORDINGS_DIR) -> SaveLocation:
    """Resolve the active recordings root, falling back to the repo folder.

    A preferred directory is only used when it already exists and is writable.
    The fallback directory is created on demand so a fresh checkout still works.
    """
    preferred_path: Path | None = None
    if preferred is not None and str(preferred).strip():
        try:
            preferred_path = Path(preferred).expanduser()
        except Exception:
            preferred_path = None
        if preferred_path is not None and is_available_directory(preferred_path):
            return SaveLocation(preferred_path.resolve(), used_fallback=False)

    fallback_path = Path(fallback).expanduser()
    fallback_reason = ""
    if preferred_path is not None:
        fallback_reason = f"Selected save directory is not available: {_display_path(preferred_path)}"

    try:
        fallback_path.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        reason = fallback_reason or "Could not create fallback recordings directory."
        return SaveLocation(fallback_path, used_fallback=True, reason=f"{reason} ({exc})")

    try:
        resolved = fallback_path.resolve()
    except Exception:
        resolved = fallback_path

    return SaveLocation(
        resolved,
        used_fallback=preferred_path is not None,
        reason=fallback_reason,
    )
