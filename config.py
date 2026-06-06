"""
Global-ish config for topside.

You can import this from anywhere:

    from config import PILOT_PUB_ENDPOINT, SENSOR_SUB_ENDPOINT, VIDEO_RPC_ENDPOINT, MANAGEMENT_RPC_ENDPOINT
"""

import os
import socket
from pathlib import Path

DEFAULT_ROV_HOST = os.environ.get("TRITON_ROV_DEFAULT_HOST", "192.168.1.4")


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return bool(default)
    return raw in ("1", "true", "yes", "on")


def _split_hosts(raw: str) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for part in raw.replace(";", ",").split(","):
        host = part.strip()
        if not host or host in seen:
            continue
        seen.add(host)
        out.append(host)
    return out


def _tcp_reachable_host(host: str, port: int, timeout_s: float) -> str | None:
    try:
        sock = socket.create_connection((host, int(port)), timeout=float(timeout_s))
    except OSError:
        return None
    reachable = host
    try:
        peer = sock.getpeername()
        if isinstance(peer, tuple) and peer:
            peer_host = str(peer[0])
            if peer_host and ":" not in peer_host:
                reachable = peer_host
    except Exception:
        pass
    try:
        sock.close()
    except Exception:
        pass
    return reachable


def _auto_detect_rov_host() -> str:
    """Pick a reachable ROV host without forcing a stale tether IP.

    The preferred path is still the wired tether at 192.168.1.4. If that route
    is down, fall back to mDNS so bench testing can continue over the Pi's Wi-Fi.
    Explicit ROV_HOST/ROV_* endpoint environment variables still win.
    """
    explicit = os.environ.get("ROV_HOST", "").strip()
    if explicit:
        return explicit

    default_host = DEFAULT_ROV_HOST
    if not _env_bool("TRITON_ROV_AUTO_DETECT", True):
        return default_host

    candidates = _split_hosts(os.environ.get("TRITON_ROV_HOSTS", f"{default_host},tritonpi.local"))
    ports = (6001, 5556)
    timeout_s = float(os.environ.get("TRITON_ROV_HOST_PROBE_TIMEOUT", "0.25"))
    for host in candidates:
        for port in ports:
            reachable = _tcp_reachable_host(host, port, timeout_s)
            if reachable:
                return reachable
    return default_host


# ROV host (can be overridden via env)
ROV_HOST = _auto_detect_rov_host()

# ZMQ endpoints
PILOT_PUB_ENDPOINT = os.environ.get("ROV_PILOT_EP", f"tcp://{ROV_HOST}:6000")
SENSOR_SUB_ENDPOINT = os.environ.get("ROV_SENSOR_EP", f"tcp://{ROV_HOST}:6001")
VIDEO_RPC_ENDPOINT = os.environ.get("ROV_VIDEO_RPC", f"tcp://{ROV_HOST}:5555")
MANAGEMENT_RPC_ENDPOINT = os.environ.get("ROV_MANAGEMENT_RPC", f"tcp://{ROV_HOST}:5556")

# Video reconnect policy. Four 1080p streams can take a few seconds to settle,
# especially while the Pi is starting every camera pipeline at once.
VIDEO_STALL_TIMEOUT_S = float(os.environ.get("TRITON_VIDEO_STALL_TIMEOUT_S", "8.0"))
VIDEO_FIRST_FRAME_TIMEOUT_S = float(os.environ.get("TRITON_VIDEO_FIRST_FRAME_TIMEOUT_S", "14.0"))
VIDEO_WARM_HIDDEN_STREAMS = os.environ.get("TRITON_VIDEO_WARM_HIDDEN_STREAMS", "0").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)
VIDEO_WARMUP_INTERVAL_MS = int(os.environ.get("TRITON_VIDEO_WARMUP_INTERVAL_MS", "750"))


def _float_env(name: str, default: float, *, min_value: float, max_value: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return float(default)
    try:
        value = float(raw)
    except Exception:
        return float(default)
    if value < float(min_value) or value > float(max_value):
        return float(default)
    return value


def _layout_count_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return int(default)
    try:
        value = int(raw)
    except Exception:
        return int(default)
    return value if value in (1, 2, 3, 4) else int(default)


# Default to quad view for piloting, and keep hidden streams warm once started
# so layout/camera switches do not need to ask TritonOS to recreate pipelines.
VIDEO_DEFAULT_LAYOUT_COUNT = _layout_count_env("TRITON_VIDEO_DEFAULT_LAYOUT_COUNT", 4)
VIDEO_STOP_HIDDEN_STREAMS = os.environ.get("TRITON_VIDEO_STOP_HIDDEN_STREAMS", "0").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)
VIDEO_DISPLAY_FPS_SINGLE = _float_env("TRITON_VIDEO_DISPLAY_FPS_SINGLE", 30.0, min_value=1.0, max_value=60.0)
VIDEO_DISPLAY_FPS_DUAL = _float_env("TRITON_VIDEO_DISPLAY_FPS_DUAL", 30.0, min_value=1.0, max_value=60.0)
VIDEO_DISPLAY_FPS_MULTI = _float_env("TRITON_VIDEO_DISPLAY_FPS_MULTI", 30.0, min_value=1.0, max_value=60.0)

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

# Roll/pitch leveling and yaw hold are sent as part of
# PilotFrame.modes["autopilot"].
# Roll/pitch leveling is intentionally GUI-first by default; yaw hold mirrors
# depth hold and is toggled by pressing down the LEFT stick (lstick).
ROLL_PITCH_LEVEL_TOGGLE_BUTTON = os.environ.get("TRITON_RP_LEVEL_TOGGLE", "").strip().lower()
ROLL_PITCH_LEVEL_DEFAULT = os.environ.get("TRITON_RP_LEVEL_DEFAULT", "0").strip().lower() in ("1", "true", "yes")
YAW_HOLD_TOGGLE_BUTTON = os.environ.get("TRITON_YAW_HOLD_TOGGLE", "lstick").strip().lower()
YAW_HOLD_DEFAULT = os.environ.get("TRITON_YAW_HOLD_DEFAULT", "0").strip().lower() in ("1", "true", "yes")

# Topside-only fallback attitude estimator convention. The onboard estimator is
# authoritative when available; these settings keep the raw-sensor page aligned
# during local fallback/replay.
ATTITUDE_VEHICLE_ROLL_AXIS = os.environ.get("TRITON_ATTITUDE_VEHICLE_ROLL_AXIS", "z").strip() or "z"
ATTITUDE_ROLL_SIGN = float(os.environ.get("TRITON_ATTITUDE_ROLL_SIGN", "1.0"))
ATTITUDE_PITCH_SIGN = float(os.environ.get("TRITON_ATTITUDE_PITCH_SIGN", "1.0"))

# Lights are toggled by sending TritonOS its normal synthetic button edge.
# Default control: keyboard L. Set TRITON_LIGHTS_TOGGLE_BUTTON if you want a
# physical button in addition to the keyboard shortcut.
LIGHTS_TOGGLE_SHORTCUT = os.environ.get("TRITON_LIGHTS_TOGGLE_SHORTCUT", "L").strip() or "L"
LIGHTS_TOGGLE_BUTTON = os.environ.get("TRITON_LIGHTS_TOGGLE_BUTTON", "").strip().lower()
LIGHTS_TOGGLE_EDGE = os.environ.get("TRITON_LIGHTS_TOGGLE_EDGE", "lights").strip().lower() or "lights"

# Arm/disarm is sent as TritonOS' normal controller menu/start edge. The laptop
# keyboard shortcut gives the pilot a backup when that hardware button fails.
ARM_DISARM_TOGGLE_SHORTCUT = os.environ.get("TRITON_ARM_DISARM_SHORTCUT", "O").strip() or "O"
ARM_DISARM_TOGGLE_EDGE = os.environ.get("TRITON_ARM_DISARM_EDGE", "menu").strip().lower() or "menu"

# Reverse drive mode rotates the pilot's translation commands by 180 degrees
# so surge/sway still match when the operator swaps to a rear camera. Yaw keeps
# its normal left/right sign.
# By default this is toggleable from the controller's left bumper (`lb`) and
# from the GUI/menu with the `R` shortcut.
REVERSE_MODE_DEFAULT = os.environ.get("TRITON_REVERSE_MODE_DEFAULT", "0").strip().lower() in ("1", "true", "yes")
REVERSE_TOGGLE_BUTTON = os.environ.get("TRITON_REVERSE_TOGGLE", "lb").strip().lower()
REVERSE_TOGGLE_SHORTCUT = os.environ.get("TRITON_REVERSE_SHORTCUT", "R").strip() or "R"


def _parse_str_list_env(var: str, default: list[str]) -> list[str]:
    s = os.environ.get(var, "").strip()
    if not s:
        return list(default)
    parts = [p.strip() for p in s.split(",")]
    return [p for p in parts if p]


REVERSE_CAMERA_NAMES = _parse_str_list_env(
    "TRITON_REVERSE_CAMERA_NAMES",
    ["Reverse Camera", "Rear Camera", "Back Camera"],
)
REVERSE_CAMERA_KEYWORDS = _parse_str_list_env(
    "TRITON_REVERSE_CAMERA_KEYWORDS",
    ["reverse", "rear", "back"],
)
FORWARD_CAMERA_KEYWORDS = _parse_str_list_env(
    "TRITON_FORWARD_CAMERA_KEYWORDS",
    ["front", "forward"],
)


# Pilot-adjustable max gain / power cap (transmitted in PilotFrame.modes["max_gain"]).
# Y = +5%, A = -5% by default (handled in input/pilot_service.py).
# Values are normalized fractions (0.0..1.0) and interpreted on the ROV side as
# a multiplier of the configured POWER_SCALE baseline.
PILOT_MAX_GAIN_DEFAULT = float(os.environ.get("TRITON_PILOT_MAX_GAIN_DEFAULT", "1.0"))
PILOT_MAX_GAIN_MIN = float(os.environ.get("TRITON_PILOT_MAX_GAIN_MIN", "0.05"))
PILOT_MAX_GAIN_MAX = float(os.environ.get("TRITON_PILOT_MAX_GAIN_MAX", "1.0"))
PILOT_MAX_GAIN_STEP = float(os.environ.get("TRITON_PILOT_MAX_GAIN_STEP", "0.05"))

# Pilot-adjustable gain for the back rotating gripper / T200 wrist motor. This
# is transmitted separately from the main vehicle max gain so the manipulator
# can be tuned independently by TritonOS. The older TRITON_T200_* environment
# names remain accepted as fallbacks.
BACK_GRIPPER_GAIN_DEFAULT = float(
    os.environ.get(
        "TRITON_BACK_GRIPPER_GAIN_DEFAULT",
        os.environ.get("TRITON_T200_WRIST_GAIN_DEFAULT", "0.50"),
    )
)
BACK_GRIPPER_GAIN_MIN = float(
    os.environ.get(
        "TRITON_BACK_GRIPPER_GAIN_MIN",
        os.environ.get("TRITON_T200_WRIST_GAIN_MIN", "0.10"),
    )
)
BACK_GRIPPER_GAIN_MAX = float(
    os.environ.get(
        "TRITON_BACK_GRIPPER_GAIN_MAX",
        os.environ.get("TRITON_T200_WRIST_GAIN_MAX", "1.0"),
    )
)
BACK_GRIPPER_GAIN_STEP = float(
    os.environ.get(
        "TRITON_BACK_GRIPPER_GAIN_STEP",
        os.environ.get("TRITON_T200_WRIST_GAIN_STEP", "0.05"),
    )
)

# Backwards-compatible names used by older Pilot code/tests/docs.
T200_WRIST_GAIN_DEFAULT = BACK_GRIPPER_GAIN_DEFAULT
T200_WRIST_GAIN_MIN = BACK_GRIPPER_GAIN_MIN
T200_WRIST_GAIN_MAX = BACK_GRIPPER_GAIN_MAX
T200_WRIST_GAIN_STEP = BACK_GRIPPER_GAIN_STEP

# Pilot-adjustable gain for the keyboard-driven arm/gripper-head movement.
ARM_GAIN_DEFAULT = float(os.environ.get("TRITON_ARM_GAIN_DEFAULT", "0.50"))
ARM_GAIN_MIN = float(os.environ.get("TRITON_ARM_GAIN_MIN", "0.10"))
ARM_GAIN_MAX = float(os.environ.get("TRITON_ARM_GAIN_MAX", "1.0"))
ARM_GAIN_STEP = float(os.environ.get("TRITON_ARM_GAIN_STEP", "0.05"))

# Keyboard WASD arm motion rate in normalized command units per second at 100%
# ARM gain. Lower values make the servo target walk more slowly while a key is
# held. The default takes about 3 seconds to cross the full normalized range at
# 100% gain, and about 6 seconds at the default 50% ARM gain.
ARM_KEYBOARD_RAMP_RATE = float(os.environ.get("TRITON_ARM_KEYBOARD_RAMP_RATE", "0.35"))


# Legacy topside walk-target display settings. Current depth-hold manual
# override/latching behavior is owned by TritonOS; keep these only for older
# environments that still reference the names.
DEPTH_HOLD_WALK_DEADBAND = float(os.environ.get("TRITON_DEPTH_HOLD_WALK_DEADBAND", "0.10"))
DEPTH_HOLD_WALK_RATE_MPS = float(os.environ.get("TRITON_DEPTH_HOLD_WALK_RATE_MPS", "0.45"))
DEPTH_HOLD_SENSOR_STALE_S = float(os.environ.get("TRITON_DEPTH_HOLD_SENSOR_STALE_S", "2.0"))

# Topside yaw-hold display freshness. Manual-yaw override and release latching
# are owned by TritonOS.
YAW_HOLD_ATTITUDE_STALE_S = float(os.environ.get("TRITON_YAW_HOLD_ATTITUDE_STALE_S", "1.0"))


# ---------------------------------------------------------------------------
# Out-of-water lens correction (DWE exploreHD)
# ---------------------------------------------------------------------------
# When enabled via View > Water Correction, each video frame is passed through
# a remap that approximates how the ExploreHD appears once submerged.
#
# The model reprojects the in-air fisheye-like image into a rectilinear view.
# `WATER_CORRECTION_TARGET_HFOV_DEG` sets the corrected horizontal field of
# view, while `WATER_CORRECTION_ZOOM` is a small trim on top:
#   1.00 = use the configured target FOV
#   >1.0 = slightly tighter crop
#   <1.0 = slightly wider crop
WATER_CORRECTION_ZOOM = float(os.environ.get("TRITON_WATER_ZOOM", "1.0"))
WATER_CORRECTION_K1   = float(os.environ.get("TRITON_WATER_K1",   "0.0"))
WATER_CORRECTION_K2   = float(os.environ.get("TRITON_WATER_K2",   "0.0"))
WATER_CORRECTION_K3   = float(os.environ.get("TRITON_WATER_K3",   "0.0"))
WATER_CORRECTION_AIR_HFOV_DEG = float(os.environ.get("TRITON_WATER_AIR_HFOV_DEG", "138.0"))
WATER_CORRECTION_TARGET_HFOV_DEG = float(os.environ.get("TRITON_WATER_TARGET_HFOV_DEG", "96.0"))

