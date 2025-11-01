# input/controller.py
from __future__ import annotations
import pygame
from dataclasses import dataclass
from typing import Tuple


@dataclass
class ControllerSnapshot:
    # axes
    lx: float
    ly: float
    rx: float
    ry: float
    lt: float
    rt: float
    # dpad
    dpad: Tuple[int, int]
    # buttons (xbox layout you probed)
    a: bool
    b: bool
    x: bool
    y: bool
    lb: bool
    rb: bool
    win: bool
    menu: bool
    lstick: bool
    rstick: bool


class GamepadSource:
    """
    Read the exact mapping you discovered with the Tk GUI:

        Axes:
            0: LS X (left -, right +)
            1: LS Y (up -, down +)
            2: RS X
            3: RS Y
            4: LT (0..1, sometimes -1..1)
            5: RT (0..1, sometimes -1..1)
        Hats: (x, y)
        Buttons: 0..9 as A,B,X,Y,LB,RB,WIN,MENU,LS,RS

    This class does NOT loop and does NOT talk to ZMQ.
    """

    def __init__(self, deadzone: float = 0.1, index: int = 0):
        pygame.init()
        pygame.joystick.init()

        if pygame.joystick.get_count() == 0:
            raise RuntimeError("No controller found")

        self.js = pygame.joystick.Joystick(index)
        self.js.init()
        self.deadzone = deadzone
        self.name = self.js.get_name()

    def _dz(self, v: float) -> float:
        return 0.0 if abs(v) < self.deadzone else v

    def read_once(self) -> ControllerSnapshot:
        pygame.event.pump()

        lx = self._dz(self.js.get_axis(0))
        ly = self._dz(-self.js.get_axis(1))  # invert Y
        rx = self._dz(self.js.get_axis(2))
        ry = self._dz(-self.js.get_axis(3))
        lt = max(0.0, self.js.get_axis(4))
        rt = max(0.0, self.js.get_axis(5))

        if self.js.get_numhats() > 0:
            dpad = self.js.get_hat(0)
        else:
            dpad = (0, 0)

        a = bool(self.js.get_button(0))
        b = bool(self.js.get_button(1))
        x = bool(self.js.get_button(2))
        y = bool(self.js.get_button(3))
        lb = bool(self.js.get_button(4))
        rb = bool(self.js.get_button(5))
        win = bool(self.js.get_button(6))
        menu = bool(self.js.get_button(7))
        lstick = bool(self.js.get_button(8))
        rstick = bool(self.js.get_button(9))

        return ControllerSnapshot(
            lx=lx, ly=ly, rx=rx, ry=ry, lt=lt, rt=rt,
            dpad=dpad,
            a=a, b=b, x=x, y=y,
            lb=lb, rb=rb,
            win=win, menu=menu,
            lstick=lstick, rstick=rstick,
        )
