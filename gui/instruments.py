from __future__ import annotations

import math
import time
from typing import Optional

from PyQt6.QtCore import Qt, QRectF
from PyQt6.QtGui import QColor, QFont, QPainter, QPen
from PyQt6.QtWidgets import (
    QWidget,
    QFrame,
    QLabel,
    QGridLayout,
    QVBoxLayout,
    QHBoxLayout,
    QProgressBar,
    QSizePolicy,
)


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

        # local style keeps it self-contained
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


class AttitudeHorizonWidget(QWidget):
    """Lightweight artificial horizon (roll/pitch) with yaw text overlay."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.roll_deg: float = 0.0
        self.pitch_deg: float = 0.0
        self.yaw_deg: Optional[float] = None
        self.mode_text: str = "-"
        self.mag_qual: Optional[float] = None
        self.updated_ts: float = 0.0
        self.setMinimumSize(150, 120)

    def set_attitude(self, roll_deg: float, pitch_deg: float, yaw_deg: Optional[float], *, mode: str = "-", mag_qual: Optional[float] = None):
        self.roll_deg = float(roll_deg)
        self.pitch_deg = float(pitch_deg)
        self.yaw_deg = None if yaw_deg is None else float(yaw_deg)
        self.mode_text = str(mode)
        self.mag_qual = None if mag_qual is None else float(mag_qual)
        self.updated_ts = time.time()
        self.update()

    def paintEvent(self, _event):  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        r = self.rect().adjusted(6, 6, -6, -6)
        w = float(r.width())
        h = float(r.height())
        if w <= 4 or h <= 4:
            return

        cx = float(r.center().x())
        cy = float(r.center().y())
        radius = min(w, h) * 0.46

        # clipped circular instrument
        p.save()
        p.setClipRect(r)

        # instrument background
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(12, 12, 16))
        p.drawRoundedRect(QRectF(r), 12.0, 12.0)

        # draw moving horizon in local coordinates centered on widget
        p.save()
        p.translate(cx, cy)
        p.rotate(-self.roll_deg)

        # pitch mapping: ~3 px / deg but clamped by size
        px_per_deg = max(1.2, radius / 30.0)
        y_off = max(-radius * 0.8, min(radius * 0.8, self.pitch_deg * px_per_deg))

        # sky / ground rectangles in rotated frame
        sky = QRectF(-radius * 1.8, -radius * 1.8 + y_off, radius * 3.6, radius * 1.8)
        ground = QRectF(-radius * 1.8, 0 + y_off, radius * 3.6, radius * 1.8)
        p.setBrush(QColor(42, 82, 140))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawRect(sky)
        p.setBrush(QColor(120, 82, 44))
        p.drawRect(ground)

        # horizon line
        p.setPen(QPen(QColor(235, 235, 235), 2))
        p.drawLine(int(-radius * 1.6), int(y_off), int(radius * 1.6), int(y_off))

        # pitch ladder marks (every 10 deg)
        p.setPen(QPen(QColor(230, 230, 230), 1))
        for deg in (-30, -20, -10, 10, 20, 30):
            yy = y_off + deg * px_per_deg
            if yy < -radius * 1.2 or yy > radius * 1.2:
                continue
            half = radius * (0.22 if deg % 20 else 0.30)
            p.drawLine(int(-half), int(yy), int(half), int(yy))

        p.restore()

        # bezel / reticle
        p.setPen(QPen(QColor(210, 210, 215), 2))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(QRectF(r), 12.0, 12.0)

        p.setPen(QPen(QColor(255, 220, 90), 2))
        p.drawLine(int(cx - radius * 0.25), int(cy), int(cx - radius * 0.06), int(cy))
        p.drawLine(int(cx + radius * 0.06), int(cy), int(cx + radius * 0.25), int(cy))
        p.drawLine(int(cx), int(cy - radius * 0.05), int(cx), int(cy + radius * 0.10))

        # top bank index marker
        p.setPen(QPen(QColor(255, 255, 255), 2))
        p.drawLine(int(cx), int(cy - radius * 0.88), int(cx - 6), int(cy - radius * 0.78))
        p.drawLine(int(cx), int(cy - radius * 0.88), int(cx + 6), int(cy - radius * 0.78))

        # status text overlays
        p.setPen(QColor(235, 235, 235))
        font = QFont(self.font())
        font.setPointSize(max(8, font.pointSize()))
        p.setFont(font)
        yaw_s = "-" if self.yaw_deg is None else f"{self.yaw_deg:6.1f}°"
        p.drawText(r.adjusted(8, 6, -8, -6), Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft, f"Yaw {yaw_s}")
        q_s = "-" if self.mag_qual is None else f"{self.mag_qual:.2f}"
        p.drawText(r.adjusted(8, 6, -8, -6), Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignRight, f"{self.mode_text} q={q_s}")

        # stale badge
        age = (time.time() - self.updated_ts) if self.updated_ts else None
        if age is None or age > 2.0:
            p.setPen(QColor(255, 170, 90))
            p.drawText(r.adjusted(8, 0, -8, -8), Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignRight, "STALE")

        p.restore()


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

        # title
        p.setPen(QColor(235, 235, 235))
        p.drawText(r.adjusted(6, 4, -6, -4), Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter, self.label)

        tube = QRectF(r.left() + r.width() * 0.28, r.top() + 26, r.width() * 0.44, r.height() - 64)
        p.setBrush(QColor(12, 12, 16))
        p.setPen(QPen(QColor(70, 70, 84), 1))
        p.drawRoundedRect(tube, 7.0, 7.0)

        # ticks
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

            # marker line for current value
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

        self.att_card = _Card("Attitude")
        self.attitude = AttitudeHorizonWidget()
        self.att_card.body.addWidget(self.attitude)

        self.depth_card = _Card("Depth")
        self.depth_gauge = VerticalGaugeWidget(label="Depth", unit="m", vmin=0.0, vmax=30.0)
        self.depth_meta = QLabel("-")
        self.depth_meta.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.depth_meta.setWordWrap(True)
        self.depth_card.body.addWidget(self.depth_gauge)
        self.depth_card.body.addWidget(self.depth_meta)

        self.env_card = _Card("Temp / Power")
        self.temp_bar = QProgressBar()
        self.temp_bar.setRange(0, 1000)  # map -10..90 C internally
        self.temp_bar.setFormat("Temp: -")
        self.power_bar = QProgressBar()
        self.power_bar.setRange(0, 1000)  # map 0..2000W
        self.power_bar.setFormat("Power: -")
        self.power_lbl = QLabel("-")
        self.power_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.power_lbl.setWordWrap(True)
        self.leak_lbl = QLabel("Leak: unknown")
        self.leak_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        for w in (self.temp_bar, self.power_bar, self.power_lbl, self.leak_lbl):
            self.env_card.body.addWidget(w)

        grid = QGridLayout(self)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(8)
        grid.addWidget(self.att_card, 0, 0, 1, 2)
        grid.addWidget(self.depth_card, 1, 0)
        grid.addWidget(self.env_card, 1, 1)

        self._last_att_ts = 0.0
        self._last_depth_ts = 0.0
        self._last_env_ts = 0.0
        self._last_power_ts = 0.0
        self._last_leak_ts = 0.0

    def update_from_sensor(self, msg: dict) -> None:
        typ = (msg or {}).get("type")
        if typ == "attitude":
            try:
                rpy = (msg or {}).get("rpy_deg") or {}
                health = (msg or {}).get("health") or {}
                self.attitude.set_attitude(
                    float(rpy.get("roll", 0.0) or 0.0),
                    float(rpy.get("pitch", 0.0) or 0.0),
                    None if rpy.get("yaw") is None else float(rpy.get("yaw") or 0.0),
                    mode=str(health.get("mode", "-")),
                    mag_qual=(None if health.get("mag_qual") is None else float(health.get("mag_qual"))),
                )
                self._last_att_ts = time.time()
            except Exception:
                pass
            return

        if typ == "external_depth":
            try:
                if (msg or {}).get("error"):
                    self.depth_gauge.set_value(None, state_text="ERR")
                    self.depth_meta.setText(str((msg or {}).get("error")))
                else:
                    depth = (msg or {}).get("depth_m")
                    temp = (msg or {}).get("temperature_c")
                    p = (msg or {}).get("pressure_mbar")
                    self.depth_gauge.set_value(None if depth is None else float(depth), secondary=(f"{float(temp):.1f} °C" if temp is not None else ""))
                    meta = []
                    if p is not None:
                        meta.append(f"{float(p):.0f} mbar")
                    if temp is not None:
                        meta.append(f"{float(temp):.1f} °C")
                    self.depth_meta.setText(" • ".join(meta) if meta else "-")
                self._last_depth_ts = time.time()
            except Exception:
                pass
            return

        if typ == "env":
            try:
                t = (msg or {}).get("temperature_c")
                p_kpa = (msg or {}).get("pressure_kpa")
                if t is not None:
                    t_f = float(t)
                    frac = max(0.0, min(1.0, (t_f + 10.0) / 100.0))
                    self.temp_bar.setValue(int(round(frac * 1000)))
                    if p_kpa is None:
                        self.temp_bar.setFormat(f"Temp: {t_f:.1f} °C")
                    else:
                        self.temp_bar.setFormat(f"Temp: {t_f:.1f} °C  |  {float(p_kpa):.1f} kPa")
                self._last_env_ts = time.time()
            except Exception:
                pass
            return

        if typ == "power":
            try:
                if (msg or {}).get("error"):
                    self.power_bar.setValue(0)
                    self.power_bar.setFormat("Power: ERR")
                    self.power_lbl.setText(str((msg or {}).get("error")))
                else:
                    v = float((msg or {}).get("voltage_v", 0.0) or 0.0)
                    a = float((msg or {}).get("current_a", 0.0) or 0.0)
                    w = float((msg or {}).get("power_w", v * a) or (v * a))
                    frac = max(0.0, min(1.0, w / 2000.0))
                    self.power_bar.setValue(int(round(frac * 1000)))
                    self.power_bar.setFormat(f"Power: {w:.0f} W")
                    state_bits = []
                    if bool((msg or {}).get("held", False)):
                        state_bits.append("HOLD")
                    elif not bool((msg or {}).get("ok", True)):
                        state_bits.append("CHECK")
                    st = f" [{' '.join(state_bits)}]" if state_bits else ""
                    self.power_lbl.setText(f"{v:.2f} V • {a:.2f} A{st}")
                self._last_power_ts = time.time()
            except Exception:
                pass
            return

        if typ == "leak":
            try:
                leak = bool((msg or {}).get("leak", False))
                self.leak_lbl.setText("Leak: DETECTED" if leak else "Leak: OK")
                if leak:
                    self.leak_lbl.setStyleSheet("color: #ff8d8d; font-weight: bold;")
                else:
                    self.leak_lbl.setStyleSheet("color: #9be89b;")
                self._last_leak_ts = time.time()
            except Exception:
                pass
            return
