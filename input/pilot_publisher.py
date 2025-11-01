# topside_pilot.py
from __future__ import annotations
import time
import json
import threading
from typing import Optional, Callable, Dict

import zmq
import pygame

from schema import PilotFrame, PilotAxes, PilotButtons


class XboxController:
    """
    Your pygame-based controller, slightly trimmed to keep this file focused.
    """
    AX_LX = 0
    AX_LY = 1
    AX_RX = 2
    AX_RY = 3
    AX_LT = 4
    AX_RT = 5

    BTN_A = 0
    BTN_B = 1
    BTN_X = 2
    BTN_Y = 3
    BTN_LB = 4
    BTN_RB = 5
    BTN_WIN = 6
    BTN_MENU = 7
    BTN_LS = 8
    BTN_RS = 9

    DEADZONE = 0.1

    def __init__(self, index: int = 0):
        pygame.init()
        pygame.joystick.init()
        if pygame.joystick.get_count() == 0:
            raise RuntimeError("No controller found")
        self.js = pygame.joystick.Joystick(index)
        self.js.init()
        self.name = self.js.get_name()

        # for edge detection
        self._prev_buttons: Dict[int, int] = {}

    def _apply_deadzone(self, v: float) -> float:
        return 0.0 if abs(v) < self.DEADZONE else float(v)

    def read_state(self) -> (PilotAxes, PilotButtons, tuple[int, int], Dict[str, str]):
        pygame.event.pump()

        # axes
        lx = self._apply_deadzone(self.js.get_axis(self.AX_LX))
        ly = self._apply_deadzone(-self.js.get_axis(self.AX_LY))  # invert
        rx = self._apply_deadzone(self.js.get_axis(self.AX_RX))
        ry = self._apply_deadzone(-self.js.get_axis(self.AX_RY))
        lt = max(0.0, self.js.get_axis(self.AX_LT))
        rt = max(0.0, self.js.get_axis(self.AX_RT))

        axes = PilotAxes(lx=lx, ly=ly, rx=rx, ry=ry, lt=lt, rt=rt)

        # dpad / hat
        dpad = (0, 0)
        if self.js.get_numhats() > 0:
            dpad = self.js.get_hat(0)

        # buttons (raw)
        raw_btns = {
            self.BTN_A: self.js.get_button(self.BTN_A),
            self.BTN_B: self.js.get_button(self.BTN_B),
            self.BTN_X: self.js.get_button(self.BTN_X),
            self.BTN_Y: self.js.get_button(self.BTN_Y),
            self.BTN_LB: self.js.get_button(self.BTN_LB),
            self.BTN_RB: self.js.get_button(self.BTN_RB),
            self.BTN_WIN: self.js.get_button(self.BTN_WIN),
            self.BTN_MENU: self.js.get_button(self.BTN_MENU),
            self.BTN_LS: self.js.get_button(self.BTN_LS),
            self.BTN_RS: self.js.get_button(self.BTN_RS),
        }

        buttons = PilotButtons(
            a=bool(raw_btns[self.BTN_A]),
            b=bool(raw_btns[self.BTN_B]),
            x=bool(raw_btns[self.BTN_X]),
            y=bool(raw_btns[self.BTN_Y]),
            lb=bool(raw_btns[self.BTN_LB]),
            rb=bool(raw_btns[self.BTN_RB]),
            win=bool(raw_btns[self.BTN_WIN]),
            menu=bool(raw_btns[self.BTN_MENU]),
            lstick=bool(raw_btns[self.BTN_LS]),
            rstick=bool(raw_btns[self.BTN_RS]),
        )

        # edges
        edges: Dict[str, str] = {}
        edges_map = {
            self.BTN_A: "a",
            self.BTN_B: "b",
            self.BTN_X: "x",
            self.BTN_Y: "y",
            self.BTN_LB: "lb",
            self.BTN_RB: "rb",
            self.BTN_MENU: "menu",
            self.BTN_WIN: "win",
            self.BTN_LS: "lstick",
            self.BTN_RS: "rstick",
        }
        for btn_id, name in edges_map.items():
            now = raw_btns[btn_id]
            prev = self._prev_buttons.get(btn_id, 0)
            if not prev and now:
                edges[name] = "rise"
            elif prev and not now:
                edges[name] = "fall"
            self._prev_buttons[btn_id] = now

        return axes, buttons, dpad, edges


class PilotPublisher:
    """
    Publishes PilotFrame at a fixed rate.
    """
    def __init__(self,
                 controller: XboxController,
                 endpoint: str = "tcp://192.168.1.2:6000",
                 rate_hz: float = 30.0,
                 make_modes: Optional[Callable[[], dict]] = None):
        self.controller = controller
        self.endpoint = endpoint
        self.period = 1.0 / rate_hz
        self.make_modes = make_modes

        ctx = zmq.Context.instance()
        self.sock = ctx.socket(zmq.PUB)
        # topside usually connects, ROV binds; but we can also bind here
        self.sock.connect(self.endpoint)

        self._seq = 0
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

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
        next_t = time.time()
        while not self._stop.is_set():
            axes, buttons, dpad, edges = self.controller.read_state()
            frame = PilotFrame(
                seq=self._seq,
                ts=time.time(),
                axes=axes,
                buttons=buttons,
                dpad=dpad,
                edges=edges,
                modes=self.make_modes() if self.make_modes else {},
            )
            self._seq += 1

            payload = json.dumps(frame.to_dict())
            # could add topic, but empty is fine
            self.sock.send_string(payload)

            next_t += self.period
            now = time.time()
            sleep_for = next_t - now
            if sleep_for > 0:
                time.sleep(sleep_for)
            else:
                # we slipped; just reset the schedule
                next_t = now

if __name__ == "__main__":
    ctrl = XboxController()
    pub = PilotPublisher(ctrl, endpoint="tcp://192.168.1.2:6000", rate_hz=30.0)
    pub.start()
    # keep main alive
    while True:
        time.sleep(1)
