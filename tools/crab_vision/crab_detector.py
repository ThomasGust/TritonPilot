"""Wrapper kept for backwards compatibility.

Core crab recognition logic lives in:
  tasks.crab_recognition

"""

from tasks.crab_recognition.crab_detector import (  # noqa: F401
    CrabDetector,
    Detection,
    create_default_detector,
    default_template_paths,
)
