# input/pilot_service.py
from __future__ import annotations
import time
import json
import threading
from typing import Optional

import zmq

from schema.pilot_common import PilotFrame, PilotAxes, PilotButtons
from input.controller import GamepadSource, ControllerSnapshot


class PilotPublisherService:
    """
    Background service:
      - pulls controller snapshots
      - builds PilotFrame
      - PUB to ROV

    Usage:
        svc = PilotPublisherService(endpoint="tcp://192.168.1.2:6000")
        svc.start()
        ...
        svc.stop()
    """
    def __init__(self,
                 endpoint: str,
                 rate_hz: float = 30.0,
                 deadzone: float = 0.1,
                 debug: bool = False):
        self.endpoint = endpoint
        self.period = 1.0 / rate_hz
        self.debug = debug

        self.controller = GamepadSource(deadzone=deadzone)

        ctx = zmq.Context.instance()
        self.sock = ctx.socket(zmq.PUB)
        self.sock.connect(self.endpoint)

        # slow joiner fix — same as your working pub_debug.py
        time.sleep(1.0)

        self.seq = 0
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_debug = 0.0

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
            t0 = time.time()

            snap: ControllerSnapshot = self.controller.read_once()

            frame = PilotFrame(
                seq=self.seq,
                ts=t0,
                axes=PilotAxes(
                    lx=snap.lx,
                    ly=snap.ly,
                    rx=snap.rx,
                    ry=snap.ry,
                    lt=snap.lt,
                    rt=snap.rt,
                ),
                buttons=PilotButtons(
                    a=snap.a,
                    b=snap.b,
                    x=snap.x,
                    y=snap.y,
                    lb=snap.lb,
                    rb=snap.rb,
                    win=snap.win,
                    menu=snap.menu,
                    lstick=snap.lstick,
                    rstick=snap.rstick,
                ),
                dpad=snap.dpad,
            )
            self.seq += 1

            self.sock.send_string(json.dumps(frame.to_dict()))

            if self.debug and (t0 - self._last_debug) > 1.0:
                print(f"[pilot] sent seq={frame.seq} axes={frame.axes} dpad={frame.dpad}")
                self._last_debug = t0

            # pacing
            elapsed = time.time() - t0
            sleep_for = self.period - elapsed
            if sleep_for > 0:
                time.sleep(sleep_for)
