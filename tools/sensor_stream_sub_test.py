#!/usr/bin/env python3
"""Subscribe to sensor telemetry over ZMQ (topside test).

This script is meant to run on the *topside* computer and connects to the
ROV's ZMQ sensor PUB endpoint (default tcp://<ROV_HOST>:6001).

It provides a small terminal dashboard (default), can print raw JSON lines,
OR can show a tiny PyQt6 window (optional) using the existing SensorPanel.

Examples
--------
  # Use default endpoint from config.py (ROV_HOST env var controls the IP)
  python3 tests/sensor_stream_sub_test.py

  # Explicit endpoint
  python3 tests/sensor_stream_sub_test.py --endpoint tcp://{ROV_HOST}:6001

  # Dashboard off; print each message as JSON
  python3 tests/sensor_stream_sub_test.py --raw

  # Optional: show a small Qt sensor table
  python3 tests/sensor_stream_sub_test.py --qt
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

import zmq

# Ensure repo root is on sys.path when run as `python3 tests/...`
_THIS = Path(__file__).resolve()
_REPO_ROOT = _THIS.parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

try:
    import config as cfg  # type: ignore
except Exception:
    cfg = None


def _default_endpoint() -> str:
    if cfg is None:
        return os.environ.get("ROV_SENSOR_EP", "tcp://127.0.0.1:6001")
    return getattr(cfg, "SENSOR_SUB_ENDPOINT", "tcp://127.0.0.1:6001")


def _connect_sub(endpoint: str) -> zmq.Socket:
    ctx = zmq.Context.instance()
    sock = ctx.socket(zmq.SUB)
    sock.connect(endpoint)
    sock.setsockopt_string(zmq.SUBSCRIBE, "")
    return sock


def _format_msg(msg: dict) -> str:
    typ = msg.get("type")
    if typ == "imu":
        a = msg.get("accel", {})
        g = msg.get("gyro", {})
        return (
            f"imu  acc=({a.get('x', 0): .2f},{a.get('y', 0): .2f},{a.get('z', 0): .2f}) "
            f"gyro=({g.get('x', 0): .2f},{g.get('y', 0): .2f},{g.get('z', 0): .2f})"
        )
    if typ == "env":
        return f"env  {msg.get('temperature_c', 0):.1f} C  {msg.get('pressure_kpa', 0):.1f} kPa"
    if typ == "external_depth":
        sensor = msg.get("sensor", "external_depth")
        return (
            f"{sensor} depth={msg.get('depth_m', 0):.2f} m  "
            f"temp={msg.get('temperature_c', 0):.1f} C  "
            f"p={msg.get('pressure_mbar', 0):.0f} mbar"
        )
    return json.dumps(msg, sort_keys=True)


def run_raw(sock: zmq.Socket, jsonl_path: Optional[str] = None) -> None:
    f = open(jsonl_path, "a", encoding="utf-8") if jsonl_path else None
    if f:
        print(f"[sensor-sub] writing jsonl to {jsonl_path}", flush=True)

    try:
        while True:
            raw = sock.recv_string()
            if f:
                f.write(raw + "\n")
                f.flush()
            try:
                msg = json.loads(raw)
            except Exception:
                print(raw)
                continue
            print(_format_msg(msg), flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        if f:
            f.close()


def run_dashboard(sock: zmq.Socket, require_types: list[str], require_timeout_s: float, jsonl_path: Optional[str]) -> None:
    try:
        import curses
    except Exception:
        print("[sensor-sub] curses not available; falling back to --raw")
        run_raw(sock, jsonl_path=jsonl_path)
        return

    f = open(jsonl_path, "a", encoding="utf-8") if jsonl_path else None

    latest: Dict[str, dict] = {}
    last_seen_t: Dict[str, float] = {}

    total = 0
    # Rate estimation per type over a short window
    win_counts: Dict[str, int] = {}
    win_t0 = time.time()
    rates: Dict[str, float] = {}

    seen_required: Dict[str, bool] = {t: False for t in require_types}
    start_t = time.time()

    def _poll_messages(max_per_tick: int = 200):
        nonlocal total, win_t0
        for _ in range(max_per_tick):
            try:
                raw = sock.recv_string(flags=zmq.NOBLOCK)
            except zmq.Again:
                break

            if f:
                f.write(raw + "\n")

            try:
                msg = json.loads(raw)
            except Exception:
                continue

            total += 1
            typ = str(msg.get("type", "?"))
            sensor = str(msg.get("sensor", typ))

            latest[sensor] = msg
            last_seen_t[sensor] = time.time()

            win_counts[typ] = win_counts.get(typ, 0) + 1

            if typ in seen_required:
                seen_required[typ] = True

        # update rate window
        now = time.time()
        if now - win_t0 >= 1.0:
            dt = max(now - win_t0, 1e-6)
            rates.update({k: v / dt for k, v in win_counts.items()})
            win_counts.clear()
            win_t0 = now

        if f:
            f.flush()

    def _render(stdscr):
        stdscr.nodelay(True)
        stdscr.timeout(0)

        while True:
            _poll_messages()

            # quit
            ch = stdscr.getch()
            if ch in (ord('q'), ord('Q')):
                break

            stdscr.erase()
            h, w = stdscr.getmaxyx()

            now = time.time()
            ep_line = f"Endpoint: {args.endpoint}"
            stdscr.addnstr(0, 0, ep_line, w - 1)

            req_ok = all(seen_required.values()) if require_types else True
            req_line = "Required: " + (" ".join([f"{t}={'OK' if seen_required.get(t) else '...'}" for t in require_types]) or "(none)")
            if not req_ok and (now - start_t) > require_timeout_s:
                req_line += "  (TIMEOUT)"
            stdscr.addnstr(1, 0, req_line, w - 1)

            stdscr.addnstr(2, 0, f"Msgs: {total}   (press 'q' to quit)", w - 1)

            row = 4

            # Show a stable order for common sensors
            preferred = ["imu", "env", "bar02", "bar30"]
            ordered_sensors = []
            for s in preferred:
                if s in latest:
                    ordered_sensors.append(s)
            for s in sorted(latest.keys()):
                if s not in ordered_sensors:
                    ordered_sensors.append(s)

            for sensor in ordered_sensors:
                if row >= h - 1:
                    break

                msg = latest.get(sensor, {})
                typ = str(msg.get("type", "?"))
                age = now - last_seen_t.get(sensor, now)
                rate = rates.get(typ, 0.0)

                line = f"{sensor:>8}  {typ:<14}  rate~{rate:5.1f}Hz  age={age:4.1f}s  {_format_msg(msg)}"
                stdscr.addnstr(row, 0, line, w - 1)
                row += 1

            stdscr.refresh()
            time.sleep(0.05)

    try:
        curses.wrapper(_render)
    finally:
        if f:
            f.close()


def run_qt(endpoint: str) -> None:
    # Late imports so the terminal dashboard doesn't require PyQt6.
    from PyQt6.QtCore import pyqtSignal, QObject
    from PyQt6.QtWidgets import QApplication, QMainWindow

    from gui.sensor_panel import SensorPanel
    from telemetry.sensor_service import SensorSubscriberService

    class Bridge(QObject):
        sig = pyqtSignal(dict)

    app = QApplication(sys.argv)

    win = QMainWindow()
    win.setWindowTitle("Sensor Stream Test")
    panel = SensorPanel()
    win.setCentralWidget(panel)
    win.resize(700, 420)

    bridge = Bridge()
    bridge.sig.connect(panel.upsert_sensor)

    svc = SensorSubscriberService(endpoint=endpoint, on_message=lambda m: bridge.sig.emit(m), debug=False)
    svc.start()

    win.show()
    try:
        sys.exit(app.exec())
    finally:
        try:
            svc.stop()
        except Exception:
            pass


def main() -> int:
    ap = argparse.ArgumentParser(description="Topside sensor streaming subscriber test")
    ap.add_argument("--endpoint", default=_default_endpoint(), help="ZMQ endpoint to connect, e.g. tcp://{ROV_HOST}:6001")
    ap.add_argument("--rov-host", default=None, help="Shortcut: set host/IP and use tcp://<host>:6001")
    ap.add_argument("--raw", action="store_true", help="Print each message (no dashboard)")
    ap.add_argument("--no-ui", action="store_true", help="Alias for --raw")
    ap.add_argument("--qt", action="store_true", help="Show a small PyQt6 window (requires PyQt6)")
    ap.add_argument("--require", nargs="*", default=["imu", "env"], help="Message types required to consider stream OK")
    ap.add_argument("--require-timeout", type=float, default=5.0, help="Seconds to wait for required message types")
    ap.add_argument("--jsonl", default=None, help="Write all received messages as JSON-lines to this path")
    global args
    args = ap.parse_args()

    if args.rov_host:
        args.endpoint = f"tcp://{args.rov_host}:6001"

    if args.qt:
        run_qt(args.endpoint)
        return 0

    sock = _connect_sub(args.endpoint)
    print(f"[sensor-sub] connected to {args.endpoint}", flush=True)

    if args.raw or args.no_ui:
        run_raw(sock, jsonl_path=args.jsonl)
        return 0

    run_dashboard(sock, require_types=list(args.require or []), require_timeout_s=args.require_timeout, jsonl_path=args.jsonl)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
