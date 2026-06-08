"""Runtime path helpers for source checkouts and packaged TritonPilot builds."""

from __future__ import annotations

import os
import sys
from pathlib import Path


APP_NAME = "TritonPilot"
APP_ORGANIZATION = "TritonRobotics"
APP_DISPLAY_NAME = "TritonPilot"


def is_packaged_app() -> bool:
    """Return True when running from a PyInstaller bundle."""
    return bool(getattr(sys, "frozen", False))


def project_root() -> Path:
    """Return the repository root while running from source."""
    return Path(__file__).resolve().parent


def bundled_resource_path(*parts: str) -> Path:
    """Return a path to a bundled resource in source or PyInstaller mode."""
    base = Path(getattr(sys, "_MEIPASS", project_root()))
    return base.joinpath(*parts)


def app_icon_path() -> Path:
    """Return the TritonPilot window/taskbar icon path."""
    return bundled_resource_path("assets", "tritonpilot_icon.ico")


def streams_file_path() -> Path:
    """Return the camera stream configuration path.

    ``TRITON_STREAMS_FILE`` lets developers or field techs point a packaged app
    at a local override without rebuilding.
    """
    override = os.environ.get("TRITON_STREAMS_FILE", "").strip()
    if override:
        return Path(override).expanduser()
    return bundled_resource_path("data", "streams.json")


def user_documents_dir() -> Path:
    """Return the best Documents directory for operator-visible files."""
    override = os.environ.get("TRITON_DOCUMENTS_DIR", "").strip()
    if override:
        return Path(override).expanduser()

    if os.name == "nt":
        userprofile = os.environ.get("USERPROFILE", "").strip()
        if userprofile:
            return Path(userprofile).expanduser() / "Documents"

    return Path.home() / "Documents"


def default_recordings_dir() -> Path:
    """Return TritonPilot's default recording root.

    Use Documents instead of the repository so packaged pilot builds behave like
    normal desktop software and source runs do not fill the checkout by default.
    """
    override = os.environ.get("TRITON_RECORDINGS_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    return user_documents_dir() / APP_NAME / "Recordings"
