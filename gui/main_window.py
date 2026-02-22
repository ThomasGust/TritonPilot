# gui/main_window.py
from __future__ import annotations

import os
import socket
import threading
import time
from collections import deque
from pathlib import Path

from PyQt6.QtCore import pyqtSignal, QObject, Qt, QTimer
from PyQt6.QtGui import QAction
from PyQt6.QtWidgets import (
    QMainWindow,
    QWidget,
    QHBoxLayout,
    QVBoxLayout,
    QLabel,
    QSizePolicy,
)
from config import (
    PILOT_PUB_ENDPOINT,
    SENSOR_SUB_ENDPOINT,
    CONTROLLER_DEADZONE,
    CONTROLLER_INDEX,
    CONTROLLER_DEBUG,
    CONTROLLER_DUMP_RAW_EVERY_S,
    ROV_HOST,
    DEPTH_HOLD_WALK_DEADBAND,
    DEPTH_HOLD_WALK_RATE_MPS,
    DEPTH_HOLD_SENSOR_STALE_S,
)

from network.net_select import list_local_ipv4_addrs
from input.pilot_service import PilotPublisherService
from telemetry.sensor_service import SensorSubscriberService
from video.cam import RemoteCameraManager
from recording.stream_recorder import StreamRecorder
from gui.video_tabs import VideoTabs
from gui.sensor_panel import SensorPanel
from gui.instruments import InstrumentPanel

class MainWindow(QMainWindow):
    # we'll receive sensor messages from a background thread → emit to UI thread
    sensor_msg_sig = pyqtSignal(dict)
    pilot_status_sig = pyqtSignal(dict)
    pilot_msg_sig = pyqtSignal(dict)

    def _set_status(self, lbl: QLabel, text: str) -> None:
        """Set status text + tooltip (so truncated UI still preserves full info)."""
        try:
            lbl.setText(text)
            lbl.setToolTip(text)
        except Exception:
            pass

    def __init__(self, streams_path: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("ROV Topside (PyQt6)")

        # link status
        self._last_sensor_ts = 0.0
        self._last_hb_ts = 0.0
        self._last_hb = {}
        self._hb_period_ema_s: float | None = None
        self._prev_hb_rx_ts: float | None = None
        self._link_state_last: str = "NO DATA"

        self._link_lbl = QLabel("Link: (no data)")
        self.statusBar().addPermanentWidget(self._link_lbl)

        self._ctrl_lbl = QLabel("Controller: (starting)")
        self.statusBar().addPermanentWidget(self._ctrl_lbl)

        self._video_lbl = QLabel("Video: -")
        self.statusBar().addPermanentWidget(self._video_lbl)

        # quick depth readout (from external depth sensor)
        self._depth_lbl = QLabel("Depth: -")
        self.statusBar().addPermanentWidget(self._depth_lbl)
        self._last_depth_ts = 0.0
        self._last_depth: dict = {}

        # quick power readout (from Power Sense Module conversion on ROV)
        self._power_lbl = QLabel("Power: -")
        self.statusBar().addPermanentWidget(self._power_lbl)
        self._last_power_ts = 0.0
        self._last_power: dict = {}

        # Depth-hold setpoint tracking (topside estimate).
        # This is purely for UI visibility; the real controller runs on the ROV.
        self._dh_enabled: bool = False
        self._dh_target_m: float | None = None
        self._dh_prev_pilot_ts: float | None = None

        # network status (tether vs wifi, local route to ROV, remote link state)
        self._net_lbl = QLabel("Net: -")
        self.statusBar().addPermanentWidget(self._net_lbl)

        # pilot mode indicator (local modes we transmit to the ROV)
        self._mode_lbl = QLabel("Mode: MANUAL")
        self.statusBar().addPermanentWidget(self._mode_lbl)
        self._last_net_ts = 0.0
        self._last_net: dict = {}
        self._route_cache = {"ts": 0.0, "iface": None, "src_ip": None, "is_wifi": None, "err": None}
        self._rov_host = str(ROV_HOST)
        self._netdiag_port = int(os.environ.get("TRITON_NETDIAG_PORT", "7700"))
        self._netdiag_stop = threading.Event()
        self._netdiag_lock = threading.Lock()
        self._netdiag = {"ts": 0.0, "ok": False, "last_rtt_ms": None, "avg_rtt_ms": None, "jitter_ms": None, "loss_pct": None, "err": None}
        self._netdiag_thread = threading.Thread(target=self._netdiag_probe_loop, daemon=True)
        self._netdiag_thread.start()

        # Make status widgets readable and avoid losing info when space is tight.
        for _lbl, _w in [
            (self._link_lbl, 260),
            (self._ctrl_lbl, 220),
            (self._video_lbl, 220),
            (self._depth_lbl, 220),
            (self._power_lbl, 240),
            (self._net_lbl, 300),
            (self._mode_lbl, 200),
        ]:
            try:
                _lbl.setMinimumWidth(int(_w))
                _lbl.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
                _lbl.setToolTip(_lbl.text())
                _lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            except Exception:
                pass

        self._link_timer = QTimer(self)
        self._link_timer.timeout.connect(self._update_link_status)
        self._link_timer.start(200)


        # connect signals to slots
        self.sensor_msg_sig.connect(self._handle_sensor_msg_on_ui)
        self.pilot_status_sig.connect(self._handle_pilot_status_on_ui)
        self.pilot_msg_sig.connect(self._handle_pilot_msg_on_ui)
        self._last_ctrl_status: dict = {'controller': 'unknown'}

        # 1) pilot publisher (xbox -> ROV)
        self.pilot_svc = PilotPublisherService(
            endpoint=PILOT_PUB_ENDPOINT,
            rate_hz=30.0,
            deadzone=CONTROLLER_DEADZONE,
            debug=CONTROLLER_DEBUG,
            index=CONTROLLER_INDEX,
            dump_raw_every_s=CONTROLLER_DUMP_RAW_EVERY_S,
            on_status=self._on_pilot_status_from_thread,
            on_send=self._on_pilot_msg_from_thread,
        )
        self.pilot_svc.start()

        # optional stream recorder (pilot + sensors + heartbeat)
        self._stream_recorder: StreamRecorder | None = None
        self._record_dir: str | None = None

        # 2) sensor subscriber (ROV -> topside)
        self.sensor_panel = SensorPanel()
        self.instrument_panel = InstrumentPanel()
        self._sensor_ui_pending: dict[tuple[str, str], dict] = {}
        self._sensor_ui_pending_order: list[tuple[str, str]] = []
        self._sensor_ui_max_batch = 32
        self._sensor_ui_timer = QTimer(self)
        self._sensor_ui_timer.setInterval(50)  # ~20 Hz UI refresh cap for sensor table/widgets
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
        try:
            if not os.path.exists(streams_path):
                # Don't block startup; just disable video.
                self.statusBar().showMessage(f"Streams config not found: {streams_path}", 10000)
            else:
                self.cam_mgr = RemoteCameraManager(streams_path)
                stream_names = self.cam_mgr.list_available()
                if stream_names:
                    self.video_panel = VideoTabs(self.cam_mgr, stream_names=stream_names)
                else:
                    self.statusBar().showMessage("No enabled video streams in streams.json", 8000)
        except Exception as e:
            self.cam_mgr = None
            self.video_panel = None
            self.statusBar().showMessage(f"Video init failed (continuing without video): {e}", 12000)

        # layout
        central = QWidget()
        outer = QHBoxLayout(central)
        if self.video_panel is not None:
            outer.addWidget(self.video_panel, 2)

        # Right column: sensor table
        right_col = QWidget()
        right_lay = QVBoxLayout(right_col)
        right_lay.setContentsMargins(0, 0, 0, 0)
        right_lay.addWidget(self.instrument_panel, 0)
        right_lay.addWidget(self.sensor_panel, 3)
        outer.addWidget(right_col, 1)

        self.setCentralWidget(central)

        self._make_menu()

        self.resize(1200, 700)

    # background → UI
    def _on_sensor_msg_from_thread(self, msg: dict):
        # called in sensor thread
        if self._stream_recorder is not None:
            self._stream_recorder.record("sensors", msg)
        self.sensor_msg_sig.emit(msg)


    def _on_pilot_status_from_thread(self, status: dict):
        # Called from the pilot publisher thread; marshal to UI thread.
        self.pilot_status_sig.emit(status)

    def _on_pilot_msg_from_thread(self, msg: dict):
        # Called from the pilot publisher thread; marshal to UI thread.
        if self._stream_recorder is not None:
            self._stream_recorder.record("pilot", msg)
        self.pilot_msg_sig.emit(msg)

    def _handle_pilot_msg_on_ui(self, msg: dict):
        # Camera tab switching (local UI only):
        #   B -> next stream (to the right)
        #   X -> previous stream (to the left)
        try:
            edges = (msg or {}).get("edges", {}) or {}
            if self.video_panel is not None:
                if edges.get("b") == "down":
                    if hasattr(self.video_panel, "next_stream"):
                        self.video_panel.next_stream()
                    else:
                        self.video_panel.tabs.setCurrentIndex((self.video_panel.tabs.currentIndex() + 1) % max(1, self.video_panel.tabs.count()))
                if edges.get("x") == "down":
                    if hasattr(self.video_panel, "prev_stream"):
                        self.video_panel.prev_stream()
                    else:
                        self.video_panel.tabs.setCurrentIndex((self.video_panel.tabs.currentIndex() - 1) % max(1, self.video_panel.tabs.count()))
        except Exception:
            pass

        # Update mode indicator from locally-transmitted modes.
        # Also maintain a simple "walk target" estimate so the pilot can see the
        # *intended* setpoint even if the onboard controller temporarily pauses.
        try:
            modes = (msg or {}).get("modes", {}) or {}
            dh = bool(modes.get("depth_hold", False))

            # Compute dt from pilot timestamps.
            ts = float((msg or {}).get("ts", 0.0) or 0.0)
            dt = None
            if self._dh_prev_pilot_ts is not None and ts > 0:
                dt = max(0.0, min(0.25, ts - float(self._dh_prev_pilot_ts)))
            self._dh_prev_pilot_ts = ts if ts > 0 else self._dh_prev_pilot_ts

            # Rising edge: capture current depth as the initial setpoint.
            if dh and (not self._dh_enabled):
                try:
                    if (self._last_depth or {}).get("error"):
                        self._dh_target_m = None
                    else:
                        d = (self._last_depth or {}).get("depth_m", None)
                        self._dh_target_m = float(d) if d is not None else None
                except Exception:
                    self._dh_target_m = None

            # Falling edge: clear setpoint.
            if (not dh) and self._dh_enabled:
                self._dh_target_m = None

            self._dh_enabled = dh

            # Walk target while enabled.
            if dh and self._dh_target_m is not None and dt is not None and dt > 0:
                axes = (msg or {}).get("axes", {}) or {}
                heave = float(axes.get("ry", 0.0) or 0.0)
                if abs(heave) > float(DEPTH_HOLD_WALK_DEADBAND):
                    # heave > 0 means "UP" => setpoint depth decreases
                    self._dh_target_m += (-heave) * float(DEPTH_HOLD_WALK_RATE_MPS) * float(dt)

            # Compose status text.
            if dh:
                # Depth freshness (UI only)
                import time
                depth_stale = (time.time() - float(self._last_depth_ts)) > float(DEPTH_HOLD_SENSOR_STALE_S)

                z_txt = "-"
                try:
                    if (self._last_depth or {}).get("error"):
                        z_txt = "ERR"
                    else:
                        d = (self._last_depth or {}).get("depth_m", None)
                        if d is not None:
                            z_txt = f"{float(d):.2f}m"
                except Exception:
                    pass

                t_txt = "-"
                if self._dh_target_m is not None:
                    t_txt = f"{float(self._dh_target_m):.2f}m"

                #s = f"Mode: DEPTH HOLD (z {z_txt} → set {t_txt})"
                s = f"z {z_txt} → set {t_txt}"
                if depth_stale:
                    s += " [DEPTH STALE]"
                self._set_status(self._mode_lbl, s)
            else:
                self._set_status(self._mode_lbl, "Mode: MANUAL")
        except Exception:
            self._set_status(self._mode_lbl, "Mode: -")

    def _handle_pilot_status_on_ui(self, status: dict):
        self._last_ctrl_status = status or {'controller': 'unknown'}
        state = (status or {}).get('controller', 'unknown')
        if state == 'connected':
            name = (status or {}).get('name') or 'controller'
            self._set_status(self._ctrl_lbl, f"Controller: OK ({name})")
        elif state == 'disconnected':
            err = (status or {}).get('error') or 'not connected'
            self._set_status(self._ctrl_lbl, f"Controller: - ({err})")
        elif state == 'stopped':
            self._set_status(self._ctrl_lbl, "Controller: stopped")
        else:
            self._set_status(self._ctrl_lbl, f"Controller: {state}")

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
        elif typ == "net" or msg.get("sensor") == "network":
            self._last_net_ts = time.time()
            self._last_net = msg
        else:
            self._last_sensor_ts = time.time()

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

    def _flush_sensor_ui(self) -> None:
        """Apply coalesced sensor updates to UI widgets at a bounded rate."""
        try:
            n = 0
            while self._sensor_ui_pending_order and n < int(self._sensor_ui_max_batch):
                key = self._sensor_ui_pending_order.pop(0)
                msg = self._sensor_ui_pending.pop(key, None)
                if not isinstance(msg, dict):
                    continue
                try:
                    self.instrument_panel.update_from_sensor(msg)
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

        parts = [f"Link: {status}"]
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
        elif sensor_age is not None:
            parts.append(f"sensor_age={sensor_age:.2f}s")

        self._set_status(self._link_lbl, " | ".join(parts))
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

        # Video indicator (show per-stream state; do not throw on missing video)
        try:
            if self.video_panel is None:
                self._set_status(self._video_lbl, "Video: -")
            else:
                name = self.video_panel.current_stream_name()
                vw = self.video_panel.current_video_widget()
                if name is None or vw is None:
                    self._set_status(self._video_lbl, "Video: -")
                else:
                    st = vw.status()
                    if st.get("state") == "playing":
                        age = st.get("age_s")
                        if age is not None:
                            self._set_status(self._video_lbl, f"Video: {name} (OK, age={age:.1f}s)")
                        else:
                            self._set_status(self._video_lbl, f"Video: {name} (OK)")
                    elif st.get("state") == "waiting":
                        self._set_status(self._video_lbl, f"Video: {name} (waiting)")
                    else:
                        self._set_status(self._video_lbl, f"Video: {name} ({st.get('state')})")
        except Exception:
            self._set_status(self._video_lbl, "Video: -")

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
            parts += ["|", "⚠ " + "; ".join(warns)]

        self._set_status(self._net_lbl, " ".join(parts))

    def _make_menu(self):
        bar = self.menuBar()
        file_menu = bar.addMenu("&File")
        rec_menu = bar.addMenu("&Record")

        # Stream log (JSONL)
        start_log = QAction("Start Stream Log", self)
        start_log.triggered.connect(self._start_stream_log)
        rec_menu.addAction(start_log)

        stop_log = QAction("Stop Stream Log", self)
        stop_log.triggered.connect(self._stop_stream_log)
        rec_menu.addAction(stop_log)

        rec_menu.addSeparator()

        snap_act = QAction("Save Snapshot", self)
        snap_act.triggered.connect(self._save_snapshot)
        rec_menu.addAction(snap_act)

        start_vid = QAction("Start Video Recording", self)
        start_vid.triggered.connect(self._start_video_recording)
        rec_menu.addAction(start_vid)

        stop_vid = QAction("Stop Video Recording", self)
        stop_vid.triggered.connect(self._stop_video_recording)
        rec_menu.addAction(stop_vid)

        quit_act = QAction("Quit", self)
        quit_act.triggered.connect(self.close)
        file_menu.addAction(quit_act)

    def closeEvent(self, event):
        try:
            if self.video_panel is not None:
                self.video_panel.stop_all()
        except Exception:
            pass
        # stop recorders
        try:
            self._stop_stream_log()
        except Exception:
            pass

        try:
            self._netdiag_stop.set()
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
        if self.video_panel is not None:
            self.video_panel.close()
        super().closeEvent(event)
    def _start_stream_log(self):
        if self._stream_recorder is not None:
            return
        session_dir = StreamRecorder.make_session_dir("recordings")
        self._record_dir = str(session_dir)
        self._stream_recorder = StreamRecorder(session_dir / "streams.jsonl")
        self._stream_recorder.start()

        # record pilot frames via callback
        try:
            self.pilot_svc.on_send = lambda msg: self._stream_recorder.record("pilot", msg)  # type: ignore
        except Exception:
            pass

        self.statusBar().showMessage(f"Recording streams → {self._record_dir}", 5000)

    def _stop_stream_log(self):
        if self._stream_recorder is None:
            return
        try:
            self.pilot_svc.on_send = None  # type: ignore
        except Exception:
            pass
        self._stream_recorder.stop()
        self._stream_recorder = None
        self.statusBar().showMessage("Stream recording stopped", 3000)
    def _current_video_widget(self):
        if self.video_panel is None:
            return None
        try:
            return self.video_panel.current_video_widget()
        except Exception:
            return None

    def _save_snapshot(self):
        if self.video_panel is None:
            return
        out_dir = self._record_dir or str(StreamRecorder.make_session_dir("recordings"))
        path = self.video_panel.save_snapshot(out_dir=out_dir)
        if path:
            self.statusBar().showMessage(f"Saved snapshot: {path}", 5000)
        else:
            self.statusBar().showMessage("No frame yet (snapshot not saved)", 3000)

    def _start_video_recording(self):
        vw = self._current_video_widget()
        if vw is None:
            return
        out_dir = self._record_dir or str(StreamRecorder.make_session_dir("recordings"))
        vw.start_recording(out_dir=out_dir, basename=vw.stream_name, fps=30.0)
        self.statusBar().showMessage(f"Video recording started → {out_dir}", 5000)

    def _stop_video_recording(self):
        vw = self._current_video_widget()
        if vw is None:
            return
        vw.stop_recording()
        self.statusBar().showMessage("Video recording stopped", 3000)


