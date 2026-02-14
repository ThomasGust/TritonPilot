# input/pilot_service.py
from __future__ import annotations

import json
import time
import threading
import traceback
from typing import Optional, Callable

import zmq

from schema.pilot_common import PilotFrame, PilotAxes, PilotButtons
from input.controller import GamepadSource, ControllerSnapshot, list_controllers, refresh_joysticks


class PilotPublisherService:
    """
    Background service:
      - opens controller in the SAME thread that reads it (important for pygame reliability)
      - pulls controller snapshots
      - builds PilotFrame
      - PUB to ROV

    Debug features:
      - prints detected controllers at start
      - prints controller identity + axis/button/hat counts
      - optional raw dumps (axes/buttons/hats)
      - exception handling with traceback + auto-retry open
    """

    def __init__(
        self,
        endpoint: str,
        rate_hz: float = 30.0,
        deadzone: float | None = None,
        debug: bool = False,
        index: int = 0,
        axis_map: list[int] | None = None,
        hat_index: int | None = None,
        menu_buttons: list[int] | None = None,
        win_buttons: list[int] | None = None,
        dump_raw_every_s: float = 0.0,  # 0 = off
        reopen_on_error_s: float = 1.0,
        on_send: Optional[Callable[[dict], None]] = None,
        on_status: Optional[Callable[[dict], None]] = None,
    ):
        self.endpoint = endpoint
        self.period = 1.0 / float(rate_hz)
        # Default deadzone comes from config/env, but can be overridden here.
        if deadzone is None:
            from config import CONTROLLER_DEADZONE
            deadzone = CONTROLLER_DEADZONE
        self.deadzone = float(deadzone)
        self.debug = bool(debug)
        self.on_send = on_send
        self.on_status = on_status
        self._last_status: Optional[dict] = None
        self.index = int(index)

        # Optional mapping overrides (useful for CLI debugging). When None, we
        # pull values from config/env in _open_controller().
        self._axis_map_override = list(axis_map) if axis_map is not None else None
        self._hat_index_override = int(hat_index) if hat_index is not None else None
        self._menu_buttons_override = list(menu_buttons) if menu_buttons is not None else None
        self._win_buttons_override = list(win_buttons) if win_buttons is not None else None

        self.dump_raw_every_s = float(dump_raw_every_s)
        self.reopen_on_error_s = float(reopen_on_error_s)

        # ZMQ sockets are NOT thread-safe; create/connect in the thread that uses them.
        self.sock = None

        # Slow joiner fix
        time.sleep(1.0)

        self.seq = 0
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

        self._last_debug = 0.0
        self._last_raw_dump = 0.0

        # Controller is created inside the run loop thread
        self._controller: Optional[GamepadSource] = None

    def start(self, threaded: bool = True):
        if threaded:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._run_loop, daemon=True)
            self._thread.start()
        else:
            # Foreground mode (useful for debugging)
            self._stop.clear()
            self._run_loop()

    def stop(self):
        self._stop.set()
        self._emit_status({'controller': 'stopped', 'index': self.index})
        if self._thread:
            self._thread.join(timeout=1.0)        # Close PUB socket if it was created
        if self.sock is not None:
            try:
                self.sock.close(0)
            finally:
                self.sock = None


    def _emit_status(self, status: dict):
        """Emit status updates (controller connected/disconnected/etc.)."""
        try:
            if self._last_status == status:
                return
            self._last_status = dict(status)
            if self.on_status:
                self.on_status(status)
        except Exception:
            pass

    def _open_controller(self) -> GamepadSource:
        # Support hotplug: if the app started with no controller connected,
        # force a rescan each time we attempt to open.
        try:
            refresh_joysticks()
        except Exception:
            pass

        # Print devices each time we try to open
        if self.debug:
            devices = list_controllers()
            print("[pilot] detected controllers:")
            for d in devices:
                print(
                    f"   index={d['index']} name='{d['name']}' guid='{d['guid']}' "
                    f"axes={d['axes']} buttons={d['buttons']} hats={d['hats']}"
                )

        # Controller mapping overrides come from config/env so the GUI can be
        # fixed without code edits when SDL axis numbering differs.
        from config import (
            CONTROLLER_AXIS_MAP,
            CONTROLLER_HAT_INDEX,
            CONTROLLER_MENU_BUTTONS,
            CONTROLLER_WIN_BUTTONS,
        )

        axis_map = self._axis_map_override if self._axis_map_override is not None else CONTROLLER_AXIS_MAP
        hat_index = self._hat_index_override if self._hat_index_override is not None else CONTROLLER_HAT_INDEX
        menu_buttons = self._menu_buttons_override if self._menu_buttons_override is not None else CONTROLLER_MENU_BUTTONS
        win_buttons = self._win_buttons_override if self._win_buttons_override is not None else CONTROLLER_WIN_BUTTONS

        ctrl = GamepadSource(
            deadzone=self.deadzone,
            index=self.index,
            debug=self.debug,
            axis_map=axis_map,
            hat_index=hat_index,
            menu_buttons=menu_buttons,
            win_buttons=win_buttons,
        )
        return ctrl

    def _build_frame(self, t0: float, snap: ControllerSnapshot) -> PilotFrame:
        return PilotFrame(
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

    def _run_loop(self):
        # Create/connect PUB socket in this thread (ZMQ sockets are thread-affine)
        ctx = zmq.Context.instance()
        self.sock = ctx.socket(zmq.PUB)
        try:
            self.sock.setsockopt(zmq.LINGER, 0)
            self.sock.setsockopt(zmq.SNDHWM, 1)
        except Exception:
            pass
        self.sock.connect(self.endpoint)

        # Create controller *inside* this thread for pygame stability
        while not self._stop.is_set() and self._controller is None:
            try:
                self._controller = self._open_controller()
                self._emit_status({'controller': 'connected', 'index': self.index, 'name': getattr(self._controller, 'name', None)})
            except Exception as e:
                self._emit_status({'controller': 'disconnected', 'index': self.index, 'error': str(e)})
                if self.debug:
                    print(f"[pilot] ERROR opening controller index={self.index}: {e}")
                if self.debug:
                    traceback.print_exc()
                time.sleep(max(0.1, self.reopen_on_error_s))
                continue

        while not self._stop.is_set():
            t0 = time.time()
            try:
                assert self._controller is not None

                snap: ControllerSnapshot = self._controller.read_once()
                frame = self._build_frame(t0, snap)
                self.seq += 1

                frame_dict = frame.to_dict()
                self.sock.send_string(json.dumps(frame_dict))
                if self.on_send:
                    try:
                        self.on_send(frame_dict)
                    except Exception:
                        pass

                # periodic debug
                if self.debug and (t0 - self._last_debug) > 1.0:
                    print(
                        f"[pilot] sent seq={frame.seq} "
                        f"axes={frame.axes} dpad={frame.dpad} "
                        f"buttons(a,b,x,y,lb,rb,win,menu,ls,rs)="
                        f"({snap.a},{snap.b},{snap.x},{snap.y},{snap.lb},{snap.rb},{snap.win},{snap.menu},{snap.lstick},{snap.rstick})"
                    )
                    self._last_debug = t0

                # raw dump (very useful when “sticks/buttons dead”)
                if self.dump_raw_every_s > 0 and (t0 - self._last_raw_dump) > self.dump_raw_every_s:
                    raw = self._controller.read_raw_state()
                    axes = [f"{v:+.3f}" for v in raw["axes"]]
                    print(f"[pilot] RAW axes={axes} buttons={raw['buttons']} hats={raw['hats']}")
                    self._last_raw_dump = t0

            except Exception as e:
                print(f"[pilot] ERROR in publish loop: {e}")
                if self.debug:
                    traceback.print_exc()

                # Try to recover by reopening controller (hotplug / SDL weirdness)
                self._emit_status({'controller': 'disconnected', 'index': self.index, 'error': str(e)})
                self._controller = None
                time.sleep(max(0.1, self.reopen_on_error_s))
                while not self._stop.is_set() and self._controller is None:
                    try:
                        self._controller = self._open_controller()
                        self._emit_status({'controller': 'connected', 'index': self.index, 'name': getattr(self._controller, 'name', None)})
                    except Exception as e2:
                        self._emit_status({'controller': 'disconnected', 'index': self.index, 'error': str(e2)})
                        if self.debug:
                            print(f"[pilot] ERROR reopening controller: {e2}")
                        if self.debug:
                            traceback.print_exc()
                        time.sleep(max(0.1, self.reopen_on_error_s))

            # pacing
            elapsed = time.time() - t0
            sleep_for = self.period - elapsed
            if sleep_for > 0:
                time.sleep(sleep_for)
