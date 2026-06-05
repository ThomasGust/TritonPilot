"""Operator instrument widgets for depth, power, hold tests, and gauges."""

from __future__ import annotations

import math
import time
from typing import Optional

from PyQt6.QtCore import Qt, QRectF, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PyQt6.QtWidgets import (
    QWidget,
    QFrame,
    QLabel,
    QGridLayout,
    QVBoxLayout,
    QHBoxLayout,
    QPushButton,
    QDoubleSpinBox,
    QProgressBar,
    QScrollArea,
    QSizePolicy,
)

from network.management_rpc import ManagementRpcService


def _finite_float(value) -> Optional[float]:
    try:
        numeric = float(value)
    except Exception:
        return None
    if not math.isfinite(numeric):
        return None
    return numeric


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
    """Compact vertical gauge used by the pilot instrument panel."""

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


class AttitudeIndicatorWidget(QWidget):
    """Compact artificial-horizon attitude indicator for the pilot page."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.roll_deg: Optional[float] = None
        self.pitch_deg: Optional[float] = None
        self.yaw_deg: Optional[float] = None
        self.setMinimumSize(170, 210)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def clear(self) -> None:
        self.roll_deg = None
        self.pitch_deg = None
        self.yaw_deg = None
        self.update()

    def set_attitude(self, msg: dict | None) -> None:
        msg = msg or {}
        roll = _finite_float(msg.get("roll_deg"))
        pitch = _finite_float(msg.get("pitch_deg"))
        yaw = _finite_float(msg.get("yaw_deg"))
        if roll is None and pitch is None and yaw is None:
            self.clear()
            return
        self.roll_deg = roll if roll is not None else self.roll_deg
        self.pitch_deg = pitch if pitch is not None else self.pitch_deg
        self.yaw_deg = yaw if yaw is not None else self.yaw_deg
        self.update()

    def paintEvent(self, _event):  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        bounds = self.rect().adjusted(8, 8, -8, -8)
        heading_h = 36
        dial_bounds = bounds.adjusted(0, 0, 0, -heading_h)
        side = min(dial_bounds.width(), dial_bounds.height())
        left = bounds.center().x() - side / 2
        top = dial_bounds.center().y() - side / 2
        dial = QRectF(left, top, side, side)
        cx = dial.center().x()
        cy = dial.center().y()
        radius = dial.width() / 2

        p.setPen(QPen(QColor(58, 62, 76), 2))
        p.setBrush(QColor(12, 13, 18))
        p.drawEllipse(dial)

        if self.roll_deg is None and self.pitch_deg is None:
            p.setPen(QColor(160, 168, 190))
            p.drawText(dial, Qt.AlignmentFlag.AlignCenter, "Attitude\nwaiting")
            self._draw_heading_tape(p, bounds, None)
            return

        roll = float(self.roll_deg or 0.0)
        pitch = max(-45.0, min(45.0, float(self.pitch_deg or 0.0)))
        pitch_px_per_deg = radius / 42.0
        horizon_y = pitch * pitch_px_per_deg

        clip = QPainterPath()
        clip.addEllipse(dial.adjusted(3, 3, -3, -3))
        p.save()
        p.setClipPath(clip)
        p.translate(cx, cy)
        p.rotate(-roll)

        span = radius * 3.0
        p.fillRect(QRectF(-span, -span + horizon_y, span * 2, span), QColor(46, 92, 142))
        p.fillRect(QRectF(-span, horizon_y, span * 2, span), QColor(74, 94, 80))
        p.setPen(QPen(QColor(246, 248, 255), 2))
        p.drawLine(int(-span), int(horizon_y), int(span), int(horizon_y))

        p.setPen(QPen(QColor(230, 234, 246), 1))
        for mark in range(-30, 31, 10):
            if mark == 0:
                continue
            y = horizon_y - (mark * pitch_px_per_deg)
            half = radius * (0.34 if mark % 20 == 0 else 0.24)
            p.drawLine(int(-half), int(y), int(half), int(y))
            label = str(abs(mark))
            p.drawText(QRectF(-half - 28, y - 8, 22, 16), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, label)
            p.drawText(QRectF(half + 6, y - 8, 22, 16), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, label)

        p.restore()

        p.setPen(QPen(QColor(245, 220, 104), 2))
        wing_y = cy
        p.drawLine(int(cx - radius * 0.42), int(wing_y), int(cx - radius * 0.12), int(wing_y))
        p.drawLine(int(cx + radius * 0.12), int(wing_y), int(cx + radius * 0.42), int(wing_y))
        p.drawLine(int(cx), int(wing_y - radius * 0.08), int(cx), int(wing_y + radius * 0.08))

        p.setPen(QPen(QColor(210, 216, 232), 1))
        for deg in (-60, -45, -30, -20, -10, 0, 10, 20, 30, 45, 60):
            rad = math.radians(deg)
            outer = radius * 0.96
            inner = radius * (0.86 if deg in (-60, -30, 0, 30, 60) else 0.90)
            x1 = cx + math.sin(rad) * inner
            y1 = cy - math.cos(rad) * inner
            x2 = cx + math.sin(rad) * outer
            y2 = cy - math.cos(rad) * outer
            p.drawLine(int(x1), int(y1), int(x2), int(y2))

        p.setPen(QPen(QColor(78, 84, 104), 2))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(dial)
        self._draw_heading_tape(p, bounds, self.yaw_deg)

    def _draw_heading_tape(self, p: QPainter, bounds, yaw: Optional[float]) -> None:
        tape = QRectF(bounds.left() + 10, bounds.bottom() - 30, bounds.width() - 20, 28)
        p.setPen(QPen(QColor(58, 62, 76), 1))
        p.setBrush(QColor(15, 16, 22))
        p.drawRoundedRect(tape, 7.0, 7.0)

        if yaw is None:
            p.setPen(QColor(150, 158, 180))
            p.drawText(tape, Qt.AlignmentFlag.AlignCenter, "deg -")
            return

        heading = float(yaw) % 360.0
        center_x = tape.center().x()
        px_per_deg = max(1.2, tape.width() / 95.0)
        nearest = int(round(heading / 30.0) * 30)

        p.save()
        clip = QPainterPath()
        clip.addRoundedRect(tape.adjusted(2, 2, -2, -2), 6.0, 6.0)
        p.setClipPath(clip)
        p.setPen(QPen(QColor(112, 122, 148), 1))
        for mark in range(nearest - 90, nearest + 91, 15):
            normalized = mark % 360
            offset = ((float(mark) - heading + 540.0) % 360.0) - 180.0
            x = center_x + offset * px_per_deg
            if x < tape.left() - 12 or x > tape.right() + 12:
                continue
            is_major = normalized % 30 == 0
            y1 = tape.top() + (5 if is_major else 9)
            y2 = tape.top() + 14
            p.drawLine(int(x), int(y1), int(x), int(y2))
            if is_major:
                text = f"{int(normalized):03d}"
                p.drawText(QRectF(x - 16, tape.top() + 12, 32, 13), Qt.AlignmentFlag.AlignCenter, text)
        p.restore()

        p.setPen(QPen(QColor(245, 220, 104), 2))
        p.drawLine(int(center_x), int(tape.top() + 3), int(center_x), int(tape.bottom() - 3))


class PilotTelemetryColumn(QWidget):
    """Right-side pilot instruments kept compact enough for quad video."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("pilotTelemetryColumn")
        self.setMinimumWidth(210)
        self.setMaximumWidth(245)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)

        self.attitude_card = _Card("Attitude")
        self.attitude_indicator = AttitudeIndicatorWidget()
        self.attitude_text = QLabel("roll - | pitch - | yaw -")
        self.attitude_text.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.attitude_text.setWordWrap(True)
        self.attitude_card.body.addWidget(self.attitude_indicator)
        self.attitude_card.body.addWidget(self.attitude_text)

        self.depth_card = _Card("Depth")
        self.depth_gauge = VerticalGaugeWidget(label="Depth", unit="m", vmin=0.0, vmax=30.0)
        self.depth_gauge.setMinimumSize(100, 190)
        self.depth_text = QLabel("-")
        self.depth_text.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.depth_text.setWordWrap(True)
        self.depth_card.body.addWidget(self.depth_gauge)
        self.depth_card.body.addWidget(self.depth_text)

        self.analysis_card = _Card("Capture / Analysis")
        self.capture_mode_text = QLabel("Capture: Camera  |  R toggles")
        self.capture_mode_text.setObjectName("pilotCaptureModeText")
        self.capture_mode_text.setWordWrap(True)
        self.capture_mode_text.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.analysis_text = QLabel("Analysis Share: -")
        self.analysis_text.setObjectName("pilotAnalysisText")
        self.analysis_text.setWordWrap(True)
        self.analysis_text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.analysis_card.body.addWidget(self.capture_mode_text)
        self.analysis_card.body.addWidget(self.analysis_text)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        layout.addWidget(self.attitude_card, 0)
        layout.addWidget(self.depth_card, 0)
        layout.addWidget(self.analysis_card, 1)

    def update_from_sensor(self, msg: dict) -> None:
        typ = (msg or {}).get("type")
        if typ == "attitude":
            self.attitude_indicator.set_attitude(msg or {})
            roll = _finite_float((msg or {}).get("roll_deg"))
            pitch = _finite_float((msg or {}).get("pitch_deg"))
            yaw = _finite_float((msg or {}).get("yaw_deg"))
            parts = [
                f"roll {roll:.1f}" if roll is not None else "roll -",
                f"pitch {pitch:.1f}" if pitch is not None else "pitch -",
                f"yaw {yaw:.1f}" if yaw is not None else "yaw -",
            ]
            self.attitude_text.setText(" | ".join(parts))
            return

        if typ == "external_depth":
            if (msg or {}).get("error"):
                self.depth_gauge.set_value(None, state_text="ERR")
                self.depth_text.setText(str((msg or {}).get("error") or "sensor error"))
                return
            depth = _finite_float((msg or {}).get("depth_m"))
            temp = _finite_float((msg or {}).get("temperature_c"))
            pressure = _finite_float((msg or {}).get("pressure_mbar"))
            self.depth_gauge.set_value(depth, secondary=(f"{temp:.1f} C" if temp is not None else ""))
            parts = []
            if depth is not None:
                parts.append(f"{depth:.2f} m")
            if pressure is not None:
                parts.append(f"{pressure:.0f} mbar")
            if temp is not None:
                parts.append(f"{temp:.1f} C")
            self.depth_text.setText(" | ".join(parts) if parts else "-")

    def set_analysis_share(self, text: str, tone: str | None = None) -> None:
        text = str(text or "Analysis Share: -")
        self.analysis_text.setText(text)
        self.analysis_text.setToolTip(text)
        if tone == "alert":
            self.analysis_text.setStyleSheet("color: #ffd9d9; font-weight: 700;")
        elif tone == "warn":
            self.analysis_text.setStyleSheet("color: #ffe6ae; font-weight: 700;")
        else:
            self.analysis_text.setStyleSheet("color: #f0f4ff; font-weight: 600;")

    def set_capture_mode(self, mode: str) -> None:
        mode_key = str(mode or "camera").strip().lower()
        if mode_key == "stereo":
            text = "Capture: Stereo pairs  |  R toggles"
            tip = "X captures a stereo pair. B starts or stops stereo pair recording."
            color = "#9fd1ff"
        else:
            text = "Capture: Camera  |  R toggles"
            tip = "X saves the active camera image. B starts or stops active-camera video."
            color = "#dfe6f5"
        self.capture_mode_text.setText(text)
        self.capture_mode_text.setToolTip(tip)
        self.capture_mode_text.setStyleSheet(f"color: {color}; font-weight: 700;")


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
        self._axis_target_spins: dict[str, QDoubleSpinBox] = {}
        self._runtime_targets: dict[str, float] = {}
        self._target_spin_edit_until: dict[int, float] = {}
        self._syncing_target_spins = False
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

        target_grid = QGridLayout()
        target_grid.setHorizontalSpacing(8)
        target_grid.setVerticalSpacing(6)

        self.depth_target_spin = self._make_target_spin(-2.0, 100.0, 0.05, " m", 2)
        self._watch_target_spin(self.depth_target_spin)
        self.depth_target_btn = QPushButton("Hold Target")
        self.depth_target_btn.clicked.connect(self._hold_depth_target)
        self.depth_off_btn = QPushButton("Off")
        self.depth_off_btn.clicked.connect(self._depth_hold_off)
        target_grid.addWidget(QLabel("Depth"), 0, 0)
        target_grid.addWidget(self.depth_target_spin, 0, 1)
        target_grid.addWidget(self.depth_target_btn, 0, 2)
        target_grid.addWidget(self.depth_off_btn, 0, 3)

        for row, axis in enumerate(("roll", "pitch", "yaw"), start=1):
            if axis == "yaw":
                spin = self._make_target_spin(-360.0, 360.0, 5.0, " deg", 1)
            else:
                spin = self._make_target_spin(-90.0, 90.0, 1.0, " deg", 1)
            self._watch_target_spin(spin)
            self._axis_target_spins[axis] = spin
            hold_btn = QPushButton("Hold Target")
            hold_btn.clicked.connect(lambda _checked=False, axis=axis: self._hold_axis_target(axis))
            off_btn = QPushButton("Off")
            off_btn.clicked.connect(lambda _checked=False, axis=axis: self._set_axis_mode(axis, "off"))
            target_grid.addWidget(QLabel(axis.title()), row, 0)
            target_grid.addWidget(spin, row, 1)
            target_grid.addWidget(hold_btn, row, 2)
            target_grid.addWidget(off_btn, row, 3)
            setattr(self, f"{axis}_target_btn", hold_btn)
            setattr(self, f"{axis}_off_btn", off_btn)
            if axis in {"roll", "pitch"}:
                level_btn = QPushButton("Level")
                level_btn.clicked.connect(lambda _checked=False, axis=axis: self._set_axis_mode(axis, "level"))
                target_grid.addWidget(level_btn, row, 4)
                setattr(self, f"{axis}_level_btn", level_btn)
        self.control_card.body.addLayout(target_grid)

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

        shortcut_hint = QLabel(f"R3 toggles depth hold; L3 toggles yaw hold; {self._lights_shortcut_text.upper()} toggles lights.")
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
                ("Roll Hold Detail", "runtime_roll_hold_detail"),
                ("Pitch Hold Detail", "runtime_pitch_hold_detail"),
                ("Yaw Hold Detail", "runtime_yaw_hold_detail"),
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

    @staticmethod
    def _make_target_spin(vmin: float, vmax: float, step: float, suffix: str, decimals: int) -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setRange(float(vmin), float(vmax))
        spin.setSingleStep(float(step))
        spin.setDecimals(int(decimals))
        spin.setSuffix(str(suffix or ""))
        spin.setKeyboardTracking(False)
        spin.setMinimumWidth(100)
        spin.setAccelerated(True)
        return spin

    def _watch_target_spin(self, spin: QDoubleSpinBox) -> None:
        spin.valueChanged.connect(lambda _value, spin=spin: self._mark_target_spin_edited(spin))
        editor = spin.lineEdit()
        if editor is not None:
            editor.textEdited.connect(lambda _text, spin=spin: self._mark_target_spin_edited(spin))

    def _mark_target_spin_edited(self, spin: QDoubleSpinBox) -> None:
        if self._syncing_target_spins:
            return
        self._target_spin_edit_until[id(spin)] = time.monotonic() + 2.0

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
        ap = dict(modes.get("autopilot") or {})
        targets = dict(ap.get("targets") or {})
        self._runtime_labels["pilot_depth_hold"].setText(self._format_pilot_depth_mode(modes, targets))
        self._runtime_labels["pilot_rp_level"].setText(self._format_pilot_rp_modes(modes, ap, targets))
        self._runtime_labels["pilot_yaw_hold"].setText(self._format_pilot_axis_mode(modes, ap, targets, "yaw"))
        self._sync_target_spins(targets)
        has_depth_controls = self._pilot_svc is not None and hasattr(self._pilot_svc, "toggle_depth_hold")
        has_rp_controls = self._pilot_svc is not None and hasattr(self._pilot_svc, "toggle_roll_pitch_level")
        has_yaw_controls = self._pilot_svc is not None and hasattr(self._pilot_svc, "toggle_yaw_hold")
        has_depth_target = self._pilot_svc is not None and hasattr(self._pilot_svc, "set_depth_hold_target")
        has_depth_off = self._pilot_svc is not None and hasattr(self._pilot_svc, "set_depth_hold_enabled")
        has_axis_mode = self._pilot_svc is not None and hasattr(self._pilot_svc, "set_autopilot_axis_mode")
        has_axis_target = self._pilot_svc is not None and hasattr(self._pilot_svc, "set_autopilot_axis_target")
        self.depth_hold_toggle_btn.setEnabled(bool(has_depth_controls))
        self.rp_level_toggle_btn.setEnabled(bool(has_rp_controls))
        self.yaw_hold_toggle_btn.setEnabled(bool(has_yaw_controls))
        self.depth_target_btn.setEnabled(bool(has_depth_target))
        self.depth_off_btn.setEnabled(bool(has_depth_off))
        for axis in ("roll", "pitch", "yaw"):
            getattr(self, f"{axis}_target_btn").setEnabled(bool(has_axis_target))
            getattr(self, f"{axis}_off_btn").setEnabled(bool(has_axis_mode))
            level_btn = getattr(self, f"{axis}_level_btn", None)
            if level_btn is not None:
                level_btn.setEnabled(bool(has_axis_mode))

    def _sync_target_spins(self, targets: dict) -> None:
        self._maybe_set_spin(self.depth_target_spin, self._target_from_command_or_runtime(targets, "depth_m"))
        for axis, spin in self._axis_target_spins.items():
            self._maybe_set_spin(spin, self._target_from_command_or_runtime(targets, f"{axis}_deg"))

    def _target_from_command_or_runtime(self, targets: dict, key: str):
        if isinstance(targets, dict) and key in targets:
            return targets.get(key)
        return self._runtime_targets.get(key)

    def _spin_is_being_edited(self, spin: QDoubleSpinBox) -> bool:
        if spin.hasFocus():
            return True
        editor = spin.lineEdit()
        if editor is not None and editor.hasFocus():
            return True
        return time.monotonic() < float(self._target_spin_edit_until.get(id(spin), 0.0))

    def _maybe_set_spin(self, spin: QDoubleSpinBox, value) -> None:
        if self._spin_is_being_edited(spin):
            return
        try:
            v = float(value)
        except Exception:
            return
        if math.isfinite(v) and abs(float(spin.value()) - v) > 1e-9:
            self._syncing_target_spins = True
            try:
                previous = spin.blockSignals(True)
                try:
                    spin.setValue(v)
                finally:
                    spin.blockSignals(previous)
            finally:
                self._syncing_target_spins = False

    def _format_pilot_depth_mode(self, modes: dict, targets: dict) -> str:
        text = "ON" if modes.get("depth_hold") else "OFF"
        if modes.get("depth_hold"):
            cmd_target = self._fmt_num(targets.get("depth_m"), "m", decimals=2)
            runtime_target = self._fmt_num(self._runtime_targets.get("depth_m"), "m", decimals=2)
            if cmd_target != "-":
                text += f" cmd {cmd_target}"
            if runtime_target != "-" and runtime_target != cmd_target:
                text += f" runtime {runtime_target}"
        return text

    def _format_pilot_axis_mode(self, modes: dict, ap: dict, targets: dict, axis: str) -> str:
        if not ap and modes.get(f"{axis}_hold"):
            return "ON"
        mode = str(ap.get(axis) or ("hold" if modes.get(f"{axis}_hold") else "off")).strip().lower()
        text = mode.upper() if mode and mode != "off" else "OFF"
        if mode and mode != "off":
            key = f"{axis}_deg"
            cmd_target = self._fmt_num(targets.get(key), "deg", decimals=1)
            runtime_target = self._fmt_num(self._runtime_targets.get(key), "deg", decimals=1)
            if cmd_target != "-":
                text += f" cmd {cmd_target}"
            if runtime_target != "-" and runtime_target != cmd_target:
                text += f" runtime {runtime_target}"
        return text

    def _format_pilot_rp_modes(self, modes: dict, ap: dict, targets: dict) -> str:
        if modes.get("roll_pitch_level") and not ap:
            return "ON"
        roll = self._format_pilot_axis_mode(modes, ap, targets, "roll")
        pitch = self._format_pilot_axis_mode(modes, ap, targets, "pitch")
        return f"roll {roll} | pitch {pitch}"

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

    def _hold_depth_target(self) -> None:
        if self._pilot_svc is None or not hasattr(self._pilot_svc, "set_depth_hold_target"):
            self._set_feedback("Depth target control is unavailable from this page.", tone="#ff8d8d")
            return
        target = float(self.depth_target_spin.value())
        try:
            self._pilot_svc.set_depth_hold_target(target, enable=True)
        except Exception as exc:
            self._set_feedback(f"Could not set depth target: {exc}", tone="#ff8d8d")
            return
        self._sync_local_hold_controls()
        self._set_feedback(f"Depth hold target set to {target:.2f} m.", tone="#9be89b")

    def _depth_hold_off(self) -> None:
        if self._pilot_svc is None or not hasattr(self._pilot_svc, "set_depth_hold_enabled"):
            self._set_feedback("Depth hold control is unavailable from this page.", tone="#ff8d8d")
            return
        try:
            self._pilot_svc.set_depth_hold_enabled(False)
        except Exception as exc:
            self._set_feedback(f"Could not turn depth hold off: {exc}", tone="#ff8d8d")
            return
        self._sync_local_hold_controls()
        self._set_feedback("Depth hold disabled from topside.", tone="#9be89b")

    def _hold_axis_target(self, axis: str) -> None:
        axis_key = str(axis or "").strip().lower()
        spin = self._axis_target_spins.get(axis_key)
        if spin is None:
            return
        if self._pilot_svc is None or not hasattr(self._pilot_svc, "set_autopilot_axis_target"):
            self._set_feedback(f"{axis_key.title()} target control is unavailable from this page.", tone="#ff8d8d")
            return
        target = float(spin.value())
        try:
            self._pilot_svc.set_autopilot_axis_target(axis_key, target, mode="hold")
        except Exception as exc:
            self._set_feedback(f"Could not set {axis_key} target: {exc}", tone="#ff8d8d")
            return
        self._sync_local_hold_controls()
        self._set_feedback(f"{axis_key.title()} hold target set to {target:.1f} deg.", tone="#9be89b")

    def _set_axis_mode(self, axis: str, mode: str) -> None:
        axis_key = str(axis or "").strip().lower()
        mode_value = str(mode or "off").strip().lower() or "off"
        if self._pilot_svc is None or not hasattr(self._pilot_svc, "set_autopilot_axis_mode"):
            self._set_feedback(f"{axis_key.title()} mode control is unavailable from this page.", tone="#ff8d8d")
            return
        try:
            self._pilot_svc.set_autopilot_axis_mode(axis_key, mode_value)
        except Exception as exc:
            self._set_feedback(f"Could not set {axis_key} mode: {exc}", tone="#ff8d8d")
            return
        self._sync_local_hold_controls()
        self._set_feedback(f"{axis_key.title()} mode set to {mode_value}.", tone="#9be89b")

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
        self._update_runtime_target_cache(depth_hold, depth_status, attitude_runtime)

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
        self._runtime_labels["runtime_roll_hold_detail"].setText(self._format_axis_detail(attitude_runtime, "roll"))
        self._runtime_labels["runtime_pitch_hold_detail"].setText(self._format_axis_detail(attitude_runtime, "pitch"))
        self._runtime_labels["runtime_yaw_hold_detail"].setText(self._format_axis_detail(attitude_runtime, "yaw"))
        self._runtime_labels["runtime_attitude_sensor"].setText(self._format_attitude_sensor(attitude_sensor))
        self._runtime_labels["runtime_attitude_debug"].setText(self._format_attitude_debug(attitude_runtime))
        self._runtime_labels["runtime_depth_sensor"].setText(self._format_depth_sensor(depth_sensor))
        self._runtime_labels["runtime_depth_debug"].setText(self._format_depth_debug(depth_status))
        self._sync_local_hold_controls()

    def _update_runtime_target_cache(self, depth_hold: dict, depth_status: dict, attitude_runtime: dict) -> None:
        targets: dict[str, float] = {}
        if depth_status.get("enabled_cmd") or depth_status.get("active"):
            depth_target = depth_status.get("target_m", depth_hold.get("target_m"))
            try:
                depth_value = float(depth_target)
            except Exception:
                depth_value = math.nan
            if math.isfinite(depth_value):
                targets["depth_m"] = depth_value

        axes = attitude_runtime.get("axes") if isinstance(attitude_runtime.get("axes"), dict) else {}
        for axis in ("roll", "pitch", "yaw"):
            st = axes.get(axis) if isinstance(axes.get(axis), dict) else {}
            if not (st.get("enabled_cmd") or st.get("active")):
                continue
            try:
                value = float(st.get("target_deg"))
            except Exception:
                value = math.nan
            if math.isfinite(value):
                targets[f"{axis}_deg"] = value
        self._runtime_targets = targets

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

    def _format_axis_detail(self, attitude: dict, axis: str) -> str:
        axes = attitude.get("axes") if isinstance(attitude.get("axes"), dict) else {}
        st = axes.get(axis) if isinstance(axes.get(axis), dict) else {}
        if not st:
            return "-"

        parts = [
            f"mode {st.get('mode') or 'off'}",
            f"enabled {self._fmt_bool(st.get('enabled_cmd'))}",
            f"active {self._fmt_bool(st.get('active'))}",
        ]
        if st.get("reason"):
            parts.append(f"reason {st.get('reason')}")

        angle = self._fmt_num(st.get("angle_deg"), "deg", decimals=1)
        target = self._fmt_num(st.get("target_deg"), "deg", decimals=1)
        error = self._fmt_num(st.get("error_deg"), "deg", decimals=1)
        rate = self._fmt_num(st.get("rate_dps"), "deg/s", decimals=1)
        out = self._fmt_num(st.get("u_out"), "", decimals=3)

        if angle != "-":
            parts.append(f"current {angle}")
        if target != "-":
            parts.append(f"target {target}")
        if error != "-":
            parts.append(f"error {error}")
        if rate != "-":
            parts.append(f"rate {rate}")
        if out != "-":
            parts.append(f"out {out}")
        return " | ".join(parts)

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
