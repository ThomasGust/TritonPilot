# telemetry/sensor_service.py
from __future__ import annotations
import json
import threading
import time
from typing import Callable, Optional

import zmq


class SensorSubscriberService:
    """
    Background ZMQ SUB that calls a Python callback for every sensor message.
    """

    def __init__(self,
                 endpoint: str,
                 on_message: Optional[Callable[[dict], None]] = None,
                 debug: bool = False):
        self.endpoint = endpoint
        self.on_message = on_message
        self.debug = debug

        ctx = zmq.Context.instance()
        self.sock = ctx.socket(zmq.SUB)
        self.sock.connect(self.endpoint)
        self.sock.setsockopt_string(zmq.SUBSCRIBE, "")

        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1.0)

    def _run(self):
        while not self._stop.is_set():
            try:
                raw = self.sock.recv_string(flags=zmq.NOBLOCK)
            except zmq.Again:
                time.sleep(0.01)
                continue

            try:
                msg = json.loads(raw)
            except Exception:
                if self.debug:
                    print("[sensor] bad json:", raw)
                continue

            if self.on_message:
                self.on_message(msg)
            elif self.debug:
                sensor = msg.get("sensor")
                msg_type = msg.get("type")
                print(f"[sensor] {sensor}/{msg_type}: {msg}")
