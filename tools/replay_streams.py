#!/usr/bin/env python3
"""Replay a recorded streams.jsonl file onto ZMQ endpoints.

Example:
  python tools/replay_streams.py recordings/20251230-101010/streams.jsonl \
      --pilot-endpoint tcp://127.0.0.1:6000 --sensor-endpoint tcp://127.0.0.1:6001

By default replays with original timing. Use --speed 2.0 for 2x faster.
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import zmq


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("jsonl", help="Path to streams.jsonl")
    ap.add_argument("--pilot-endpoint", default=None, help="PUB endpoint for pilot frames (tcp://*:6000)")
    ap.add_argument("--sensor-endpoint", default=None, help="PUB endpoint for sensor/heartbeat frames (tcp://*:6001)")
    ap.add_argument("--speed", type=float, default=1.0, help="Playback speed (1.0 = real time)")
    args = ap.parse_args()

    ctx = zmq.Context.instance()

    pub_pilot = None
    pub_sensors = None

    if args.pilot_endpoint:
        pub_pilot = ctx.socket(zmq.PUB)
        pub_pilot.bind(args.pilot_endpoint)

    if args.sensor_endpoint:
        pub_sensors = ctx.socket(zmq.PUB)
        pub_sensors.bind(args.sensor_endpoint)

    t0 = None
    wall0 = None
    path = Path(args.jsonl)
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        ev = json.loads(line)
        t = float(ev.get("t", time.time()))
        stream = ev.get("stream")
        msg = ev.get("msg", {})

        if t0 is None:
            t0 = t
            wall0 = time.time()

        # sleep to preserve relative timing
        dt = (t - t0) / max(args.speed, 1e-9)
        while True:
            now = time.time()
            if now - wall0 >= dt:
                break
            time.sleep(0.001)

        raw = json.dumps(msg)

        if stream == "pilot" and pub_pilot is not None:
            pub_pilot.send_string(raw)
        elif stream in ("sensors", "heartbeat") and pub_sensors is not None:
            pub_sensors.send_string(raw)
        # else: ignore un-mapped streams

    print("Replay finished.")


if __name__ == "__main__":
    main()
