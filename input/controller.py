# input/controller.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple, List, Optional, Dict, Any
import time

try:
    import pygame  # type: ignore
except Exception:  # pragma: no cover
    pygame = None  # type: ignore


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
    """Initialize pygame joystick subsystem.

    Raises a clear error if pygame isn't installed so the GUI can still run.
    """
    if pygame is None:
        raise RuntimeError("pygame is not installed; controller support unavailable")
    # These are idempotent in pygame
    pygame.init()
    pygame.joystick.init()


def refresh_joysticks() -> None:
    """Force SDL/pygame to rescan joystick devices.

    This is mainly to support *hotplug* when the pilot app starts with no
    controller connected and the controller is plugged in later.

    We only call this when no controller object is active (during open/reopen).
    """
    if pygame is None:
        return
    try:
        ensure_pygame_joystick()
    except Exception:
        return
    try:
        # Pump events so SDL processes device-add/remove notifications.
        pygame.event.pump()
    except Exception:
        pass
    try:
        # Re-init joystick subsystem to trigger a device rescan.
        pygame.joystick.quit()
        pygame.joystick.init()
    except Exception:
        pass


def list_controllers() -> List[Dict[str, Any]]:
    """
    Returns a list of dicts describing currently detected controllers.
    Safe to call even if no controllers exist.
    """
    if pygame is None:
        return []
    ensure_pygame_joystick()
    try:
        pygame.event.pump()
    except Exception:
        pass
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
        axis_map: Optional[List[int]] = None,
        hat_index: int = 0,
        menu_buttons: Optional[List[int]] = None,
        win_buttons: Optional[List[int]] = None,
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
        # Axis map can be forced from config/env, or auto-detected.
        #
        # Two common Xbox layouts under SDL/pygame:
        #   A) 0=lx, 1=ly, 2=rx, 3=ry, 4=lt, 5=rt  -> schema map [0,1,2,3,4,5]
        #   B) 0=lx, 1=ly, 2=lt, 3=rx, 4=ry, 5=rt  -> schema map [0,1,3,4,2,5]
        #
        # Auto-detect is best-effort using the controller's "rest" axis values
        # (triggers often rest near -1.0 or +1.0, while sticks rest near 0.0).
        self._axis_map_auto = axis_map is None
        self.axis_map = list(axis_map) if axis_map is not None else [0, 1, 2, 3, 4, 5]
        # Ensure we always have 6 entries (lx,ly,rx,ry,lt,rt)
        if len(self.axis_map) < 6:
            self.axis_map = (self.axis_map + [0, 1, 2, 3, 4, 5])[:6]
        else:
            self.axis_map = self.axis_map[:6]

        self.hat_index = int(hat_index)

        # Optional button overrides (indices). If provided, these take precedence
        # over the built-in heuristics later.
        self._menu_buttons_override = list(menu_buttons) if menu_buttons else []
        self._win_buttons_override = list(win_buttons) if win_buttons else []
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

        # If axis_map was not forced, choose a best-effort mapping based on the
        # rest values we just sampled.
        if self._axis_map_auto:
            inferred = self._infer_axis_map(self._rest_axes)
            if inferred is not None:
                self.axis_map = inferred
                # keep 6 entries
                self.axis_map = (self.axis_map + [0, 1, 2, 3, 4, 5])[:6]
                if self.debug:
                    print(f"[controller] auto axis_map -> {self.axis_map} (rest={['%+.2f'%v for v in self._rest_axes]})")
            elif self.debug:
                print(f"[controller] auto axis_map: could not infer, using default {self.axis_map} (rest={['%+.2f'%v for v in self._rest_axes]})")

        if self.debug:
            self.print_device_summary(prefix="[controller] ")

    @staticmethod
    def _infer_axis_map(rest_axes: List[float]) -> Optional[List[int]]:
        """Infer the axis_map (lx,ly,rx,ry,lt,rt) -> pygame indices.

        We try to distinguish between the two common layouts:
          A) [0,1,2,3,4,5]  where axes 4/5 are triggers
          B) [0,1,3,4,2,5]  where axes 2/5 are triggers

        Heuristic:
          - Trigger axes often rest far from 0 (e.g. -1.0 or +1.0)
          - Stick axes rest near 0.0

        If we can't tell, we fall back to layout A.
        """
        if not rest_axes:
            return None

        # Consider only the first 6 axes; extra axes (if any) are ignored.
        ra = [float(v) for v in rest_axes[:6]]
        if len(ra) < 6:
            # Not enough axes to safely infer.
            return None

        # Axes whose rest value is far from 0 are likely triggers.
        far = {i for i, v in enumerate(ra) if abs(v) > 0.35}

        # If axis 2 rests far from 0 but axis 4 does not, it's probably layout B.
        if (2 in far) and (4 not in far):
            return [0, 1, 3, 4, 2, 5]

        # If axis 4 rests far from 0 but axis 2 does not, it's probably layout A.
        if (4 in far) and (2 not in far):
            return [0, 1, 2, 3, 4, 5]

        # Ambiguous: pick the more common "A" layout (matches many Xbox One mappings).
        return [0, 1, 2, 3, 4, 5]

    def print_device_summary(self, prefix: str = "") -> None:
        print(
            f"{prefix}opened index={self.index} name='{self.name}' guid='{self.guid}' "
            f"instance_id={self.instance_id} axes={self.js.get_numaxes()} "
            f"buttons={self.js.get_numbuttons()} hats={self.js.get_numhats()}"
        )

    def close(self) -> None:
        """Release the joystick handle (best-effort)."""
        try:
            if hasattr(self, "js") and self.js is not None:
                try:
                    self.js.quit()
                except Exception:
                    pass
        finally:
            pass

    def is_attached(self) -> bool:
        """Best-effort attachment check for hotplug recovery.

        pygame/SDL can sometimes leave a Joystick object readable even after a
        disconnect/reconnect cycle. We consult several indicators so the pilot
        service can proactively reopen the controller instead of requiring an app
        restart.
        """
        if pygame is None:
            return False

        try:
            pygame.event.pump()
        except Exception:
            pass

        # Newer pygame exposes explicit attachment status.
        fn_attached = getattr(self.js, "get_attached", None)
        if callable(fn_attached):
            try:
                if not bool(fn_attached()):
                    return False
            except Exception:
                return False

        # Generic init status check.
        fn_init = getattr(self.js, "get_init", None)
        if callable(fn_init):
            try:
                if not bool(fn_init()):
                    return False
            except Exception:
                return False

        # If we know the SDL instance_id, verify it still exists in the current
        # joystick list. This catches stale handles after unplug/replug.
        iid = getattr(self, "instance_id", None)
        try:
            count = int(pygame.joystick.get_count())
        except Exception:
            count = 0

        if iid is not None:
            found = False
            for i in range(max(0, count)):
                try:
                    jsi = pygame.joystick.Joystick(i)
                    jsi.init()
                    get_iid = getattr(jsi, "get_instance_id", None)
                    cur_iid = get_iid() if callable(get_iid) else None
                    if cur_iid == iid:
                        found = True
                        break
                except Exception:
                    continue
            if not found:
                return False

        return True

    def healthcheck(self) -> None:
        """Raise if the controller appears detached/stale."""
        if not self.is_attached():
            raise RuntimeError("Controller detached/stale (SDL reports device not attached)")

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

        # Some drivers use [-1..1] with rest near +1.0 (pressed moves toward -1.0).
        if rest > 0.5:
            return self._clamp01((1.0 - raw) * 0.5)

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

        # Axes mapping (schema: lx,ly,rx,ry,lt,rt)
        ax_lx, ax_ly, ax_rx, ax_ry, ax_lt, ax_rt = self.axis_map

        lx = self._dz(self._axis_raw(ax_lx))
        ly_raw = self._axis_raw(ax_ly)
        ly = self._dz(-ly_raw if self.invert_ly else ly_raw)

        rx = self._dz(self._axis_raw(ax_rx))
        ry_raw = self._axis_raw(ax_ry)
        ry = self._dz(-ry_raw if self.invert_ry else ry_raw)

        lt = self._normalize_trigger(ax_lt)
        rt = self._normalize_trigger(ax_rt)

        dpad = self._hat_raw(self.hat_index)

        # Button indices vary slightly across drivers (SDL/evdev) and OS
        # versions. For the Xbox One S controller on Linux, Start is commonly
        # 7 and Back is 6, but we've seen other layouts.
        def _b(*idxs: int) -> bool:
            for i in idxs:
                if bool(self._button_raw(i)):
                    return True
            return False

        a = _b(0)
        b = _b(1)
        x = _b(2)
        y = _b(3)
        lb = _b(4)
        rb = _b(5)

        is_xbox = "xbox" in (self.name or "").lower()

        # "menu" = Start; "win" = Back / Guide.
        #
        # IMPORTANT: We must avoid overlapping indices between menu/win and the
        # stick-click buttons (L3/R3). Earlier defaults accidentally included
        # L3/R3 indices inside win/menu for some SDL mappings, which caused
        # lights toggles (L3) to also arm/disarm/kill.
        #
        # Button indices vary by controller model and OS/driver, so we keep
        # conservative defaults and allow overrides via config/env.
        if is_xbox:
            # Common Xbox (SDL) layout:
            #  6=Back/View, 7=Start/Menu, 8=L3, 9=R3, 10=Guide
            lstick_idxs = [8]
            rstick_idxs = [9]
            lstick = _b(*lstick_idxs)
            rstick = _b(*rstick_idxs)
            default_menu = [7]
            default_win = [6, 10]
        else:
            # Generic controller defaults (best-effort)
            lstick_idxs = [8, 10]
            rstick_idxs = [9, 11]
            lstick = _b(*lstick_idxs)
            rstick = _b(*rstick_idxs)
            default_menu = [7, 11]
            default_win = [6, 10]

        # Final safeguard: remove any overlaps with the stick-click indices.
        # (If a driver maps a stick click onto 10/11, we'd rather lose a default
        # win/menu binding than have safety state coupled to lights.)
        _l3 = set(lstick_idxs)
        _r3 = set(rstick_idxs)
        default_menu = [i for i in default_menu if i not in _l3 and i not in _r3]
        default_win = [i for i in default_win if i not in _l3 and i not in _r3]

        menu_idxs = self._menu_buttons_override or default_menu
        win_idxs = self._win_buttons_override or default_win
        menu = _b(*menu_idxs)
        win = _b(*win_idxs)

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

if __name__ == "__main__":
    contrs = list_controllers()
    print(contrs)