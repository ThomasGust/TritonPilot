"""Background publisher that turns controller state into PilotFrame messages.

``PilotPublisherService`` is the topside half of the live-control path. It
polls pygame in its own thread, tracks pilot-side toggles, merges GUI-provided
auxiliary axes/edges, and publishes JSON frames over ZeroMQ. It deliberately
does not mix thrusters or make hardware-safety decisions; TritonOS owns those.
"""

from __future__ import annotations

import json
import math
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

        self.seq = 0
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

        self._last_debug = 0.0
        self._last_raw_dump = 0.0

        # --- modes / toggles ----------------------------------------------
        from config import (
            ARM_AIM_MODIFIER_BUTTON,
            ARM_GAIN_DEFAULT,
            ARM_GAIN_MAX,
            ARM_GAIN_MIN,
            ARM_GAIN_STEP,
            ARM_INIT_PITCH,
            ARM_INIT_WRIST,
            ARM_PARK_PITCH,
            ARM_PARK_WRIST,
            ARM_RATE,
            ARM_STICK_DEADZONE,
            ARM_STICK_PITCH_AXIS,
            ARM_STICK_PITCH_INVERT,
            ARM_STICK_WRIST_AXIS,
            ARM_STICK_WRIST_INVERT,
            BACK_GRIPPER_GAIN_DEFAULT,
            BACK_GRIPPER_GAIN_MAX,
            BACK_GRIPPER_GAIN_MIN,
            BACK_GRIPPER_GAIN_STEP,
            CURRENT_BUDGET_DEFAULT,
            CURRENT_BUDGET_MAX_A_DEFAULT,
            CURRENT_BUDGET_MAX_A_MIN,
            CURRENT_BUDGET_MAX_A_MAX,
            DEPTH_HOLD_TOGGLE_BUTTON,
            DEPTH_HOLD_DEFAULT,
            LIGHTS_TOGGLE_BUTTON,
            LIGHTS_TOGGLE_EDGE,
            PILOT_MAX_GAIN_DEFAULT,
            PILOT_MAX_GAIN_MIN,
            PILOT_MAX_GAIN_MAX,
            PILOT_MAX_GAIN_STEP,
            REVERSE_MODE_DEFAULT,
            REVERSE_TOGGLE_BUTTON,
            ROLL_PITCH_LEVEL_DEFAULT,
            ROLL_PITCH_LEVEL_TOGGLE_BUTTON,
            YAW_HOLD_DEFAULT,
            YAW_HOLD_TOGGLE_BUTTON,
        )

        self._depth_hold_toggle_button = str(DEPTH_HOLD_TOGGLE_BUTTON or "rstick").strip().lower()
        self._roll_pitch_level_toggle_button = str(ROLL_PITCH_LEVEL_TOGGLE_BUTTON or "").strip().lower()
        self._yaw_hold_toggle_button = str(YAW_HOLD_TOGGLE_BUTTON or "").strip().lower()
        self._lights_toggle_button = str(LIGHTS_TOGGLE_BUTTON or "").strip().lower()
        self._lights_toggle_edge = str(LIGHTS_TOGGLE_EDGE or "lights").strip().lower() or "lights"
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

        self._back_gripper_gain_min = float(BACK_GRIPPER_GAIN_MIN)
        self._back_gripper_gain_max = float(BACK_GRIPPER_GAIN_MAX)
        if self._back_gripper_gain_max < self._back_gripper_gain_min:
            self._back_gripper_gain_min, self._back_gripper_gain_max = self._back_gripper_gain_max, self._back_gripper_gain_min
        self._back_gripper_gain_step = max(0.0, float(BACK_GRIPPER_GAIN_STEP))
        self._back_gripper_gain = max(
            self._back_gripper_gain_min,
            min(self._back_gripper_gain_max, float(BACK_GRIPPER_GAIN_DEFAULT)),
        )

        self._arm_gain_min = float(ARM_GAIN_MIN)
        self._arm_gain_max = float(ARM_GAIN_MAX)
        if self._arm_gain_max < self._arm_gain_min:
            self._arm_gain_min, self._arm_gain_max = self._arm_gain_max, self._arm_gain_min
        self._arm_gain_step = max(0.0, float(ARM_GAIN_STEP))
        self._arm_gain = max(
            self._arm_gain_min,
            min(self._arm_gain_max, float(ARM_GAIN_DEFAULT)),
        )

        self._current_budget_max_a_min = float(CURRENT_BUDGET_MAX_A_MIN)
        self._current_budget_max_a_max = float(CURRENT_BUDGET_MAX_A_MAX)
        if self._current_budget_max_a_max < self._current_budget_max_a_min:
            self._current_budget_max_a_min, self._current_budget_max_a_max = (
                self._current_budget_max_a_max,
                self._current_budget_max_a_min,
            )
        self._current_budget_max_a = max(
            self._current_budget_max_a_min,
            min(self._current_budget_max_a_max, float(CURRENT_BUDGET_MAX_A_DEFAULT)),
        )

        self._modes = {
            "depth_hold": bool(DEPTH_HOLD_DEFAULT),
            "max_gain": float(self._max_gain),
            "current_budget": bool(CURRENT_BUDGET_DEFAULT),
            "current_budget_max_a": float(self._current_budget_max_a),
            "reverse": bool(REVERSE_MODE_DEFAULT),
            "roll_pitch_level": bool(ROLL_PITCH_LEVEL_DEFAULT),
            "yaw_hold": bool(YAW_HOLD_DEFAULT),
            "station_keep": False,
            "autopilot": {
                "depth": bool(DEPTH_HOLD_DEFAULT),
                "roll": "level" if bool(ROLL_PITCH_LEVEL_DEFAULT) else "off",
                "pitch": "level" if bool(ROLL_PITCH_LEVEL_DEFAULT) else "off",
                "yaw": "hold" if bool(YAW_HOLD_DEFAULT) else "off",
                "station_keep": False,
                "targets": {},
            },
            "back_gripper_gain": float(self._back_gripper_gain),
            # Compatibility alias for older TritonOS builds and recordings.
            "t200_wrist_gain": float(self._back_gripper_gain),
            "arm_gain": float(self._arm_gain),
            # Live differential-arm tuning overrides (empty = ROV uses rov_config).
            "arm_tune": {},
        }
        self._prev_buttons: Optional[PilotButtons] = None

        # External/GUI-provided auxiliary controls.
        self._aux_lock = threading.Lock()
        self._aux_axes: dict[str, float] = {}
        self._edge_lock = threading.Lock()
        self._pending_edges: list[tuple[str, str]] = []

        # --- Differential arm (servo wrist) position integrator ---------------
        # Single source of truth for the arm pose. The right stick supplies a
        # proportional intent while ARM_AIM_MODIFIER_BUTTON is held. The publish
        # loop integrates that intent at ARM_RATE * arm_gain and publishes the
        # absolute pose as PilotFrame.aux gripper_pitch/gripper_yaw.
        self._arm_lock = threading.Lock()
        self._arm_pitch = self._clamp_unit(ARM_INIT_PITCH)
        self._arm_wrist = self._clamp_unit(ARM_INIT_WRIST)
        self._arm_park_pitch = self._clamp_unit(ARM_PARK_PITCH)
        self._arm_park_wrist = self._clamp_unit(ARM_PARK_WRIST)
        self._arm_kb_pitch_dir = 0.0
        self._arm_kb_wrist_dir = 0.0
        self._arm_last_t: Optional[float] = None
        self._arm_modifier_button = str(ARM_AIM_MODIFIER_BUTTON or "").strip().lower()
        self._arm_stick_pitch_axis = str(ARM_STICK_PITCH_AXIS or "ry").strip().lower()
        self._arm_stick_wrist_axis = str(ARM_STICK_WRIST_AXIS or "rx").strip().lower()
        self._arm_stick_deadzone = max(0.0, min(0.95, float(ARM_STICK_DEADZONE)))
        self._arm_stick_pitch_invert = float(ARM_STICK_PITCH_INVERT)
        self._arm_stick_wrist_invert = float(ARM_STICK_WRIST_INVERT)
        self._arm_rate = max(0.0, float(ARM_RATE))

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

    def current_max_gain(self) -> float:
        with self._mode_lock:
            return float(self._max_gain)

    def max_gain_step(self) -> float:
        return float(self._max_gain_step)

    def adjust_max_gain(self, delta: float) -> bool:
        changed = self._adjust_max_gain(delta)
        if changed:
            self._emit_status(self._status_payload(controller="connected"))
        return changed

    def _adjust_back_gripper_gain(self, delta: float) -> bool:
        """Adjust the pilot-side back rotating gripper gain cap."""
        try:
            step = float(delta)
        except Exception:
            step = 0.0
        if step == 0.0:
            return False
        with self._mode_lock:
            prev = float(self._back_gripper_gain)
            new_val = prev + step
            new_val = max(float(self._back_gripper_gain_min), min(float(self._back_gripper_gain_max), float(new_val)))
            new_val = round(new_val, 2)
            changed = abs(new_val - prev) > 1e-9
            self._back_gripper_gain = float(new_val)
            self._modes["back_gripper_gain"] = float(self._back_gripper_gain)
            self._modes["t200_wrist_gain"] = float(self._back_gripper_gain)
        return changed

    def current_back_gripper_gain(self) -> float:
        with self._mode_lock:
            return float(self._back_gripper_gain)

    def back_gripper_gain_step(self) -> float:
        return float(self._back_gripper_gain_step)

    def adjust_back_gripper_gain(self, delta: float) -> bool:
        changed = self._adjust_back_gripper_gain(delta)
        if changed:
            self._emit_status(self._status_payload(controller="connected"))
        return changed

    def current_t200_wrist_gain(self) -> float:
        return self.current_back_gripper_gain()

    def t200_wrist_gain_step(self) -> float:
        return self.back_gripper_gain_step()

    def adjust_t200_wrist_gain(self, delta: float) -> bool:
        changed = self._adjust_back_gripper_gain(delta)
        if changed:
            self._emit_status(self._status_payload(controller="connected"))
        return changed

    def _adjust_arm_gain(self, delta: float) -> bool:
        """Adjust the pilot-side arm/gripper-head gain cap."""
        try:
            step = float(delta)
        except Exception:
            step = 0.0
        if step == 0.0:
            return False
        with self._mode_lock:
            prev = float(self._arm_gain)
            new_val = prev + step
            new_val = max(float(self._arm_gain_min), min(float(self._arm_gain_max), float(new_val)))
            new_val = round(new_val, 2)
            changed = abs(new_val - prev) > 1e-9
            self._arm_gain = float(new_val)
            self._modes["arm_gain"] = float(self._arm_gain)
        return changed

    def current_arm_gain(self) -> float:
        with self._mode_lock:
            return float(self._arm_gain)

    def arm_gain_step(self) -> float:
        return float(self._arm_gain_step)

    def adjust_arm_gain(self, delta: float) -> bool:
        changed = self._adjust_arm_gain(delta)
        if changed:
            self._emit_status(self._status_payload(controller="connected"))
        return changed

    # --- live differential-arm tuning -------------------------------------
    def set_arm_tune(self, key: str, value) -> None:
        """Set one live arm-tuning override (streamed in modes["arm_tune"]).

        The ROV applies these per-frame on top of its rov_config defaults, so
        inverts / neutral / range can be dialed in without a TritonOS restart.
        Pass ``value=None`` to clear that key (fall back to rov_config).
        """
        valid = {
            "left_invert", "right_invert", "pitch_invert", "yaw_invert",
            "pitch_neutral_deg", "wrist_neutral_deg", "servo_range_deg",
            "pitch_span_deg", "wrist_span_deg",
        }
        k = str(key or "").strip()
        if k not in valid:
            return
        with self._mode_lock:
            tune = dict(self._modes.get("arm_tune") or {})
            if value is None:
                tune.pop(k, None)
            else:
                try:
                    tune[k] = float(value)
                except Exception:
                    return
            self._modes["arm_tune"] = tune

    def clear_arm_tune(self) -> None:
        """Drop all arm-tuning overrides so the ROV uses its rov_config values."""
        with self._mode_lock:
            self._modes["arm_tune"] = {}

    def current_arm_tune(self) -> dict:
        with self._mode_lock:
            return dict(self._modes.get("arm_tune") or {})


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

    # --- differential arm position integrator -----------------------------
    def set_arm_keyboard_intent(self, pitch_dir: float, wrist_dir: float) -> None:
        """Compatibility no-op: WASD no longer drives the differential arm."""
        with self._arm_lock:
            self._arm_kb_pitch_dir = 0.0
            self._arm_kb_wrist_dir = 0.0

    def clear_arm_keyboard_intent(self) -> None:
        """Drop any legacy keyboard arm intent (e.g. on focus loss)."""
        with self._arm_lock:
            self._arm_kb_pitch_dir = 0.0
            self._arm_kb_wrist_dir = 0.0

    def arm_position(self) -> tuple[float, float]:
        """Return the current integrated (pitch, wrist) arm position in [-1, 1]."""
        with self._arm_lock:
            return float(self._arm_pitch), float(self._arm_wrist)

    def arm_park_position(self) -> tuple[float, float]:
        """Return the current pilot-side park target in command space."""
        with self._arm_lock:
            return float(self._arm_park_pitch), float(self._arm_park_wrist)

    def set_arm_park_position(self, pitch: float, wrist: float) -> tuple[float, float]:
        """Set the pilot-side park target in command space."""
        with self._arm_lock:
            self._arm_park_pitch = self._clamp_unit(pitch)
            self._arm_park_wrist = self._clamp_unit(wrist)
            return float(self._arm_park_pitch), float(self._arm_park_wrist)

    def park_arm(self) -> tuple[float, float]:
        """Command the differential arm to the configured park target."""
        with self._arm_lock:
            pitch = float(self._arm_park_pitch)
            wrist = float(self._arm_park_wrist)
        return self.set_arm_position(pitch, wrist)

    def set_arm_position(self, pitch: float, wrist: float) -> tuple[float, float]:
        """Set the absolute differential-arm target in [-1, 1].

        Used by setup/alignment actions that need to command a known pose directly
        instead of walking there through controller stick intent.
        """
        with self._arm_lock:
            self._arm_pitch = self._clamp_unit(pitch)
            self._arm_wrist = self._clamp_unit(wrist)
            self._arm_kb_pitch_dir = 0.0
            self._arm_kb_wrist_dir = 0.0
            return float(self._arm_pitch), float(self._arm_wrist)

    @staticmethod
    def _stick_axis(value: float, deadzone: float) -> float:
        """Deadzone + rescale a stick axis into a proportional [-1, 1] intent."""
        x = max(-1.0, min(1.0, float(value)))
        dz = max(0.0, min(0.95, float(deadzone)))
        if abs(x) <= dz:
            return 0.0
        span = max(1e-6, 1.0 - dz)
        sign = 1.0 if x > 0 else -1.0
        return sign * min(1.0, (abs(x) - dz) / span)

    def _integrate_arm(self, snap: ControllerSnapshot, modifier_held: bool, dt: float) -> tuple[float, float]:
        """Advance the arm position from modifier-gated stick intent."""
        sp = sw = 0.0
        if modifier_held:
            try:
                pv = float(getattr(snap, self._arm_stick_pitch_axis, 0.0) or 0.0)
            except Exception:
                pv = 0.0
            try:
                wv = float(getattr(snap, self._arm_stick_wrist_axis, 0.0) or 0.0)
            except Exception:
                wv = 0.0
            sp = self._stick_axis(pv, self._arm_stick_deadzone) * self._arm_stick_pitch_invert
            sw = self._stick_axis(wv, self._arm_stick_deadzone) * self._arm_stick_wrist_invert

        pitch_intent = sp
        wrist_intent = sw

        try:
            gain = float(self.current_arm_gain())
        except Exception:
            gain = 1.0
        gain = max(0.0, min(1.0, gain))
        step = float(self._arm_rate) * gain * max(0.0, min(0.1, float(dt)))

        with self._arm_lock:
            self._arm_pitch = self._clamp_unit(self._arm_pitch + pitch_intent * step)
            self._arm_wrist = self._clamp_unit(self._arm_wrist + wrist_intent * step)
            return float(self._arm_pitch), float(self._arm_wrist)

    def queue_edge(self, name: str, state: str = "down") -> None:
        key = str(name or "").strip().lower()
        edge_state = str(state or "").strip().lower()
        if not key or not edge_state:
            return
        with self._edge_lock:
            self._pending_edges.append((key, edge_state))

    def _drain_pending_edges(self) -> dict[str, str]:
        with self._edge_lock:
            items = list(self._pending_edges)
            self._pending_edges.clear()
        edges: dict[str, str] = {}
        for key, edge_state in items:
            if key and edge_state:
                edges[str(key)] = str(edge_state)
        return edges

    @staticmethod
    def _wrap_deg(deg: float) -> float:
        return ((float(deg) + 180.0) % 360.0) - 180.0

    @staticmethod
    def _finite_float(value) -> float | None:
        try:
            v = float(value)
        except Exception:
            return None
        if not math.isfinite(v):
            return None
        return v

    @staticmethod
    def _copy_modes_payload(modes: dict) -> dict:
        out = dict(modes or {})
        tune = out.get("arm_tune")
        if isinstance(tune, dict):
            out["arm_tune"] = dict(tune)
        ap = out.get("autopilot")
        if isinstance(ap, dict):
            ap_copy = dict(ap)
            targets = ap_copy.get("targets")
            ap_copy["targets"] = dict(targets) if isinstance(targets, dict) else {}
            visual = ap_copy.get("visual")
            if isinstance(visual, dict):
                visual_copy = dict(visual)
                command = visual_copy.get("command")
                if isinstance(command, dict):
                    visual_copy["command"] = dict(command)
                ap_copy["visual"] = visual_copy
            out["autopilot"] = ap_copy
        return out

    def current_modes(self) -> dict:
        with self._mode_lock:
            return self._copy_modes_payload(self._modes)

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

    def is_current_budget_enabled(self) -> bool:
        return bool(self.current_modes().get("current_budget", False))

    def set_current_budget_enabled(self, enabled: bool) -> bool:
        """Enable/disable the ROV's intelligent current limiter live."""
        enabled = bool(enabled)
        with self._mode_lock:
            prev = bool(self._modes.get("current_budget", False))
            self._modes["current_budget"] = enabled
        changed = prev != enabled
        if changed:
            self._emit_status(self._status_payload())
        return changed

    def toggle_current_budget_enabled(self) -> bool:
        new_state = not self.is_current_budget_enabled()
        self.set_current_budget_enabled(new_state)
        return new_state

    def current_budget_max_a(self) -> float:
        return float(self.current_modes().get("current_budget_max_a", self._current_budget_max_a))

    def current_budget_max_a_bounds(self) -> tuple:
        return (float(self._current_budget_max_a_min), float(self._current_budget_max_a_max))

    def set_current_budget_max_a(self, amps: float) -> bool:
        """Set the live total-thruster-current cap (amps) sent to the ROV."""
        try:
            value = float(amps)
        except Exception:
            return False
        value = max(self._current_budget_max_a_min, min(self._current_budget_max_a_max, value))
        with self._mode_lock:
            prev = float(self._modes.get("current_budget_max_a", self._current_budget_max_a))
            self._current_budget_max_a = value
            self._modes["current_budget_max_a"] = value
        changed = abs(prev - value) > 1e-9
        if changed:
            self._emit_status(self._status_payload())
        return changed

    def _autopilot_modes_locked(self) -> dict:
        current = self._modes.get("autopilot")
        if isinstance(current, dict):
            ap = dict(current)
        else:
            ap = {}
        targets = ap.get("targets")
        ap["targets"] = dict(targets) if isinstance(targets, dict) else {}
        ap.setdefault("depth", bool(self._modes.get("depth_hold", False)))
        ap.setdefault("roll", "off")
        ap.setdefault("pitch", "off")
        ap.setdefault("yaw", "off")
        ap.setdefault("station_keep", bool(self._modes.get("station_keep", False)))
        return ap

    def set_autopilot_axis_mode(self, axis: str, mode: str) -> bool:
        axis_key = str(axis or "").strip().lower()
        if axis_key not in {"roll", "pitch", "yaw"}:
            return False
        mode_value = str(mode or "off").strip().lower() or "off"
        if mode_value not in {"hold", "level", "damp", "off"}:
            mode_value = "off"
        with self._mode_lock:
            ap = self._autopilot_modes_locked()
            targets = dict(ap.get("targets") or {})
            had_target = f"{axis_key}_deg" in targets
            prev = str(ap.get(axis_key, "off"))
            ap[axis_key] = mode_value
            if mode_value != "hold":
                targets.pop(f"{axis_key}_deg", None)
                ap["targets"] = targets
            if axis_key in {"roll", "pitch"}:
                ap["roll_pitch_level"] = ap.get("roll") == "level" and ap.get("pitch") == "level"
                self._modes["roll_pitch_level"] = bool(ap["roll_pitch_level"])
            elif axis_key == "yaw":
                self._modes["yaw_hold"] = mode_value == "hold"
            self._modes["autopilot"] = ap
        changed = prev != mode_value or (mode_value != "hold" and had_target)
        if changed:
            self._emit_status(self._status_payload())
        return changed

    def set_autopilot_axis_target(self, axis: str, target_deg: float, *, mode: str = "hold") -> bool:
        axis_key = str(axis or "").strip().lower()
        if axis_key not in {"roll", "pitch", "yaw"}:
            return False
        target = self._finite_float(target_deg)
        if target is None:
            return False
        target = self._wrap_deg(target)
        mode_value = str(mode or "hold").strip().lower() or "hold"
        if mode_value not in {"hold", "level", "damp", "off"}:
            mode_value = "hold"
        if mode_value in {"level", "damp"}:
            # These modes do not use a manual angle setpoint.
            mode_value = "hold"

        with self._mode_lock:
            ap = self._autopilot_modes_locked()
            targets = dict(ap.get("targets") or {})
            key = f"{axis_key}_deg"
            prev_target = self._finite_float(targets.get(key))
            prev_mode = str(ap.get(axis_key, "off"))
            targets[key] = float(target)
            ap["targets"] = targets
            ap[axis_key] = mode_value
            if axis_key in {"roll", "pitch"}:
                ap["roll_pitch_level"] = ap.get("roll") == "level" and ap.get("pitch") == "level"
                self._modes["roll_pitch_level"] = bool(ap["roll_pitch_level"])
            else:
                self._modes["yaw_hold"] = mode_value == "hold"
            self._modes["autopilot"] = ap

        changed = prev_mode != mode_value or prev_target is None or abs(self._wrap_deg(prev_target - target)) > 1e-9
        if changed:
            self._emit_status(self._status_payload(controller="connected"))
        return changed

    def clear_autopilot_axis_target(self, axis: str) -> bool:
        axis_key = str(axis or "").strip().lower()
        if axis_key not in {"roll", "pitch", "yaw"}:
            return False
        with self._mode_lock:
            ap = self._autopilot_modes_locked()
            targets = dict(ap.get("targets") or {})
            key = f"{axis_key}_deg"
            changed = key in targets
            targets.pop(key, None)
            ap["targets"] = targets
            self._modes["autopilot"] = ap
        if changed:
            self._emit_status(self._status_payload(controller="connected"))
        return changed

    def is_roll_pitch_level_enabled(self) -> bool:
        return bool(self.current_modes().get("roll_pitch_level", False))

    def set_roll_pitch_level_enabled(self, enabled: bool) -> bool:
        enabled = bool(enabled)
        with self._mode_lock:
            ap = self._autopilot_modes_locked()
            targets = dict(ap.get("targets") or {})
            had_targets = "roll_deg" in targets or "pitch_deg" in targets
            targets.pop("roll_deg", None)
            targets.pop("pitch_deg", None)
            ap["targets"] = targets
            prev = bool(self._modes.get("roll_pitch_level", False))
            ap["roll"] = "level" if enabled else "off"
            ap["pitch"] = "level" if enabled else "off"
            ap["roll_pitch_level"] = enabled
            self._modes["roll_pitch_level"] = enabled
            self._modes["autopilot"] = ap
        changed = prev != enabled or had_targets
        if changed:
            self._emit_status(self._status_payload(controller="connected"))
        return changed

    def toggle_roll_pitch_level(self) -> bool:
        new_state = not self.is_roll_pitch_level_enabled()
        self.set_roll_pitch_level_enabled(new_state)
        return new_state

    def is_yaw_hold_enabled(self) -> bool:
        return bool(self.current_modes().get("yaw_hold", False))

    def set_yaw_hold_enabled(self, enabled: bool) -> bool:
        enabled = bool(enabled)
        with self._mode_lock:
            ap = self._autopilot_modes_locked()
            targets = dict(ap.get("targets") or {})
            had_target = "yaw_deg" in targets
            targets.pop("yaw_deg", None)
            ap["targets"] = targets
            prev = bool(self._modes.get("yaw_hold", False))
            ap["yaw"] = "hold" if enabled else "off"
            self._modes["yaw_hold"] = enabled
            self._modes["autopilot"] = ap
        changed = prev != enabled or had_target
        if changed:
            self._emit_status(self._status_payload(controller="connected"))
        return changed

    def toggle_yaw_hold(self) -> bool:
        new_state = not self.is_yaw_hold_enabled()
        self.set_yaw_hold_enabled(new_state)
        return new_state

    # --- visual station-keeping (optical-tracking autopilot) ------------------
    def is_station_keep_enabled(self) -> bool:
        return bool(self.current_modes().get("station_keep", False))

    def set_station_keep_enabled(self, enabled: bool) -> bool:
        """Engage/disengage the ROV visual station-keep controller.

        While engaged, the per-frame visual error/command set via
        :meth:`set_visual_target` is published in the pilot command; the ROV
        falls back to manual whenever there is no valid lock, so engaging it with
        no CV running is safe (the controller stays inert).
        """
        enabled = bool(enabled)
        with self._mode_lock:
            ap = self._autopilot_modes_locked()
            prev = bool(self._modes.get("station_keep", False))
            ap["station_keep"] = enabled
            if not enabled:
                ap.pop("visual", None)  # drop any stale lock when disengaging
            self._modes["station_keep"] = enabled
            self._modes["autopilot"] = ap
        changed = prev != enabled
        if changed:
            self._emit_status(self._status_payload(controller="connected"))
        return changed

    def toggle_station_keep(self) -> bool:
        new_state = not self.is_station_keep_enabled()
        self.set_station_keep_enabled(new_state)
        return new_state

    def set_visual_target(self, payload: Optional[dict]) -> None:
        """Inject the optical tracker's per-frame error/command into the modes.

        High-rate hook for the (future) CV model: it rides the normal pilot
        frame and only affects the ROV while station-keep is engaged. Pass the
        dict from ``VisualTargetError.to_visual_payload()`` /
        ``StationKeepCommand.to_autopilot_modes()["autopilot"]["visual"]``.
        ``None`` clears it (controller treats absence as no-lock).
        """
        with self._mode_lock:
            ap = self._autopilot_modes_locked()
            if payload is None:
                ap.pop("visual", None)
            else:
                ap["visual"] = dict(payload)
            self._modes["autopilot"] = ap

    def clear_visual_target(self) -> None:
        self.set_visual_target(None)

    def _handle_mode_edges(self, edges: dict[str, str]) -> None:
        if edges.get(self._depth_hold_toggle_button) == "down":
            self.toggle_depth_hold()

        if self._roll_pitch_level_toggle_button and edges.get(self._roll_pitch_level_toggle_button) == "down":
            self.toggle_roll_pitch_level()

        yaw_toggled = False
        if self._yaw_hold_toggle_button and edges.get(self._yaw_hold_toggle_button) == "down":
            self.toggle_yaw_hold()
            yaw_toggled = True

        lights_button_conflicts_with_yaw = (
            yaw_toggled
            and self._lights_toggle_button
            and self._lights_toggle_button == self._yaw_hold_toggle_button
        )
        if (
            self._lights_toggle_button
            and not lights_button_conflicts_with_yaw
            and edges.get(self._lights_toggle_button) == "down"
        ):
            edges[self._lights_toggle_edge] = "down"

        if self._reverse_toggle_button and edges.get(self._reverse_toggle_button) == "down":
            self.toggle_reverse_enabled()

        if edges.get("y") == "down":
            if self._adjust_max_gain(+self._max_gain_step):
                self._emit_status(self._status_payload(controller="connected"))
        if edges.get("a") == "down":
            if self._adjust_max_gain(-self._max_gain_step):
                self._emit_status(self._status_payload(controller="connected"))

    def set_depth_hold_enabled(self, enabled: bool) -> bool:
        enabled = bool(enabled)
        with self._mode_lock:
            prev = bool(self._modes.get("depth_hold", False))
            self._modes["depth_hold"] = enabled
            ap = self._autopilot_modes_locked()
            targets = dict(ap.get("targets") or {})
            had_target = "depth_m" in targets
            targets.pop("depth_m", None)
            ap["targets"] = targets
            ap["depth"] = enabled
            self._modes["autopilot"] = ap
        changed = prev != enabled or had_target
        if changed:
            self._emit_status(self._status_payload(controller="connected"))
        return changed

    def toggle_depth_hold(self) -> bool:
        new_state = not bool(self.current_modes().get("depth_hold", False))
        self.set_depth_hold_enabled(new_state)
        return new_state

    def set_depth_hold_target(self, target_m: float, *, enable: bool = True) -> bool:
        target = self._finite_float(target_m)
        if target is None:
            return False
        with self._mode_lock:
            ap = self._autopilot_modes_locked()
            targets = dict(ap.get("targets") or {})
            prev_target = self._finite_float(targets.get("depth_m"))
            prev_enabled = bool(self._modes.get("depth_hold", False))
            targets["depth_m"] = float(target)
            ap["targets"] = targets
            if enable:
                self._modes["depth_hold"] = True
                ap["depth"] = True
            self._modes["autopilot"] = ap
        changed = prev_target is None or abs(prev_target - target) > 1e-9 or (bool(enable) and not prev_enabled)
        if changed:
            self._emit_status(self._status_payload(controller="connected"))
        return changed

    def clear_depth_hold_target(self) -> bool:
        with self._mode_lock:
            ap = self._autopilot_modes_locked()
            targets = dict(ap.get("targets") or {})
            changed = "depth_m" in targets
            targets.pop("depth_m", None)
            ap["targets"] = targets
            self._modes["autopilot"] = ap
        if changed:
            self._emit_status(self._status_payload(controller="connected"))
        return changed

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

    @staticmethod
    def _apply_reverse_axes(frame: PilotFrame) -> None:
        # Rear camera view is rotated 180 degrees in the horizontal plane.
        # Flip surge and sway so translation follows the rear view. Yaw remains
        # in the vehicle's normal left/right direction.
        frame.axes.lx = -frame.axes.lx
        frame.axes.ly = -frame.axes.ly

    def _build_frame(self, t0: float, snap: ControllerSnapshot, *, apply_reverse: bool = True) -> PilotFrame:
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
        frame.edges = self._drain_pending_edges()
        if apply_reverse and bool(self.current_modes().get("reverse", False)):
            self._apply_reverse_axes(frame)
        return frame

    def _publish_neutral_frame(self, t: float) -> None:
        """Publish one zeroed pilot frame (neutral sticks, no button edges, current
        modes, last arm pose) to keep the ROV's pilot link fresh while the controller
        is unavailable.

        Without this, a sleeping/repluging gamepad starves the ROV of frames during
        the controller-reopen wait; the ROV's stale-frame failsafe then auto-disarms
        mid-hold (recording 20260624-220944: a 3.6 s frame gap -> pilot_age 3.0 s >
        failsafe_disarm_s 2.0 s -> disarm while station-keep was still engaged).

        Safe by construction: if the topside *app* dies or the tether drops, these
        frames stop too, so the failsafe still protects against a real topside loss --
        this only suppresses the benign gamepad-asleep case, during which the autopilot
        is what actually holds the vehicle. The pilot can always disarm explicitly."""
        sock = getattr(self, "sock", None)
        if sock is None:
            return
        with self._arm_lock:
            arm_pitch = float(self._arm_pitch)
            arm_wrist = float(self._arm_wrist)
        self.seq += 1
        frame_dict = {
            "type": "pilot",
            "schema": 1,
            "seq": self.seq,
            "ts": t,
            "axes": {"lx": 0.0, "ly": 0.0, "rx": 0.0, "ry": 0.0, "lt": 0.0, "rt": 0.0},
            "buttons": {k: False for k in (
                "a", "b", "x", "y", "lb", "rb", "win", "menu", "lstick", "rstick")},
            "dpad": [0, 0],
            "edges": {},
            "modes": self.current_modes(),
            "aux": {"gripper_pitch": arm_pitch, "gripper_yaw": arm_wrist},
        }
        if self.on_send:
            try:
                self.on_send(frame_dict)
            except Exception:
                pass
        try:
            sock.send_string(json.dumps(frame_dict, separators=(",", ":")), flags=zmq.NOBLOCK)
        except zmq.Again:
            pass
        except Exception:
            pass

    def _keepalive_wait(self, duration_s: float) -> None:
        """Wait ~duration_s while still emitting neutral keepalive frames at the normal
        publish rate, so a controller dropout/reopen doesn't trip the ROV failsafe.
        Replaces a plain sleep in the controller-(re)open paths."""
        end = time.time() + max(0.0, float(duration_s))
        while not self._stop.is_set() and time.time() < end:
            t = time.time()
            self._publish_neutral_frame(t)
            slp = self.period - (time.time() - t)
            if slp > 0:
                time.sleep(slp)

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
                self._keepalive_wait(max(0.1, self.reopen_on_error_s))
                continue

        while not self._stop.is_set():
            t0 = time.time()
            try:
                assert self._controller is not None

                if (t0 - self._last_ctrl_health_check) >= float(self._ctrl_health_check_period_s):
                    self._last_ctrl_health_check = t0
                    self._controller.healthcheck()

                snap: ControllerSnapshot = self._controller.read_once()
                frame = self._build_frame(t0, snap, apply_reverse=False)

                # Compute edges + handle local mode toggles.
                edges = dict(frame.edges or {})
                controller_edges = self._compute_edges(self._prev_buttons, frame.buttons)
                if controller_edges:
                    edges.update(controller_edges)

                self._handle_mode_edges(edges)
                frame.edges = dict(edges)

                # Always include the latest local mode values on the wire.
                frame.modes = self.current_modes()
                if bool(frame.modes.get("reverse", False)):
                    self._apply_reverse_axes(frame)
                self._prev_buttons = frame.buttons

                # Differential arm: integrate position from the modifier-gated
                # right stick, and publish the absolute pose.
                dt_arm = self.period if self._arm_last_t is None else (t0 - self._arm_last_t)
                self._arm_last_t = t0
                modifier_held = (
                    bool(getattr(snap, self._arm_modifier_button, False))
                    if self._arm_modifier_button
                    else False
                )
                arm_pitch, arm_wrist = self._integrate_arm(snap, modifier_held, dt_arm)
                frame.aux = {**(frame.aux or {}), "gripper_pitch": arm_pitch, "gripper_yaw": arm_wrist}
                if modifier_held:
                    # While aiming the arm, suppress yaw/heave so the ROV holds station.
                    frame.axes.rx = 0.0
                    frame.axes.ry = 0.0

                self.seq += 1

                frame_dict = frame.to_dict()
                if self.on_send:
                    try:
                        self.on_send(frame_dict)
                    except Exception:
                        pass
                try:
                    self.sock.send_string(json.dumps(frame_dict, separators=(",", ":")), flags=zmq.NOBLOCK)
                except zmq.Again:
                    # Keep control loop real-time: drop stale frame instead of blocking.
                    continue

                # periodic debug
                if self.debug and (t0 - self._last_debug) > 1.0:
                    print(
                        f"[pilot] sent seq={frame.seq} "
                        f"axes={frame.axes} dpad={frame.dpad} "
                        f"buttons(a,b,x,y,lb,rb,win,menu,ls,rs)="
                        f"({snap.a},{snap.b},{snap.x},{snap.y},{snap.lb},{snap.rb},{snap.win},{snap.menu},{snap.lstick},{snap.rstick})"
                    )
                    self._last_debug = t0

                # Raw dump, useful when sticks/buttons appear dead.
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
                self._keepalive_wait(max(0.1, self.reopen_on_error_s))
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
                        self._keepalive_wait(max(0.1, self.reopen_on_error_s))

            # pacing
            elapsed = time.time() - t0
            sleep_for = self.period - elapsed
            if sleep_for > 0:
                time.sleep(sleep_for)
