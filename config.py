"""
Global-ish config for topside.

You can import this from anywhere:

    from config import PILOT_PUB_ENDPOINT, SENSOR_SUB_ENDPOINT, VIDEO_RPC_ENDPOINT
"""

import os
from pathlib import Path

# ROV IP (can be overridden via env)
ROV_HOST = os.environ.get("ROV_HOST", "192.168.1.4")

# ZMQ endpoints
PILOT_PUB_ENDPOINT = os.environ.get("ROV_PILOT_EP", f"tcp://{ROV_HOST}:6000")
SENSOR_SUB_ENDPOINT = os.environ.get("ROV_SENSOR_EP", f"tcp://{ROV_HOST}:6001")
VIDEO_RPC_ENDPOINT = os.environ.get("ROV_VIDEO_RPC", f"tcp://{ROV_HOST}:5555")

# Where your JSON with stream definitions lives
STREAMS_FILE = Path(__file__).parent / "data" / "streams.json"

# Controller shaping
#
# Stick drift is common on Xbox controllers, and even small drift can cause
# noticeable thruster creep. We apply a deadzone on the pilot side and ALSO
# apply additional deadbands on the ROV side for safety.
#
# You can override at runtime:
#   TRITON_CONTROLLER_DEADZONE=0.15 python -m main
CONTROLLER_DEADZONE = float(os.environ.get("TRITON_CONTROLLER_DEADZONE", "0.15"))


# Controller selection / mapping
#
# Why this exists:
#   - On some machines SDL/pygame exposes multiple joystick devices; index 0 may
#     not be the one you're holding.
#   - Axis/button numbering varies by controller model + OS/driver.
#
# These settings let you fix things without editing code.
#
# Examples:
#   TRITON_CONTROLLER_INDEX=1 python -m main_topside
#   TRITON_CONTROLLER_AXIS_MAP=0,1,3,4,2,5 python -m main_topside
#
def _parse_int_list_env(var: str, default: list[int]) -> list[int]:
    s = os.environ.get(var, "").strip()
    if not s:
        return list(default)
    try:
        return [int(x.strip()) for x in s.split(",") if x.strip() != ""]
    except Exception:
        return list(default)


CONTROLLER_INDEX = int(os.environ.get("TRITON_CONTROLLER_INDEX", "0"))

# Optional diagnostic logging.
CONTROLLER_DEBUG = os.environ.get("TRITON_CONTROLLER_DEBUG", "0").strip().lower() in ("1", "true", "yes")
CONTROLLER_DUMP_RAW_EVERY_S = float(os.environ.get("TRITON_CONTROLLER_DUMP_RAW_EVERY", "0"))

# Axis mapping (lx,ly,rx,ry,lt,rt) -> pygame axis indices.
#
# Unfortunately SDL/pygame axis numbering varies across OS/driver/controller.
# Two very common Xbox layouts are:
#   A) 0=lx, 1=ly, 2=rx, 3=ry, 4=lt, 5=rt
#   B) 0=lx, 1=ly, 2=lt, 3=rx, 4=ry, 5=rt
#
# If you DON'T set TRITON_CONTROLLER_AXIS_MAP, we will auto-detect (best-effort)
# using the controller's "rest" axis values.
#
# To force a mapping, set one of these examples:
#   TRITON_CONTROLLER_AXIS_MAP=0,1,2,3,4,5   (layout A)
#   TRITON_CONTROLLER_AXIS_MAP=0,1,3,4,2,5   (layout B)
_AXIS_MAP_ENV = os.environ.get("TRITON_CONTROLLER_AXIS_MAP", "").strip().lower()
if (not _AXIS_MAP_ENV) or _AXIS_MAP_ENV in ("auto", "detect", "default"):
    CONTROLLER_AXIS_MAP = None
else:
    CONTROLLER_AXIS_MAP = _parse_int_list_env("TRITON_CONTROLLER_AXIS_MAP", [0, 1, 2, 3, 4, 5])

# D-pad hat index (usually 0).
CONTROLLER_HAT_INDEX = int(os.environ.get("TRITON_CONTROLLER_HAT_INDEX", "0"))

# Button candidate overrides (comma-separated indices). If provided, these take
# precedence over the built-in heuristics.
CONTROLLER_MENU_BUTTONS = _parse_int_list_env("TRITON_CONTROLLER_MENU_BUTTONS", [])
CONTROLLER_WIN_BUTTONS = _parse_int_list_env("TRITON_CONTROLLER_WIN_BUTTONS", [])

# ---------------------------------------------------------------------------
# Control modes
# ---------------------------------------------------------------------------
# Depth hold is toggled topside and transmitted in PilotFrame.modes.
# Default button: press down the RIGHT stick (rstick).

DEPTH_HOLD_TOGGLE_BUTTON = os.environ.get("TRITON_DEPTH_HOLD_TOGGLE", "rstick").strip().lower()
DEPTH_HOLD_DEFAULT = os.environ.get("TRITON_DEPTH_HOLD_DEFAULT", "0").strip().lower() in ("1", "true", "yes")


# Pilot-adjustable max gain / power cap (transmitted in PilotFrame.modes["max_gain"]).
# Y = +5%, A = -5% by default (handled in input/pilot_service.py).
# Values are normalized fractions (0.0..1.0) and interpreted on the ROV side as
# a multiplier of the configured POWER_SCALE baseline.
PILOT_MAX_GAIN_DEFAULT = float(os.environ.get("TRITON_PILOT_MAX_GAIN_DEFAULT", "1.0"))
PILOT_MAX_GAIN_MIN = float(os.environ.get("TRITON_PILOT_MAX_GAIN_MIN", "0.05"))
PILOT_MAX_GAIN_MAX = float(os.environ.get("TRITON_PILOT_MAX_GAIN_MAX", "1.0"))
PILOT_MAX_GAIN_STEP = float(os.environ.get("TRITON_PILOT_MAX_GAIN_STEP", "0.05"))


# These are for TOPSIDE display/interaction only (they don't change onboard behavior
# unless you also update rov_config.py on the ROV side). They are used to show the
# estimated setpoint when using "walk target" depth hold.
DEPTH_HOLD_WALK_DEADBAND = float(os.environ.get("TRITON_DEPTH_HOLD_WALK_DEADBAND", "0.08"))
DEPTH_HOLD_WALK_RATE_MPS = float(os.environ.get("TRITON_DEPTH_HOLD_WALK_RATE_MPS", "0.60"))
DEPTH_HOLD_SENSOR_STALE_S = float(os.environ.get("TRITON_DEPTH_HOLD_SENSOR_STALE_S", "2.0"))
