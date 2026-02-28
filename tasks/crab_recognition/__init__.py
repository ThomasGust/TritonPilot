"""Crab recognition task.

This module contains the core CV logic used by the Pilot app.

For isolated testing, see: tools/crab_vision (wrapper scripts).
"""

from .crab_detector import CrabDetector, Detection

__all__ = ["CrabDetector", "Detection"]
