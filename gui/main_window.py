"""Main TritonPilot window and top-level service composition.

``MainWindow`` is the operator shell. It starts controller publishing,
telemetry subscription, video widgets, raw sensor views,
and management tools, then routes background-thread updates back onto Qt's UI
thread.
"""

from __future__ import annotations

import os
import ipaddress
import json
import logging
import math
import socket
import threading
import time
from collections import deque
from dataclasses import replace
from pathlib import Path
from threading import Thread as BackgroundThread

import numpy as np
from PyQt6.QtCore import pyqtSignal, Qt, QTimer, QEvent, QSettings
from PyQt6.QtGui import QAction, QImage
from PyQt6.QtWidgets import (
    QApplication,
    QFileDialog,
    QMainWindow,
    QWidget,
    QHBoxLayout,
    QVBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QAbstractSpinBox,
    QComboBox,
    QLineEdit,
    QPlainTextEdit,
    QSizePolicy,
    QStackedWidget,
    QTabBar,
    QTextEdit,
)
from config import (
    ARM_DISARM_TOGGLE_EDGE,
    ARM_DISARM_TOGGLE_SHORTCUT,
    ARM_PARK_SHORTCUT,
    PILOT_PUB_ENDPOINT,
    SENSOR_SUB_ENDPOINT,
    MANAGEMENT_RPC_ENDPOINT,
    VIDEO_RPC_ENDPOINT,
    CONTROLLER_DEADZONE,
    CONTROLLER_INDEX,
    CONTROLLER_DEBUG,
    CONTROLLER_DUMP_RAW_EVERY_S,
    ROV_HOST,
    DEPTH_HOLD_SENSOR_STALE_S,
    YAW_HOLD_ATTITUDE_STALE_S,
    REVERSE_CAMERA_KEYWORDS,
    REVERSE_CAMERA_NAMES,
    LIGHTS_TOGGLE_EDGE,
    LIGHTS_TOGGLE_SHORTCUT,
    REVERSE_TOGGLE_BUTTON,
    REVERSE_TOGGLE_SHORTCUT,
    PILOT_PUBLISH_RATE_HZ,
    TETHER_ROV_HOST,
    TETHER_WINDOWS_HOST,
    TRANSECT_ROTATION_SERVO_DEFAULT,
    TRANSECT_TARGET_BLUE_WIDTH_PERCENT_DEFAULT,
    TRANSECT_TARGET_BLUE_WIDTH_PERCENT_MIN,
    TRANSECT_TARGET_BLUE_WIDTH_PERCENT_MAX,
)

from input.pilot_service import PilotPublisherService
from telemetry.sensor_service import SensorSubscriberService
from video.cam import RemoteCameraManager
from recording.capture_trace import trace_event
from recording.stream_recorder import StreamRecorder
from recording.save_location import DEFAULT_RECORDINGS_DIR, SaveLocation, is_available_directory, resolve_recordings_dir
from stereo.capture import StereoCaptureSession, default_stereo_session_name, safe_filename_component
from stereo.pairs import load_stereo_pairs
from recording.video_recorder import (
    RECORD_FANOUT_HOST,
    VideoRecorder,
    VideoRecorderConfig,
    cv_fanout_port,
    record_fanout_port,
)
from tracking import (
    NullOpticalTracker,
    StationKeepCommand,
    TransectModel,
    TransectPolicy,
    VisualTargetError,
)
from gui.direct_gst_video_widget import (
    _resolve_windows_host as resolve_video_host,
    _start_kwargs as video_start_kwargs,
    _stream_options as video_stream_options,
)
from gui.video_tabs import VideoTabs
from gui.sensor_panel import SensorPanel
from gui.instruments import InstrumentPanel, HoldTestPanel, PilotTelemetryColumn
from gui.raw_sensor_page import RawSensorPage
from gui.management_page import ManagementPage
from gui.transect_page import TransectPage
from gui.transect_overlay_view import TransectHudOverlayView
from gui.ssh_page import SshConsolePage, default_pilot_ssh_presets
from gui.competition_clock import CompetitionClock
from gui.current_budget_panel import CurrentBudgetPanel
from gui.responsive import resize_to_available_screen, vertical_scroll_area
from network.net_select import LocalAddr, list_local_ipv4_addrs, parse_zmq_endpoint
from tools.analysis_transfer_server import DEFAULT_STABLE_SECONDS, build_index, create_server, start_server_in_thread


logger = logging.getLogger(__name__)


class MainWindow(QMainWindow):
    """Topside control window for live piloting and data logging."""

    SAVE_DIR_SETTINGS_KEY = "recording/save_dir"

    # Background services emit through these signals so widgets update on the
    # Qt UI thread.
    sensor_msg_sig = pyqtSignal(dict)
    pilot_status_sig = pyqtSignal(dict)
    pilot_msg_sig = pyqtSignal(dict)
    snapshot_result_sig = pyqtSignal(str, str, bool, str)
    stereo_capture_result_sig = pyqtSignal(str, str, bool, str)
    stereo_recording_state_sig = pyqtSignal(bool, str)  # recording, session_dir
    stereo_recording_progress_sig = pyqtSignal(int, float)  # count, last_delta_ms
    video_recording_state_sig = pyqtSignal(bool, str, str)  # recording, stream, path_or_error
    analysis_transfer_index_sig = pyqtSignal(dict)

    @staticmethod
    def _env_truthy(name: str, default: bool = False) -> bool:
        raw = os.environ.get(name)
        if raw is None:
            return bool(default)
        text = str(raw).strip().lower()
        if not text:
            return bool(default)
        return text in {"1", "true", "yes", "on", "debug"}

    @staticmethod
    def _tether_audio_enabled() -> bool:
        raw = os.environ.get("TRITON_TETHER_AUDIO", "1").strip().lower()
        return raw not in {"0", "false", "no", "off"}

    def _play_tether_audio_cue(self, ready: bool) -> None:
        if not self._tether_audio_enabled():
            return
        try:
            if os.name == "nt":
                import winsound

                alias = "SystemAsterisk" if ready else "SystemHand"
                winsound.PlaySound(alias, winsound.SND_ALIAS | winsound.SND_ASYNC)
                return
        except Exception:
            pass
        try:
            QApplication.beep()
            if not ready:
                QTimer.singleShot(180, QApplication.beep)
        except Exception:
            pass

    @staticmethod
    def _env_float(name: str, default: float, *, min_value: float, max_value: float) -> float:
        raw = os.environ.get(name, "").strip()
        if not raw:
            return float(default)
        try:
            value = float(raw)
        except Exception:
            return float(default)
        return max(float(min_value), min(float(max_value), value))

    def _set_status(self, lbl: QLabel, text: str) -> None:
        """Set status text + tooltip (so truncated UI still preserves full info)."""
        try:
            text = str(text)
            if lbl.text() == text and lbl.toolTip() == text:
                return
            lbl.setText(text)
            lbl.setToolTip(text)
        except Exception:
            pass

    def _set_status_tone(self, lbl: QLabel, tone: str | None = None) -> None:
        try:
            tone_key = tone or ""
            if lbl.property("tone") == tone_key:
                return
            lbl.setProperty("tone", tone_key)
            lbl.style().unpolish(lbl)
            lbl.style().polish(lbl)
            lbl.update()
        except Exception:
            pass

    def _start_ui_lag_probe_if_requested(self) -> None:
        if not self._env_truthy("TRITON_UI_LAG_PROBE", False):
            return
        interval_ms = int(self._env_float("TRITON_UI_LAG_PROBE_INTERVAL_MS", 100.0, min_value=50.0, max_value=1000.0))
        self._ui_lag_probe_interval_s = interval_ms / 1000.0
        self._ui_lag_warn_ms = self._env_float("TRITON_UI_LAG_WARN_MS", 120.0, min_value=10.0, max_value=5000.0)
        self._ui_lag_last_tick_s = time.monotonic()
        self._ui_lag_last_report_s = 0.0
        self._ui_lag_timer = QTimer(self)
        self._ui_lag_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._ui_lag_timer.setInterval(interval_ms)
        self._ui_lag_timer.timeout.connect(self._on_ui_lag_probe_tick)
        self._ui_lag_timer.start()
        logger.info(
            "UI lag probe enabled: interval=%sms warn=%sms",
            interval_ms,
            self._ui_lag_warn_ms,
        )

    def _on_ui_lag_probe_tick(self) -> None:
        now_s = time.monotonic()
        previous_s = float(getattr(self, "_ui_lag_last_tick_s", now_s))
        expected_s = float(getattr(self, "_ui_lag_probe_interval_s", 0.1))
        self._ui_lag_last_tick_s = now_s
        lag_ms = max(0.0, (now_s - previous_s - expected_s) * 1000.0)
        warn_ms = float(getattr(self, "_ui_lag_warn_ms", 120.0))
        if lag_ms < warn_ms:
            return
        last_report_s = float(getattr(self, "_ui_lag_last_report_s", 0.0))
        if now_s - last_report_s < 1.0:
            return
        self._ui_lag_last_report_s = now_s
        popup = False
        try:
            app = QApplication.instance()
            popup = bool(app is not None and app.activePopupWidget() is not None)
        except Exception:
            popup = False
        trace_event(
            "qt_ui_event_loop_lag",
            lag_ms=lag_ms,
            warn_ms=warn_ms,
            active_page=getattr(self, "_active_page_name", ""),
            popup_active=popup,
        )
        logger.warning(
            "Qt UI event loop lag %.1f ms (page=%s popup=%s)",
            lag_ms,
            getattr(self, "_active_page_name", ""),
            popup,
        )

    @staticmethod
    def _analysis_transfer_host_score(addr: LocalAddr) -> tuple[int, str]:
        ip_text = str(getattr(addr, "ip", "") or "").strip()
        try:
            ip_obj = ipaddress.ip_address(ip_text)
        except ValueError:
            return (-1, ip_text)
        if ip_obj.version != 4 or ip_obj.is_loopback or ip_obj.is_unspecified:
            return (-1, ip_text)

        score = 0
        if ip_text == "10.77.0.1":
            score += 120
        elif ip_text.startswith("10.77.0."):
            score += 110
        elif ip_text.startswith("192.168.1."):
            score += 85
        elif ip_obj.is_private:
            score += 70
        elif ip_obj.is_link_local:
            score += 25

        is_wifi = getattr(addr, "is_wifi", None)
        if is_wifi is False:
            score += 5
        elif is_wifi is True:
            score -= 4
        return (score, ip_text)

    @staticmethod
    def _parse_tether_probe_ports(raw: str | None) -> list[int]:
        ports: list[int] = []
        for part in str(raw or "").replace(";", ",").split(","):
            text = part.strip()
            if not text:
                continue
            try:
                port = int(text)
            except Exception:
                continue
            if 0 < port < 65536 and port not in ports:
                ports.append(port)
        return ports or [5555, 6001, 6000, 5556]

    @classmethod
    def _default_analysis_transfer_advertise_host(cls) -> str:
        try:
            candidates = list_local_ipv4_addrs()
        except Exception:
            candidates = []
        scored = [cls._analysis_transfer_host_score(candidate) for candidate in candidates]
        scored = [item for item in scored if item[0] >= 0 and item[1]]
        if not scored:
            return "127.0.0.1"
        scored.sort(reverse=True)
        return scored[0][1]

    @staticmethod
    def _stream_name_matches(name: str | None, tokens: list[str]) -> bool:
        if not name:
            return False
        hay = str(name).strip().lower()
        return any(str(tok).strip().lower() in hay for tok in tokens if str(tok).strip())

    def _select_reverse_stream_name(self, stream_names: list[str]) -> str | None:
        exact = {str(name).strip().lower(): name for name in stream_names}
        for preferred in REVERSE_CAMERA_NAMES:
            match = exact.get(str(preferred).strip().lower())
            if match:
                return match
        for name in stream_names:
            if self._stream_name_matches(name, REVERSE_CAMERA_KEYWORDS):
                return name
        return None

    def _select_transect_stream_name(self, stream_names: list[str]) -> str | None:
        """Pick the default transect camera (the arm camera) by name/keyword."""
        exact = {str(name).strip().lower(): name for name in stream_names}
        for preferred in ("Arm Camera",):
            match = exact.get(str(preferred).strip().lower())
            if match:
                return match
        for name in stream_names:
            if self._stream_name_matches(name, ["arm", "transect"]):
                return name
        return stream_names[0] if stream_names else None

    def _sync_reverse_action(self) -> None:
        act = getattr(self, "_reverse_act", None)
        if act is None:
            return
        try:
            act.blockSignals(True)
            act.setChecked(bool(self._reverse_enabled))
        finally:
            try:
                act.blockSignals(False)
            except Exception:
                pass

    def _refresh_drive_status(self) -> None:
        direction = "REVERSE" if self._reverse_enabled else "FORWARD"
        parts = [
            f"Mode: {direction}",
            self._depth_hold_status_text,
            self._attitude_hold_status_text,
            self._yaw_hold_status_text,
        ]
        sk_text = self._format_station_keep_status()
        if sk_text:
            parts.append(sk_text)
        self._set_status(self._mode_lbl, " | ".join(parts))
        self._set_status_tone(self._mode_lbl, "alert" if self._reverse_enabled else None)

    def _refresh_video_status(self, *, force: bool = True) -> None:
        if not force:
            now_mono = time.monotonic()
            min_interval = max(0.1, float(getattr(self, "_video_status_min_interval_s", 0.5)))
            last = float(getattr(self, "_video_status_last_refresh_s", 0.0))
            if now_mono - last < min_interval:
                return
            self._video_status_last_refresh_s = now_mono
        if self.video_panel is None:
            self._set_status(self._video_lbl, "Camera: -")
            self._set_status_tone(self._video_lbl, None)
            return

        name = self.video_panel.current_stream_name()
        vw = self.video_panel.current_video_widget()
        if name is None or vw is None:
            self._set_status(self._video_lbl, "Camera: -")
            self._set_status_tone(self._video_lbl, None)
            return

        visible = self.video_panel.visible_stream_names()

        st = vw.status()
        state = str(st.get("state") or "-")
        if bool(st.get("rov_link_lost")):
            state_txt = "ROV link lost; reconnecting after heartbeat"
        elif state == "playing":
            age = st.get("age_s")
            state_txt = f"live, age={float(age):.1f}s" if isinstance(age, (int, float)) else "live"
        elif state == "waiting":
            state_txt = "waiting"
        else:
            state_txt = state

        if len(visible) > 1:
            parts = [f"Cameras: {', '.join(visible)}", f"active: {name}", state_txt]
        else:
            parts = [f"Camera: {name}", state_txt]
        mismatch = False
        if self._reverse_enabled:
            if self._reverse_camera_name is None:
                parts.append("reverse mode active; no rear camera matched")
                mismatch = True
            elif self._reverse_camera_name not in visible:
                parts.append(f"reverse mode expects {self._reverse_camera_name} on screen")
                mismatch = True
            elif name == self._reverse_camera_name:
                parts.append("rear pane active")
            else:
                parts.append(f"rear visible: {self._reverse_camera_name}")

        self._set_status(self._video_lbl, " | ".join(parts))
        self._set_status_tone(self._video_lbl, "warn" if mismatch else None)

    def _set_reverse_mode(self, enabled: bool, *, announce: bool = True) -> None:
        enabled = bool(enabled)
        self._reverse_enabled = enabled
        try:
            self.pilot_svc.set_reverse_enabled(enabled)
        except Exception:
            pass
        self._sync_reverse_action()
        self._refresh_drive_status()
        self._refresh_video_status()
        if announce:
            if enabled:
                detail = self._reverse_camera_name or "no rear camera matched"
                self.statusBar().showMessage(f"Reverse drive ON | {detail}", 4000)
            else:
                self.statusBar().showMessage("Reverse drive OFF", 3000)

    def _toggle_reverse_mode(self, checked: bool | None = None) -> None:
        if checked is None:
            checked = not self._reverse_enabled
        checked = bool(checked)
        self._set_reverse_mode(checked)

    @staticmethod
    def _clamp_transect_target_blue_width_percent(value: float) -> float:
        try:
            v = float(value)
        except Exception:
            v = float(TRANSECT_TARGET_BLUE_WIDTH_PERCENT_DEFAULT)
        lo = max(1.0, float(TRANSECT_TARGET_BLUE_WIDTH_PERCENT_MIN))
        hi = min(100.0, max(lo, float(TRANSECT_TARGET_BLUE_WIDTH_PERCENT_MAX)))
        if not math.isfinite(v):
            v = float(TRANSECT_TARGET_BLUE_WIDTH_PERCENT_DEFAULT)
        return min(max(v, lo), hi)

    @staticmethod
    def _transect_model_with_blue_width_percent(model: TransectModel, percent: float) -> TransectModel:
        return replace(model, target_blue_fraction=max(1e-6, float(percent)) / 100.0)

    def _replace_transect_model(self, model: TransectModel) -> None:
        self._transect_model = model
        self._transect_policy = TransectPolicy(model)
        self._transect_last_lock = "no_target"
        self._transect_last_conf = 0.0
        self._transect_last_err = (0.0, 0.0, 0.0, 0.0, 0.0)
        source = getattr(self, "_transect_cv_source", None)
        if source is not None:
            setter = getattr(source, "set_policy", None)
            if callable(setter):
                setter(self._transect_policy)
            else:
                source.policy = self._transect_policy
                try:
                    source.policy.reset()
                except Exception:
                    pass
        try:
            self._transect_overlay_view.clear()
        except Exception:
            pass

    def _set_transect_rotation_servo_enabled(self, enabled: bool) -> None:
        enabled = bool(enabled)
        self._transect_rotation_servo_enabled = enabled
        page = getattr(self, "_transect_page", None)
        if page is not None:
            try:
                page.set_rotation_servo_enabled(enabled, emit=False)
            except Exception:
                pass
        act = getattr(self, "_transect_rotation_servo_act", None)
        if act is not None:
            prev = act.blockSignals(True)
            try:
                act.setChecked(enabled)
            finally:
                try:
                    act.blockSignals(prev)
                except Exception:
                    pass
        state = "ON" if enabled else "OFF"
        self.statusBar().showMessage(f"Transect yaw/er authority {state}", 2500)
        self._update_transect_cv_status()

    def _set_transect_target_blue_width_percent(self, value: float) -> None:
        percent = self._clamp_transect_target_blue_width_percent(value)
        self._transect_target_blue_width_percent = percent
        self._replace_transect_model(
            self._transect_model_with_blue_width_percent(self._transect_model, percent)
        )
        page = getattr(self, "_transect_page", None)
        if page is not None:
            try:
                page.set_target_blue_width_percent(percent, emit=False)
            except Exception:
                pass
        self.statusBar().showMessage(f"Transect blue width target {percent:.1f}%", 2500)
        self._update_transect_cv_status()

    def _transect_error_for_publish(self, error: VisualTargetError) -> VisualTargetError:
        if self._transect_rotation_servo_enabled:
            return error
        return replace(error, er=0.0)

    # --- optical-tracking station-keep (CV-era) -------------------------------
    def _sync_station_keep_action(self) -> None:
        act = getattr(self, "_station_keep_act", None)
        if act is None:
            return
        try:
            enabled = bool(self.pilot_svc.is_station_keep_enabled())
        except Exception:
            enabled = False
        act.blockSignals(True)
        try:
            act.setChecked(enabled)
        finally:
            act.blockSignals(False)

    def _set_station_keep_enabled(self, enabled: bool) -> None:
        enabled = bool(enabled)
        svc = getattr(self, "pilot_svc", None)
        if svc is None:
            return
        try:
            svc.set_station_keep_enabled(enabled)
            yaw_mode = getattr(svc, "set_autopilot_axis_mode", None)
            if enabled:
                # The transect hold relies on depth hold owning bulk altitude (the
                # ROV then vision-servos the depth setpoint from es); enable it with
                # the hold (enable-only -- disengaging leaves the pilot's holds as-is).
                # RP (roll/pitch) level is intentionally NOT auto-enabled: the pilot
                # preferred holding without leveling (2026-06-24). Toggle it with P /
                # the Autopilot menu if you want a leveled camera for a given run.
                for setter in ("set_depth_hold_enabled",):
                    fn = getattr(svc, setter, None)
                    if callable(fn):
                        try:
                            fn(True)
                        except Exception as exc:
                            logger.debug("%s failed: %s", setter, exc)
                # Yaw is left FREE. Both yaw references are unusable: the autopilot
                # "damp" mode uses the estimator's yaw RATE (mag-corrupted -- recording
                # 20260619-192426 read -42deg/s while the raw gyro read -0.6, anti-damping
                # = the spin), and the ROV station-keep yaw<-er axis is now KP=0 because
                # a 90deg-symmetric square's rotation is unmeasurable (recording
                # 20260619-193838: er noise std ~30deg rocked the vehicle back and forth
                # while it was already squared up). Free yaw drifts slowly (~0.5deg/s).
                # Re-enable a real hold only with a CLEAN raw-gyro (imu.gyro.z) rate damp.
                if callable(yaw_mode):
                    try:
                        yaw_mode("yaw", "off")
                    except Exception as exc:
                        logger.debug("yaw off failed: %s", exc)
            else:
                if callable(yaw_mode):
                    try:
                        yaw_mode("yaw", "off")
                    except Exception as exc:
                        logger.debug("yaw off failed: %s", exc)
                if hasattr(self._optical_tracker, "reset"):
                    self._optical_tracker.reset()
        except Exception as exc:
            logger.exception("Station-keep toggle failed: %s", exc)
        # Auto-record the hold so every attempt is captured for later review.
        self._auto_record_hold(enabled)
        self._sync_station_keep_action()
        self._sync_roll_pitch_level_action()
        self._refresh_drive_status()
        rec = " + recording" if (enabled and self._hold_owns_recording) else ""
        yaw_er = "ON" if getattr(self, "_transect_rotation_servo_enabled", False) else "OFF"
        self.statusBar().showMessage(
            (
                f"Optical Hold ENGAGED (station-keep + depth; RP level via P; yaw/er {yaw_er}{rec})"
                if enabled else "Optical Hold OFF"
            ),
            3000,
        )
        trace_event("station_keep_toggle", enabled=enabled, recording=bool(self._hold_owns_recording))

    def _resolve_hold_recording_stream(self) -> str | None:
        """Which camera to record for an Optical Hold: the one the transect CV/hold
        actually uses (the arm camera), falling back to the configured transect
        camera or the currently selected stream."""
        s = getattr(self, "_transect_cv_stream", None)
        if s:
            return s
        try:
            names = list(self.cam_mgr.list_available()) if self.cam_mgr is not None else []
        except Exception:
            names = []
        s = self._select_transect_stream_name(names) if names else None
        if s:
            return s
        try:
            return self.video_panel.current_stream_name() if self.video_panel is not None else None
        except Exception:
            return None

    def _auto_record_hold(self, engaged: bool) -> None:
        """Start/stop a recording tied to the Optical Hold so every hold attempt is
        captured (arm-cam mp4 + the synchronized pilot/sensors/autopilot/tracking
        log) for offline review. Only touches a recording the hold itself started;
        a recording the pilot started manually is left running."""
        if not getattr(self, "_auto_record_holds", True):
            return
        try:
            if engaged:
                if self._video_recording or self._video_recording_busy:
                    # A recording (manual or a not-yet-finalized prior hold) is
                    # already running -- don't start a second or claim ownership.
                    self._hold_owns_recording = False
                    return
                stream = self._resolve_hold_recording_stream()
                if not stream:
                    trace_event("hold_recording_skipped", reason="no_stream")
                    return
                self._hold_owns_recording = True
                self._hold_recording_stop_pending = False
                self._start_video_recording(stream)
            else:
                if not self._hold_owns_recording:
                    return
                if self._video_recording:
                    self._stop_video_recording()
                    self._hold_owns_recording = False
                elif self._video_recording_busy:
                    # Disengaged before the start worker finished; stop as soon as
                    # the recorder reports ready (handled in _on_video_recording_state).
                    self._hold_recording_stop_pending = True
                else:
                    self._hold_owns_recording = False
        except Exception as exc:
            logger.debug("auto hold recording (engaged=%s) failed: %s", engaged, exc)

    def _toggle_station_keep_from_ui(self, checked: bool | None = None) -> None:
        if checked is None:
            try:
                checked = not bool(self.pilot_svc.is_station_keep_enabled())
            except Exception:
                checked = True
        self._set_station_keep_enabled(bool(checked))

    def _toggle_station_keep_from_keyboard(self) -> None:
        try:
            new_state = not bool(self.pilot_svc.is_station_keep_enabled())
        except Exception:
            new_state = True
        self._set_station_keep_enabled(new_state)

    def _set_roll_pitch_level_enabled_ui(self, enabled: bool) -> None:
        """Set roll/pitch (RP) level hold and keep the UI in sync. Station-keep
        engage force-enables RP level for a stable camera; the P key / Autopilot
        menu item is how you turn it back OFF -- e.g. to fly a hold without
        leveling for testing."""
        enabled = bool(enabled)
        svc = getattr(self, "pilot_svc", None)
        if svc is None:
            return
        try:
            svc.set_roll_pitch_level_enabled(enabled)
        except Exception as exc:
            logger.exception("RP level set failed: %s", exc)
            return
        self._sync_roll_pitch_level_action()
        self._refresh_drive_status()
        self.statusBar().showMessage("RP Level ON" if enabled else "RP Level OFF", 3000)
        trace_event("roll_pitch_level_toggle", enabled=enabled)

    def _toggle_roll_pitch_level_from_keyboard(self) -> None:
        try:
            new_state = not bool(self.pilot_svc.is_roll_pitch_level_enabled())
        except Exception:
            new_state = False
        self._set_roll_pitch_level_enabled_ui(new_state)

    def _toggle_roll_pitch_level_from_ui(self, checked: bool | None = None) -> None:
        if checked is None:
            try:
                checked = not bool(self.pilot_svc.is_roll_pitch_level_enabled())
            except Exception:
                checked = False
        self._set_roll_pitch_level_enabled_ui(bool(checked))

    def _sync_roll_pitch_level_action(self) -> None:
        act = getattr(self, "_roll_pitch_level_act", None)
        if act is None:
            return
        try:
            enabled = bool(self.pilot_svc.is_roll_pitch_level_enabled())
        except Exception:
            enabled = False
        act.blockSignals(True)
        try:
            act.setChecked(enabled)
        finally:
            act.blockSignals(False)

    def publish_visual_target(self, sample) -> None:
        """Integration point for the future CV: push one tracker output to the ROV.

        Accepts a :class:`VisualTargetError`, a :class:`StationKeepCommand`, or a
        ready-made ``visual`` payload dict. Only takes effect while Optical Hold
        is engaged; logged to the active capture's ``tracking`` stream.
        """
        svc = getattr(self, "pilot_svc", None)
        if svc is None:
            return
        try:
            if isinstance(sample, StationKeepCommand):
                payload = sample.to_autopilot_modes()["autopilot"].get("visual")
            elif isinstance(sample, VisualTargetError):
                payload = sample.to_visual_payload()
            elif isinstance(sample, dict) or sample is None:
                payload = sample
            else:
                return
            svc.set_visual_target(payload)
            self.record_tracking_sample({"visual": payload})
        except Exception as exc:
            logger.debug("publish_visual_target failed: %s", exc)

    def _on_video_tab_changed(self, *_args) -> None:
        self._refresh_video_status()
        self._prewarm_snapshot_capture_feeds()

    def _prewarm_snapshot_capture_feeds(self) -> None:
        manager = getattr(self, "cam_mgr", None)
        warmer = getattr(manager, "prewarm_snapshot_taps", None)
        if not callable(warmer):
            return
        try:
            warmer(None)
        except Exception as exc:
            logger.debug("Snapshot prewarm request failed: %s", exc)

    def _make_center_placeholder(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setObjectName("videoPanePlaceholder")
        lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl.setWordWrap(True)
        return lbl

    def _attach_shared_video_panel(self, target_layout: QVBoxLayout | None) -> None:
        if self.video_panel is None or target_layout is None:
            return
        try:
            self.video_panel.setParent(None)
        except Exception:
            pass
        target_layout.addWidget(self.video_panel, 1)
        try:
            self.video_panel.show()
            self.video_panel.raise_()
        except Exception:
            pass
        self._refresh_video_panel_geometry()

    def _refresh_video_panel_geometry(self, *, defer: bool = True) -> None:
        if self.video_panel is None:
            return
        try:
            self.video_panel.updateGeometry()
            self.video_panel.update()
        except Exception:
            pass
        refresher = getattr(self.video_panel, "refresh_layout_geometry", None)
        if callable(refresher):
            try:
                refresher()
            except Exception:
                pass
        if defer:
            try:
                QTimer.singleShot(0, lambda: self._refresh_video_panel_geometry(defer=False))
                QTimer.singleShot(80, lambda: self._refresh_video_panel_geometry(defer=False))
            except Exception:
                pass

    def _set_video_panel_square_display(self, enabled: bool) -> None:
        if self.video_panel is None:
            return
        setter = getattr(self.video_panel, "set_square_display_enabled", None)
        if callable(setter):
            try:
                setter(bool(enabled))
            except Exception:
                pass

    def _resume_video_panel(self) -> None:
        if self.video_panel is None:
            return
        try:
            resume = getattr(self.video_panel, "resume_visible_streams", None)
            if callable(resume):
                resume()
        except Exception:
            pass

    def _suspend_video_panel_if_hidden(self) -> None:
        if self.video_panel is None:
            return
        try:
            suspend = getattr(self.video_panel, "suspend_all", None)
            if callable(suspend):
                suspend()
                return
            stop_all = getattr(self.video_panel, "stop_all", None)
            if callable(stop_all):
                stop_all()
        except Exception:
            pass

    def _on_page_tab_changed(self, index: int) -> None:
        page_name = {
            0: "pilot",
            1: "transect",
            2: "hold_test",
            3: "raw_sensors",
            4: "management",
            5: "ssh",
        }.get(int(index), "pilot")
        self._set_center_page(page_name)

    def _set_center_page(self, page_name: str, *, announce: bool = True) -> None:
        page_name = str(page_name)
        if page_name not in {"pilot", "transect", "reverse_drive", "hold_test", "raw_sensors", "management", "ssh"}:
            page_name = "pilot"
        if page_name == getattr(self, "_active_page_name", "pilot"):
            return

        previous_page = getattr(self, "_active_page_name", "")
        if previous_page == "reverse_drive" and page_name != "reverse_drive":
            if self.video_panel is not None:
                self._pilot_layout_count_restore = int(self.video_panel.layout_count())
            if getattr(self, "_reverse_page_owns_mode", False):
                self._reverse_page_owns_mode = False
                self._set_reverse_mode(False, announce=False)
        if previous_page == "transect" and page_name != "transect":
            try:
                self._transect_status_timer.stop()
            except Exception:
                pass
            self._stop_transect_cv()  # also clears/hides the overlay view
            self._transect_page.set_cv_status("Autopilot CV: off", "off")
            if self.video_panel is not None:
                try:
                    self._transect_page.detach_video_panel(self.video_panel)
                except Exception:
                    pass
                self._set_video_panel_square_display(False)
                if getattr(self, "_transect_layout_restore_snapshot", None) is not None:
                    try:
                        self.video_panel.restore_layout_snapshot(
                            self._transect_layout_restore_snapshot,
                            save=False,
                            emit=True,
                        )
                    except Exception:
                        pass
                    self._transect_layout_restore_snapshot = None

        if page_name == "reverse_drive":
            if self.video_panel is not None:
                if self._active_page_name == "pilot":
                    self._pilot_layout_count_restore = int(self.video_panel.layout_count())
                self._resume_video_panel()
                self._attach_shared_video_panel(self._reverse_video_host_layout)
                self.video_panel.set_layout_controls_visible(True)
                self.video_panel.set_layout_controls_enabled(True)
            if not self._reverse_enabled:
                self._reverse_page_owns_mode = True
                self._set_reverse_mode(True, announce=False)
            else:
                self._reverse_page_owns_mode = False
            self._page_stack.setCurrentWidget(self._reverse_page)
        elif page_name == "transect":
            if self.video_panel is not None:
                if self._active_page_name == "pilot":
                    self._pilot_layout_count_restore = int(self.video_panel.layout_count())
                try:
                    self._transect_layout_restore_snapshot = self.video_panel.layout_snapshot()
                except Exception:
                    self._transect_layout_restore_snapshot = None
                self.video_panel.set_layout_controls_visible(False)
                self.video_panel.set_layout_controls_enabled(False)
                self._resume_video_panel()
                self._transect_page.attach_video_panel(self.video_panel)
                self._apply_transect_camera_view()
            self._page_stack.setCurrentWidget(self._transect_page)
            # Start the live CV AFTER the page is shown and on the next event-loop
            # tick: spawning the receiver subprocess + the mirror RPC must not
            # stall the tab switch. The overlay reveals itself once frames arrive;
            # until then (or with no ROV) the normal Direct3D video shows through.
            self._transect_page.set_cv_status("Autopilot CV: starting…", "warn")
            self._transect_status_timer.start()
            QTimer.singleShot(0, self._maybe_start_transect_cv)
        elif page_name == "hold_test":
            if self.video_panel is not None:
                if self._active_page_name == "pilot":
                    self._pilot_layout_count_restore = int(self.video_panel.layout_count())
                self.video_panel.set_layout_controls_visible(True)
                self.video_panel.set_layout_controls_enabled(False)
                self.video_panel.set_layout_count(1)
                self._resume_video_panel()
                self._attach_shared_video_panel(self._hold_test_video_host_layout)
            self._page_stack.setCurrentWidget(self._hold_test_page)
        elif page_name == "management":
            if self.video_panel is not None:
                if self._active_page_name == "pilot":
                    self._pilot_layout_count_restore = int(self.video_panel.layout_count())
                try:
                    self.video_panel.set_layout_controls_visible(True)
                    self.video_panel.setParent(None)
                except Exception:
                    pass
            self._page_stack.setCurrentWidget(self._management_page)
            try:
                self._management_page.refresh_state()
            except Exception:
                pass
        elif page_name == "raw_sensors":
            if self.video_panel is not None:
                if self._active_page_name == "pilot":
                    self._pilot_layout_count_restore = int(self.video_panel.layout_count())
                try:
                    self.video_panel.set_layout_controls_visible(True)
                    self.video_panel.setParent(None)
                except Exception:
                    pass
            self._page_stack.setCurrentWidget(self._raw_sensor_page)
        elif page_name == "ssh":
            if self.video_panel is not None:
                if self._active_page_name == "pilot":
                    self._pilot_layout_count_restore = int(self.video_panel.layout_count())
                try:
                    self.video_panel.set_layout_controls_visible(True)
                    self.video_panel.setParent(None)
                except Exception:
                    pass
            self._page_stack.setCurrentWidget(self._ssh_page)
        else:
            if self.video_panel is not None:
                self.video_panel.set_layout_controls_visible(True)
                self.video_panel.set_layout_controls_enabled(True)
                self._resume_video_panel()
                self._attach_shared_video_panel(self._pilot_video_host_layout)
                if previous_page != "reverse_drive":
                    self.video_panel.set_layout_count(int(self._pilot_layout_count_restore))
                    self._resume_video_panel()
            self._page_stack.setCurrentWidget(self._pilot_page)

        self._active_page_name = page_name

        prev = False
        try:
            prev = self._page_tabs.blockSignals(True)
            tab_index = {
                "pilot": 0,
                "transect": 1,
                "hold_test": 2,
                "raw_sensors": 3,
                "management": 4,
                "ssh": 5,
            }.get(page_name, 0)
            self._page_tabs.setCurrentIndex(tab_index)
        finally:
            try:
                self._page_tabs.blockSignals(prev)
            except Exception:
                pass

        self._refresh_video_status()
        if announce:
            label = {
                "pilot": "Pilot",
                "transect": "Transect",
                "reverse_drive": "Reverse Drive",
                "hold_test": "Hold Test",
                "raw_sensors": "Raw Sensors",
                "management": "Vehicle Setup",
                "ssh": "SSH",
            }.get(page_name, "Pilot")
            self.statusBar().showMessage(f"Switched to {label} page", 3000)

    def _on_transect_camera_changed(self, name: str) -> None:
        if getattr(self, "_active_page_name", "") == "transect":
            self._apply_transect_camera_view(name)
            # Re-point the CV source at the newly selected camera (overlay
            # re-reveals itself when frames arrive on the new stream).
            self._start_transect_cv()

    def _transect_cv_startup_allowed(self) -> bool:
        """Return True when connection state is fresh enough to start CV plumbing."""
        if str(getattr(self, "_link_state_last", "NO DATA") or "").upper() != "OK":
            return False
        tether_state = getattr(self, "_tether_ui_ready_last", None)
        if tether_state is False:
            return False
        return True

    def _maybe_retry_transect_cv_start(self) -> bool:
        if getattr(self, "_active_page_name", "") != "transect":
            return False
        if not self._transect_cv_startup_allowed():
            return False
        now_mono = time.monotonic()
        last = float(getattr(self, "_transect_cv_last_start_attempt_mono", 0.0) or 0.0)
        if now_mono - last < 2.0:
            return False
        self._transect_cv_last_start_attempt_mono = now_mono
        return self._start_transect_cv()

    def _start_transect_cv(self) -> bool:
        """Start the live transect CV source on the selected camera. Returns started.

        Inert/safe when no ROV is connected (no mirror, no receiver) so the normal
        Direct3D transect view is shown instead. The annotated overlay is layered
        on top only while this is running.
        """
        if not self._transect_cv_startup_allowed():
            return False
        self._transect_cv_last_start_attempt_mono = time.monotonic()
        cam_mgr = getattr(self, "cam_mgr", None)
        if cam_mgr is None or getattr(cam_mgr, "rov", None) is None:
            return False
        try:
            stream = str(self._transect_page.current_stream_name() or "").strip()
        except Exception:
            stream = ""
        if not stream:
            return False
        if self._transect_cv_source is not None and self._transect_cv_stream == stream:
            return True
        self._stop_transect_cv()
        try:
            opts = video_stream_options(cam_mgr, stream)
            host = resolve_video_host(cam_mgr, opts)
            bind = host if bool(opts.get("bind_receiver_to_host", True)) else "0.0.0.0"
            width = int(opts.get("width", 0) or 0)
            height = int(opts.get("height", 0) or 0)
            if width <= 0 or height <= 0:
                logger.warning("transect CV: stream '%s' has no dimensions; CV disabled", stream)
                return False
            tx_is_h264 = (
                str(opts.get("video_format", "")).lower() == "h264"
                or str(opts.get("encode", "")).lower() == "h264"
            )
            codec = "h264" if tx_is_h264 else "jpeg"
            channel_order = str(opts.get("channel_order", "BGR") or "BGR")
            # Read the H.264 the laptop already receives via the display pipeline's
            # always-on loopback fan-out -- NO ROV tether mirror, so the CV feed no
            # longer competes for tether bandwidth (which lost packets -> corrupt
            # frames + lag straight into the autopilot). Because the source is a
            # lossless loopback, the big jitter buffer the old tether feed needed is
            # just latency now: default down to 60ms (override: transect_cv_latency_ms).
            latency_ms = int(opts.get("transect_cv_latency_ms", 60) or 60)
            cv_port = cv_fanout_port(int(opts.get("port", 5000)))
        except Exception as exc:
            logger.debug("transect CV: could not resolve stream '%s': %s", stream, exc)
            return False

        from tracking.transect_source import TransectVisionSource, default_receiver_factory

        if getattr(self, "_transect_detector", None) is None:
            from tracking.transect_cv import ClassicalTransectDetector
            self._transect_detector = ClassicalTransectDetector()

        try:
            source = TransectVisionSource(
                width=width, height=height,
                on_estimate=self._on_transect_estimate,
                receiver_factory=default_receiver_factory(
                    port=cv_port, codec=codec, width=width, height=height,
                    bind_address=RECORD_FANOUT_HOST, channel_order=channel_order, latency_ms=latency_ms,
                ),
                detector=self._transect_detector,
                policy=self._transect_policy,
                mirror_setter=None,  # loopback fan-out is part of the display pipeline; no ROV mirror
                name=f"transect-cv:{stream}",
            )
            source.start()
        except Exception as exc:
            logger.warning("transect CV start failed: %s", exc)
            return False
        self._transect_cv_source = source
        self._transect_cv_stream = stream
        trace_event("transect_cv_started", stream=stream, cv_fanout_port=cv_port, codec=codec,
                    width=width, height=height)
        return True

    def _maybe_start_transect_cv(self) -> None:
        """Deferred CV start; no-op if the user already left the Transect tab."""
        if getattr(self, "_active_page_name", "") != "transect":
            return
        self._start_transect_cv()
        self._update_transect_cv_status()

    def _stop_transect_cv(self, *, background: bool = True) -> None:
        source = self._transect_cv_source
        self._transect_cv_source = None
        self._transect_cv_stream = None
        try:
            self._transect_overlay_view.clear()
        except Exception:
            pass
        if source is None:
            return
        if background:
            # source.stop() joins the worker (<=2s) + receiver shutdown + mirror
            # RPC; do it off the GUI thread so leaving the tab is instant.
            threading.Thread(
                target=self._stop_transect_source_safe, args=(source,),
                name="transect-cv-stop", daemon=True,
            ).start()
        else:
            self._stop_transect_source_safe(source)

    def _stop_transect_source_safe(self, source) -> None:
        try:
            source.stop()
        except Exception as exc:
            logger.debug("transect CV stop failed: %s", exc)
        trace_event("transect_cv_stopped")

    def _on_transect_engage_toggled(self, checked: bool) -> None:
        """The transect-page Engage button (mirrors the K-key Optical Hold toggle)."""
        self._set_station_keep_enabled(bool(checked))

    def _toggle_transect_stopwatch_from_keyboard(self) -> None:
        page = getattr(self, "_transect_page", None)
        if page is None:
            return
        page.toggle_stopwatch()
        state = "running" if page.stopwatch_running() else "paused"
        try:
            self.statusBar().showMessage(f"Transect stopwatch {state}: {page.stopwatch_label.text()}", 2500)
        except Exception:
            pass

    def _reset_transect_stopwatch_from_keyboard(self) -> None:
        page = getattr(self, "_transect_page", None)
        if page is None:
            return
        page.reset_stopwatch()
        try:
            self.statusBar().showMessage(f"Transect stopwatch reset: {page.stopwatch_label.text()}", 2500)
        except Exception:
            pass

    def _update_transect_cv_status(self) -> None:
        """Poll the CV source health + last lock and refresh the status chip."""
        page = getattr(self, "_transect_page", None)
        if page is None:
            return
        # Keep the engage button in sync with the actual hold state (it may have
        # been toggled by the K key) and highlight it green when a lock is ready.
        try:
            svc = getattr(self, "pilot_svc", None)
            engaged = bool(svc.is_station_keep_enabled()) if svc is not None else False
            page.update_engage_state(engaged=engaged, lock_ready=(self._transect_last_lock == "lock"))
        except Exception:
            pass
        source = self._transect_cv_source
        if source is None:
            if getattr(self, "_tether_ui_ready_last", None) is False:
                page.set_cv_status("Autopilot CV: waiting for tether", "warn")
                return
            if str(getattr(self, "_link_state_last", "NO DATA") or "").upper() != "OK":
                page.set_cv_status("Autopilot CV: waiting for ROV link", "warn")
                return
            if self._maybe_retry_transect_cv_start():
                source = self._transect_cv_source
            if source is None:
                page.set_cv_status("Autopilot CV: unavailable (no ROV)", "off")
                return
        st = source.stats()
        frames = int(st.get("frames", 0) or 0)
        age = st.get("last_frame_age_s")
        since_start = st.get("age_since_start_s") or 0.0
        if frames == 0:
            if since_start < 4.0:
                page.set_cv_status("Autopilot CV: connecting…", "warn")
            else:
                page.set_cv_status("Autopilot CV: no frames", "bad")
            return
        if age is not None and age > 2.0:
            page.set_cv_status(f"Autopilot CV: stalled ({age:.0f}s)", "bad")
            return
        fps = float(st.get("fps", 0.0) or 0.0)
        lock = self._transect_last_lock
        tone = {"lock": "ok", "acquiring": "warn"}.get(lock, "neutral")
        label = {"lock": "LOCK", "acquiring": "ACQUIRING", "lost": "LOST", "no_target": "searching"}.get(lock, lock)
        ex, ey, es, er, viol = getattr(self, "_transect_last_err", (0.0, 0.0, 0.0, 0.0, 0.0))
        txt = f"Optical Hold: {label} {self._transect_last_conf * 100:.0f}% · {fps:.0f}fps"
        if lock in ("lock", "acquiring"):
            # Prominent "how square does the target look" cue (squaring up = max margin).
            a = abs(er)
            sq = "SQUARE ✓" if a < 0.15 else ("TILTED" if a < 0.5 else "DIAMOND ⚠")
            txt += f"  |  {sq}  ex{ex:+.2f} ey{ey:+.2f} es{es:+.2f} er{er:+.2f}"
            if not getattr(self, "_transect_rotation_servo_enabled", False):
                txt += " yaw/er OFF"
            if viol > 0.05:
                txt += f"  RED {viol * 100:.0f}%"
        page.set_cv_status(txt, tone)

    def _on_transect_estimate(self, estimate, observation, frame) -> None:
        """CV worker-thread callback: record the result, then publish if engaged.

        The CV does NOT drive the transect video any more -- the tab shows the
        smooth hardware-decoded (d3d11) feed and never gets replaced by raw CV
        frames, so the CV's own decode stalls during a fast arm move can't
        flicker the picture. The detection result only feeds the transparent
        lock/error HUD + the ROV.
        """
        self._transect_last_lock = estimate.lock_state
        self._transect_last_conf = float(estimate.confidence)
        e = estimate.error
        command_error = self._transect_error_for_publish(e)
        self._transect_last_err = (
            e.ex,
            e.ey,
            e.es,
            e.er,
            estimate.violation,
        )
        try:
            self._transect_overlay_view.submit_estimate(
                self._transect_model,
                estimate,
                observation,
                getattr(frame, "shape", None),
            )
        except Exception as exc:
            logger.debug("transect overlay submit failed: %s", exc)
        # Only stream the visual target to the ROV once the pilot has engaged
        # Optical Hold; otherwise this is a passive lock preview.
        try:
            svc = getattr(self, "pilot_svc", None)
            if svc is not None and bool(svc.is_station_keep_enabled()):
                self.publish_visual_target(command_error)
        except Exception as exc:
            logger.debug("transect publish failed: %s", exc)

    def _apply_transect_camera_view(self, name: str | None = None) -> None:
        if self.video_panel is None:
            return
        selected = str(name or "").strip()
        if not selected:
            try:
                selected = str(self._transect_page.current_stream_name() or "")
            except Exception:
                selected = ""
        if not self.video_panel.has_stream(selected):
            try:
                selected = str(self.video_panel.current_stream_name() or "")
            except Exception:
                selected = ""
        if not self.video_panel.has_stream(selected):
            return
        try:
            self._transect_page.set_current_stream(selected, emit=False)
        except Exception:
            pass
        self._set_video_panel_square_display(True)
        try:
            self.video_panel.apply_temporary_layout(
                1,
                [selected],
                active_name=selected,
                emit=True,
            )
            self._resume_video_panel()
        except Exception:
            pass

    def __init__(self, streams_path: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("TritonPilot")
        self._settings = QSettings("TritonPilot", "ROVTopside")
        self._preferred_save_dir: str = str(self._settings.value(self.SAVE_DIR_SETTINGS_KEY, "") or "").strip()
        self._save_dir_act: QAction | None = None
        self._reset_save_dir_act: QAction | None = None
        self._fullscreen_act: QAction | None = None
        self._analysis_transfer_start_act: QAction | None = None
        self._analysis_transfer_stop_act: QAction | None = None
        self._analysis_transfer_restart_act: QAction | None = None
        self._transect_rotation_servo_act: QAction | None = None
        self._roll_pitch_level_act: QAction | None = None
        self._analysis_transfer_server = None
        self._analysis_transfer_thread = None
        self._analysis_transfer_root: Path | None = None
        self._analysis_transfer_error = ""
        self._analysis_transfer_host = os.environ.get("TRITON_PILOT_TRANSFER_HOST", "0.0.0.0").strip() or "0.0.0.0"
        self._analysis_transfer_advertise_host = os.environ.get("TRITON_PILOT_TRANSFER_ADVERTISE_HOST", "").strip()
        self._analysis_transfer_resolved_advertise_host = ""
        self._analysis_transfer_port = int(os.environ.get("TRITON_PILOT_TRANSFER_PORT", "8765") or "8765")
        self._analysis_transfer_stable_seconds = float(
            os.environ.get("TRITON_PILOT_TRANSFER_STABLE_SECONDS", str(DEFAULT_STABLE_SECONDS))
            or str(DEFAULT_STABLE_SECONDS)
        )
        self._analysis_transfer_include_hidden = (
            os.environ.get("TRITON_PILOT_TRANSFER_INCLUDE_HIDDEN", "").strip().lower() in {"1", "true", "yes", "on"}
        )
        self._analysis_transfer_autostart = (
            os.environ.get("TRITON_PILOT_TRANSFER_AUTOSTART", "1").strip().lower()
            not in {"0", "false", "no", "off"}
        )
        self._analysis_transfer_index_lock = threading.Lock()
        self._analysis_transfer_index_cache: dict = {}
        self._analysis_transfer_index_refreshing = False
        self._analysis_transfer_index_last_start_s = 0.0
        self._analysis_transfer_index_refresh_interval_s = self._env_float(
            "TRITON_PILOT_TRANSFER_INDEX_REFRESH_S",
            10.0,
            min_value=1.0,
            max_value=120.0,
        )

        # link status
        self._last_sensor_ts = 0.0
        self._last_hb_ts = 0.0
        self._last_hb = {}
        self._hb_period_ema_s: float | None = None
        self._prev_hb_rx_ts: float | None = None
        self._link_state_last: str = "NO DATA"
        self._reverse_enabled: bool = False
        self._reverse_camera_name: str | None = None
        self._depth_hold_status_text: str = "Depth Hold: OFF"
        self._attitude_hold_status_text: str = "RP Level: OFF"
        self._yaw_hold_status_text: str = "Yaw Hold: OFF"
        # Optical-tracking station-keep (CV-era). The tracker is a no-lock
        # placeholder until the real CV model is dropped in; engaging the mode
        # with it is safe (the ROV controller stays inert without a valid lock).
        self._station_keep_act = None
        self._optical_tracker = NullOpticalTracker()
        # Transect autopilot CV: geometry model + policy + a live raw frame source
        # that runs only while the Transect tab is open (lazily created; the
        # detector is a stub until the CV lands). The CV is detection-only and does
        # NOT drive the video -- the tab keeps the smooth d3d11 feed, and results
        # surface in the transparent lock/error HUD so a CV decode stall can
        # never replace/flicker the picture.
        # Nadir arm camera (post 90-deg mount fix): target_cx/cy=0.5 is the
        # geometric center, and the operator-facing blue-width default is 50% of
        # frame width. Be careful when changing target_blue_fraction from a
        # recording's median: that may just reflect how high the ROV happened to
        # fly. Use tools/transect_replay.py --calibrate to re-check centering if
        # the mount moves.
        self._transect_rotation_servo_enabled = bool(TRANSECT_ROTATION_SERVO_DEFAULT)
        self._transect_target_blue_width_percent = self._clamp_transect_target_blue_width_percent(
            TRANSECT_TARGET_BLUE_WIDTH_PERCENT_DEFAULT
        )
        self._transect_model = self._transect_model_with_blue_width_percent(
            TransectModel(),
            self._transect_target_blue_width_percent,
        )
        self._transect_policy = TransectPolicy(self._transect_model)
        self._transect_detector = None  # lazily created ClassicalTransectDetector
        self._transect_overlay_view = TransectHudOverlayView()
        self._transect_cv_source = None
        self._transect_cv_stream: str | None = None
        self._transect_cv_last_start_attempt_mono = 0.0
        self._transect_last_lock: str = "no_target"
        self._transect_last_conf: float = 0.0
        self._transect_last_err = (0.0, 0.0, 0.0, 0.0, 0.0)  # ex, ey, es, er, violation
        self._transect_status_timer = QTimer(self)
        self._transect_status_timer.setInterval(500)
        self._transect_status_timer.timeout.connect(self._update_transect_cv_status)

        self._link_lbl = QLabel("Heartbeat: (no data)")
        self.statusBar().addPermanentWidget(self._link_lbl)

        self._ctrl_lbl = QLabel("Controller: (starting)")
        self.statusBar().addPermanentWidget(self._ctrl_lbl)

        self._depth_lbl = QLabel("Depth: -", self)
        self._depth_lbl.hide()

        self._gain_lbl = QLabel("Max Gain: 100%")
        self.statusBar().addPermanentWidget(self._gain_lbl)

        self._mode_lbl = QLabel("Mode: FORWARD | Depth Hold: OFF")
        self.statusBar().addPermanentWidget(self._mode_lbl, 1)

        self._analysis_transfer_lbl = QLabel("Analysis Share: starting", self)
        self._analysis_transfer_lbl.hide()
        self._analysis_transfer_line = QLabel("Analysis Share: starting", self)
        self._analysis_transfer_line.setObjectName("analysisTransferLine")
        self._analysis_transfer_line.setWordWrap(True)
        self._analysis_transfer_line.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._analysis_transfer_line.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        self._analysis_transfer_line.setStyleSheet(
            """
            QLabel#analysisTransferLine {
                background: #202028;
                border: 1px solid #343442;
                border-radius: 6px;
                padding: 5px 8px;
                color: #f0f4ff;
                font-weight: 600;
            }
            QLabel#analysisTransferLine[tone="alert"] {
                background: #3b2525;
                border-color: #9c4a4a;
                color: #ffd9d9;
                font-weight: 700;
            }
            QLabel#analysisTransferLine[tone="warn"] {
                background: #332b1d;
                border-color: #a07e34;
                color: #ffe6ae;
            }
            """
        )
        self._analysis_transfer_line.hide()

        self._video_lbl = QLabel("Camera: -")
        self._power_lbl = QLabel("Power: -")

        # quick depth readout (from external depth sensor)
        self._last_depth_ts = 0.0
        self._last_depth: dict = {}

        # quick power readout (from Power Sense Module conversion on ROV)
        self._last_power_ts = 0.0
        self._last_power: dict = {}

        # Depth-hold display cache. The real controller and release latch run
        # on the ROV; TritonPilot only mirrors the reported runtime target.
        self._dh_enabled: bool = False
        self._dh_target_m: float | None = None
        self._last_autopilot_status_ts = 0.0
        self._last_autopilot_status: dict = {}

        # Yaw-hold display cache. The real manual-yaw release latch runs on the
        # ROV; TritonPilot mirrors the reported runtime target.
        self._last_attitude_ts = 0.0
        self._last_attitude: dict = {}
        self._yh_target_deg: float | None = None

        # network status (tether vs wifi, local route to ROV, remote link state)
        self._net_lbl = QLabel("Net: -")
        self._last_net_ts = 0.0
        self._last_net: dict = {}
        self._route_cache = {"ts": 0.0, "iface": None, "src_ip": None, "is_wifi": None, "err": None}
        self._rov_host = str(ROV_HOST)
        self._tether_host = str(TETHER_ROV_HOST or "192.168.1.4")
        self._tether_windows_host = str(TETHER_WINDOWS_HOST or "192.168.1.1")
        self._tether_probe_ports = self._parse_tether_probe_ports(
            os.environ.get("TRITON_TETHER_PROBE_PORTS", "5555,6001,6000,5556")
        )
        self._tether_probe_timeout_s = float(os.environ.get("TRITON_TETHER_PROBE_TIMEOUT", "0.25") or "0.25")
        self._tether_probe_interval_s = float(os.environ.get("TRITON_TETHER_PROBE_INTERVAL", "1.0") or "1.0")
        self._tether_status_lock = threading.Lock()
        self._tether_status: dict = {
            "ts": 0.0,
            "ready": False,
            "host": self._tether_host,
            "local_ip": None,
            "iface": None,
            "port": None,
            "reason": "checking tether network",
        }
        self._tether_video_ready = False
        self._tether_ui_ready_last: bool | None = None
        self._tether_probe_stop = threading.Event()
        self._netdiag_port = int(os.environ.get("TRITON_NETDIAG_PORT", "7700"))
        self._netdiag_stop = threading.Event()
        self._netdiag_lock = threading.Lock()
        self._netdiag = {"ts": 0.0, "ok": False, "last_rtt_ms": None, "avg_rtt_ms": None, "jitter_ms": None, "loss_pct": None, "err": None}
        self._netdiag_thread = threading.Thread(target=self._netdiag_probe_loop, daemon=True)
        self._netdiag_thread.start()
        self._tether_probe_thread = threading.Thread(target=self._tether_probe_loop, daemon=True)
        self._tether_probe_thread.start()

        self._tether_top_lbl = QLabel("Tether: checking")
        self._tether_top_lbl.setObjectName("tetherStatusPill")
        self._tether_top_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._tether_top_lbl.setMinimumWidth(230)
        self._tether_top_lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

        self._tether_banner = QLabel("TETHER NETWORK: checking", self)
        self._tether_banner.setObjectName("tetherStatusBanner")
        self._tether_banner.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._tether_banner.setWordWrap(True)
        self._tether_banner.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._set_status_tone(self._tether_top_lbl, "warn")
        self._set_status_tone(self._tether_banner, "warn")

        # Keep the piloting status bar compact and focused on the essentials.
        for _lbl, _w in [
            (self._link_lbl, 230),
            (self._ctrl_lbl, 220),
            (self._gain_lbl, 125),
            (self._mode_lbl, 430),
        ]:
            try:
                _lbl.setMinimumWidth(int(_w))
                _lbl.setToolTip(_lbl.text())
                _lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            except Exception:
                pass

        self._video_status_last_refresh_s = 0.0
        self._video_status_min_interval_s = self._env_float(
            "TRITON_VIDEO_STATUS_REFRESH_INTERVAL_S",
            0.5,
            min_value=0.1,
            max_value=5.0,
        )
        self._ui_lag_timer: QTimer | None = None
        self._start_ui_lag_probe_if_requested()

        self._link_timer = QTimer(self)
        self._link_timer.timeout.connect(self._update_link_status)
        self._link_timer.start(200)

        self._analysis_transfer_timer = QTimer(self)
        self._analysis_transfer_timer.timeout.connect(self._refresh_analysis_transfer_status)
        self._analysis_transfer_timer.start(2000)

        self._tether_ui_timer = QTimer(self)
        self._tether_ui_timer.timeout.connect(self._refresh_tether_status_ui)
        self._tether_ui_timer.start(300)

        # connect signals to slots
        self.sensor_msg_sig.connect(self._handle_sensor_msg_on_ui)
        self.pilot_status_sig.connect(self._handle_pilot_status_on_ui)
        self.pilot_msg_sig.connect(self._handle_pilot_msg_on_ui)
        self.snapshot_result_sig.connect(self._handle_snapshot_result_on_ui)
        self.stereo_capture_result_sig.connect(self._handle_stereo_capture_result_on_ui)
        self.stereo_recording_state_sig.connect(self._handle_stereo_recording_state_on_ui)
        self.stereo_recording_progress_sig.connect(self._handle_stereo_recording_progress_on_ui)
        self.video_recording_state_sig.connect(self._on_video_recording_state)
        self.analysis_transfer_index_sig.connect(self._handle_analysis_transfer_index_result)

        self._last_ctrl_status: dict = {'controller': 'unknown'}
        self._last_pilot_msg_ts: float = 0.0
        self._last_pilot_msg: dict = {}

        # 1) pilot publisher (xbox -> ROV)
        # The servo arm is driven from the gamepad (right stick while the aim
        # modifier is held); W/A/S/D are intentionally NOT bound to the arm so
        # they are free for other hotkeys.
        self._back_gripper_gain_shortcuts = {
            Qt.Key.Key_1: -1.0,
            Qt.Key.Key_2: +1.0,
            Qt.Key.Key_BracketLeft: -1.0,
            Qt.Key.Key_BracketRight: +1.0,
        }
        self._arm_gain_shortcuts = {
            Qt.Key.Key_6: -1.0,
            Qt.Key.Key_7: +1.0,
        }
        self._rov_gain_shortcuts = {
            Qt.Key.Key_Minus: -1.0,
            Qt.Key.Key_Underscore: -1.0,
            Qt.Key.Key_Plus: +1.0,
            Qt.Key.Key_Equal: +1.0,
        }
        self._lights_toggle_shortcut_text = str(LIGHTS_TOGGLE_SHORTCUT or "L").strip() or "L"
        self._lights_toggle_edge = str(LIGHTS_TOGGLE_EDGE or "lights").strip().lower() or "lights"
        self._arm_disarm_shortcut_text = str(ARM_DISARM_TOGGLE_SHORTCUT or "O").strip() or "O"
        self._arm_disarm_edge = str(ARM_DISARM_TOGGLE_EDGE or "menu").strip().lower() or "menu"
        self._arm_park_shortcut_text = str(ARM_PARK_SHORTCUT or "A").strip() or "A"
        # Tracks the last armed state pushed into the pilot arm integrator so the
        # arm freezes (and snaps to its park pose) while the ROV is disarmed.
        self._arm_integrator_armed: bool | None = None

        self.pilot_svc = PilotPublisherService(
            endpoint=PILOT_PUB_ENDPOINT,
            rate_hz=PILOT_PUBLISH_RATE_HZ,
            deadzone=CONTROLLER_DEADZONE,
            debug=CONTROLLER_DEBUG,
            index=CONTROLLER_INDEX,
            dump_raw_every_s=CONTROLLER_DUMP_RAW_EVERY_S,
            on_status=self._on_pilot_status_from_thread,
            on_send=self._on_pilot_msg_from_thread,
        )
        self.pilot_svc.start()
        self._reverse_enabled = bool(self.pilot_svc.is_reverse_enabled())

        # optional stream recorder (pilot + sensors + heartbeat)
        self._stream_recorder: StreamRecorder | None = None
        self._record_dir: str | None = None
        self._app_session_dir: Path | None = None
        self._app_session_location: SaveLocation | None = None
        self._last_snapshot_request_mono: float = 0.0
        self._streams_path = str(streams_path)
        self._capture_mode = "standard"
        self._stereo_pairs = []
        self._active_stereo_pair = None
        self._stereo_capture_session: StereoCaptureSession | None = None
        self._last_stereo_capture_request_mono: float = 0.0
        self._stereo_capture_in_flight = False
        # Stereo burst recording ("orbit" mode): captures clean synced pairs
        # back-to-back into a dedicated session until toggled off.
        self._stereo_recording = False
        self._stereo_recording_session: StereoCaptureSession | None = None
        self._stereo_recording_thread: threading.Thread | None = None
        self._stereo_recording_stop = threading.Event()
        self._stereo_recording_count = 0
        self._last_stereo_recording_toggle_mono: float = 0.0
        # Single-camera video recording (standard mode): records the selected
        # stream's H.264 feed to mp4 topside via a live ROV mirror UDP port.
        self._video_recording = False
        self._video_recorder: VideoRecorder | None = None
        self._video_recording_stream: str | None = None
        self._video_recording_mirror_port: int | None = None
        self._video_recording_busy = False
        self._video_recording_started_mono: float = 0.0
        self._last_video_recording_toggle_mono: float = 0.0
        # Data capture: a video recording also bundles a synchronized state log
        # (pilot cmds, sensors incl. autopilot/station-keep status) + a manifest
        # tying them together, so a pool run yields a ready-to-train dataset.
        self._video_capture_session_dir: str | None = None
        self._video_capture_owns_log = False
        self._stream_log_path: str | None = None
        # Auto-record every Optical Hold attempt (arm-cam video + the synchronized
        # streams/tracking log) so each hold is reviewable with tools/transect_replay.
        self._auto_record_holds = True
        self._hold_owns_recording = False        # this recording was started by a hold
        self._hold_recording_stop_pending = False  # disengaged before the start finished
        # 2) sensor subscriber (ROV -> topside)
        self.sensor_panel = SensorPanel()
        self.instrument_panel = InstrumentPanel()
        self.pilot_telemetry_column = PilotTelemetryColumn()
        self.pilot_telemetry_scroll = vertical_scroll_area(
            self.pilot_telemetry_column,
            object_name="pilotTelemetryScroll",
        )
        self.pilot_telemetry_scroll.setMinimumWidth(224)
        self.pilot_telemetry_scroll.setMaximumWidth(252)
        self.pilot_telemetry_scroll.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        try:
            self._refresh_gain_indicators_from_modes(self.pilot_svc.current_modes())
        except Exception:
            pass
        self.hold_test_panel = HoldTestPanel(pilot_svc=self.pilot_svc, endpoint=MANAGEMENT_RPC_ENDPOINT)
        self.raw_sensor_page = RawSensorPage(
            recording_session_provider=lambda: self._make_recording_session_dir()[0]
        )
        self.hold_test_panel.setMinimumWidth(320)
        self._sensor_thread_lock = threading.Lock()
        self._sensor_thread_pending: dict[tuple[str, str], dict] = {}
        self._sensor_thread_pending_order: list[tuple[str, str]] = []
        self._sensor_ui_pending: dict[tuple[str, str], dict] = {}
        self._sensor_ui_pending_order: list[tuple[str, str]] = []
        self._sensor_ui_max_batch = 32
        self._sensor_ui_timer = QTimer(self)
        self._sensor_ui_timer.setInterval(33)  # ~30 Hz UI refresh cap for sensor table/widgets
        self._sensor_ui_timer.timeout.connect(self._flush_sensor_ui)
        self._sensor_ui_timer.start()
        self.sensor_svc = SensorSubscriberService(
            endpoint=SENSOR_SUB_ENDPOINT,
            on_message=self._on_sensor_msg_from_thread,
            debug=False,
        )
        self.sensor_svc.start()

        # 3) video (failsafe: GUI should boot even if ROV/video isn't available yet)
        self.cam_mgr = None
        self.video_panel = None
        stream_names: list[str] = []
        try:
            if not os.path.exists(streams_path):
                # Don't block startup; just disable video.
                self.statusBar().showMessage(f"Streams config not found: {streams_path}", 10000)
            else:
                self.cam_mgr = RemoteCameraManager(streams_path)
                # Active topside video-recording mirror ports, keyed by stream
                # name. _stream_options() folds these into start_stream so the
                # recording survives a display reconnect.
                self.cam_mgr.recording_mirror_ports = {}
                try:
                    self._stereo_pairs = load_stereo_pairs(streams_path)
                    self._active_stereo_pair = self._stereo_pairs[0] if self._stereo_pairs else None
                except Exception as exc:
                    logger.warning("Could not load stereo pairs from %s: %s", streams_path, exc)
                    self._stereo_pairs = []
                    self._active_stereo_pair = None
                stream_names = self.cam_mgr.list_available()
                if stream_names:
                    self.video_panel = VideoTabs(self.cam_mgr, stream_names=stream_names)
                    self._reverse_camera_name = self._select_reverse_stream_name(stream_names)
                    self.video_panel.selectionChanged.connect(self._on_video_tab_changed)
                    self._update_capture_status_label()
                    QTimer.singleShot(1000, self._prewarm_snapshot_capture_feeds)
                else:
                    self.statusBar().showMessage("No enabled video streams in streams.json", 8000)
        except Exception as e:
            self.cam_mgr = None
            self.video_panel = None
            self.statusBar().showMessage(f"Video init failed (continuing without video): {e}", 12000)

        self._pilot_layout_count_restore = 4
        if self.video_panel is not None:
            self._pilot_layout_count_restore = int(self.video_panel.layout_count())
            tether_setter = getattr(self.video_panel, "set_tether_status", None)
            if callable(tether_setter):
                try:
                    tether_setter(False, "checking tether network")
                except Exception:
                    pass
        self._transect_layout_restore_snapshot: dict | None = None
        self._reverse_page_owns_mode: bool = False
        self._active_page_name = ""

        # layout
        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(4, 4, 4, 4)
        root.setSpacing(4)

        self._page_tabs = QTabBar()
        self._page_tabs.setDocumentMode(True)
        self._page_tabs.setExpanding(False)
        self._page_tabs.addTab("Pilot")
        self._page_tabs.addTab("Transect")
        self._page_tabs.addTab("Hold Test")
        self._page_tabs.addTab("Raw Sensors")
        self._page_tabs.addTab("Vehicle Setup")
        self._page_tabs.addTab("SSH")
        self._page_tabs.currentChanged.connect(self._on_page_tab_changed)

        top_bar = QWidget()
        top_bar_lay = QHBoxLayout(top_bar)
        top_bar_lay.setContentsMargins(0, 0, 0, 0)
        top_bar_lay.setSpacing(6)
        top_bar_lay.addWidget(self._page_tabs, 0)
        top_bar_lay.addStretch(1)
        self._competition_clock = CompetitionClock()
        top_bar_lay.addWidget(self._competition_clock, 0)
        cb_cap, cb_cap_min, cb_cap_max = self._initial_current_budget_cap()
        self._current_budget_panel = CurrentBudgetPanel(
            enabled=self._initial_current_budget_enabled(),
            budget_a=cb_cap,
            budget_min=cb_cap_min,
            budget_max=cb_cap_max,
        )
        self._current_budget_panel.toggled.connect(self._on_current_budget_toggled)
        self._current_budget_panel.budget_changed.connect(self._on_current_budget_cap_changed)
        top_bar_lay.addWidget(self._current_budget_panel, 0)
        top_bar_lay.addWidget(self._tether_top_lbl, 0)
        self._arm_disarm_btn = QPushButton()
        self._arm_disarm_btn.setObjectName("armDisarmButton")
        self._arm_disarm_btn.setMinimumWidth(132)
        self._arm_disarm_btn.clicked.connect(self._toggle_arm_disarm_from_ui)
        top_bar_lay.addWidget(self._arm_disarm_btn, 0)
        root.addWidget(top_bar, 0)
        root.addWidget(self._tether_banner, 0)

        self._page_stack = QStackedWidget()
        root.addWidget(self._page_stack, 1)

        self._pilot_page = QWidget()
        pilot_outer = QHBoxLayout(self._pilot_page)
        pilot_outer.setContentsMargins(0, 0, 0, 0)
        pilot_outer.setSpacing(2)
        self._pilot_video_host = QWidget()
        self._pilot_video_host_layout = QVBoxLayout(self._pilot_video_host)
        self._pilot_video_host_layout.setContentsMargins(0, 0, 0, 0)
        self._pilot_video_host_layout.setSpacing(0)
        if self.video_panel is not None:
            pilot_outer.addWidget(self._pilot_video_host, 1)
            pilot_outer.addWidget(self.pilot_telemetry_scroll, 0)
        else:
            # Keep the sensor/instrument widgets alive for data processing, but
            # only surface them when video is unavailable so the main piloting
            # view stays focused on cameras.
            right_col = QWidget()
            right_lay = QVBoxLayout(right_col)
            right_lay.setContentsMargins(0, 0, 0, 0)
            right_lay.addWidget(self.instrument_panel, 0)
            right_lay.addWidget(self.sensor_panel, 3)
            pilot_outer.addWidget(right_col, 1)
        self._page_stack.addWidget(self._pilot_page)

        self._transect_page = TransectPage(
            stream_names=stream_names,
            rotation_servo_enabled=self._transect_rotation_servo_enabled,
            target_blue_width_percent=self._transect_target_blue_width_percent,
            target_blue_width_min_percent=TRANSECT_TARGET_BLUE_WIDTH_PERCENT_MIN,
            target_blue_width_max_percent=TRANSECT_TARGET_BLUE_WIDTH_PERCENT_MAX,
        )
        # Default the transect view to the arm camera (the square-aspect task feed).
        transect_default = self._select_transect_stream_name(stream_names)
        if transect_default:
            self._transect_page.set_current_stream(transect_default, emit=False)
        self._transect_page.cameraSelectionChanged.connect(self._on_transect_camera_changed)
        self._transect_page.engageToggled.connect(self._on_transect_engage_toggled)
        self._transect_page.rotationServoToggled.connect(self._set_transect_rotation_servo_enabled)
        self._transect_page.targetBlueWidthChanged.connect(self._set_transect_target_blue_width_percent)
        self._transect_page.set_overlay_widget(self._transect_overlay_view)
        if self.video_panel is None:
            self._transect_page.attach_video_placeholder("Video unavailable.")
        self._page_stack.addWidget(self._transect_page)

        self._reverse_page = QWidget()
        reverse_outer = QHBoxLayout(self._reverse_page)
        reverse_outer.setContentsMargins(0, 0, 0, 0)
        reverse_outer.setSpacing(8)
        self._reverse_video_host = QWidget()
        self._reverse_video_host_layout = QVBoxLayout(self._reverse_video_host)
        self._reverse_video_host_layout.setContentsMargins(0, 0, 0, 0)
        self._reverse_video_host_layout.setSpacing(0)
        if self.video_panel is None:
            self._reverse_video_host_layout.addWidget(
                self._make_center_placeholder("Video unavailable.\nReverse drive controls can still be toggled from the View menu."),
                1,
            )
        reverse_outer.addWidget(self._reverse_video_host, 1)
        self._page_stack.addWidget(self._reverse_page)

        self._hold_test_page = QWidget()
        hold_outer = QHBoxLayout(self._hold_test_page)
        hold_outer.setContentsMargins(0, 0, 0, 0)
        hold_outer.setSpacing(8)
        self._hold_test_video_host = QWidget()
        self._hold_test_video_host_layout = QVBoxLayout(self._hold_test_video_host)
        self._hold_test_video_host_layout.setContentsMargins(0, 0, 0, 0)
        self._hold_test_video_host_layout.setSpacing(0)
        if self.video_panel is not None:
            hold_outer.addWidget(self._hold_test_video_host, 3)
        else:
            self._hold_test_video_host_layout.addWidget(
                self._make_center_placeholder("Video unavailable.\nThe Hold Test instruments will still follow telemetry."),
                1,
            )
            hold_outer.addWidget(self._hold_test_video_host, 3)
        hold_outer.addWidget(self.hold_test_panel, 1)
        self._page_stack.addWidget(self._hold_test_page)

        self._management_page = ManagementPage(endpoint=MANAGEMENT_RPC_ENDPOINT, pilot_svc=self.pilot_svc)
        self._management_page.live_limiter_changed.connect(self._sync_current_budget_panel_from_service)
        self._page_stack.addWidget(self._management_page)

        self._raw_sensor_page = self.raw_sensor_page
        self._page_stack.addWidget(self._raw_sensor_page)

        self._ssh_page = SshConsolePage(presets=default_pilot_ssh_presets(str(ROV_HOST)))
        self._page_stack.addWidget(self._ssh_page)

        if self.video_panel is not None:
            self._attach_shared_video_panel(self._pilot_video_host_layout)

        self.setCentralWidget(central)

        self._make_menu()
        self._set_center_page("pilot", announce=False)
        self._sync_reverse_action()
        self._refresh_arm_disarm_button()
        self._refresh_drive_status()
        self._refresh_video_status()
        self._refresh_tether_status_ui()
        if self._analysis_transfer_autostart:
            QTimer.singleShot(0, self._start_analysis_transfer_server)
        else:
            self._set_analysis_transfer_label("Analysis Share: OFF", "warn")

        try:
            self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
            app = QApplication.instance()
            if app is not None:
                app.installEventFilter(self)
        except Exception:
            pass

        resize_to_available_screen(self, 1440, 860, min_width=980, min_height=620, height_ratio=0.96)


    def _refresh_gain_indicators_from_modes(self, modes: dict) -> None:
        column = getattr(self, "pilot_telemetry_column", None)
        if column is None:
            return
        try:
            column.set_gains(
                back=(modes or {}).get("back_gripper_gain", (modes or {}).get("t200_wrist_gain")),
                rov=(modes or {}).get("max_gain"),
                arm=(modes or {}).get("arm_gain"),
            )
        except Exception:
            pass

    def _adjust_back_gripper_gain_from_keyboard(self, direction: float) -> None:
        try:
            step = float(self.pilot_svc.back_gripper_gain_step()) * float(direction)
        except Exception:
            step = 0.0
        if step == 0.0:
            return
        try:
            changed = bool(self.pilot_svc.adjust_back_gripper_gain(step))
            gain = float(self.pilot_svc.current_back_gripper_gain())
        except Exception:
            return
        if changed:
            pct = int(round(max(0.0, min(1.0, gain)) * 100.0))
            self._refresh_gain_indicators_from_modes(self.pilot_svc.current_modes())
            self.statusBar().showMessage(f"Back gripper gain: {pct}%  |  keys: 1 / 2", 3000)

    def _adjust_arm_gain_from_keyboard(self, direction: float) -> None:
        try:
            step = float(self.pilot_svc.arm_gain_step()) * float(direction)
        except Exception:
            step = 0.0
        if step == 0.0:
            return
        try:
            changed = bool(self.pilot_svc.adjust_arm_gain(step))
            gain = float(self.pilot_svc.current_arm_gain())
        except Exception:
            return
        if changed:
            pct = int(round(max(0.0, min(1.0, gain)) * 100.0))
            self._refresh_gain_indicators_from_modes(self.pilot_svc.current_modes())
            self.statusBar().showMessage(f"Arm gain: {pct}%  |  keys: 6 / 7", 3000)

    def _adjust_rov_gain_from_keyboard(self, direction: float) -> None:
        try:
            step = float(self.pilot_svc.max_gain_step()) * float(direction)
        except Exception:
            step = 0.0
        if step == 0.0:
            return
        try:
            changed = bool(self.pilot_svc.adjust_max_gain(step))
            gain = float(self.pilot_svc.current_max_gain())
        except Exception:
            return
        if changed:
            pct = int(round(max(0.0, min(1.0, gain)) * 100.0))
            self._refresh_gain_indicators_from_modes(self.pilot_svc.current_modes())
            self._set_status(self._gain_lbl, f"Max Gain: {pct}%")
            self.statusBar().showMessage(f"ROV motion gain: {pct}%  |  keys: - / +", 3000)

    def _toggle_lights_from_keyboard(self) -> None:
        try:
            self.pilot_svc.queue_edge(self._lights_toggle_edge)
        except Exception:
            return
        self.statusBar().showMessage(
            f"Lights toggle sent  |  key: {self._lights_toggle_shortcut_text.upper()}",
            3000,
        )

    def _arm_disarm_button_state(self) -> tuple[str, bool, bool]:
        armed_known = bool(self._last_hb_ts > 0 and isinstance(self._last_hb, dict) and "armed" in self._last_hb)
        armed = bool((self._last_hb or {}).get("armed", False)) if armed_known else False
        action_text = "Disarm" if armed else "Arm"
        if not armed_known:
            action_text = "Arm/Disarm"
        return action_text, armed, armed_known

    def _refresh_arm_disarm_button(self) -> None:
        btn = getattr(self, "_arm_disarm_btn", None)
        if btn is None:
            return
        action_text, armed, armed_known = self._arm_disarm_button_state()
        shortcut_text = str(self._arm_disarm_shortcut_text or "O").upper()
        btn.setText(f"{action_text} ({shortcut_text})")
        btn.setToolTip(
            f"Send the ROV arm/disarm toggle. Keyboard shortcut: {shortcut_text}."
        )
        btn.setProperty("armed", "true" if armed else "false")
        btn.setProperty("known", "true" if armed_known else "false")
        try:
            btn.style().unpolish(btn)
            btn.style().polish(btn)
            btn.update()
        except Exception:
            pass

    def _toggle_arm_disarm_from_ui(self, *_args) -> None:
        try:
            self.pilot_svc.queue_edge(self._arm_disarm_edge)
        except Exception as exc:
            self.statusBar().showMessage(f"Could not send arm/disarm toggle: {exc}", 5000)
            return
        shortcut_text = str(self._arm_disarm_shortcut_text or "O").upper()
        self.statusBar().showMessage(
            f"Arm/disarm toggle sent  |  key: {shortcut_text}",
            3000,
        )

    def _send_arm_to_park_from_keyboard(self) -> None:
        """Command the differential arm straight to its park pose (key A)."""
        svc = getattr(self, "pilot_svc", None)
        mover = getattr(svc, "move_arm_to_park", None) if svc is not None else None
        if mover is None:
            return
        try:
            mover()
        except Exception as exc:
            self.statusBar().showMessage(f"Could not send arm to park: {exc}", 5000)
            return
        shortcut_text = str(self._arm_park_shortcut_text or "A").upper()
        if getattr(self, "_arm_integrator_armed", None) is False:
            msg = f"Arm already at park (disarmed)  |  key: {shortcut_text}"
        else:
            msg = f"Arm sent to park pose  |  key: {shortcut_text}"
        self.statusBar().showMessage(msg, 3000)

    def _sync_arm_integrator_armed(self, hb: dict) -> None:
        """Mirror the ROV's armed state from the heartbeat into the pilot arm
        integrator. While disarmed the integrator freezes and snaps to the park
        pose, so the servo target cannot drift and snap on re-arm. Only acts on a
        real change, and ignores heartbeats that omit the armed flag."""
        if not isinstance(hb, dict) or "armed" not in hb:
            return
        armed = bool(hb.get("armed", False))
        if armed == self._arm_integrator_armed:
            return
        self._arm_integrator_armed = armed
        try:
            self.pilot_svc.set_armed(armed)
        except Exception:
            pass

    @staticmethod
    def _widget_or_parent_is_text_entry(widget) -> bool:
        text_entry_types = (QLineEdit, QPlainTextEdit, QTextEdit, QAbstractSpinBox)
        current = widget if isinstance(widget, QWidget) else None
        while current is not None:
            if isinstance(current, text_entry_types):
                return True
            if isinstance(current, QComboBox):
                try:
                    if current.isEditable():
                        return True
                except Exception:
                    return True
            try:
                current = current.parentWidget()
            except Exception:
                return False
        return False

    def _keyboard_vehicle_shortcuts_suppressed(self, obj=None) -> bool:
        if getattr(self, "_active_page_name", "") == "ssh":
            return True
        try:
            if self._widget_or_parent_is_text_entry(obj):
                return True
        except Exception:
            pass
        try:
            if self._widget_or_parent_is_text_entry(QApplication.focusWidget()):
                return True
        except Exception:
            pass
        return False

    def _keyboard_competition_clock_suppressed(self, obj=None) -> bool:
        try:
            if self._widget_or_parent_is_text_entry(obj):
                return True
        except Exception:
            pass
        try:
            if self._widget_or_parent_is_text_entry(QApplication.focusWidget()):
                return True
        except Exception:
            pass
        return False

    def _start_competition_clock_from_keyboard(self) -> None:
        clock = getattr(self, "_competition_clock", None)
        if clock is None:
            return
        outcome = clock.start_from_keyboard()
        messages = {
            "started": "Competition clock started",
            "running": "Competition clock already running",
            "disabled": "Competition clock disabled; enable it with the mouse",
            "complete": "Competition clock finished; reset it with the mouse",
        }
        try:
            self.statusBar().showMessage(messages.get(outcome, "Competition clock"), 2500)
        except Exception:
            pass

    def eventFilter(self, obj, event):
        try:
            et = event.type()
            if et in (QEvent.Type.KeyPress, QEvent.Type.KeyRelease):
                if hasattr(event, "isAutoRepeat") and event.isAutoRepeat():
                    return False
                if et == QEvent.Type.KeyPress:
                    if event.key() == Qt.Key.Key_F11:
                        self._toggle_fullscreen_mode()
                        return True
                    if event.key() == Qt.Key.Key_Escape and self.isFullScreen():
                        self.set_fullscreen_mode(False)
                        return True
                    if event.key() == Qt.Key.Key_M and not self._keyboard_competition_clock_suppressed(obj):
                        self._start_competition_clock_from_keyboard()
                        return True
                if self._keyboard_vehicle_shortcuts_suppressed(obj):
                    return False
                if et == QEvent.Type.KeyPress and getattr(self, "_active_page_name", "") == "transect":
                    if event.key() == Qt.Key.Key_T:
                        self._toggle_transect_stopwatch_from_keyboard()
                        return True
                    if event.key() == Qt.Key.Key_R:
                        self._reset_transect_stopwatch_from_keyboard()
                        return True
                if et == QEvent.Type.KeyPress:
                    if event.key() == Qt.Key.Key_R:
                        self._toggle_reverse_mode()
                        return True
                    try:
                        shortcut_text = self._lights_toggle_shortcut_text.upper()
                    except Exception:
                        shortcut_text = "L"
                    try:
                        arm_shortcut_text = self._arm_disarm_shortcut_text.upper()
                    except Exception:
                        arm_shortcut_text = "O"
                    try:
                        arm_park_shortcut_text = self._arm_park_shortcut_text.upper()
                    except Exception:
                        arm_park_shortcut_text = "A"
                    if event.text().upper() == arm_shortcut_text:
                        self._toggle_arm_disarm_from_ui()
                        return True
                    if event.text().upper() == arm_park_shortcut_text:
                        self._send_arm_to_park_from_keyboard()
                        return True
                    if event.text().upper() == shortcut_text:
                        self._toggle_lights_from_keyboard()
                        return True
                    if event.key() == Qt.Key.Key_C:
                        self._toggle_capture_mode()
                        return True
                    if event.key() == Qt.Key.Key_N:
                        self._start_new_stereo_session()
                        return True
                    if event.key() == Qt.Key.Key_B:
                        self._toggle_recording_for_mode()
                        return True
                    if event.key() == Qt.Key.Key_K:
                        self._toggle_station_keep_from_keyboard()
                        return True
                    if event.key() == Qt.Key.Key_P:
                        self._toggle_roll_pitch_level_from_keyboard()
                        return True
                    direction = self._back_gripper_gain_shortcuts.get(event.key())
                    if direction is not None:
                        self._adjust_back_gripper_gain_from_keyboard(direction)
                        return True
                    direction = self._arm_gain_shortcuts.get(event.key())
                    if direction is not None:
                        self._adjust_arm_gain_from_keyboard(direction)
                        return True
                    direction = self._rov_gain_shortcuts.get(event.key())
                    if direction is not None:
                        text = str(event.text() or "")
                        if direction > 0 and text not in ("", "+"):
                            return False
                        if direction < 0 and text not in ("", "-"):
                            return False
                        self._adjust_rov_gain_from_keyboard(direction)
                        return True
                    return False
        except Exception:
            pass
        return super().eventFilter(obj, event)

    @staticmethod
    def _finite_float(value) -> float | None:
        try:
            numeric = float(value)
        except Exception:
            return None
        if not math.isfinite(numeric):
            return None
        return numeric

    @staticmethod
    def _wrap_degrees(deg: float) -> float:
        return ((float(deg) + 180.0) % 360.0) - 180.0

    @staticmethod
    def _pilot_axis_value(msg: dict, axis_name: str) -> float:
        axes = (msg or {}).get("axes", {}) or {}
        axis_key = str(axis_name or "").strip().lower()
        try:
            return float(axes.get(axis_key, 0.0) or 0.0)
        except Exception:
            return 0.0

    def _latest_depth_m(self) -> tuple[float | None, bool]:
        stale = (time.time() - float(self._last_depth_ts)) > float(DEPTH_HOLD_SENSOR_STALE_S)
        if (self._last_depth or {}).get("error"):
            return None, True
        depth_m = self._finite_float((self._last_depth or {}).get("depth_m"))
        if depth_m is None:
            return None, True
        return float(depth_m), stale

    def _runtime_depth_hold_status(self) -> dict:
        runtime = self._last_autopilot_status.get("depth_hold")
        return dict(runtime) if isinstance(runtime, dict) else {}

    def _format_depth_hold_status(self, msg: dict, depth_hold: bool) -> str:
        if not depth_hold:
            return "Depth Hold: OFF"

        depth_m, stale = self._latest_depth_m()
        modes = (msg or {}).get("modes", {}) or {}
        ap = modes.get("autopilot") if isinstance(modes.get("autopilot"), dict) else {}
        targets = ap.get("targets") if isinstance(ap.get("targets"), dict) else {}
        runtime = self._runtime_depth_hold_status()
        target = self._finite_float(targets.get("depth_m"))
        if target is None:
            target = self._finite_float(runtime.get("target_m"))
        if target is None:
            target = self._dh_target_m
        elif bool(runtime):
            self._dh_target_m = float(target)

        z_txt = "-" if depth_m is None else f"{float(depth_m):.2f}m"
        target_txt = "-" if target is None else f"{float(target):.2f}m"
        text = f"Depth Hold: z {z_txt} -> set {target_txt}"
        if str(runtime.get("reason", "")).strip().lower() == "manual_override":
            text += " [manual]"
        if stale:
            text += " [DEPTH STALE]"
        return text

    def _latest_attitude_yaw_deg(self) -> tuple[float | None, bool]:
        stale = (time.time() - float(self._last_attitude_ts)) > float(YAW_HOLD_ATTITUDE_STALE_S)
        if stale:
            return None, True
        yaw = self._finite_float((self._last_attitude or {}).get("yaw_deg"))
        if yaw is None:
            return None, True
        return self._wrap_degrees(yaw), False

    def _runtime_axis_status(self, axis: str) -> dict:
        attitude = self._last_autopilot_status.get("attitude")
        if not isinstance(attitude, dict):
            return {}
        axes = attitude.get("axes")
        if not isinstance(axes, dict):
            return {}
        runtime = axes.get(str(axis))
        return dict(runtime) if isinstance(runtime, dict) else {}

    def _format_yaw_hold_status(self, msg: dict, yaw_hold: bool) -> str:
        if not yaw_hold:
            return "Yaw Hold: OFF"
        yaw_deg, stale = self._latest_attitude_yaw_deg()
        modes = (msg or {}).get("modes", {}) or {}
        ap = modes.get("autopilot") if isinstance(modes.get("autopilot"), dict) else {}
        targets = ap.get("targets") if isinstance(ap.get("targets"), dict) else {}
        runtime = self._runtime_axis_status("yaw")
        target = self._finite_float(targets.get("yaw_deg"))
        if target is None:
            target = self._finite_float(runtime.get("target_deg"))
        if target is None:
            target = self._yh_target_deg
        elif bool(runtime):
            self._yh_target_deg = self._wrap_degrees(target)

        yaw_txt = "-" if yaw_deg is None else f"{float(yaw_deg):.1f}deg"
        target_txt = "-" if target is None else f"{float(self._wrap_degrees(target)):.1f}deg"
        text = f"Yaw Hold: y {yaw_txt} -> set {target_txt}"
        if str(runtime.get("reason", "")).strip().lower() == "manual_override":
            text += " [manual]"
        if stale:
            text += " [ATT STALE]"
        return text

    def _format_station_keep_status(self) -> str:
        """Optical-hold status for the drive bar; empty string when disengaged."""
        svc = getattr(self, "pilot_svc", None)
        try:
            enabled = bool(svc.is_station_keep_enabled()) if svc is not None else False
        except Exception:
            enabled = False
        if not enabled:
            return ""
        sk = self._last_autopilot_status.get("station_keep") if isinstance(self._last_autopilot_status, dict) else None
        if not isinstance(sk, dict):
            return "Optical Hold: ON (no data)"
        if bool(sk.get("active")):
            return "Optical Hold: ACTIVE"
        reason = str(sk.get("reason", "")).strip().lower()
        label = {
            "no_lock": "NO LOCK",
            "stale_lock": "STALE",
            "locked_idle": "LOCK",
            "active": "ACTIVE",
            "disabled": "OFF",
            "off": "ON",
        }.get(reason, reason.upper() or "ON")
        return f"Optical Hold: {label}"

    # Background thread to UI thread.
    def _on_sensor_msg_from_thread(self, msg: dict):
        # called in sensor thread
        if self._stream_recorder is not None:
            self._stream_recorder.record("sensors", msg)
        derived_msgs = []
        try:
            derived_msgs = self.raw_sensor_page.record_message(msg) or []
        except Exception:
            pass
        self._queue_sensor_msg_from_thread(msg)
        for derived in derived_msgs:
            if self._stream_recorder is not None:
                self._stream_recorder.record("attitude", derived)
            self._queue_sensor_msg_from_thread(derived)


    def _on_pilot_status_from_thread(self, status: dict):
        # Called from the pilot publisher thread; marshal to UI thread.
        self.pilot_status_sig.emit(status)

    def _on_pilot_msg_from_thread(self, msg: dict):
        # Called from the pilot publisher thread; marshal to UI thread.
        if self._stream_recorder is not None:
            self._stream_recorder.record("pilot", msg)
        self.pilot_msg_sig.emit(msg)

    def _handle_pilot_msg_on_ui(self, msg: dict):
        try:
            self._last_pilot_msg_ts = time.time()
            self._last_pilot_msg = dict(msg or {})
        except Exception:
            pass
        try:
            edges = (msg or {}).get("edges", {}) or {}
            if str(edges.get("x", "")).strip().lower() == "down":
                self._capture_from_current_mode()
            if str(edges.get("b", "")).strip().lower() == "down":
                self._toggle_recording_for_mode()
        except Exception as exc:
            logger.exception("Snapshot trigger failed: %s", exc)

        # Update mode indicator from locally-transmitted modes.
        try:
            modes = (msg or {}).get("modes", {}) or {}
            dh = bool(modes.get("depth_hold", False))
            rp_level = bool(modes.get("roll_pitch_level", False))
            yaw_hold = bool(modes.get("yaw_hold", False))
            reverse = bool(modes.get("reverse", False))
            if reverse != self._reverse_enabled:
                self._reverse_enabled = reverse
                self._sync_reverse_action()

            # Pilot max gain display (Y/A adjusts this topside).
            try:
                mg = modes.get("max_gain", None)
                if mg is not None:
                    pct = int(round(max(0.0, min(1.0, float(mg))) * 100.0))
                    self._set_status(self._gain_lbl, f"Max Gain: {pct}%")
            except Exception:
                pass
            self._refresh_gain_indicators_from_modes(modes)

            self._depth_hold_status_text = self._format_depth_hold_status(msg or {}, dh)
            self._attitude_hold_status_text = "RP Level: ON" if rp_level else "RP Level: OFF"
            self._yaw_hold_status_text = self._format_yaw_hold_status(msg or {}, yaw_hold)

        except Exception:
            self._depth_hold_status_text = "Depth Hold: -"
            self._attitude_hold_status_text = "RP Level: -"
            self._yaw_hold_status_text = "Yaw Hold: -"
        self._refresh_drive_status()
        self._refresh_video_status()

    def _handle_pilot_status_on_ui(self, status: dict):
        self._last_ctrl_status = status or {'controller': 'unknown'}
        try:
            self._reverse_enabled = bool((status or {}).get('reverse', self._reverse_enabled))
        except Exception:
            pass
        try:
            if "roll_pitch_level" in (status or {}):
                self._attitude_hold_status_text = "RP Level: ON" if bool((status or {}).get("roll_pitch_level")) else "RP Level: OFF"
            if "yaw_hold" in (status or {}):
                self._yaw_hold_status_text = "Yaw Hold: ON" if bool((status or {}).get("yaw_hold")) else "Yaw Hold: OFF"
            self._refresh_gain_indicators_from_modes(status or {})
        except Exception:
            pass
        self._sync_reverse_action()
        state = (status or {}).get('controller', 'unknown')
        if state == 'connected':
            name = (status or {}).get('name') or 'controller'
            reverse_tag = " [REV]" if self._reverse_enabled else ""
            self._set_status(self._ctrl_lbl, f"Controller: OK ({name}){reverse_tag}")
        elif state == 'disconnected':
            err = (status or {}).get('error') or 'not connected'
            self._set_status(self._ctrl_lbl, f"Controller: - ({err})")
        elif state == 'stopped':
            self._set_status(self._ctrl_lbl, "Controller: stopped")
        else:
            self._set_status(self._ctrl_lbl, f"Controller: {state}")
        self._refresh_drive_status()

    def _initial_current_budget_enabled(self) -> bool:
        """Initial checkbox state: the pilot service's current mode, else config."""
        svc = getattr(self, "pilot_svc", None)
        if svc is not None:
            try:
                return bool(svc.is_current_budget_enabled())
            except Exception:
                pass
        try:
            from config import CURRENT_BUDGET_DEFAULT
            return bool(CURRENT_BUDGET_DEFAULT)
        except Exception:
            return True

    def _on_current_budget_toggled(self, enabled: bool) -> None:
        """Pilot toggled the intelligent current limiter from the top bar."""
        svc = getattr(self, "pilot_svc", None)
        if svc is not None:
            try:
                svc.set_current_budget_enabled(bool(enabled))
            except Exception:
                pass
        try:
            self.statusBar().showMessage(
                f"Intelligent current limiter {'ON' if enabled else 'OFF'}", 2500
            )
        except Exception:
            pass

    def _initial_current_budget_cap(self) -> tuple:
        """Initial (value, min, max) amps for the cap box: pilot service, else config."""
        svc = getattr(self, "pilot_svc", None)
        value = None
        lo, hi = 5.0, 40.0
        if svc is not None:
            try:
                value = float(svc.current_budget_max_a())
                lo, hi = (float(x) for x in svc.current_budget_max_a_bounds())
            except Exception:
                value = None
        if value is None:
            try:
                from config import (
                    CURRENT_BUDGET_MAX_A_DEFAULT,
                    CURRENT_BUDGET_MAX_A_MIN,
                    CURRENT_BUDGET_MAX_A_MAX,
                )
                value = float(CURRENT_BUDGET_MAX_A_DEFAULT)
                lo, hi = float(CURRENT_BUDGET_MAX_A_MIN), float(CURRENT_BUDGET_MAX_A_MAX)
            except Exception:
                value = 22.0
        return float(value), float(lo), float(hi)

    def _sync_current_budget_panel_from_service(self) -> None:
        """Mirror the top-bar limiter panel after a change made in Vehicle Setup."""
        panel = getattr(self, "_current_budget_panel", None)
        svc = getattr(self, "pilot_svc", None)
        if panel is None or svc is None:
            return
        try:
            panel.set_enabled_state(bool(svc.is_current_budget_enabled()))
        except Exception:
            pass
        try:
            panel.set_budget_value(float(svc.current_budget_max_a()))
        except Exception:
            pass

    def _on_current_budget_cap_changed(self, amps: float) -> None:
        """Pilot changed the live current cap from the top bar."""
        svc = getattr(self, "pilot_svc", None)
        if svc is not None:
            try:
                svc.set_current_budget_max_a(float(amps))
            except Exception:
                pass
        try:
            self.statusBar().showMessage(f"Current cap set to {float(amps):.0f} A", 2000)
        except Exception:
            pass

    def _update_current_budget_readout(self, msg: dict) -> None:
        """Refresh the live estimated-draw readout from autopilot_status telemetry."""
        panel = getattr(self, "_current_budget_panel", None)
        if panel is None:
            return
        try:
            status = ((msg or {}).get("control") or {}).get("status") or {}
            cb = status.get("current_budget") or {}
            if not cb or not cb.get("enabled"):
                # ROV not running the limiter (disarmed, or config master off).
                panel.clear_estimate()
                return
            predicted = cb.get("predicted_before_a")
            panel.update_estimate(
                None if predicted is None else float(predicted),
                active=cb.get("active"),
                applied=bool(cb.get("applied")),
                budget_a=cb.get("budget_a"),
            )
        except Exception:
            pass

    def _handle_sensor_msg_on_ui(self, msg: dict):
        import time
        typ = msg.get("type")
        if msg.get("sensor") == "heartbeat" or typ == "heartbeat":
            now_ts = time.time()
            if self._prev_hb_rx_ts is not None:
                dt = now_ts - float(self._prev_hb_rx_ts)
                if 0.05 < dt < 10.0:
                    if self._hb_period_ema_s is None:
                        self._hb_period_ema_s = float(dt)
                    else:
                        self._hb_period_ema_s = (0.8 * float(self._hb_period_ema_s)) + (0.2 * float(dt))
            self._prev_hb_rx_ts = now_ts
            self._last_hb_ts = now_ts
            self._last_hb = msg
            self._sync_arm_integrator_armed(msg)
            self._refresh_arm_disarm_button()
        elif typ == "net" or msg.get("sensor") == "network":
            self._last_net_ts = time.time()
            self._last_net = msg
        else:
            self._last_sensor_ts = time.time()

            if typ == "autopilot_status":
                self._last_autopilot_status_ts = time.time()
                self._last_autopilot_status = dict(msg or {})
                self._update_current_budget_readout(msg)
                runtime_depth = self._runtime_depth_hold_status()
                target = self._finite_float(runtime_depth.get("target_m"))
                if target is not None:
                    self._dh_target_m = float(target)
                runtime_yaw = self._runtime_axis_status("yaw")
                yaw_target = self._finite_float(runtime_yaw.get("target_deg"))
                if yaw_target is not None:
                    self._yh_target_deg = self._wrap_degrees(yaw_target)
                # Reflect live optical-hold state (NO LOCK / ACTIVE / ...).
                try:
                    if self.pilot_svc.is_station_keep_enabled():
                        self._refresh_drive_status()
                except Exception:
                    pass

            # Update a compact depth readout in the status bar.
            if typ == "external_depth":
                self._last_depth_ts = time.time()
                self._last_depth = msg or {}
                sensor = (msg or {}).get("sensor", "depth")
                if (msg or {}).get("error"):
                    self._set_status(self._depth_lbl, f"Depth: {sensor} (ERR)")
                else:
                    try:
                        d = (msg or {}).get("depth_m", None)
                        p = (msg or {}).get("pressure_mbar", None)
                        t = (msg or {}).get("temperature_c", None)
                        if d is None:
                            self._set_status(self._depth_lbl, f"Depth: {sensor} -")
                        else:
                            s = f"Depth: {sensor} {float(d):.2f}m"
                            if p is not None:
                                s += f" {float(p):.0f}mbar"
                            if t is not None:
                                s += f" {float(t):.1f}C"
                            self._set_status(self._depth_lbl, s)
                    except Exception:
                        self._set_status(self._depth_lbl, f"Depth: {sensor} -")

            if typ == "attitude":
                yaw = self._finite_float((msg or {}).get("yaw_deg"))
                if yaw is not None:
                    self._last_attitude_ts = time.time()
                    self._last_attitude = dict(msg or {})

            # Update a compact power readout in the status bar.
            if typ == "power":
                self._last_power_ts = time.time()
                self._last_power = msg or {}
                if (msg or {}).get("error"):
                    self._set_status(self._power_lbl, "Power: (ERR)")
                else:
                    try:
                        v = float((msg or {}).get("voltage_v", 0.0) or 0.0)
                        a = float((msg or {}).get("current_a", 0.0) or 0.0)
                        w = float((msg or {}).get("power_w", v * a) or (v * a))
                        ok = bool((msg or {}).get("ok", True))
                        held = bool((msg or {}).get("held", False))
                        s = f"Power: {v:.2f}V {a:.2f}A {w:.0f}W"
                        if held:
                            s += " (hold)"
                        elif not ok:
                            s += " (check)"
                        self._set_status(self._power_lbl, s)
                    except Exception:
                        self._set_status(self._power_lbl, "Power: -")

        self._queue_sensor_ui_msg(msg)

    def _queue_sensor_ui_msg(self, msg: dict) -> None:
        try:
            sensor = str((msg or {}).get("sensor", "unknown"))
            typ = str((msg or {}).get("type", "-"))
            key = (sensor, typ)
            if key not in self._sensor_ui_pending:
                self._sensor_ui_pending_order.append(key)
            self._sensor_ui_pending[key] = dict(msg or {})
        except Exception:
            pass

    def _queue_sensor_msg_from_thread(self, msg: dict) -> None:
        """Coalesce telemetry before it becomes Qt UI work."""
        try:
            sensor = str((msg or {}).get("sensor", "unknown"))
            typ = str((msg or {}).get("type", "-"))
            key = (sensor, typ)
            payload = dict(msg or {})
            with self._sensor_thread_lock:
                if key not in self._sensor_thread_pending:
                    self._sensor_thread_pending_order.append(key)
                self._sensor_thread_pending[key] = payload
        except Exception:
            pass

    def _drain_sensor_thread_msgs(self) -> None:
        try:
            with self._sensor_thread_lock:
                order = list(self._sensor_thread_pending_order)
                pending = dict(self._sensor_thread_pending)
                self._sensor_thread_pending_order.clear()
                self._sensor_thread_pending.clear()
        except Exception:
            return
        for key in order:
            msg = pending.get(key)
            if isinstance(msg, dict):
                self._handle_sensor_msg_on_ui(msg)

    def _flush_sensor_ui(self) -> None:
        """Apply coalesced sensor updates to UI widgets at a bounded rate."""
        try:
            self._drain_sensor_thread_msgs()
            n = 0
            while self._sensor_ui_pending_order and n < int(self._sensor_ui_max_batch):
                key = self._sensor_ui_pending_order.pop(0)
                msg = self._sensor_ui_pending.pop(key, None)
                if not isinstance(msg, dict):
                    continue
                try:
                    self.pilot_telemetry_column.update_from_sensor(msg)
                except Exception:
                    pass
                try:
                    self.instrument_panel.update_from_sensor(msg)
                except Exception:
                    pass
                try:
                    self.hold_test_panel.update_from_sensor(msg)
                except Exception:
                    pass
                try:
                    self.raw_sensor_page.update_from_sensor(msg)
                except Exception:
                    pass
                try:
                    self.sensor_panel.upsert_sensor(msg)
                except Exception:
                    pass
                n += 1
        except Exception:
            pass

    def _update_link_status(self):
        import time
        now = time.time()

        # Prefer heartbeat if present, fall back to any sensor traffic.
        hb_age = None
        if self._last_hb_ts > 0:
            hb_age = now - self._last_hb_ts
        sensor_age = None
        if self._last_sensor_ts > 0:
            sensor_age = now - self._last_sensor_ts

        # Determine link state from heartbeat when available. The heartbeat is
        # typically ~1 Hz, so a 0.5 s "OK" threshold can visibly flap between
        # OK/WARN even when the link is healthy. Use a cadence-aware threshold +
        # light hysteresis to avoid false UI flicker.
        age = hb_age if hb_age is not None else sensor_age
        if self._hb_period_ema_s is None:
            hb_period = 1.0
        else:
            hb_period = max(0.2, min(5.0, float(self._hb_period_ema_s)))

        ok_th = max(0.9, 1.35 * hb_period)
        warn_th = max(2.5, 3.25 * hb_period)
        # Hysteresis margin keeps the label from oscillating on threshold edges.
        margin = 0.20 * hb_period

        prev = str(getattr(self, "_link_state_last", "NO DATA"))
        if age is None:
            status = "NO DATA"
        else:
            # Start with nominal thresholds.
            if age < ok_th:
                status = "OK"
            elif age < warn_th:
                status = "WARN"
            else:
                status = "LOST"

            # Hysteresis based on previous state.
            if prev == "OK" and age < (ok_th + margin):
                status = "OK"
            elif prev == "WARN":
                if age < (ok_th + margin):
                    status = "OK"
                elif age < (warn_th + margin):
                    status = "WARN"
            elif prev == "LOST" and age < (warn_th - margin):
                status = "WARN" if age >= ok_th else "OK"

        self._link_state_last = status
        try:
            if self.video_panel is not None:
                self.video_panel.set_rov_link_status(status)
        except Exception:
            pass

        parts = [f"Heartbeat: {status}"]
        if hb_age is not None:
            armed = bool(self._last_hb.get("armed", False))
            pilot_age = self._last_hb.get("pilot_age", None)
            if pilot_age is not None:
                try:
                    parts.append(f"pilot_age={float(pilot_age):.2f}s")
                except Exception:
                    parts.append(f"pilot_age={pilot_age}")
            if self._hb_period_ema_s is not None:
                parts.append(f"hb~{(1.0/max(1e-3,float(self._hb_period_ema_s))):.1f}Hz")
            parts.append("ARMED" if armed else "disarmed")
            try:
                arm_pitch, arm_wrist = self.pilot_svc.arm_position()
            except Exception:
                arm_pitch, arm_wrist = 0.0, 0.0
            parts.append(
                f"arm_pitch={arm_pitch:+.2f} arm_wrist={arm_wrist:+.2f}"
            )
        elif sensor_age is not None:
            parts.append(f"sensor_age={sensor_age:.2f}s")
        else:
            parts.append(f"host={self._rov_host}")

        self._set_status(self._link_lbl, " | ".join(parts))
        self._refresh_arm_disarm_button()
        try:
            if status == "OK":
                self._link_lbl.setStyleSheet("color: #9be89b;")
            elif status == "WARN":
                self._link_lbl.setStyleSheet("color: #ffd38a;")
            elif status == "LOST":
                self._link_lbl.setStyleSheet("color: #ff8d8d; font-weight: bold;")
            else:
                self._link_lbl.setStyleSheet("")
        except Exception:
            pass

        # Controller freshness indicator: the controller can appear "connected"
        # but the publisher thread may be wedged or no pilot frames may be making
        # it to the UI anymore. Mark it stale without waiting for a manual restart.
        try:
            ctrl_state = str((self._last_ctrl_status or {}).get("controller", "unknown"))
            if ctrl_state == "connected":
                age = None
                if self._last_pilot_msg_ts > 0:
                    age = max(0.0, now - float(self._last_pilot_msg_ts))
                if age is None:
                    # no pilot frame yet after connect: tolerate a short startup window
                    pass
                elif age > 1.5:
                    name = (self._last_ctrl_status or {}).get("name") or "controller"
                    self._set_status(self._ctrl_lbl, f"Controller: STALE ({name}, age={age:.1f}s)")
        except Exception:
            pass

        try:
            self._refresh_video_status(force=False)
        except Exception:
            self._set_status(self._video_lbl, "Camera: -")

        # Network indicator (lightweight; throttled internally)
        try:
            self._update_network_status()
        except Exception:
            pass

    def _update_netdiag_snapshot(self, **kwargs) -> None:
        try:
            with self._netdiag_lock:
                cur = dict(self._netdiag)
                cur.update(kwargs)
                self._netdiag = cur
        except Exception:
            pass

    def _netdiag_probe_loop(self) -> None:
        """Low-overhead UDP echo probe to estimate RTT/jitter/loss to the ROV."""
        hist = deque(maxlen=24)  # RTTs in ms, or None on timeout/loss
        seq = 0
        sock = None
        while not self._netdiag_stop.is_set():
            t_cycle = time.time()
            try:
                if sock is None:
                    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    sock.settimeout(0.20)
                    try:
                        sock.setsockopt(socket.IPPROTO_IP, socket.IP_TOS, 0xB8)
                    except Exception:
                        pass
                t0 = time.time()
                payload = f"{t0:.6f}|{seq}".encode("ascii")
                seq += 1
                sock.sendto(payload, (self._rov_host, int(self._netdiag_port)))
                data, _ = sock.recvfrom(4096)
                t1 = time.time()
                if not data:
                    raise RuntimeError("empty")
                rtt_ms = (t1 - t0) * 1000.0
                hist.append(rtt_ms)
                vals = [v for v in hist if isinstance(v, (int, float))]
                loss_pct = (100.0 * (len(hist) - len(vals)) / len(hist)) if hist else 0.0
                avg = (sum(vals) / len(vals)) if vals else None
                if len(vals) >= 2:
                    diffs = [abs(vals[i] - vals[i - 1]) for i in range(1, len(vals))]
                    jitter = (sum(diffs) / len(diffs)) if diffs else 0.0
                else:
                    jitter = None
                self._update_netdiag_snapshot(
                    ts=t1,
                    ok=True,
                    err=None,
                    last_rtt_ms=float(rtt_ms),
                    avg_rtt_ms=(None if avg is None else float(avg)),
                    jitter_ms=(None if jitter is None else float(jitter)),
                    loss_pct=float(loss_pct),
                )
            except Exception as e:
                hist.append(None)
                vals = [v for v in hist if isinstance(v, (int, float))]
                loss_pct = (100.0 * (len(hist) - len(vals)) / len(hist)) if hist else None
                self._update_netdiag_snapshot(ts=time.time(), ok=False, err=str(e), loss_pct=loss_pct)
                try:
                    if sock is not None:
                        sock.close()
                except Exception:
                    pass
                sock = None

            sleep_s = 0.5 - (time.time() - t_cycle)
            if sleep_s > 0:
                self._netdiag_stop.wait(sleep_s)

        try:
            if sock is not None:
                sock.close()
        except Exception:
            pass

    def _get_netdiag_snapshot(self) -> dict:
        try:
            with self._netdiag_lock:
                return dict(self._netdiag)
        except Exception:
            return {}

    def _tether_prefix(self) -> str:
        parts = str(self._tether_host or "").split(".")
        if len(parts) >= 3:
            return ".".join(parts[:3]) + "."
        return "192.168.1."

    def _tether_local_candidates(self) -> list[LocalAddr]:
        prefix = self._tether_prefix()
        expected = str(self._tether_windows_host or "").strip()
        remote = str(self._tether_host or "").strip()
        candidates: list[LocalAddr] = []
        for addr in list_local_ipv4_addrs():
            ip_text = str(getattr(addr, "ip", "") or "").strip()
            if not ip_text or ip_text == remote:
                continue
            if ip_text == expected or ip_text.startswith(prefix):
                candidates.append(addr)

        def score(addr: LocalAddr) -> tuple[int, str]:
            ip_text = str(getattr(addr, "ip", "") or "")
            iface = str(getattr(addr, "iface", "") or "").lower()
            value = 0
            if ip_text == expected:
                value += 100
            if getattr(addr, "is_wifi", None) is False:
                value += 20
            elif getattr(addr, "is_wifi", None) is True:
                value -= 40
            if any(token in iface for token in ("ethernet", "asix", "usb", "gbe", "lan")):
                value += 8
            return (value, ip_text)

        candidates.sort(key=score, reverse=True)
        return candidates

    @staticmethod
    def _tcp_probe_from(local_ip: str, remote_host: str, remote_port: int, timeout_s: float) -> tuple[bool, str | None]:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.settimeout(max(0.05, float(timeout_s)))
            sock.bind((str(local_ip), 0))
            sock.connect((str(remote_host), int(remote_port)))
            return True, None
        except Exception as exc:
            return False, str(exc)
        finally:
            try:
                sock.close()
            except Exception:
                pass

    def _probe_tether_once(self) -> dict:
        now = time.time()
        snapshot = {
            "ts": now,
            "ready": False,
            "host": self._tether_host,
            "local_ip": None,
            "iface": None,
            "port": None,
            "reason": "",
        }
        try:
            candidates = self._tether_local_candidates()
        except Exception as exc:
            snapshot["reason"] = f"could not inspect local adapters: {exc}"
            return snapshot

        if not candidates:
            snapshot["reason"] = f"local tether IP {self._tether_windows_host} missing"
            return snapshot

        first = candidates[0]
        snapshot["local_ip"] = str(getattr(first, "ip", "") or "")
        snapshot["iface"] = str(getattr(first, "iface", "") or "")
        last_error = ""
        for candidate in candidates:
            local_ip = str(getattr(candidate, "ip", "") or "").strip()
            if not local_ip:
                continue
            for port in self._tether_probe_ports:
                ok, err = self._tcp_probe_from(
                    local_ip,
                    self._tether_host,
                    int(port),
                    self._tether_probe_timeout_s,
                )
                if ok:
                    snapshot.update(
                        {
                            "ready": True,
                            "local_ip": local_ip,
                            "iface": str(getattr(candidate, "iface", "") or ""),
                            "port": int(port),
                            "reason": "",
                        }
                    )
                    return snapshot
                if err:
                    last_error = err

        ports = ",".join(str(port) for port in self._tether_probe_ports)
        detail = f"{self._tether_host} not reachable on ports {ports}"
        if last_error:
            detail += f" ({last_error})"
        snapshot["reason"] = detail
        return snapshot

    def _set_tether_status_snapshot(self, snapshot: dict) -> None:
        try:
            with self._tether_status_lock:
                self._tether_status = dict(snapshot or {})
        except Exception:
            pass

    def _get_tether_status_snapshot(self) -> dict:
        try:
            with self._tether_status_lock:
                return dict(self._tether_status)
        except Exception:
            return {}

    def _tether_probe_loop(self) -> None:
        while not self._tether_probe_stop.is_set():
            try:
                self._set_tether_status_snapshot(self._probe_tether_once())
            except Exception as exc:
                self._set_tether_status_snapshot(
                    {
                        "ts": time.time(),
                        "ready": False,
                        "host": self._tether_host,
                        "local_ip": None,
                        "iface": None,
                        "port": None,
                        "reason": str(exc),
                    }
                )
            self._tether_probe_stop.wait(max(0.2, float(self._tether_probe_interval_s)))

    def _retarget_video_to_tether(self, local_ip: str | None) -> None:
        if self.cam_mgr is None:
            return
        try:
            _host, port = parse_zmq_endpoint(VIDEO_RPC_ENDPOINT)
        except Exception:
            port = 5555
        endpoint = f"tcp://{self._tether_host}:{int(port)}"
        setter = getattr(self.cam_mgr, "set_rpc_endpoint", None)
        if not callable(setter):
            return
        try:
            changed = bool(setter(endpoint, windows_host=(str(local_ip).strip() if local_ip else None)))
        except Exception as exc:
            trace_event("pilot_tether_video_endpoint_failed", endpoint=endpoint, error=str(exc))
            return
        if changed:
            trace_event("pilot_tether_video_endpoint_applied", endpoint=endpoint, windows_host=local_ip)
            self._prewarm_snapshot_capture_feeds()

    def _refresh_tether_status_ui(self) -> None:
        snapshot = self._get_tether_status_snapshot()
        ready = bool(snapshot.get("ready", False))
        host = str(snapshot.get("host") or self._tether_host)
        local_ip = str(snapshot.get("local_ip") or "").strip()
        iface = str(snapshot.get("iface") or "").strip()
        port = snapshot.get("port")
        reason = str(snapshot.get("reason") or "").strip()

        if ready:
            route = f"{local_ip} -> {host}:{port}" if local_ip and port else host
            if iface:
                route += f" ({iface})"
            pill_text = f"Tether: OK {local_ip}" if local_ip else "Tether: OK"
            banner_text = f"Tether: OK {route}"
            tone = "ok"
            self._retarget_video_to_tether(local_ip)
        else:
            detail = reason or f"{host} is not reachable on the tether"
            pill_text = "TETHER NETWORK UNREACHABLE"
            banner_text = f"TETHER NETWORK UNREACHABLE | {detail}"
            tone = "alert"

        self._set_status(self._tether_top_lbl, pill_text)
        try:
            self._tether_top_lbl.setToolTip(banner_text)
        except Exception:
            pass
        self._set_status_tone(self._tether_top_lbl, tone)
        self._set_status(self._tether_banner, banner_text)
        self._set_status_tone(self._tether_banner, tone)
        try:
            self._tether_banner.setVisible(not ready)
        except Exception:
            pass

        try:
            if self.video_panel is not None:
                setter = getattr(self.video_panel, "set_tether_status", None)
                if callable(setter):
                    setter(ready, banner_text)
        except Exception:
            pass

        previous = self._tether_ui_ready_last
        self._tether_ui_ready_last = ready
        if previous is None:
            return
        if ready and previous is False:
            self._play_tether_audio_cue(True)
            self.statusBar().showMessage("Tether network ready; reconnecting video on the tether", 4500)
        elif (not ready) and previous is True:
            self._play_tether_audio_cue(False)
            self.statusBar().showMessage("TETHER NETWORK UNREACHABLE - video waits for the tether", 7000)

    def _iface_is_wifi_linux(self, iface: str) -> bool:
        try:
            import os

            return os.path.isdir(f"/sys/class/net/{iface}/wireless")
        except Exception:
            # name heuristic fallback
            return iface.startswith("wl") or iface.startswith("wlan")

    def _refresh_route_cache(self):
        """Determine which local interface is used to reach the ROV host."""
        import time
        now = time.time()
        self._route_cache = {"ts": now, "iface": None, "src_ip": None, "is_wifi": None, "err": None}

        # Prefer Linux 'ip route get' for accurate dev+src.
        try:
            import subprocess

            out = subprocess.check_output(
                ["ip", "route", "get", self._rov_host],
                timeout=0.75,
                stderr=subprocess.DEVNULL,
                text=True,
            ).strip()
            # Example: "192.168.1.4 dev eth0 src 192.168.1.2 uid 1000"
            parts = out.split()
            if "dev" in parts:
                i = parts.index("dev")
                if i + 1 < len(parts):
                    self._route_cache["iface"] = parts[i + 1]
            if "src" in parts:
                i = parts.index("src")
                if i + 1 < len(parts):
                    self._route_cache["src_ip"] = parts[i + 1]
            iface = self._route_cache.get("iface")
            if iface:
                self._route_cache["is_wifi"] = bool(self._iface_is_wifi_linux(str(iface)))
            return
        except Exception as e:
            self._route_cache["err"] = str(e)

        # Fallback: UDP connect trick to get the chosen source IP (iface unknown).
        try:
            import socket

            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect((self._rov_host, 9))
            self._route_cache["src_ip"] = s.getsockname()[0]
            s.close()
        except Exception as e:
            self._route_cache["err"] = str(e)

    def _fmt_bps(self, bps: float | None) -> str:
        if bps is None:
            return "-"
        try:
            b = float(bps) * 8.0
        except Exception:
            return "-"
        if b < 0:
            return "-"
        if b >= 1e9:
            return f"{b/1e9:.2f}Gb/s"
        if b >= 1e6:
            return f"{b/1e6:.2f}Mb/s"
        if b >= 1e3:
            return f"{b/1e3:.1f}Kb/s"
        return f"{b:.0f}b/s"

    def _update_network_status(self):
        import time

        now = time.time()
        # Refresh local route info at most every 2 seconds to avoid frequent subprocess calls.
        if now - float(self._route_cache.get("ts", 0.0)) > 2.0:
            self._refresh_route_cache()

        local_iface = self._route_cache.get("iface")
        local_ip = self._route_cache.get("src_ip")
        local_wifi = self._route_cache.get("is_wifi")

        # Remote (ROV) network telemetry
        remote = self._last_net if (now - self._last_net_ts) < 3.0 else None
        if remote and isinstance(remote, dict):
            rif = remote.get("selected_iface") or remote.get("iface") or "-"
            rdef_if = remote.get("default_iface") or None
            rsel_reason = remote.get("selection_reason") or None
            rlink = remote.get("link") or {}
            rkind = rlink.get("kind") or "-"
            rstate = rlink.get("state") or "-"
            rsp = rlink.get("speed_mbps")
            rsp_s = f"{int(rsp)}Mbps" if isinstance(rsp, (int, float)) and rsp and rsp > 0 else "-"
            rtether = bool(remote.get("is_tether"))
            rdef_wifi = remote.get("default_is_wifi")
            rx_s = self._fmt_bps(remote.get("rx_bps"))
            tx_s = self._fmt_bps(remote.get("tx_bps"))
        else:
            rif = rkind = rstate = rsp_s = rx_s = tx_s = "-"
            rdef_if = None
            rsel_reason = None
            rdef_wifi = None
            rtether = False

        # Compose status
        local_part = "local="
        if local_iface:
            local_part += str(local_iface)
            if local_wifi is True:
                local_part += "(wifi)"
        elif local_ip:
            local_part += str(local_ip)
        else:
            local_part += "-"

        remote_part = f"rov={rif} {rkind} {rstate} {rsp_s}"
        if remote and (remote.get("ip") or None):
            remote_part += f" ip={remote.get('ip')}"
        if rdef_if and rdef_if != rif:
            remote_part += f" (def={rdef_if}"
            if rdef_wifi is True:
                remote_part += "/wifi"
            if rsel_reason:
                remote_part += f", {rsel_reason}"
            remote_part += ")"

        # Optional RTT/jitter/loss probe (ROV netdiag UDP echo).
        nd = self._get_netdiag_snapshot()
        nd_age = None
        try:
            if nd.get("ts"):
                nd_age = now - float(nd.get("ts", 0.0))
        except Exception:
            nd_age = None
        rtt_part = None
        if nd and (nd_age is not None) and nd_age < 2.5:
            last_rtt = nd.get("last_rtt_ms")
            avg_rtt = nd.get("avg_rtt_ms")
            jitter = nd.get("jitter_ms")
            loss_pct = nd.get("loss_pct")
            segs = []
            if isinstance(last_rtt, (int, float)):
                segs.append(f"rtt={float(last_rtt):.1f}ms")
            if isinstance(avg_rtt, (int, float)):
                segs.append(f"avg={float(avg_rtt):.1f}ms")
            if isinstance(jitter, (int, float)):
                segs.append(f"jit={float(jitter):.1f}ms")
            if isinstance(loss_pct, (int, float)):
                segs.append(f"loss={float(loss_pct):.0f}%")
            if segs:
                rtt_part = "probe:" + " ".join(segs)

        # Warnings
        warns = []
        if remote and (not rtether):
            warns.append("ROV stats not tether")
        if local_wifi is True and rtether:
            # Informational: route for control/RPC may differ from the ROV stats interface.
            warns.append("local route via Wi-Fi")

        parts = ["Net:", local_part, "|", remote_part]
        if remote:
            parts += ["|", f"rx={rx_s}", f"tx={tx_s}"]
        if rtt_part:
            parts += ["|", rtt_part]
        if warns:
            parts += ["|", "WARN " + "; ".join(warns)]

        self._set_status(self._net_lbl, " ".join(parts))

    def _toggle_water_correction(self, checked: bool) -> None:
        if self.video_panel is not None:
            self.video_panel.set_water_correction(checked)
        self.statusBar().showMessage(
            "Water correction ON (out-of-water mode)" if checked else "Water correction OFF",
            3000,
        )

    def _set_video_layout(self, pane_count: int) -> None:
        if self.video_panel is None:
            return
        if self._active_page_name == "hold_test":
            try:
                pane_count = int(pane_count)
            except Exception:
                pane_count = 1
            if pane_count in (1, 2, 4):
                self._pilot_layout_count_restore = pane_count
            self.video_panel.set_layout_controls_enabled(False)
            self.video_panel.set_layout_count(1)
            labels = {1: "single-camera", 2: "stacked dual-camera", 4: "quad-camera"}
            target_label = labels.get(int(self._pilot_layout_count_restore), "custom")
            self.statusBar().showMessage(
                f"Hold Test stays single-camera. Pilot page layout saved as {target_label}.",
                3500,
            )
            return
        self.video_panel.set_layout_count(pane_count)
        self._prewarm_snapshot_capture_feeds()
        labels = {1: "single-camera", 2: "stacked dual-camera", 3: "reverse-pane", 4: "quad-camera"}
        self.statusBar().showMessage(f"Video layout set to {labels.get(int(pane_count), 'custom')} view", 3000)

    @staticmethod
    def _safe_snapshot_stream_stem(stream_name: str | None) -> str:
        text = str(stream_name or "").strip()
        chars: list[str] = []
        last_was_sep = False
        for ch in text:
            if ("A" <= ch <= "Z") or ("a" <= ch <= "z") or ("0" <= ch <= "9") or ch in {".", "-"}:
                chars.append(ch)
                last_was_sep = False
            else:
                if not last_was_sep:
                    chars.append("_")
                    last_was_sep = True
        safe = "".join(chars).strip("._-")
        return safe or "stream"

    @classmethod
    def _snapshot_path(
        cls,
        session_dir: Path,
        stream_name: str | None,
        *,
        now: float | None = None,
        suffix: str = ".png",
    ) -> Path:
        ts = time.time() if now is None else float(now)
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(ts))
        millis = int(max(0.0, min(0.999, ts - int(ts))) * 1000.0)
        stem = f"{cls._safe_snapshot_stream_stem(stream_name)}_{stamp}-{millis:03d}"
        suffix = str(suffix or ".png").strip()
        if not suffix.startswith("."):
            suffix = "." + suffix
        target = Path(session_dir) / f"{stem}{suffix}"
        counter = 1
        while target.exists():
            counter += 1
            target = Path(session_dir) / f"{stem}_{counter:02d}{suffix}"
        return target

    @staticmethod
    def _unused_snapshot_path(target: Path) -> Path:
        target = Path(target)
        if not target.exists():
            return target
        counter = 1
        while True:
            counter += 1
            candidate = target.with_name(f"{target.stem}_{counter:02d}{target.suffix}")
            if not candidate.exists():
                return candidate

    def _current_stereo_pair(self):
        pair = getattr(self, "_active_stereo_pair", None)
        if pair is not None:
            return pair
        pairs = list(getattr(self, "_stereo_pairs", []) or [])
        if pairs:
            self._active_stereo_pair = pairs[0]
            return pairs[0]
        return None

    def _toggle_capture_mode(self) -> None:
        if self._stereo_recording or self._video_recording or self._video_recording_busy:
            self.statusBar().showMessage("Stop recording (B) before changing capture mode", 3000)
            return
        if str(getattr(self, "_capture_mode", "standard")) == "stereo":
            self._capture_mode = "standard"
            self.statusBar().showMessage("Capture mode: standard snapshots", 3000)
            trace_event("capture_mode_changed", mode="standard")
            self._update_capture_status_label()
            return
        pair = self._current_stereo_pair()
        if pair is None:
            self.statusBar().showMessage("Stereo capture unavailable: no stereo pair configured", 5000)
            trace_event("capture_mode_change_failed", mode="stereo", reason="no_pair")
            return
        self._capture_mode = "stereo"
        self.statusBar().showMessage(f"Capture mode: stereo ({pair.left} + {pair.right})", 4000)
        trace_event("capture_mode_changed", mode="stereo", pair=pair.name, left=pair.left, right=pair.right)
        self._update_capture_status_label()

    def _start_new_stereo_session(self) -> None:
        if self._stereo_recording:
            self.statusBar().showMessage("Stop recording (B) before starting a new session", 3000)
            return
        if self._stereo_capture_in_flight:
            self.statusBar().showMessage("Stereo capture busy; wait for the current pair to finish", 3500)
            return
        pair = self._current_stereo_pair()
        if pair is None or self.cam_mgr is None:
            self.statusBar().showMessage("Stereo session unavailable: no stereo pair configured", 5000)
            trace_event("stereo_session_new_failed", reason="no_pair_or_manager")
            return
        try:
            output_root, _location = self._make_recording_session_dir()
            old_session = getattr(self, "_stereo_capture_session", None)
            if old_session is not None:
                try:
                    old_session.stop()
                except Exception:
                    pass
            session = StereoCaptureSession(
                self.cam_mgr,
                pair,
                output_root=output_root,
                session_name=default_stereo_session_name(),
            )
            session.start()
            self._stereo_capture_session = session
            self._capture_mode = "stereo"
            self.statusBar().showMessage(f"New stereo session -> {session.session_dir}", 5000)
            trace_event(
                "stereo_session_new",
                pair=pair.name,
                session_dir=str(session.session_dir),
                output_root=str(output_root),
            )
        except Exception as exc:
            self.statusBar().showMessage(f"Could not start stereo session: {exc}", 7000)
            trace_event("stereo_session_new_failed", reason="exception", error=str(exc))

    def _ensure_stereo_session(self) -> StereoCaptureSession | None:
        session = getattr(self, "_stereo_capture_session", None)
        if session is not None:
            return session
        self._start_new_stereo_session()
        return getattr(self, "_stereo_capture_session", None)

    # ------------- Stereo burst recording ("orbit" mode) ------------- #
    def _update_capture_status_label(self) -> None:
        panel = getattr(self, "video_panel", None)
        setter = getattr(panel, "set_capture_status", None) if panel is not None else None
        if not callable(setter):
            return
        if self._stereo_recording:
            pair = self._current_stereo_pair()
            name = pair.name if pair is not None else "Stereo"
            setter(f"● REC  {name}  —  {self._stereo_recording_count} pairs", recording=True)
            return
        if self._video_recording:
            name = self._video_recording_stream or "Camera"
            secs = int(max(0.0, time.monotonic() - float(self._video_recording_started_mono)))
            setter(f"● REC  {name}  —  {secs // 60:02d}:{secs % 60:02d}", recording=True)
            return
        if self._video_recording_busy:
            setter("Starting video recording…", recording=True)
            return
        mode = str(getattr(self, "_capture_mode", "standard"))
        if mode == "stereo":
            pair = self._current_stereo_pair()
            suffix = f" ({pair.name})" if pair is not None else ""
            setter(f"Capture: Stereo{suffix}", recording=False)
        else:
            setter("Capture: Standard", recording=False)

    # ------------- B button dispatch (mode-aware) ------------- #
    def _toggle_recording_for_mode(self) -> None:
        """Controller/keyboard B: stereo burst in stereo mode, video in standard mode."""
        if str(getattr(self, "_capture_mode", "standard")) == "stereo":
            self._toggle_stereo_recording()
        else:
            self._toggle_video_recording()

    # ------------- Single-camera video recording ------------- #
    def _toggle_video_recording(self) -> None:
        now_mono = time.monotonic()
        if now_mono - float(getattr(self, "_last_video_recording_toggle_mono", 0.0)) < 0.5:
            trace_event("video_recording_toggle_ignored", reason="debounce")
            return
        self._last_video_recording_toggle_mono = now_mono
        if self._video_recording_busy:
            self.statusBar().showMessage("Video recording is starting/stopping; please wait", 2500)
            return
        if self._video_recording:
            self._stop_video_recording()
        else:
            self._start_video_recording()

    def _start_video_recording(self, stream_name: str | None = None) -> None:
        if self.cam_mgr is None or self.video_panel is None:
            self.statusBar().showMessage("Video recording unavailable: no video panel", 3000)
            return
        # Caller may pin a specific stream (e.g. the auto-hold recorder forcing the
        # arm/transect camera); otherwise record whatever camera is selected.
        if not stream_name:
            stream_name = self.video_panel.current_stream_name()
        if not stream_name:
            self.statusBar().showMessage("Select a camera (click its pane) before recording", 3500)
            trace_event("video_recording_start_failed", reason="no_selected_camera")
            return
        # One session dir holds the mp4, the synchronized state log, and a
        # manifest. Compute it (and start the log) on the UI thread.
        try:
            session_dir, _loc = self._make_recording_session_dir()
        except Exception as exc:
            self.statusBar().showMessage(f"Could not prepare capture folder: {exc}", 5000)
            return
        self._video_capture_session_dir = str(session_dir)
        self._video_capture_owns_log = False
        if self._stream_recorder is None:
            self._start_stream_log(Path(session_dir))
            self._video_capture_owns_log = self._stream_recorder is not None
        self._video_recording_busy = True
        self._video_recording_stream = stream_name
        self._video_recording_started_mono = time.monotonic()
        self._update_capture_status_label()
        self.statusBar().showMessage(f"Starting capture: {stream_name}…", 2500)
        threading.Thread(
            target=self._video_recording_start_worker,
            args=(stream_name, str(session_dir)),
            name="video-recording-start",
            daemon=True,
        ).start()

    def _stop_video_recording(self) -> None:
        if not self._video_recording or self._video_recorder is None:
            return
        rec = self._video_recorder
        stream_name = self._video_recording_stream or ""
        fanout_port = int(self._video_recording_mirror_port or 0)
        self._video_recorder = None
        self._video_recording_busy = True
        self.statusBar().showMessage("Finalizing video recording…", 2500)
        self._update_capture_status_label()
        threading.Thread(
            target=self._video_recording_stop_worker,
            args=(rec, stream_name, fanout_port),
            name="video-recording-stop",
            daemon=True,
        ).start()

    def _stream_current_extra(self, stream_name: str) -> dict:
        """Best-effort fetch of the ROV's *current* extra dict for one stream.

        Sending the live extra back (plus/minus the mirror) keeps the change a
        mirror-only one, which the ROV applies as a live multiudpsink update
        instead of rebuilding the pipeline (no display interruption).
        """
        try:
            status = self.cam_mgr.rov.list_status() or {}
            cfg = status.get(stream_name) or {}
            extra = cfg.get("extra")
            if isinstance(extra, dict):
                return dict(extra)
        except Exception:
            pass
        try:
            return dict(video_stream_options(self.cam_mgr, stream_name).get("extra") or {})
        except Exception:
            return {}

    def _set_stream_mirror(self, stream_name: str, mirror_port: int, *, add: bool) -> None:
        """Add/remove one recording mirror UDP port on the ROV stream (live)."""
        extra = self._stream_current_extra(stream_name)
        port = int(mirror_port)
        existing: list[int] = []
        for raw in extra.get("udp_mirror_ports") or []:
            try:
                existing.append(int(raw))
            except Exception:
                continue
        present = port in existing
        if (add and present) or ((not add) and not present):
            return
        ports = [p for p in existing if p != port]
        if add:
            ports.append(port)
        if ports:
            extra["udp_mirror_ports"] = ports
        else:
            extra.pop("udp_mirror_ports", None)
        self.cam_mgr.rov.update_stream(name=stream_name, extra=extra)

    def _write_capture_manifest(
        self,
        session_dir: "Path",
        *,
        stream_name: str,
        mp4_path: "Path",
        codec: str,
        opts: dict,
        ended_wall: float | None = None,
    ) -> None:
        """Write/update capture_manifest.json tying the mp4 to the state log.

        A training script can load the mp4 + the streams JSONL and align them by
        wall clock: the video's ``started_wall_ts`` marks ~t0 of the recording.
        """
        session_dir = Path(session_dir)
        manifest_path = session_dir / "capture_manifest.json"

        def _rel(p: "Path | str | None") -> str | None:
            if not p:
                return None
            p = Path(p)
            try:
                return str(p.relative_to(session_dir)).replace("\\", "/")
            except Exception:
                return str(p)

        data: dict = {}
        if manifest_path.exists():
            try:
                data = json.loads(manifest_path.read_text(encoding="utf-8"))
            except Exception:
                data = {}
        if not data:
            data = {
                "schema": "tritonpilot.capture_manifest",
                "version": 1,
                "started_wall_ts": time.time(),
                "started_mono_ts": time.monotonic(),
                "video": {
                    "stream": stream_name,
                    "path": _rel(mp4_path),
                    "codec": codec,
                    "width": int(opts.get("width", 0) or 0),
                    "height": int(opts.get("height", 0) or 0),
                    "fps": int(opts.get("fps", 0) or 0),
                },
                "streams_log": _rel(self._stream_log_path),
                "streams": ["pilot", "sensors", "attitude", "tracking"],
                "notes": {
                    "alignment": (
                        "Align mp4 frame time to the streams JSONL by wall clock; "
                        "video.started_wall_ts marks ~t=0 of the mp4 (a few hundred "
                        "ms of pipeline latency). 'tracking' stream holds model "
                        "error/command samples when the CV is running."
                    ),
                },
            }
        if ended_wall is not None:
            data["ended_wall_ts"] = float(ended_wall)
        try:
            session_dir.mkdir(parents=True, exist_ok=True)
            manifest_path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        except Exception:
            pass

    def _video_recording_start_worker(self, stream_name: str, session_dir: str) -> None:
        try:
            opts = video_stream_options(self.cam_mgr, stream_name)
            host = resolve_video_host(self.cam_mgr, opts)
            kwargs = video_start_kwargs(opts, host=host)
            port = int(kwargs.get("port", 5000))
            # Record the H.264 the laptop already receives: the live display
            # receiver fans the exact RTP out to this loopback port (zero extra
            # tether load -- the old ROV-side mirror doubled the stream over the
            # tether and corrupted both the live picture and the file). The fan-out
            # is part of the display pipeline, so it also survives display
            # reconnects without any ROV round-trip.
            fanout_port = record_fanout_port(port)
            tx_is_h264 = (
                str(kwargs.get("video_format", "")).lower() == "h264"
                or str(kwargs.get("encode", "")).lower() == "h264"
            )
            codec = "h264" if tx_is_h264 else "jpeg"

            video_dir = Path(session_dir) / "video"
            stamp = time.strftime("%Y%m%d-%H%M%S")
            fname = f"{safe_filename_component(stream_name)}-{stamp}.mp4"
            out_path = self._unused_snapshot_path(video_dir / fname)

            recorder = VideoRecorder(
                VideoRecorderConfig(
                    name=stream_name,
                    out_path=str(out_path),
                    codec=codec,
                    port=fanout_port,
                    bind_address=RECORD_FANOUT_HOST,
                )
            )
            recorder.start()
            self._video_recorder = recorder
            self._video_recording_mirror_port = fanout_port
            try:
                self._write_capture_manifest(
                    Path(session_dir),
                    stream_name=stream_name,
                    mp4_path=Path(out_path),
                    codec=codec,
                    opts=opts,
                )
            except Exception as exc:
                trace_event("capture_manifest_write_failed", error=str(exc))
            trace_event(
                "video_recording_started",
                stream=stream_name,
                fanout_port=fanout_port,
                codec=codec,
                path=str(out_path),
                session_dir=str(session_dir),
                streams_log=str(self._stream_log_path or ""),
            )
            self.video_recording_state_sig.emit(True, stream_name, str(out_path))
        except Exception as exc:
            trace_event("video_recording_start_failed", reason="exception", error=str(exc))
            self.video_recording_state_sig.emit(False, stream_name, f"ERROR: {exc}")

    def _video_recording_stop_worker(self, recorder, stream_name: str, fanout_port: int) -> None:
        path = ""
        try:
            if recorder is not None:
                path = recorder.stop(grace_s=6.0)
        except Exception as exc:
            trace_event("video_recording_stop_error", stream=stream_name, error=str(exc))
        finally:
            # Nothing to tear down on the ROV: the loopback fan-out lives in the
            # display pipeline and stays harmlessly idle once the recorder exits.
            session_dir = self._video_capture_session_dir
            if session_dir:
                try:
                    self._write_capture_manifest(
                        Path(session_dir),
                        stream_name=stream_name,
                        mp4_path=Path(path) if path else Path(session_dir) / "video",
                        codec="h264",
                        opts={},
                        ended_wall=time.time(),
                    )
                except Exception:
                    pass
            trace_event("video_recording_stopped", stream=stream_name, path=str(path))
            self.video_recording_state_sig.emit(False, stream_name, str(path))

    def _on_video_recording_state(self, recording: bool, stream: str, path_or_error: str) -> None:
        if recording:
            self._video_recording = True
            self._video_recording_busy = False
            self._video_recording_stream = stream
            self._video_recording_started_mono = time.monotonic()
            timer = getattr(self, "_video_rec_timer", None)
            if timer is None:
                timer = QTimer(self)
                timer.setInterval(1000)
                timer.timeout.connect(self._update_capture_status_label)
                self._video_rec_timer = timer
            timer.start()
            self.statusBar().showMessage(f"Recording {stream} -> {path_or_error}", 4000)
            # The hold was disengaged before this recording finished starting --
            # stop it now that the recorder is live.
            if self._hold_recording_stop_pending:
                self._hold_recording_stop_pending = False
                self._hold_owns_recording = False
                self._stop_video_recording()
        else:
            self._video_recording = False
            self._video_recording_busy = False
            self._video_recording_stream = None
            self._video_recording_mirror_port = None
            self._hold_owns_recording = False
            timer = getattr(self, "_video_rec_timer", None)
            if timer is not None:
                timer.stop()
            # Stop the synchronized state log only if this capture started it.
            if self._video_capture_owns_log:
                self._stop_stream_log()
                self._video_capture_owns_log = False
            session_dir = self._video_capture_session_dir
            self._video_capture_session_dir = None
            if str(path_or_error).startswith("ERROR:"):
                self.statusBar().showMessage(f"Capture failed: {path_or_error[6:].strip()}", 7000)
            elif path_or_error:
                where = session_dir or path_or_error
                self.statusBar().showMessage(f"Capture saved -> {where}", 6000)
        self._update_capture_status_label()

    def _toggle_stereo_recording(self) -> None:
        now_mono = time.monotonic()
        if now_mono - float(getattr(self, "_last_stereo_recording_toggle_mono", 0.0)) < 0.4:
            trace_event("stereo_recording_toggle_ignored", reason="debounce")
            return
        self._last_stereo_recording_toggle_mono = now_mono
        if self._stereo_recording:
            self._stop_stereo_recording()
        else:
            self._start_stereo_recording()

    def _start_stereo_recording(self) -> None:
        if self._stereo_recording:
            return
        if self._stereo_capture_in_flight:
            self.statusBar().showMessage("Stereo capture busy; wait for the current pair to finish", 3000)
            return
        pair = self._current_stereo_pair()
        if pair is None or self.cam_mgr is None:
            self.statusBar().showMessage("Stereo recording unavailable: no stereo pair configured", 5000)
            trace_event("stereo_recording_start_failed", reason="no_pair_or_manager")
            return
        try:
            output_root, _location = self._make_recording_session_dir()
            session = StereoCaptureSession(
                self.cam_mgr,
                pair,
                output_root=output_root,
                session_name=default_stereo_session_name(),
            )
            session.start()
        except Exception as exc:
            self.statusBar().showMessage(f"Could not start stereo recording: {exc}", 7000)
            trace_event("stereo_recording_start_failed", reason="exception", error=str(exc))
            return
        self._stereo_recording_session = session
        self._stereo_recording_count = 0
        self._stereo_recording_stop = threading.Event()
        self._stereo_recording = True
        self._capture_mode = "stereo"
        thread = threading.Thread(
            target=self._stereo_recording_worker,
            args=(session, self._stereo_recording_stop),
            name="stereo-recording",
            daemon=True,
        )
        self._stereo_recording_thread = thread
        thread.start()
        self._update_capture_status_label()
        self.statusBar().showMessage(f"Stereo recording -> {session.session_dir}", 4000)
        trace_event("stereo_recording_started", pair=pair.name, session_dir=str(session.session_dir))

    def _stop_stereo_recording(self) -> None:
        if not self._stereo_recording:
            return
        self._stereo_recording = False
        stop_event = getattr(self, "_stereo_recording_stop", None)
        if stop_event is not None:
            stop_event.set()
        self.statusBar().showMessage("Stopping stereo recording...", 2500)
        trace_event("stereo_recording_stopping", count=self._stereo_recording_count)
        self._update_capture_status_label()

    def _stereo_recording_worker(self, session: StereoCaptureSession, stop_event: threading.Event) -> None:
        # Capture clean, synced pairs back-to-back (no artificial delay) until
        # stopped. Each on-demand pair is already gated for sync + cleanliness,
        # so "as fast as cleanly able" is just the natural per-pair latency.
        pair_timeout_s = 6.0
        consecutive_failures = 0
        try:
            while not stop_event.is_set():
                try:
                    record = session.capture_once(wait_s=pair_timeout_s)
                    consecutive_failures = 0
                except Exception as exc:
                    consecutive_failures += 1
                    trace_event(
                        "stereo_recording_capture_failed",
                        error=str(exc),
                        consecutive=consecutive_failures,
                    )
                    if consecutive_failures >= 10:
                        trace_event("stereo_recording_aborted", reason="too_many_failures")
                        break
                    if stop_event.wait(0.2):
                        break
                    continue
                count = int(record.get("index", 0) or 0)
                delta = float(record.get("pair_delta_ms", 0.0) or 0.0)
                self.stereo_recording_progress_sig.emit(count, delta)
        finally:
            try:
                session.stop()
            except Exception:
                pass
            self.stereo_recording_state_sig.emit(False, str(session.session_dir))

    def _handle_stereo_recording_progress_on_ui(self, count: int, delta_ms: float) -> None:
        self._stereo_recording_count = int(count)
        flasher = getattr(self.video_panel, "flash_snapshot_badge", None) if self.video_panel is not None else None
        pair = self._current_stereo_pair()
        if callable(flasher) and pair is not None:
            for stream_name in (pair.left, pair.right):
                try:
                    flasher(stream_name)
                except Exception:
                    pass
        self._update_capture_status_label()

    def _handle_stereo_recording_state_on_ui(self, recording: bool, session_dir: str) -> None:
        if not recording:
            self._stereo_recording = False
            self._stereo_recording_thread = None
            self._stereo_recording_session = None
            # The next single stereo capture starts a fresh session.
            self._stereo_capture_session = None
            self.statusBar().showMessage(
                f"Stereo recording saved -> {session_dir} ({self._stereo_recording_count} pairs)", 6000
            )
            trace_event(
                "stereo_recording_stopped",
                session_dir=str(session_dir),
                count=self._stereo_recording_count,
            )
        self._update_capture_status_label()

    def _capture_from_current_mode(self) -> None:
        if self._stereo_recording:
            self.statusBar().showMessage("Stereo recording in progress; press B to stop", 2500)
            trace_event("capture_request_ignored", reason="recording")
            return
        if str(getattr(self, "_capture_mode", "standard")) == "stereo":
            self._capture_stereo_pair_snapshot()
            return
        self._capture_selected_stream_snapshot()

    def _capture_stereo_pair_snapshot(self) -> None:
        now_mono = time.monotonic()
        if now_mono - float(getattr(self, "_last_stereo_capture_request_mono", 0.0)) < 0.05:
            trace_event("stereo_capture_request_ignored", reason="debounce")
            return
        self._last_stereo_capture_request_mono = now_mono
        if self._stereo_capture_in_flight:
            self.statusBar().showMessage("Stereo capture busy; wait for the current pair to finish", 2500)
            trace_event("stereo_capture_request_ignored", reason="busy")
            return
        pair = self._current_stereo_pair()
        if pair is None:
            self.statusBar().showMessage("Stereo capture unavailable: no stereo pair configured", 5000)
            trace_event("stereo_capture_request_ignored", reason="no_pair")
            return
        session = self._ensure_stereo_session()
        if session is None:
            return
        flasher = getattr(self.video_panel, "flash_snapshot_badge", None) if self.video_panel is not None else None
        if callable(flasher):
            for stream_name in (pair.left, pair.right):
                try:
                    flasher(stream_name)
                except Exception:
                    pass
        self._stereo_capture_in_flight = True
        self._capture_and_save_stereo_pair_async(session)
        self.statusBar().showMessage(f"Stereo capture queued -> {session.session_dir}", 2500)
        trace_event("stereo_capture_queued", pair=pair.name, session_dir=str(session.session_dir))

    def _capture_selected_stream_snapshot(self) -> None:
        now_mono = time.monotonic()
        if now_mono - float(getattr(self, "_last_snapshot_request_mono", 0.0)) < 0.05:
            trace_event("snapshot_request_ignored", reason="debounce")
            return
        self._last_snapshot_request_mono = now_mono

        if self.video_panel is None:
            self.statusBar().showMessage("Snapshot unavailable: no video panel", 3000)
            trace_event("snapshot_request_ignored", reason="no_video_panel")
            return

        stream_name = self.video_panel.current_stream_name()
        widget = self.video_panel.current_video_widget()
        if not stream_name or widget is None:
            self.statusBar().showMessage("Snapshot unavailable: no selected camera", 3000)
            trace_event("snapshot_request_ignored", reason="no_selected_camera", stream=stream_name)
            return

        try:
            session_dir, _location = self._make_recording_session_dir()
            has_onboard_capture = callable(getattr(self.cam_mgr, "capture_onboard_snapshot", None))
            target = self._snapshot_path(session_dir, stream_name, suffix=".jpg" if has_onboard_capture else ".png")
        except Exception as exc:
            self.statusBar().showMessage(f"Could not prepare snapshot folder: {exc}", 5000)
            trace_event("snapshot_request_ignored", reason="session_dir_failed", stream=stream_name, error=str(exc))
            return

        trace_event("snapshot_requested", stream=stream_name, path=str(target))
        flasher = getattr(self.video_panel, "flash_snapshot_badge", None)
        if callable(flasher):
            try:
                flasher(stream_name)
            except Exception:
                pass

        onboard_capturer = getattr(self.cam_mgr, "capture_onboard_snapshot", None)
        source_capturer = getattr(self.cam_mgr, "capture_snapshot_frame", None)
        if callable(onboard_capturer) or callable(source_capturer):
            self._capture_and_save_snapshot_async(str(stream_name), target)
        else:
            image: QImage | None = None
            snapshotter = getattr(widget, "snapshot_image", None)
            if callable(snapshotter):
                try:
                    image = snapshotter()
                except Exception as exc:
                    logger.warning("Snapshot image capture failed for %s: %s", stream_name, exc)
                    image = None
            if image is None or image.isNull():
                self.statusBar().showMessage(f"Snapshot unavailable: {stream_name} has no frame yet", 3000)
                trace_event("snapshot_request_ignored", reason="widget_no_frame", stream=stream_name)
                return
            self._save_snapshot_image_async(image.copy(), target, str(stream_name))
        self.statusBar().showMessage(f"Snapshot queued -> {target}", 2500)
        trace_event("snapshot_queued", stream=stream_name, path=str(target))

    @staticmethod
    def _qimage_from_bgr_frame(frame) -> QImage:
        arr = np.ascontiguousarray(frame)
        if arr.ndim != 3 or arr.shape[2] != 3:
            raise ValueError(f"Expected BGR frame with 3 channels, got shape {arr.shape}")
        height, width, _channels = arr.shape
        image = QImage(arr.data, int(width), int(height), int(arr.strides[0]), QImage.Format.Format_BGR888)
        return image.copy()

    @staticmethod
    def _save_snapshot_image_file(image: QImage, target: Path) -> None:
        target = Path(target)
        tmp = target.with_name(f".{target.stem}.partial{target.suffix}")
        target.parent.mkdir(parents=True, exist_ok=True)
        if tmp.exists():
            try:
                tmp.unlink()
            except Exception:
                pass
        try:
            if not image.save(str(tmp), "PNG"):
                raise RuntimeError("Qt image encoder returned failure")
            tmp.replace(target)
        except Exception:
            try:
                if tmp.exists():
                    tmp.unlink()
            except Exception:
                pass
            raise

    @staticmethod
    def _snapshot_extension_for_packet(packet) -> str:
        extension = str(getattr(packet, "extension", "") or "").strip().lower()
        if extension.startswith("."):
            extension = extension[1:]
        mime_type = str(getattr(packet, "mime_type", "") or "").strip().lower()
        if not extension:
            extension = "jpg" if mime_type == "image/jpeg" else "bin"
        extension = "".join(ch for ch in extension if ("a" <= ch <= "z") or ("0" <= ch <= "9"))
        if not extension:
            extension = "jpg" if mime_type == "image/jpeg" else "bin"
        return "." + extension

    @staticmethod
    def _save_snapshot_bytes_file(data: bytes, target: Path) -> None:
        target = Path(target)
        tmp = target.with_name(f".{target.stem}.partial{target.suffix}")
        target.parent.mkdir(parents=True, exist_ok=True)
        if tmp.exists():
            try:
                tmp.unlink()
            except Exception:
                pass
        try:
            with open(tmp, "wb") as f:
                f.write(bytes(data))
                f.flush()
                os.fsync(f.fileno())
            tmp.replace(target)
        except Exception:
            try:
                if tmp.exists():
                    tmp.unlink()
            except Exception:
                pass
            raise

    def _capture_and_save_snapshot_async(self, stream_name: str, target: Path) -> None:
        stream_name = str(stream_name)
        target = Path(target)

        def _work() -> None:
            ok = False
            err = ""
            saved_target = target
            try:
                trace_event("snapshot_capture_started", stream=stream_name, path=str(target))
                onboard_capturer = getattr(self.cam_mgr, "capture_onboard_snapshot", None)
                if callable(onboard_capturer):
                    try:
                        packet = onboard_capturer(stream_name, timeout_s=4.0)
                        data = bytes(getattr(packet, "image_bytes", b"") or b"")
                        if not data:
                            raise RuntimeError("ROV snapshot returned no image bytes")
                        extension = self._snapshot_extension_for_packet(packet)
                        save_target = target if target.suffix.lower() == extension else target.with_suffix(extension)
                        saved_target = self._unused_snapshot_path(save_target)
                        self._save_snapshot_bytes_file(data, saved_target)
                        ok = True
                        trace_event(
                            "snapshot_onboard_saved",
                            stream=stream_name,
                            path=str(saved_target),
                            byte_count=int(getattr(packet, "byte_count", len(data)) or len(data)),
                            mime_type=str(getattr(packet, "mime_type", "") or ""),
                        )
                        self.snapshot_result_sig.emit(stream_name, str(saved_target), ok, err)
                        return
                    except Exception as onboard_exc:
                        trace_event(
                            "snapshot_onboard_failed",
                            stream=stream_name,
                            path=str(target),
                            error=str(onboard_exc),
                        )
                        logger.warning(
                            "ROV onboard snapshot failed for %s; falling back to source frame: %s",
                            stream_name,
                            onboard_exc,
                        )
                capturer = getattr(self.cam_mgr, "capture_snapshot_frame", None)
                if not callable(capturer):
                    raise RuntimeError("source capture path is unavailable")
                packet = capturer(stream_name, timeout_s=4.0)
                frame = getattr(packet, "frame_bgr", None)
                if frame is None:
                    raise RuntimeError("source capture returned no frame")
                saved_target = target if target.suffix.lower() == ".png" else target.with_suffix(".png")
                saved_target = self._unused_snapshot_path(saved_target)
                trace_event(
                    "snapshot_frame_captured",
                    stream=stream_name,
                    path=str(saved_target),
                    seq=int(getattr(packet, "seq", 0) or 0),
                    frame_shape=str(getattr(frame, "shape", "")),
                )
                image = self._qimage_from_bgr_frame(frame)
                self._save_snapshot_image_file(image, saved_target)
                ok = True
            except Exception as exc:
                err = str(exc)
                logger.warning("Snapshot capture/save failed for %s -> %s: %s", stream_name, saved_target, err)
            self.snapshot_result_sig.emit(stream_name, str(saved_target), ok, err)

        threading.Thread(target=_work, name=f"snapshot-capture-{stream_name}", daemon=True).start()

    def _save_snapshot_image_async(self, image: QImage, target: Path, stream_name: str) -> None:
        target = Path(target)
        image = image.copy()

        def _write() -> None:
            ok = False
            err = ""
            try:
                self._save_snapshot_image_file(image, target)
                ok = True
            except Exception as exc:
                err = str(exc)
            self.snapshot_result_sig.emit(str(stream_name), str(target), ok, err)

        threading.Thread(target=_write, name=f"snapshot-save-{stream_name}", daemon=True).start()

    def _capture_and_save_stereo_pair_async(self, session: StereoCaptureSession) -> None:
        pair_name = str(getattr(session.pair, "name", "stereo"))

        def _work() -> None:
            ok = False
            err = ""
            path = str(session.manifest_path)
            try:
                record = session.capture_once(wait_s=5.0)
                ok = True
                path = str(session.manifest_path)
                trace_event(
                    "stereo_capture_saved",
                    pair=pair_name,
                    session_dir=str(session.session_dir),
                    manifest_path=path,
                    index=record.get("index"),
                    pair_delta_ms=record.get("pair_delta_ms"),
                )
            except Exception as exc:
                err = str(exc)
                logger.warning("Stereo capture failed for %s -> %s: %s", pair_name, path, err)
            self.stereo_capture_result_sig.emit(pair_name, path, ok, err)

        threading.Thread(target=_work, name=f"stereo-capture-{pair_name}", daemon=True).start()

    def _handle_snapshot_result_on_ui(self, stream_name: str, path: str, ok: bool, err: str) -> None:
        if ok:
            self.statusBar().showMessage(f"Saved snapshot {stream_name} -> {path}", 5000)
            trace_event("snapshot_saved", stream=stream_name, path=path)
            return
        detail = err or "unknown error"
        self.statusBar().showMessage(f"Snapshot save failed for {stream_name}: {detail}", 7000)
        trace_event("snapshot_save_failed", stream=stream_name, path=path, error=detail)

    def _handle_stereo_capture_result_on_ui(self, pair_name: str, path: str, ok: bool, err: str) -> None:
        self._stereo_capture_in_flight = False
        if ok:
            self.statusBar().showMessage(f"Saved stereo pair {pair_name} -> {path}", 5000)
            trace_event("stereo_capture_result", pair=pair_name, path=path, ok=True)
            return
        detail = err or "unknown error"
        self.statusBar().showMessage(f"Stereo capture failed for {pair_name}: {detail}", 7000)
        trace_event("stereo_capture_result", pair=pair_name, path=path, ok=False, error=detail)

    def _analysis_transfer_configured_root(self) -> Path:
        override = os.environ.get("TRITON_PILOT_TRANSFER_ROOT", "").strip()
        if override:
            root = Path(override).expanduser()
            root.mkdir(parents=True, exist_ok=True)
            return root.resolve()
        root, _location = self._recordings_output_dir()
        return root.resolve()

    def _analysis_transfer_display_url(self) -> str:
        if self._analysis_transfer_server is None:
            port = self._analysis_transfer_port
        else:
            _bound_host, port = self._analysis_transfer_server.server_address
        host = self._analysis_transfer_advertise_host.strip()
        if not host and self._analysis_transfer_host not in {"0.0.0.0", "::"}:
            host = self._analysis_transfer_host
        if not host:
            host = self._analysis_transfer_resolved_advertise_host.strip()
        if not host:
            host = self._default_analysis_transfer_advertise_host()
            self._analysis_transfer_resolved_advertise_host = host
        return f"http://{host}:{int(port)}"

    @staticmethod
    def _short_analysis_transfer_path(path: object, *, max_chars: int = 34) -> str:
        text = str(path or "")
        if len(text) <= max_chars:
            return text
        return "..." + text[-max(0, max_chars - 3) :]

    def _set_analysis_transfer_label(self, text: str, tone: str | None = None) -> None:
        self._set_status(self._analysis_transfer_lbl, text)
        self._set_status_tone(self._analysis_transfer_lbl, tone)
        self._set_status(self._analysis_transfer_line, text)
        self._set_status_tone(self._analysis_transfer_line, tone)
        try:
            self.pilot_telemetry_column.set_analysis_share(text, tone)
        except Exception:
            pass
        self._refresh_analysis_transfer_actions()

    def _refresh_analysis_transfer_actions(self) -> None:
        running = self._analysis_transfer_server is not None
        if self._analysis_transfer_start_act is not None:
            self._analysis_transfer_start_act.setEnabled(not running)
        if self._analysis_transfer_stop_act is not None:
            self._analysis_transfer_stop_act.setEnabled(running)
        if self._analysis_transfer_restart_act is not None:
            self._analysis_transfer_restart_act.setEnabled(True)

    def _start_analysis_transfer_server(self) -> None:
        if self._analysis_transfer_server is not None:
            self._refresh_analysis_transfer_status()
            return
        try:
            self._analysis_transfer_resolved_advertise_host = ""
            root = self._analysis_transfer_configured_root()
            server = create_server(
                root=root,
                host=self._analysis_transfer_host,
                port=self._analysis_transfer_port,
                stable_seconds=self._analysis_transfer_stable_seconds,
                include_hidden=self._analysis_transfer_include_hidden,
            )
            thread = start_server_in_thread(server)
        except Exception as exc:
            self._analysis_transfer_server = None
            self._analysis_transfer_thread = None
            self._analysis_transfer_root = None
            self._analysis_transfer_error = str(exc)
            self._set_analysis_transfer_label(f"Analysis Share: ERR | {self._analysis_transfer_error}", "alert")
            self.statusBar().showMessage(f"Analysis transfer server failed: {exc}", 7000)
            return

        self._analysis_transfer_server = server
        self._analysis_transfer_thread = thread
        self._analysis_transfer_root = root
        self._analysis_transfer_error = ""
        self._refresh_analysis_transfer_status()
        self.statusBar().showMessage(f"Analysis transfer serving {root}", 5000)

    def _stop_analysis_transfer_server(self) -> None:
        server = self._analysis_transfer_server
        thread = self._analysis_transfer_thread
        self._analysis_transfer_server = None
        self._analysis_transfer_thread = None
        self._analysis_transfer_root = None
        self._analysis_transfer_error = ""
        if server is not None:
            try:
                server.shutdown()
            except Exception:
                pass
            try:
                server.server_close()
            except Exception:
                pass
        if thread is not None:
            try:
                thread.join(timeout=1.0)
            except Exception:
                pass
        self._set_analysis_transfer_label("Analysis Share: OFF", "warn")

    def _restart_analysis_transfer_server(self) -> None:
        self._stop_analysis_transfer_server()
        self._start_analysis_transfer_server()

    def _copy_analysis_transfer_url(self) -> None:
        url = self._analysis_transfer_display_url()
        try:
            QApplication.clipboard().setText(url)
        except Exception:
            pass
        self.statusBar().showMessage(f"Analysis transfer URL copied: {url}", 3000)

    def _analysis_transfer_cached_index(self, root: Path) -> dict:
        root_key = str(Path(root).expanduser().resolve())
        with self._analysis_transfer_index_lock:
            cache = dict(self._analysis_transfer_index_cache)
        if str(cache.get("root") or "") != root_key:
            return {}
        return cache

    def _queue_analysis_transfer_index_refresh(self, root: Path) -> None:
        root = Path(root).expanduser().resolve()
        root_key = str(root)
        now_s = time.monotonic()
        with self._analysis_transfer_index_lock:
            cache = dict(self._analysis_transfer_index_cache)
            cache_matches = str(cache.get("root") or "") == root_key
            if bool(self._analysis_transfer_index_refreshing):
                return
            last_start = float(self._analysis_transfer_index_last_start_s or 0.0)
            interval = float(self._analysis_transfer_index_refresh_interval_s)
            if cache_matches and now_s - last_start < interval:
                return
            self._analysis_transfer_index_refreshing = True
            self._analysis_transfer_index_last_start_s = now_s

        BackgroundThread(
            target=self._analysis_transfer_index_worker,
            args=(root, float(self._analysis_transfer_stable_seconds), bool(self._analysis_transfer_include_hidden)),
            name="analysis-transfer-index",
            daemon=True,
        ).start()

    def _analysis_transfer_index_worker(self, root: Path, stable_seconds: float, include_hidden: bool) -> None:
        payload = {
            "root": str(Path(root).expanduser().resolve()),
            "generated_at_mono": time.monotonic(),
        }
        try:
            index = build_index(root, stable_seconds=stable_seconds, include_hidden=include_hidden)
            payload.update(
                {
                    "ok": True,
                    "file_count": int(index.get("file_count", 0)),
                    "total_bytes": int(index.get("total_bytes", 0)),
                    "error": "",
                }
            )
        except Exception as exc:
            payload.update({"ok": False, "file_count": 0, "total_bytes": 0, "error": str(exc)})
        try:
            self.analysis_transfer_index_sig.emit(payload)
        except RuntimeError:
            with self._analysis_transfer_index_lock:
                self._analysis_transfer_index_refreshing = False

    def _handle_analysis_transfer_index_result(self, payload: dict) -> None:
        with self._analysis_transfer_index_lock:
            self._analysis_transfer_index_cache = dict(payload or {})
            self._analysis_transfer_index_refreshing = False
        try:
            root = self._analysis_transfer_root
            if root is not None and str(Path(root).expanduser().resolve()) == str((payload or {}).get("root") or ""):
                self._refresh_analysis_transfer_status()
        except Exception:
            pass

    def _refresh_analysis_transfer_status(self) -> None:
        server = self._analysis_transfer_server
        if server is None:
            if self._analysis_transfer_error:
                self._set_analysis_transfer_label(f"Analysis Share: ERR | {self._analysis_transfer_error}", "alert")
            else:
                self._set_analysis_transfer_label("Analysis Share: OFF", "warn")
            return

        root = self._analysis_transfer_root or Path(getattr(server, "root", ""))
        self._queue_analysis_transfer_index_refresh(root)
        index_cache = self._analysis_transfer_cached_index(root)
        if index_cache.get("error"):
            self._analysis_transfer_error = str(index_cache.get("error") or "")
            self._set_analysis_transfer_label(f"Analysis Share: ERR | {self._analysis_transfer_error}", "alert")
            return
        index_ready = bool(index_cache.get("ok"))
        file_count = int(index_cache.get("file_count", 0) or 0)
        total_mb = float(index_cache.get("total_bytes", 0) or 0) / (1024 * 1024)

        try:
            snapshot = server.request_snapshot()
        except Exception:
            snapshot = {}
        last_request_ts = float(snapshot.get("last_request_ts") or 0.0)
        last_request_path = str(snapshot.get("last_request_path") or "")
        active_file_transfers = int(snapshot.get("active_file_transfers") or 0)
        active_file_paths = list(snapshot.get("active_file_paths") or [])
        last_file_path = str(snapshot.get("last_file_path") or "")
        last_file_completed_ts = float(snapshot.get("last_file_completed_ts") or 0.0)
        if active_file_transfers > 0:
            active_path = active_file_paths[-1] if active_file_paths else last_file_path
            short_path = self._short_analysis_transfer_path(active_path)
            pull_text = f"sending {active_file_transfers} file(s)"
            if short_path:
                pull_text = f"{pull_text}: {short_path}"
            tone = "ok"
        elif last_file_completed_ts > 0 and time.time() - last_file_completed_ts < 30.0:
            age = max(0.0, time.time() - last_file_completed_ts)
            short_path = self._short_analysis_transfer_path(last_file_path)
            sent_bytes = int(snapshot.get("last_file_bytes_sent") or 0)
            if sent_bytes >= 1024 * 1024:
                size_text = f"{sent_bytes / (1024 * 1024):.1f} MB"
            elif sent_bytes >= 1024:
                size_text = f"{sent_bytes / 1024:.1f} KB"
            else:
                size_text = f"{sent_bytes} B"
            pull_text = f"sent {size_text} {age:.0f}s ago"
            if short_path:
                pull_text = f"{pull_text}: {short_path}"
            tone = "ok"
        elif last_request_ts > 0:
            age = max(0.0, time.time() - last_request_ts)
            if last_request_path in {"/events", "/api/events"}:
                if age < 65.0:
                    pull_text = f"Analysis listening {age:.0f}s"
                    tone = "ok"
                else:
                    pull_text = f"last Analysis contact {age / 60.0:.0f}m"
                    tone = "warn"
            else:
                pull_text = f"last Analysis check {age:.0f}s" if age < 60.0 else f"last Analysis check {age / 60.0:.0f}m"
                tone = "ok" if age < 20.0 else "warn"
        else:
            pull_text = "waiting for Analysis"
            tone = ""

        root_name = root.name or str(root)
        index_text = f"{file_count} files/{total_mb:.1f} MB" if index_ready else "indexing..."
        text = (
            f"Analysis Share: ON {self._analysis_transfer_display_url()} "
            f"| {root_name} | {index_text} | {pull_text}"
        )
        self._set_analysis_transfer_label(text, tone)

    def _recordings_location(self) -> SaveLocation:
        return resolve_recordings_dir(self._preferred_save_dir)

    def _recordings_output_dir(self) -> tuple[Path, SaveLocation]:
        location = self._recordings_location()
        try:
            location.path.mkdir(parents=True, exist_ok=True)
            return location.path, location
        except Exception as exc:
            fallback = resolve_recordings_dir(None)
            if location.path == fallback.path:
                raise
            try:
                fallback.path.mkdir(parents=True, exist_ok=True)
            except Exception:
                raise exc
            reason = f"Could not use save directory {location.path}: {exc}"
            return fallback.path, SaveLocation(fallback.path, used_fallback=True, reason=reason)

    def _app_session_output_dir(self) -> tuple[Path, SaveLocation]:
        session_dir = self._app_session_dir
        session_location = self._app_session_location
        if session_dir is not None and session_location is not None and is_available_directory(session_dir):
            return session_dir, session_location

        root, location = self._recordings_output_dir()
        session_dir = StreamRecorder.make_session_dir(root)
        self._app_session_dir = session_dir
        self._app_session_location = location
        return session_dir, location

    def _make_recording_session_dir(self) -> tuple[Path, SaveLocation]:
        return self._app_session_output_dir()

    def _log_output_dir(self) -> tuple[Path, SaveLocation | None]:
        if self._record_dir and self._stream_recorder is not None and is_available_directory(self._record_dir):
            return Path(self._record_dir), None

        if self._record_dir and self._stream_recorder is None:
            self._record_dir = None

        return self._app_session_output_dir()

    def _save_location_note(self, location: SaveLocation | None) -> str:
        if location is not None and location.used_fallback and self._preferred_save_dir:
            return " (using app default)"
        return ""

    def _current_save_dir_summary(self) -> str:
        location = self._recordings_location()
        if location.used_fallback and self._preferred_save_dir:
            return f"Current save root: {location.path}\nFallback active: {location.reason}"
        return f"Current save root: {location.path}"

    def _refresh_save_directory_actions(self) -> None:
        summary = self._current_save_dir_summary()
        if self._save_dir_act is not None:
            self._save_dir_act.setToolTip(summary)
        if self._reset_save_dir_act is not None:
            self._reset_save_dir_act.setEnabled(bool(self._preferred_save_dir))
            self._reset_save_dir_act.setToolTip(f"Use {DEFAULT_RECORDINGS_DIR} for new stream logs.")

    def _choose_save_directory(self) -> None:
        start_dir = str(self._recordings_location().path)
        selected_dir = QFileDialog.getExistingDirectory(
            self,
            "Choose recordings folder",
            start_dir,
        )
        if not selected_dir:
            return

        selected_path = Path(selected_dir).expanduser()
        self._preferred_save_dir = str(selected_path)
        self._settings.setValue(self.SAVE_DIR_SETTINGS_KEY, self._preferred_save_dir)
        if self._stream_recorder is None:
            self._record_dir = None
            self._app_session_dir = None
            self._app_session_location = None
        self._refresh_save_directory_actions()

        location = self._recordings_location()
        if location.used_fallback:
            QMessageBox.warning(
                self,
                "Save Directory",
                f"{location.reason}\n\nNew stream logs will use:\n{location.path}",
            )
        else:
            self.statusBar().showMessage(f"Save directory set: {location.path}", 5000)
        if not os.environ.get("TRITON_PILOT_TRANSFER_ROOT", "").strip():
            self._restart_analysis_transfer_server()

    def _reset_save_directory(self) -> None:
        self._preferred_save_dir = ""
        self._settings.remove(self.SAVE_DIR_SETTINGS_KEY)
        if self._stream_recorder is None:
            self._record_dir = None
            self._app_session_dir = None
            self._app_session_location = None
        self._refresh_save_directory_actions()
        self.statusBar().showMessage(f"Save directory reset: {DEFAULT_RECORDINGS_DIR}", 5000)
        if not os.environ.get("TRITON_PILOT_TRANSFER_ROOT", "").strip():
            self._restart_analysis_transfer_server()

    def set_fullscreen_mode(self, enabled: bool) -> None:
        if bool(enabled):
            self.showFullScreen()
        else:
            self.showMaximized()
        self._sync_fullscreen_action()

    def _toggle_fullscreen_mode(self, checked: bool | None = None) -> None:
        if checked is None:
            checked = not self.isFullScreen()
        self.set_fullscreen_mode(bool(checked))

    def _sync_fullscreen_action(self) -> None:
        act = self._fullscreen_act
        if act is None:
            return
        try:
            prev = act.blockSignals(True)
            act.setChecked(bool(self.isFullScreen()))
        finally:
            try:
                act.blockSignals(prev)
            except Exception:
                pass

    def _make_menu(self):
        bar = self.menuBar()
        file_menu = bar.addMenu("&File")
        rec_menu = bar.addMenu("&Record")
        transfer_menu = bar.addMenu("&Transfer")
        autopilot_menu = bar.addMenu("&Autopilot")
        view_menu = bar.addMenu("&View")

        # Optical-tracking station-keep (CV-era). Engaging is safe with no CV
        # running (ROV stays inert without a valid lock). Toggle key handled in
        # eventFilter (K) to match the other vehicle shortcuts.
        self._station_keep_act = QAction("Optical Hold (Station-Keep)  [K]", self)
        self._station_keep_act.setCheckable(True)
        self._station_keep_act.setChecked(False)
        self._station_keep_act.setToolTip(
            "Engage the visual station-keep autopilot. Holds position in current "
            "from the transect camera once the CV target tracker is running; "
            "inert (falls back to manual) until then."
        )
        self._station_keep_act.toggled.connect(self._toggle_station_keep_from_ui)
        autopilot_menu.addAction(self._station_keep_act)

        self._transect_rotation_servo_act = QAction("Transect yaw/er authority", self)
        self._transect_rotation_servo_act.setCheckable(True)
        self._transect_rotation_servo_act.setChecked(bool(self._transect_rotation_servo_enabled))
        self._transect_rotation_servo_act.setToolTip(
            "Allow the transect rotation error channel (er) to command yaw through station-keep."
        )
        self._transect_rotation_servo_act.toggled.connect(self._set_transect_rotation_servo_enabled)
        autopilot_menu.addAction(self._transect_rotation_servo_act)

        # Roll/Pitch level hold. Station-keep engage force-enables this for a stable
        # camera; this action (and the P key) is how to turn it back OFF -- e.g. to
        # fly a hold without leveling for testing. Toggle key handled in eventFilter.
        self._roll_pitch_level_act = QAction("Roll/Pitch Level Hold  [P]", self)
        self._roll_pitch_level_act.setCheckable(True)
        self._roll_pitch_level_act.setChecked(False)
        self._roll_pitch_level_act.setToolTip(
            "Level the vehicle (roll/pitch -> 0). Auto-enabled when Optical Hold "
            "engages; toggle off here (or press P) to fly without leveling."
        )
        self._roll_pitch_level_act.toggled.connect(self._toggle_roll_pitch_level_from_ui)
        autopilot_menu.addAction(self._roll_pitch_level_act)

        self._fullscreen_act = QAction("Full Screen", self)
        self._fullscreen_act.setCheckable(True)
        self._fullscreen_act.setShortcut("F11")
        self._fullscreen_act.setToolTip("Toggle full-screen pilot view.")
        self._fullscreen_act.toggled.connect(self._toggle_fullscreen_mode)
        view_menu.addAction(self._fullscreen_act)
        self.addAction(self._fullscreen_act)
        view_menu.addSeparator()

        self._save_dir_act = QAction("Set Save Directory...", self)
        self._save_dir_act.triggered.connect(self._choose_save_directory)
        rec_menu.addAction(self._save_dir_act)

        self._reset_save_dir_act = QAction("Use Default Recordings Folder", self)
        self._reset_save_dir_act.triggered.connect(self._reset_save_directory)
        rec_menu.addAction(self._reset_save_dir_act)
        rec_menu.addSeparator()

        self._analysis_transfer_start_act = QAction("Start Analysis Transfer Server", self)
        self._analysis_transfer_start_act.triggered.connect(self._start_analysis_transfer_server)
        transfer_menu.addAction(self._analysis_transfer_start_act)

        self._analysis_transfer_stop_act = QAction("Stop Analysis Transfer Server", self)
        self._analysis_transfer_stop_act.triggered.connect(self._stop_analysis_transfer_server)
        transfer_menu.addAction(self._analysis_transfer_stop_act)

        self._analysis_transfer_restart_act = QAction("Restart Analysis Transfer Server", self)
        self._analysis_transfer_restart_act.triggered.connect(self._restart_analysis_transfer_server)
        transfer_menu.addAction(self._analysis_transfer_restart_act)

        copy_transfer_url_act = QAction("Copy Transfer URL", self)
        copy_transfer_url_act.triggered.connect(self._copy_analysis_transfer_url)
        transfer_menu.addAction(copy_transfer_url_act)

        self._reverse_act = QAction("Reverse Drive", self)
        self._reverse_act.setCheckable(True)
        self._reverse_act.setChecked(bool(self._reverse_enabled))
        if str(REVERSE_TOGGLE_SHORTCUT or "").strip().upper() != "R":
            self._reverse_act.setShortcut(REVERSE_TOGGLE_SHORTCUT)
        self._reverse_act.setToolTip(
            "Swap to reverse driving mode: flips surge/sway while keeping yaw and the video layout unchanged."
        )
        self._reverse_act.toggled.connect(self._toggle_reverse_mode)
        view_menu.addAction(self._reverse_act)
        self.addAction(self._reverse_act)

        water_act = QAction("Water Correction (out-of-water mode)", self)
        water_act.setCheckable(True)
        water_act.setChecked(False)
        water_act.setToolTip(
            "Simulate underwater optics for bench testing: undistorts fisheye "
            "and narrows FOV to match what the exploreHD sees when submerged."
        )
        water_act.toggled.connect(self._toggle_water_correction)
        view_menu.addAction(water_act)

        layout_menu = view_menu.addMenu("Camera Layout")
        for label, pane_count in [("Single Camera", 1), ("Stacked Dual Camera", 2), ("Quad Camera", 4)]:
            act = QAction(label, self)
            act.triggered.connect(lambda _checked=False, panes=pane_count: self._set_video_layout(panes))
            layout_menu.addAction(act)

        # Stream log (JSONL)
        start_log = QAction("Start Stream Log", self)
        start_log.triggered.connect(self._start_stream_log)
        rec_menu.addAction(start_log)

        stop_log = QAction("Stop Stream Log", self)
        stop_log.triggered.connect(self._stop_stream_log)
        rec_menu.addAction(stop_log)

        self._refresh_save_directory_actions()
        self._refresh_analysis_transfer_actions()

        quit_act = QAction("Quit", self)
        quit_act.triggered.connect(self.close)
        file_menu.addAction(quit_act)

    def closeEvent(self, event):
        try:
            app = QApplication.instance()
            if app is not None:
                app.removeEventFilter(self)
        except Exception:
            pass
        for timer_name in ("_link_timer", "_analysis_transfer_timer", "_sensor_ui_timer", "_tether_ui_timer", "_ui_lag_timer", "_transect_status_timer"):
            try:
                timer = getattr(self, timer_name, None)
                if timer is not None:
                    timer.stop()
            except Exception:
                pass
        try:
            self._stop_transect_cv(background=False)  # finish receiver shutdown before exit
        except Exception:
            pass
        try:
            if self._stereo_recording:
                self._stereo_recording = False
                stop_event = getattr(self, "_stereo_recording_stop", None)
                if stop_event is not None:
                    stop_event.set()
            recording_thread = getattr(self, "_stereo_recording_thread", None)
            if recording_thread is not None:
                recording_thread.join(timeout=5.0)
        except Exception:
            pass
        # Finalize an in-progress single-camera video recording so the mp4 is
        # closed cleanly (EOS) instead of being abandoned on exit.
        try:
            recorder = getattr(self, "_video_recorder", None)
            if recorder is not None:
                self._video_recording = False
                self._video_recorder = None
                recorder.stop(grace_s=5.0)
                stream = getattr(self, "_video_recording_stream", None)
                mirrors = getattr(self.cam_mgr, "recording_mirror_ports", None)
                if isinstance(mirrors, dict) and stream:
                    mirrors.pop(stream, None)
        except Exception:
            pass
        try:
            if self.video_panel is not None:
                self.video_panel.stop_all()
        except Exception:
            pass
        try:
            closer = getattr(self.cam_mgr, "close_snapshot_taps", None)
            if callable(closer):
                closer(reason="app_close")
        except Exception:
            pass
        try:
            session = getattr(self, "_stereo_capture_session", None)
            if session is not None:
                session.stop()
        except Exception:
            pass
        video_panel = self.video_panel
        self.video_panel = None
        # Stop stream/data logging.
        try:
            self._stop_stream_log()
        except Exception:
            pass

        try:
            self._netdiag_stop.set()
        except Exception:
            pass
        try:
            self._tether_probe_stop.set()
        except Exception:
            pass
        try:
            self._stop_analysis_transfer_server()
        except Exception:
            pass
        # stop services
        try:
            self.sensor_svc.stop()
        except Exception:
            pass
        try:
            self.pilot_svc.stop()
        except Exception:
            pass
        try:
            self._management_page.shutdown()
        except Exception:
            pass
        try:
            self.hold_test_panel.shutdown()
        except Exception:
            pass
        try:
            self.raw_sensor_page.shutdown()
        except Exception:
            pass
        try:
            self._ssh_page.shutdown()
        except Exception:
            pass
        if video_panel is not None:
            try:
                video_panel.setParent(None)
                video_panel.deleteLater()
            except Exception:
                pass
        super().closeEvent(event)

    def _start_stream_log(self, out_dir: "Path | None" = None):
        if self._stream_recorder is not None:
            return
        location = None
        try:
            if out_dir is not None:
                out_dir = Path(out_dir)
                out_dir.mkdir(parents=True, exist_ok=True)
            else:
                out_dir, location = self._log_output_dir()
        except Exception as exc:
            self.statusBar().showMessage(f"Could not prepare save directory: {exc}", 5000)
            return
        self._record_dir = str(out_dir)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        target = out_dir / f"{stamp}_streams.jsonl"
        counter = 1
        while target.exists():
            counter += 1
            target = out_dir / f"{stamp}_streams_{counter:02d}.jsonl"
        self._stream_recorder = StreamRecorder(target)
        try:
            self._stream_recorder.start()
        except Exception as exc:
            self._stream_recorder = None
            self._record_dir = None
            self.statusBar().showMessage(f"Could not start stream log: {exc}", 5000)
            return
        self._stream_log_path = str(target)
        self.statusBar().showMessage(f"Recording streams -> {target}{self._save_location_note(location)}", 5000)

    def _stop_stream_log(self):
        if self._stream_recorder is None:
            return
        self._stream_recorder.stop()
        self._stream_recorder = None
        self._stream_log_path = None
        self.statusBar().showMessage("Stream recording stopped", 3000)

    def record_tracking_sample(self, payload: dict) -> None:
        """Log one optical-tracking sample (model error/command) to the active
        stream log under the "tracking" stream, aligned with video + state.

        This is the hook the future CV/policy calls each tick so its inputs and
        outputs are captured alongside the video for offline model iteration.
        """
        rec = self._stream_recorder
        if rec is not None:
            try:
                rec.record("tracking", dict(payload or {}))
            except Exception:
                pass
