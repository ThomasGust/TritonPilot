# input/controller.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple, List, Optional, Dict, Any
import time

import pygame


@dataclass
class ControllerSnapshot:
    # axes (mapped schema)
    lx: float
    ly: float
    rx: float
    ry: float
    lt: float
    rt: float
    # dpad
    dpad: Tuple[int, int]
    # buttons (xbox-ish schema)
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


def _safe_get_guid(js: pygame.joystick.Joystick) -> str:
    fn = getattr(js, "get_guid", None)
    if callable(fn):
        try:
            return str(fn())
        except Exception:
            return "unknown"
    return "n/a"


def ensure_pygame_joystick() -> None:
    # These are idempotent in pygame
    pygame.init()
    pygame.joystick.init()


def list_controllers() -> List[Dict[str, Any]]:
    """
    Returns a list of dicts describing currently detected controllers.
    Safe to call even if no controllers exist.
    """
    ensure_pygame_joystick()
    out: List[Dict[str, Any]] = []
    count = pygame.joystick.get_count()
    for i in range(count):
        js = pygame.joystick.Joystick(i)
        js.init()
        out.append(
            {
                "index": i,
                "name": js.get_name(),
                "guid": _safe_get_guid(js),
                "axes": js.get_numaxes(),
                "buttons": js.get_numbuttons(),
                "hats": js.get_numhats(),
            }
        )
    return out


class GamepadSource:
    """
    Robust controller reader with:
      - device listing
      - safe axis/button/hat getters
      - optional debug prints

    NOTE: For best reliability, create and read this object from the SAME thread.
    """

    def __init__(
        self,
        deadzone: float = 0.1,
        index: int = 0,
        invert_ly: bool = True,
        invert_ry: bool = True,
        debug: bool = False,
    ):
        ensure_pygame_joystick()

        count = pygame.joystick.get_count()
        if count == 0:
            raise RuntimeError("No controller found (pygame.joystick.get_count() == 0)")

        if not (0 <= index < count):
            raise RuntimeError(f"Controller index {index} out of range (0..{count-1})")

        self.deadzone = float(deadzone)
        self.invert_ly = bool(invert_ly)
        self.invert_ry = bool(invert_ry)
        self.debug = bool(debug)

        self.js = pygame.joystick.Joystick(index)
        self.js.init()

        self.index = index
        self.name = self.js.get_name()
        self.guid = _safe_get_guid(self.js)

        # Instance ID is useful for hotplug diagnostics (pygame 2)
        try:
            self.instance_id = self.js.get_instance_id()
        except Exception:
            self.instance_id = None

        # Cache initial "rest" axis values for trigger normalization heuristics
        pygame.event.pump()
        self._rest_axes = [self._axis_raw(i) for i in range(self.js.get_numaxes())]

        if self.debug:
            self.print_device_summary(prefix="[controller] ")

    def print_device_summary(self, prefix: str = "") -> None:
        print(
            f"{prefix}opened index={self.index} name='{self.name}' guid='{self.guid}' "
            f"instance_id={self.instance_id} axes={self.js.get_numaxes()} "
            f"buttons={self.js.get_numbuttons()} hats={self.js.get_numhats()}"
        )

    def _dz(self, v: float) -> float:
        return 0.0 if abs(v) < self.deadzone else v

    def _axis_raw(self, i: int) -> float:
        n = self.js.get_numaxes()
        if 0 <= i < n:
            try:
                return float(self.js.get_axis(i))
            except Exception:
                return 0.0
        return 0.0

    def _button_raw(self, i: int) -> int:
        n = self.js.get_numbuttons()
        if 0 <= i < n:
            try:
                return int(self.js.get_button(i))
            except Exception:
                return 0
        return 0

    def _hat_raw(self, i: int) -> Tuple[int, int]:
        n = self.js.get_numhats()
        if 0 <= i < n:
            try:
                x, y = self.js.get_hat(i)
                return int(x), int(y)
            except Exception:
                return (0, 0)
        return (0, 0)

    @staticmethod
    def _clamp01(v: float) -> float:
        if v < 0.0:
            return 0.0
        if v > 1.0:
            return 1.0
        return v

    def _normalize_trigger(self, axis_index: int) -> float:
        """
        Attempt to normalize trigger axes to [0..1] robustly.

        Common patterns:
          - [0..1] where rest is 0.0
          - [-1..1] where rest is -1.0 -> map (v+1)/2
          - sometimes noisy; clamp to [0..1]
        """
        raw = self._axis_raw(axis_index)
        rest = self._rest_axes[axis_index] if axis_index < len(self._rest_axes) else 0.0

        # If it looks like [-1..1] with rest near -1.0, map to [0..1]
        if rest < -0.5:
            return self._clamp01((raw + 1.0) * 0.5)

        # Otherwise treat as [0..1] (or best-effort clamp)
        return self._clamp01(raw)

    def read_raw_state(self) -> Dict[str, Any]:
        """
        Returns raw (unmapped) state for debugging.
        """
        pygame.event.pump()
        axes = [self._axis_raw(i) for i in range(self.js.get_numaxes())]
        buttons = [self._button_raw(i) for i in range(self.js.get_numbuttons())]
        hats = [self._hat_raw(i) for i in range(self.js.get_numhats())]
        return {
            "ts": time.time(),
            "index": self.index,
            "name": self.name,
            "guid": self.guid,
            "instance_id": self.instance_id,
            "axes": axes,
            "buttons": buttons,
            "hats": hats,
        }

    def read_once(self) -> ControllerSnapshot:
        """
        Read mapped snapshot according to your schema.
        This is SAFE even if axes/buttons are missing (it will return zeros/False).
        """
        pygame.event.pump()

        # Axes mapping (your existing schema)
        lx = self._dz(self._axis_raw(0))
        ly_raw = self._axis_raw(1)
        ly = self._dz(-ly_raw if self.invert_ly else ly_raw)

        rx = self._dz(self._axis_raw(2))
        ry_raw = self._axis_raw(3)
        ry = self._dz(-ry_raw if self.invert_ry else ry_raw)

        lt = self._normalize_trigger(4)
        rt = self._normalize_trigger(5)

        dpad = self._hat_raw(0)

        a = bool(self._button_raw(0))
        b = bool(self._button_raw(1))
        x = bool(self._button_raw(2))
        y = bool(self._button_raw(3))
        lb = bool(self._button_raw(4))
        rb = bool(self._button_raw(5))
        win = bool(self._button_raw(6))
        menu = bool(self._button_raw(7))
        lstick = bool(self._button_raw(8))
        rstick = bool(self._button_raw(9))

        return ControllerSnapshot(
            lx=lx,
            ly=ly,
            rx=rx,
            ry=ry,
            lt=lt,
            rt=rt,
            dpad=dpad,
            a=a,
            b=b,
            x=x,
            y=y,
            lb=lb,
            rb=rb,
            win=win,
            menu=menu,
            lstick=lstick,
            rstick=rstick,
        )
