"""
Global-ish config for topside.

You can import this from anywhere:

    from config import PILOT_PUB_ENDPOINT, SENSOR_SUB_ENDPOINT, VIDEO_RPC_ENDPOINT
"""

import os
from pathlib import Path

# ROV IP (can be overridden via env)
ROV_HOST = os.environ.get("ROV_HOST", "192.168.1.1")

# ZMQ endpoints
PILOT_PUB_ENDPOINT = os.environ.get("ROV_PILOT_EP", f"tcp://{ROV_HOST}:6000")
SENSOR_SUB_ENDPOINT = os.environ.get("ROV_SENSOR_EP", f"tcp://{ROV_HOST}:6001")
VIDEO_RPC_ENDPOINT = os.environ.get("ROV_VIDEO_RPC", f"tcp://{ROV_HOST}:5555")

# Where your JSON with stream definitions lives
STREAMS_FILE = Path(__file__).parent / "data" / "streams.json"
