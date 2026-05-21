from __future__ import annotations

import math
from typing import Optional

from PyQt6.QtCore import Qt, QRectF, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QPainter, QPen
from PyQt6.QtWidgets import (
    QWidget,
    QFrame,
    QLabel,
    QGridLayout,
    QVBoxLayout,
    QHBoxLayout,
    QPushButton,
    QProgressBar,
    QScrollArea,
    QSizePolicy,
)

from network.management_rpc import ManagementRpcService


class _Card(QFrame):
    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        self.setObjectName("InstrumentCard")
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

        self.title = QLabel(title)
        f = QFont(self.title.font())
        f.setBold(True)
        self.title.setFont(f)
        self.title.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.body = QVBoxLayout()
        self.body.setContentsMargins(6, 4, 6, 6)
        self.body.setSpacing(6)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(6)
        lay.addWidget(self.title)
        lay.addLayout(self.body)

        self.setStyleSheet(
            """
            QFrame#InstrumentCard {
                border: 1px solid #2a2a32;
                border-radius: 10px;
                background: #16161b;
            }
            QProgressBar {
                border: 1px solid #2a2a32;
                border-radius: 6px;
                text-align: center;
                background: #111115;
                min-height: 14px;
            }
            QProgressBar::chunk {
                background: #5a86ff;
                border-radius: 5px;
            }
            """
        )


class VerticalGaugeWidget(QWidget):
    def __init__(self, *, label: str, unit: str, vmin: float, vmax: float, invert: bool = False, parent=None):
        super().__init__(parent)
        self.label = label
        self.unit = unit
        self.vmin = float(vmin)
        self.vmax = float(vmax)
        self.invert = bool(invert)
        self.value: Optional[float] = None
        self.secondary: str = ""
        self.state_text: str = "-"
        self.setMinimumSize(90, 140)

    def set_value(self, value: Optional[float], *, secondary: str = "", state_text: str = ""):
        self.value = None if value is None else float(value)
        self.secondary = str(secondary or "")
        self.state_text = str(state_text or "")
        self.update()

    def paintEvent(self, _event):  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        r = self.rect().adjusted(6, 6, -6, -6)

        p.setPen(QPen(QColor(42, 42, 50), 1))
        p.setBrush(QColor(22, 22, 27))
        p.drawRoundedRect(QRectF(r), 10.0, 10.0)

        p.setPen(QColor(235, 235, 235))
        p.drawText(r.adjusted(6, 4, -6, -4), Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter, self.label)

        tube = QRectF(r.left() + r.width() * 0.28, r.top() + 26, r.width() * 0.44, r.height() - 64)
        p.setBrush(QColor(12, 12, 16))
        p.setPen(QPen(QColor(70, 70, 84), 1))
        p.drawRoundedRect(tube, 7.0, 7.0)

        p.setPen(QPen(QColor(90, 90, 105), 1))
        for i in range(6):
            y = tube.bottom() - (tube.height() * i / 5.0)
            p.drawLine(int(tube.right() + 4), int(y), int(tube.right() + 10), int(y))

        frac = None
        if self.value is not None and math.isfinite(self.value):
            span = max(1e-6, self.vmax - self.vmin)
            frac = (self.value - self.vmin) / span
            frac = max(0.0, min(1.0, frac))
            if self.invert:
                frac = 1.0 - frac

        if frac is not None:
            fill_h = tube.height() * frac
            fill = QRectF(tube.left() + 2, tube.bottom() - fill_h + 2, tube.width() - 4, max(0.0, fill_h - 4))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(90, 134, 255))
            p.drawRoundedRect(fill, 5.0, 5.0)

            y = tube.bottom() - tube.height() * frac
            p.setPen(QPen(QColor(255, 220, 90), 2))
            p.drawLine(int(tube.left() - 6), int(y), int(tube.right() + 6), int(y))

        vtxt = "-" if self.value is None else f"{self.value:.2f} {self.unit}".strip()
        p.setPen(QColor(235, 235, 235))
        p.drawText(r.adjusted(4, 0, -4, -20), Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignHCenter, vtxt)

        if self.secondary or self.state_text:
            p.setPen(QColor(190, 190, 200))
            line = self.secondary if self.secondary else self.state_text
            p.drawText(r.adjusted(4, 0, -4, -4), Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignHCenter, line)


class InstrumentPanel(QWidget):
    """Compact pilot-oriented instruments fed by telemetry messages."""

    def __init__(self, parent=None):
        super().__init__(parent)

        self.depth_card = _Card("Depth")
        self.depth_gauge = VerticalGaugeWidget(label="Depth", unit="m", vmin=0.0, vmax=30.0)
        self.depth_meta = QLabel("-")
        self.depth_meta.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.depth_meta.setWordWrap(True)
        self.depth_card.body.addWidget(self.depth_gauge)
        self.depth_card.body.addWidget(self.depth_meta)

        self.env_card = _Card("Temp / Leak")
        self.temp_bar = QProgressBar()
        self.temp_bar.setRange(0, 1000)
        self.temp_bar.setFormat("Temp: -")
        self.leak_lbl = QLabel("Leak: unknown")
        self.leak_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        for w in (self.temp_bar, self.leak_lbl):
            self.env_card.body.addWidget(w)

        grid = QGridLayout(self)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(8)
        grid.addWidget(self.depth_card, 0, 0)
        grid.addWidget(self.env_card, 0, 1)

    def update_from_sensor(self, msg: dict) -> None:
        typ = (msg or {}).get("type")
        if typ == "external_depth":
            try:
                if (msg or {}).get("error"):
                    self.depth_gauge.set_value(None, state_text="ERR")
                    self.depth_meta.setText(str((msg or {}).get("error")))
                else:
                    depth = (msg or {}).get("depth_m")
                    temp = (msg or {}).get("temperature_c")
                    pressure = (msg or {}).get("pressure_mbar")
                    self.depth_gauge.set_value(
                        None if depth is None else float(depth),
                        secondary=(f"{float(temp):.1f} C" if temp is not None else ""),
                    )
                    meta = []
                    if pressure is not None:
                        meta.append(f"{float(pressure):.0f} mbar")
                    if temp is not None:
                        meta.append(f"{float(temp):.1f} C")
                    self.depth_meta.setText(" | ".join(meta) if meta else "-")
            except Exception:
                pass
            return

        if typ == "env":
            try:
                temp = (msg or {}).get("temperature_c")
                p_kpa = (msg or {}).get("pressure_kpa")
                if temp is not None:
                    t_f = float(temp)
                    frac = max(0.0, min(1.0, (t_f + 10.0) / 100.0))
                    self.temp_bar.setValue(int(round(frac * 1000)))
                    if p_kpa is None:
                        self.temp_bar.setFormat(f"Temp: {t_f:.1f} C")
                    else:
                        self.temp_bar.setFormat(f"Temp: {t_f:.1f} C  |  {float(p_kpa):.1f} kPa")
            except Exception:
                pass
            return

        if typ == "power":
            return

        if typ == "leak":
            try:
                leak = bool((msg or {}).get("leak", False))
                self.leak_lbl.setText("Leak: DETECTED" if leak else "Leak: OK")
                if leak:
                    self.leak_lbl.setStyleSheet("color: #ff8d8d; font-weight: bold;")
                else:
                    self.leak_lbl.setStyleSheet("color: #9be89b;")
            except Exception:
                pass


class HoldTestPanel(QWidget):
    """Focused depth-hold panel for stabilization testing."""

    rpc_result_sig = pyqtSignal(dict)

    def __init__(self, pilot_svc=None, endpoint: str | None = None, parent=None):
        super().__init__(parent)

        from config import LIGHTS_TOGGLE_SHORTCUT, MANAGEMENT_RPC_ENDPOINT

        self._pilot_svc = pilot_svc
        self._endpoint = str(endpoint or MANAGEMENT_RPC_ENDPOINT)
        self._lights_shortcut_text = str(LIGHTS_TOGGLE_SHORTCUT or "L").strip() or "L"
        self._runtime_labels: dict[str, QLabel] = {}
        self._runtime_request_pending = False
        self._svc = ManagementRpcService(endpoint=self._endpoint, on_result=self._on_rpc_result_from_thread)
        self._svc.start()
        self.setMinimumWidth(520)

        title = QLabel("Hold Test")
        title_font = QFont(title.font())
        title_font.setBold(True)
        title_font.setPointSize(max(12, title_font.pointSize() + 1))
        title.setFont(title_font)

        subtitle = QLabel("Single-camera piloting page with autopilot controls, runtime telemetry, and live depth instruments.")
        subtitle.setWordWrap(True)
        subtitle.setStyleSheet("color: #b6bac8;")

        self.control_card = _Card("Hold Controls")
        self.feedback_label = QLabel("Use this page to engage depth hold and verify the onboard controller.")
        self.feedback_label.setWordWrap(True)
        self.feedback_label.setStyleSheet("color: #b6bac8;")
        self.control_card.body.addWidget(self.feedback_label)

        button_row = QHBoxLayout()
        button_row.setSpacing(6)
        self.depth_hold_toggle_btn = QPushButton("Toggle Depth Hold")
        self.depth_hold_toggle_btn.clicked.connect(self._toggle_depth_hold)
        button_row.addWidget(self.depth_hold_toggle_btn)
        self.rp_level_toggle_btn = QPushButton("Roll/Pitch Level")
        self.rp_level_toggle_btn.clicked.connect(self._toggle_roll_pitch_level)
        button_row.addWidget(self.rp_level_toggle_btn)
        self.yaw_hold_toggle_btn = QPushButton("Yaw Hold")
        self.yaw_hold_toggle_btn.clicked.connect(self._toggle_yaw_hold)
        button_row.addWidget(self.yaw_hold_toggle_btn)
        self.control_card.body.addLayout(button_row)

        control_grid = QGridLayout()
        control_grid.setHorizontalSpacing(10)
        control_grid.setVerticalSpacing(6)
        for row, (label_text, key) in enumerate(
            [
                ("Pilot Depth Hold", "pilot_depth_hold"),
                ("Pilot Roll/Pitch Level", "pilot_rp_level"),
                ("Pilot Yaw Hold", "pilot_yaw_hold"),
                ("Runtime RPC", "runtime_rpc"),
            ]
        ):
            label = QLabel(label_text)
            value = QLabel("-")
            value.setWordWrap(True)
            control_grid.addWidget(label, row, 0)
            control_grid.addWidget(value, row, 1)
            self._runtime_labels[key] = value
        self.control_card.body.addLayout(control_grid)

        shortcut_hint = QLabel(f"R3 toggles depth hold; L3 and {self._lights_shortcut_text.upper()} toggle lights.")
        shortcut_hint.setWordWrap(True)
        shortcut_hint.setStyleSheet("color: #8f96aa;")
        self.control_card.body.addWidget(shortcut_hint)

        self.runtime_card = _Card("Hold Runtime")
        runtime_grid = QGridLayout()
        runtime_grid.setHorizontalSpacing(10)
        runtime_grid.setVerticalSpacing(6)
        for row, (label_text, key) in enumerate(
            [
                ("Control Loop", "runtime_loop"),
                ("Armed", "runtime_armed"),
                ("Autopilot Runtime", "runtime_autopilot"),
                ("Depth Hold Runtime", "runtime_depth_hold"),
                ("Attitude Runtime", "runtime_attitude"),
                ("Attitude Sensor", "runtime_attitude_sensor"),
                ("Attitude Debug", "runtime_attitude_debug"),
                ("Depth Sensor", "runtime_depth_sensor"),
                ("Depth Debug", "runtime_depth_debug"),
            ]
        ):
            label = QLabel(label_text)
            value = QLabel("-")
            value.setWordWrap(True)
            runtime_grid.addWidget(label, row, 0)
            runtime_grid.addWidget(value, row, 1)
            self._runtime_labels[key] = value
        self.runtime_card.body.addLayout(runtime_grid)

        self.depth_card = _Card("Depth")
        self.depth_gauge = VerticalGaugeWidget(label="Depth", unit="m", vmin=0.0, vmax=30.0)
        self.depth_readout = QLabel("Depth: -")
        depth_font = QFont(self.depth_readout.font())
        depth_font.setBold(True)
        depth_font.setPointSize(max(13, depth_font.pointSize() + 2))
        self.depth_readout.setFont(depth_font)
        self.depth_readout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.depth_meta = QLabel("-")
        self.depth_meta.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.depth_meta.setWordWrap(True)
        self.depth_card.body.addWidget(self.depth_gauge)
        self.depth_card.body.addWidget(self.depth_readout)
        self.depth_card.body.addWidget(self.depth_meta)

        content = QWidget()
        lay = QVBoxLayout(content)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        lay.addWidget(title)
        lay.addWidget(subtitle)
        lay.addWidget(self.control_card)
        lay.addWidget(self.runtime_card)
        lay.addWidget(self.depth_card)
        lay.addStretch(1)

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(content)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(scroll)

        self.rpc_result_sig.connect(self._handle_rpc_result)
        self._runtime_timer = QTimer(self)
        self._runtime_timer.setInterval(500)
        self._runtime_timer.timeout.connect(self._poll_runtime_state)
        self._runtime_timer.start()
        self._sync_local_hold_controls()
        QTimer.singleShot(0, self._poll_runtime_state)

    def shutdown(self) -> None:
        self._runtime_timer.stop()
        self._svc.stop()

    def _on_rpc_result_from_thread(self, result: dict) -> None:
        self.rpc_result_sig.emit(result)

    def _poll_runtime_state(self) -> None:
        self._sync_local_hold_controls()
        if not self.isVisible() or self._runtime_request_pending:
            return
        self._runtime_request_pending = True
        self._svc.request("get_hold_status", {})

    def _handle_rpc_result(self, result: dict) -> None:
        self._runtime_request_pending = False
        if result.get("ok"):
            self._runtime_labels["runtime_rpc"].setText(f"Connected | {self._endpoint}")
            self._apply_runtime_state(dict(result.get("data") or {}))
            return
        self._runtime_labels["runtime_rpc"].setText(f"Disconnected | {self._endpoint}")
        self._apply_runtime_state({})

    def _sync_local_hold_controls(self) -> None:
        modes = {}
        if self._pilot_svc is not None and hasattr(self._pilot_svc, "current_modes"):
            try:
                modes = dict(self._pilot_svc.current_modes() or {})
            except Exception:
                modes = {}
        self._runtime_labels["pilot_depth_hold"].setText("ON" if modes.get("depth_hold") else "OFF")
        self._runtime_labels["pilot_rp_level"].setText("ON" if modes.get("roll_pitch_level") else "OFF")
        self._runtime_labels["pilot_yaw_hold"].setText("ON" if modes.get("yaw_hold") else "OFF")
        has_depth_controls = self._pilot_svc is not None and hasattr(self._pilot_svc, "toggle_depth_hold")
        has_rp_controls = self._pilot_svc is not None and hasattr(self._pilot_svc, "toggle_roll_pitch_level")
        has_yaw_controls = self._pilot_svc is not None and hasattr(self._pilot_svc, "toggle_yaw_hold")
        self.depth_hold_toggle_btn.setEnabled(bool(has_depth_controls))
        self.rp_level_toggle_btn.setEnabled(bool(has_rp_controls))
        self.yaw_hold_toggle_btn.setEnabled(bool(has_yaw_controls))

    def _toggle_depth_hold(self) -> None:
        if self._pilot_svc is None or not hasattr(self._pilot_svc, "toggle_depth_hold"):
            self._set_feedback("Depth hold control is unavailable from this page.", tone="#ff8d8d")
            return
        try:
            enabled = bool(self._pilot_svc.toggle_depth_hold())
        except Exception as exc:
            self._set_feedback(f"Could not toggle depth hold: {exc}", tone="#ff8d8d")
            return
        self._sync_local_hold_controls()
        self._set_feedback(f"Depth hold {'enabled' if enabled else 'disabled'} from topside.", tone="#9be89b")

    def _toggle_roll_pitch_level(self) -> None:
        if self._pilot_svc is None or not hasattr(self._pilot_svc, "toggle_roll_pitch_level"):
            self._set_feedback("Roll/pitch level control is unavailable from this page.", tone="#ff8d8d")
            return
        try:
            enabled = bool(self._pilot_svc.toggle_roll_pitch_level())
        except Exception as exc:
            self._set_feedback(f"Could not toggle roll/pitch level: {exc}", tone="#ff8d8d")
            return
        self._sync_local_hold_controls()
        self._set_feedback(f"Roll/pitch level {'enabled' if enabled else 'disabled'} from topside.", tone="#9be89b")

    def _toggle_yaw_hold(self) -> None:
        if self._pilot_svc is None or not hasattr(self._pilot_svc, "toggle_yaw_hold"):
            self._set_feedback("Yaw hold control is unavailable from this page.", tone="#ff8d8d")
            return
        try:
            enabled = bool(self._pilot_svc.toggle_yaw_hold())
        except Exception as exc:
            self._set_feedback(f"Could not toggle yaw hold: {exc}", tone="#ff8d8d")
            return
        self._sync_local_hold_controls()
        self._set_feedback(f"Yaw hold {'enabled' if enabled else 'disabled'} from topside.", tone="#9be89b")

    def _set_feedback(self, text: str, *, tone: str) -> None:
        self.feedback_label.setText(str(text))
        self.feedback_label.setStyleSheet(f"color: {tone};")

    def _apply_runtime_state(self, runtime: dict) -> None:
        autopilot = dict(runtime.get("autopilot") or {})
        autopilot_status = dict(autopilot.get("status") or {})
        attitude_runtime = dict(autopilot_status.get("attitude") or {})
        attitude_sensor = dict(autopilot.get("attitude_sensor") or {})
        depth_hold = dict(runtime.get("depth_hold") or {})
        depth_status = dict(depth_hold.get("status") or {})
        depth_sensor = dict(depth_hold.get("sensor") or {})

        self._runtime_labels["runtime_loop"].setText("available" if runtime.get("control_loop_available") else "unavailable")
        self._runtime_labels["runtime_armed"].setText("yes" if runtime.get("armed") else "no")
        self._runtime_labels["runtime_autopilot"].setText(self._format_autopilot_runtime(autopilot))
        self._runtime_labels["runtime_depth_hold"].setText(
            self._format_hold_runtime(
                available=depth_hold.get("available"),
                sensor_available=depth_hold.get("sensor_available"),
                enabled_cmd=depth_status.get("enabled_cmd"),
                active=depth_status.get("active"),
                reason=depth_status.get("reason"),
                target_text=self._fmt_num(depth_hold.get("target_m"), "m", decimals=2),
                status_age_s=depth_hold.get("status_age_s"),
            )
        )
        self._runtime_labels["runtime_attitude"].setText(self._format_attitude_runtime(attitude_runtime))
        self._runtime_labels["runtime_attitude_sensor"].setText(self._format_attitude_sensor(attitude_sensor))
        self._runtime_labels["runtime_attitude_debug"].setText(self._format_attitude_debug(attitude_runtime))
        self._runtime_labels["runtime_depth_sensor"].setText(self._format_depth_sensor(depth_sensor))
        self._runtime_labels["runtime_depth_debug"].setText(self._format_depth_debug(depth_status))

    @staticmethod
    def _fmt_bool(value) -> str:
        if value is None:
            return "-"
        return "yes" if bool(value) else "no"

    @staticmethod
    def _fmt_num(value, unit: str = "", *, decimals: int = 3) -> str:
        try:
            text = f"{float(value):.{int(decimals)}f}"
        except Exception:
            return "-"
        if unit:
            return f"{text} {unit}"
        return text

    def _format_hold_runtime(
        self,
        *,
        available,
        sensor_available,
        enabled_cmd,
        active,
        reason,
        target_text: str,
        status_age_s,
    ) -> str:
        parts = [
            f"available {self._fmt_bool(available)}",
            f"sensor {self._fmt_bool(sensor_available)}",
            f"enabled_cmd {self._fmt_bool(enabled_cmd)}",
            f"active {self._fmt_bool(active)}",
        ]
        if reason:
            parts.append(f"reason {reason}")
        if target_text and target_text != "-":
            parts.append(f"target {target_text}")
        if status_age_s is not None:
            parts.append(f"status age {self._fmt_num(status_age_s, 's', decimals=2)}")
        return " | ".join(parts)

    def _format_autopilot_runtime(self, autopilot: dict) -> str:
        parts = [
            f"available {self._fmt_bool(autopilot.get('available'))}",
            f"sensor {self._fmt_bool(autopilot.get('sensor_available'))}",
        ]
        status_age = autopilot.get("status_age_s")
        if status_age is not None:
            parts.append(f"status age {self._fmt_num(status_age, 's', decimals=2)}")
        return " | ".join(parts)

    def _format_attitude_runtime(self, attitude: dict) -> str:
        parts = [
            f"enabled_cmd {self._fmt_bool(attitude.get('enabled_cmd'))}",
            f"active {self._fmt_bool(attitude.get('active'))}",
        ]
        if attitude.get("reason"):
            parts.append(f"reason {attitude.get('reason')}")
        if attitude.get("source"):
            parts.append(f"src {attitude.get('source')}")
        return " | ".join(parts)

    def _format_attitude_sensor(self, sensor: dict) -> str:
        parts: list[str] = [f"available {self._fmt_bool(sensor.get('available'))}"]
        if sensor.get("source"):
            parts.append(str(sensor.get("source")))
        sample_age = sensor.get("sample_age_s")
        if sample_age is not None:
            parts.append(f"sample age {self._fmt_num(sample_age, 's', decimals=2)}")
        raw = sensor.get("raw") if isinstance(sensor.get("raw"), dict) else {}
        for label, key in (("r", "roll_deg"), ("p", "pitch_deg"), ("y", "yaw_deg")):
            text = self._fmt_num(raw.get(key), "deg", decimals=1)
            if text != "-":
                parts.append(f"{label} {text}")
        return " | ".join(parts) if parts else "-"

    def _format_attitude_debug(self, attitude: dict) -> str:
        axes = attitude.get("axes") if isinstance(attitude.get("axes"), dict) else {}
        parts: list[str] = []
        for axis in ("roll", "pitch", "yaw"):
            st = axes.get(axis) if isinstance(axes.get(axis), dict) else {}
            mode = str(st.get("mode") or "off")
            if mode == "off" and not st.get("active"):
                continue
            bit = f"{axis} {mode}"
            if st.get("active"):
                err = self._fmt_num(st.get("error_deg"), "deg", decimals=2)
                out = self._fmt_num(st.get("u_out"), "", decimals=3)
                if err != "-":
                    bit += f" err {err}"
                if out != "-":
                    bit += f" out {out}"
            elif st.get("reason"):
                bit += f" {st.get('reason')}"
            parts.append(bit)
        return " | ".join(parts) if parts else "-"

    def _format_depth_sensor(self, sensor: dict) -> str:
        parts: list[str] = []
        depth_text = self._fmt_num(sensor.get("depth_m"), "m", decimals=2)
        if depth_text != "-":
            parts.append(f"depth {depth_text}")
        if sensor.get("sensor_name"):
            parts.append(str(sensor.get("sensor_name")))
        sample_age = sensor.get("sample_age_s")
        if sample_age is not None:
            parts.append(f"sample age {self._fmt_num(sample_age, 's', decimals=2)}")
        stream_age = sensor.get("stream_age_s")
        if stream_age is not None:
            parts.append(f"stream age {self._fmt_num(stream_age, 's', decimals=2)}")
        return " | ".join(parts) if parts else "-"

    def _format_depth_debug(self, status: dict) -> str:
        parts: list[str] = []
        for label, key, unit, decimals in (
            ("depth_f", "depth_f_m", "m", 2),
            ("error", "error_m", "m", 3),
            ("dz", "dz_mps", "m/s", 3),
            ("out", "u_out", "", 3),
        ):
            text = self._fmt_num(status.get(key), unit, decimals=decimals)
            if text != "-":
                parts.append(f"{label} {text}")
        return " | ".join(parts) if parts else "-"

    def update_from_sensor(self, msg: dict) -> None:
        if (msg or {}).get("type") != "external_depth":
            return

        try:
            sensor = str((msg or {}).get("sensor", "depth"))
            if (msg or {}).get("error"):
                self.depth_gauge.set_value(None, state_text="ERR")
                self.depth_readout.setText(f"Depth: {sensor} (ERR)")
                self.depth_meta.setText(str((msg or {}).get("error")))
                return

            depth = (msg or {}).get("depth_m")
            temp = (msg or {}).get("temperature_c")
            pressure = (msg or {}).get("pressure_mbar")

            self.depth_gauge.set_value(
                None if depth is None else float(depth),
                secondary=(f"{float(temp):.1f} C" if temp is not None else ""),
            )

            if depth is None:
                self.depth_readout.setText(f"Depth: {sensor} -")
            else:
                self.depth_readout.setText(f"Depth: {sensor} {float(depth):.2f} m")

            meta: list[str] = []
            if pressure is not None:
                meta.append(f"{float(pressure):.0f} mbar")
            if temp is not None:
                meta.append(f"{float(temp):.1f} C")
            self.depth_meta.setText(" | ".join(meta) if meta else "-")
        except Exception:
            pass
