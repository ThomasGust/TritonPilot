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
