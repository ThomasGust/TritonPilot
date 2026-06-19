"""Topside optical-tracking foundation for the station-keeping autopilot.

This package defines the *contract* between a (future) computer-vision target
tracker and the ROV-side visual station-keep controller
(``TritonOS/control/station_keep.py``). The CV is intentionally NOT implemented
yet -- only the interface and the payload it must produce.
"""

from tracking.optical_tracker import (
    NullOpticalTracker,
    OpticalTracker,
    StationKeepCommand,
    VisualTargetError,
    station_keep_modes,
)
from tracking.transect_policy import (
    TransectEstimate,
    TransectModel,
    TransectObservation,
    TransectPolicy,
)

__all__ = [
    "VisualTargetError",
    "StationKeepCommand",
    "OpticalTracker",
    "NullOpticalTracker",
    "station_keep_modes",
    "TransectModel",
    "TransectObservation",
    "TransectEstimate",
    "TransectPolicy",
]
