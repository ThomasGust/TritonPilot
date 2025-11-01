import pygame
import time
from dataclasses import dataclass, field
from typing import Tuple


# ---------- 1) State object ----------

@dataclass
class ControllerState:
    # axes
    lx: float = 0.0
    ly: float = 0.0
    rx: float = 0.0
    ry: float = 0.0
    lt: float = 0.0
    rt: float = 0.0

    # dpad / hat
    dpad: Tuple[int, int] = (0, 0)

    # buttons
    a: bool = False
    b: bool = False
    x: bool = False
    y: bool = False
    lb: bool = False
    rb: bool = False
    win: bool = False     # button 6
    menu: bool = False    # button 7
    lstick: bool = False  # button 8
    rstick: bool = False  # button 9

    # to help detect edges (pressed this frame, etc.)
    # we'll fill this after reading
    _raw_buttons: dict = field(default_factory=dict, repr=False)


# ---------- 2) Controller wrapper ----------

class XboxController:
    """
    Non-blocking pygame Xbox controller wrapper, using your exact mapping.
    Call .update() frequently to refresh .state
    """

    # your mapping
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
        # pygame init (safe to call multiple times)
        pygame.init()
        pygame.joystick.init()

        if pygame.joystick.get_count() == 0:
            raise RuntimeError("No controller found")

        self.js = pygame.joystick.Joystick(index)
        self.js.init()
        self.name = self.js.get_name()

        self.state = ControllerState()

    def _apply_deadzone(self, v: float) -> float:
        return 0.0 if abs(v) < self.DEADZONE else v

    def update(self):
        """
        Poll the controller once and update self.state.
        Non-blocking: just returns immediately.
        Call this each frame / loop iteration.
        """
        # let pygame process USB events
        pygame.event.pump()

        s = self.state

        # --- axes ---
        lx = self.js.get_axis(self.AX_LX)
        ly = self.js.get_axis(self.AX_LY)
        rx = self.js.get_axis(self.AX_RX)
        ry = self.js.get_axis(self.AX_RY)
        lt = self.js.get_axis(self.AX_LT)
        rt = self.js.get_axis(self.AX_RT)

        # invert Y because you said "up is negative, down is positive"
        ly = -ly
        ry = -ry

        s.lx = self._apply_deadzone(lx)
        s.ly = self._apply_deadzone(ly)
        s.rx = self._apply_deadzone(rx)
        s.ry = self._apply_deadzone(ry)

        # triggers usually 0..1, but pygame sometimes gives -1..1
        # your note said: "pulling down makes it go positive" -> we'll just pass it through
        s.lt = max(0.0, lt)
        s.rt = max(0.0, rt)

        # --- hat / dpad ---
        # you have only one hat in your mapping
        if self.js.get_numhats() > 0:
            s.dpad = self.js.get_hat(0)
        else:
            s.dpad = (0, 0)

        # --- buttons ---
        btns = {
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

        s.a = bool(btns[self.BTN_A])
        s.b = bool(btns[self.BTN_B])
        s.x = bool(btns[self.BTN_X])
        s.y = bool(btns[self.BTN_Y])
        s.lb = bool(btns[self.BTN_LB])
        s.rb = bool(btns[self.BTN_RB])
        s.win = bool(btns[self.BTN_WIN])
        s.menu = bool(btns[self.BTN_MENU])
        s.lstick = bool(btns[self.BTN_LS])
        s.rstick = bool(btns[self.BTN_RS])

        # store raw for edge detection
        s._raw_buttons = btns

    # optional helper: was button just pressed?
    def was_pressed(self, button: int) -> bool:
        """
        Call once per frame *after* update()
        to detect rising edge (0 -> 1).
        """
        # we could track previous state in here;
        # easiest is to add a per-instance dict
        if not hasattr(self, "_prev_buttons"):
            self._prev_buttons = {}
        now = self.state._raw_buttons.get(button, 0)
        prev = self._prev_buttons.get(button, 0)
        self._prev_buttons[button] = now
        return (not prev) and bool(now)
