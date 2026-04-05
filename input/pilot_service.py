# input/pilot_service.py
from __future__ import annotations

import json
import time
import threading
import traceback
from typing import Optional, Callable

from dataclasses import fields

import zmq

from network.zmq_hotplug import apply_hotplug_opts

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

        # --- modes / toggles ----------------------------------------------
        from config import (
            ATTITUDE_HOLD_DEFAULT,
            ATTITUDE_HOLD_TOGGLE_BUTTON,
            DEPTH_HOLD_TOGGLE_BUTTON,
            DEPTH_HOLD_DEFAULT,
            PILOT_MAX_GAIN_DEFAULT,
            PILOT_MAX_GAIN_MIN,
            PILOT_MAX_GAIN_MAX,
            PILOT_MAX_GAIN_STEP,
            REVERSE_MODE_DEFAULT,
            REVERSE_TOGGLE_BUTTON,
            T200_WRIST_GAIN_DEFAULT,
            T200_WRIST_GAIN_MIN,
            T200_WRIST_GAIN_MAX,
            T200_WRIST_GAIN_STEP,
        )

        self._depth_hold_toggle_button = str(DEPTH_HOLD_TOGGLE_BUTTON or "rstick").strip().lower()
        self._attitude_hold_toggle_button = str(ATTITUDE_HOLD_TOGGLE_BUTTON or "").strip().lower()
        self._reverse_toggle_button = str(REVERSE_TOGGLE_BUTTON or "").strip().lower()
        self._mode_lock = threading.Lock()

        # Pilot-adjustable gain cap (Y +5%, A -5%). This is transmitted in
        # PilotFrame.modes["max_gain"] and interpreted on the ROV side as a
        # multiplier of the configured POWER_SCALE baseline.
        self._max_gain_min = float(PILOT_MAX_GAIN_MIN)
        self._max_gain_max = float(PILOT_MAX_GAIN_MAX)
        if self._max_gain_max < self._max_gain_min:
            self._max_gain_min, self._max_gain_max = self._max_gain_max, self._max_gain_min
        self._max_gain_step = max(0.0, float(PILOT_MAX_GAIN_STEP))
        self._max_gain = max(self._max_gain_min, min(self._max_gain_max, float(PILOT_MAX_GAIN_DEFAULT)))

        self._t200_wrist_gain_min = float(T200_WRIST_GAIN_MIN)
        self._t200_wrist_gain_max = float(T200_WRIST_GAIN_MAX)
        if self._t200_wrist_gain_max < self._t200_wrist_gain_min:
            self._t200_wrist_gain_min, self._t200_wrist_gain_max = self._t200_wrist_gain_max, self._t200_wrist_gain_min
        self._t200_wrist_gain_step = max(0.0, float(T200_WRIST_GAIN_STEP))
        self._t200_wrist_gain = max(
            self._t200_wrist_gain_min,
            min(self._t200_wrist_gain_max, float(T200_WRIST_GAIN_DEFAULT)),
        )

        self._modes = {
            "depth_hold": bool(DEPTH_HOLD_DEFAULT),
            "attitude_hold": bool(ATTITUDE_HOLD_DEFAULT),
            "max_gain": float(self._max_gain),
            "reverse": bool(REVERSE_MODE_DEFAULT),
            "t200_wrist_gain": float(self._t200_wrist_gain),
        }
        self._prev_buttons: Optional[PilotButtons] = None

        # External/GUI-provided auxiliary controls (e.g. keyboard-controlled manipulator axes).
        self._aux_lock = threading.Lock()
        self._aux_axes: dict[str, float] = {}

        # Controller is created inside the run loop thread
        self._controller: Optional[GamepadSource] = None
        self._last_ctrl_health_check = 0.0
        self._ctrl_health_check_period_s = 0.5

    @staticmethod
    def _buttons_to_dict(b: PilotButtons) -> dict:
        return {f.name: bool(getattr(b, f.name, False)) for f in fields(PilotButtons)}

    @classmethod
    def _compute_edges(cls, prev: Optional[PilotButtons], cur: PilotButtons) -> dict:
        if prev is None:
            return {}
        p = cls._buttons_to_dict(prev)
        c = cls._buttons_to_dict(cur)
        edges = {}
        for k, cv in c.items():
            pv = bool(p.get(k, False))
            if (not pv) and cv:
                edges[k] = "down"
            elif pv and (not cv):
                edges[k] = "up"
        return edges

    def _adjust_max_gain(self, delta: float) -> bool:
        """Adjust pilot max gain cap. Returns True if the value changed."""
        try:
            step = float(delta)
        except Exception:
            step = 0.0
        if step == 0.0:
            return False
        with self._mode_lock:
            prev = float(self._max_gain)
            new_val = prev + step
            new_val = max(float(self._max_gain_min), min(float(self._max_gain_max), float(new_val)))
            # Snap to 1% granularity for stable UI text / wire representation.
            new_val = round(new_val, 2)
            changed = abs(new_val - prev) > 1e-9
            self._max_gain = float(new_val)
            self._modes["max_gain"] = float(self._max_gain)
        return changed

    def _adjust_t200_wrist_gain(self, delta: float) -> bool:
        """Adjust the pilot-side T200 wrist gain cap."""
        try:
            step = float(delta)
        except Exception:
            step = 0.0
        if step == 0.0:
            return False
        with self._mode_lock:
            prev = float(self._t200_wrist_gain)
            new_val = prev + step
            new_val = max(float(self._t200_wrist_gain_min), min(float(self._t200_wrist_gain_max), float(new_val)))
            new_val = round(new_val, 2)
            changed = abs(new_val - prev) > 1e-9
            self._t200_wrist_gain = float(new_val)
            self._modes["t200_wrist_gain"] = float(self._t200_wrist_gain)
        return changed

    def current_t200_wrist_gain(self) -> float:
        with self._mode_lock:
            return float(self._t200_wrist_gain)

    def t200_wrist_gain_step(self) -> float:
        return float(self._t200_wrist_gain_step)

    def adjust_t200_wrist_gain(self, delta: float) -> bool:
        changed = self._adjust_t200_wrist_gain(delta)
        if changed:
            self._emit_status(self._status_payload(controller="connected"))
        return changed


    @staticmethod
    def _clamp_unit(x: float) -> float:
        try:
            v = float(x)
        except Exception:
            v = 0.0
        if v < -1.0:
            return -1.0
        if v > 1.0:
            return 1.0
        return v

    def set_aux_axis(self, name: str, value: float) -> None:
        key = str(name or "").strip()
        if not key:
            return
        v = self._clamp_unit(value)
        with self._aux_lock:
            if abs(v) < 1e-9:
                self._aux_axes.pop(key, None)
            else:
                self._aux_axes[key] = v

    def clear_aux_axis(self, name: str) -> None:
        key = str(name or "").strip()
        if not key:
            return
        with self._aux_lock:
            self._aux_axes.pop(key, None)

    def get_aux_axes(self) -> dict[str, float]:
        with self._aux_lock:
            return dict(self._aux_axes)

    def current_modes(self) -> dict:
        with self._mode_lock:
            return dict(self._modes)

    def is_reverse_enabled(self) -> bool:
        return bool(self.current_modes().get("reverse", False))

    def set_reverse_enabled(self, enabled: bool) -> bool:
        enabled = bool(enabled)
        with self._mode_lock:
            prev = bool(self._modes.get("reverse", False))
            self._modes["reverse"] = enabled
        changed = prev != enabled
        if changed:
            self._emit_status(self._status_payload())
        return changed

    def toggle_reverse_enabled(self) -> bool:
        new_state = not self.is_reverse_enabled()
        self.set_reverse_enabled(new_state)
        return new_state

    def set_depth_hold_enabled(self, enabled: bool) -> bool:
        enabled = bool(enabled)
        with self._mode_lock:
            prev = bool(self._modes.get("depth_hold", False))
            self._modes["depth_hold"] = enabled
        changed = prev != enabled
        if changed:
            self._emit_status(self._status_payload(controller="connected"))
        return changed

    def toggle_depth_hold(self) -> bool:
        new_state = not bool(self.current_modes().get("depth_hold", False))
        self.set_depth_hold_enabled(new_state)
        return new_state

    def set_attitude_hold_enabled(self, enabled: bool) -> bool:
        enabled = bool(enabled)
        with self._mode_lock:
            prev = bool(self._modes.get("attitude_hold", False))
            self._modes["attitude_hold"] = enabled
        changed = prev != enabled
        if changed:
            self._emit_status(self._status_payload(controller="connected"))
        return changed

    def toggle_attitude_hold(self) -> bool:
        new_state = not bool(self.current_modes().get("attitude_hold", False))
        self.set_attitude_hold_enabled(new_state)
        return new_state

    def _status_payload(self, controller: str | None = None, error: str | None = None) -> dict:
        state = str(controller or (self._last_status or {}).get("controller") or ("connected" if self._controller is not None else "unknown"))
        payload = {
            "controller": state,
            "index": self.index,
        }
        if state == "connected":
            payload["name"] = getattr(self._controller, "name", None)
        if error:
            payload["error"] = error
        elif state != "connected":
            prev_err = (self._last_status or {}).get("error")
            if prev_err:
                payload["error"] = prev_err
        payload.update(self.current_modes())
        payload["max_gain"] = float(self._max_gain)
        return payload

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
            # Wait briefly for the publisher thread to exit
            self._thread.join(timeout=1.0)
        if self._controller is not None:
            try:
                self._controller.close()
            except Exception:
                pass
            self._controller = None
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
        frame.aux = self.get_aux_axes()
        if bool(self.current_modes().get("reverse", False)):
            # Rear camera view is rotated 180 degrees in the horizontal plane.
            # Flip surge, sway, and yaw so the operator's inputs stay aligned
            # with what is on screen.
            frame.axes.lx = -frame.axes.lx
            frame.axes.ly = -frame.axes.ly
            frame.axes.rx = -frame.axes.rx
        return frame

    def _run_loop(self):
        # Create/connect PUB socket in this thread (ZMQ sockets are thread-affine)
        ctx = zmq.Context.instance()
        self.sock = ctx.socket(zmq.PUB)
        # Hotplug-friendly: short keepalive + ZMQ heartbeats + fast reconnect.
        apply_hotplug_opts(
            self.sock,
            linger_ms=0,
            snd_hwm=1,
            reconnect_ivl_ms=250,
            reconnect_ivl_max_ms=2000,
            heartbeat_ivl_ms=1000,
            heartbeat_timeout_ms=3000,
            heartbeat_ttl_ms=6000,
            tcp_keepalive=True,
            tcp_keepalive_idle_s=10,
            tcp_keepalive_intvl_s=5,
            tcp_keepalive_cnt=3,
            tcp_nodelay=True,
            tos=0xB8,  # DSCP EF for control frames (best-effort)
            priority=6,
        )
        self.sock.connect(self.endpoint)

        # Create controller *inside* this thread for pygame stability
        while not self._stop.is_set() and self._controller is None:
            try:
                self._controller = self._open_controller()
                self._prev_buttons = None
                self._last_ctrl_health_check = 0.0
                self._emit_status(self._status_payload(controller="connected"))
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

                if (t0 - self._last_ctrl_health_check) >= float(self._ctrl_health_check_period_s):
                    self._last_ctrl_health_check = t0
                    self._controller.healthcheck()

                snap: ControllerSnapshot = self._controller.read_once()
                frame = self._build_frame(t0, snap)

                # Compute edges + handle local mode toggles.
                edges = self._compute_edges(self._prev_buttons, frame.buttons)
                if edges:
                    frame.edges = dict(edges)

                if edges.get(self._depth_hold_toggle_button) == "down":
                    self.toggle_depth_hold()

                if self._attitude_hold_toggle_button and edges.get(self._attitude_hold_toggle_button) == "down":
                    self.toggle_attitude_hold()

                if self._reverse_toggle_button and edges.get(self._reverse_toggle_button) == "down":
                    self.toggle_reverse_enabled()

                if edges.get("y") == "down":
                    if self._adjust_max_gain(+self._max_gain_step):
                        self._emit_status(self._status_payload(controller="connected"))
                if edges.get("a") == "down":
                    if self._adjust_max_gain(-self._max_gain_step):
                        self._emit_status(self._status_payload(controller="connected"))

                # Always include the latest local mode values on the wire.
                frame.modes = self.current_modes()
                self._prev_buttons = frame.buttons

                self.seq += 1

                frame_dict = frame.to_dict()
                try:
                    self.sock.send_string(json.dumps(frame_dict), flags=zmq.NOBLOCK)
                except zmq.Again:
                    # Keep control loop real-time: drop stale frame instead of blocking.
                    continue
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
                try:
                    if self._controller is not None:
                        self._controller.close()
                except Exception:
                    pass
                self._controller = None
                self._prev_buttons = None
                time.sleep(max(0.1, self.reopen_on_error_s))
                while not self._stop.is_set() and self._controller is None:
                    try:
                        self._controller = self._open_controller()
                        self._prev_buttons = None
                        self._last_ctrl_health_check = 0.0
                        self._emit_status(self._status_payload(controller="connected"))
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
