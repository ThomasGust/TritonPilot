from __future__ import annotations

import math
import time
from collections import deque
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import Qt, QPointF, QRectF
from PyQt6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PyQt6.QtWidgets import (
    QWidget,
    QFrame,
    QLabel,
    QGridLayout,
    QVBoxLayout,
    QHBoxLayout,
    QPushButton,
    QProgressBar,
    QSizePolicy,
)
from recording.stream_recorder import StreamRecorder


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
    """Artificial horizon with an 8-ball style attitude sphere and numeric readouts."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.roll_deg: float = 0.0
        self.pitch_deg: float = 0.0
        self.yaw_deg: Optional[float] = None
        self.mode_text: str = "-"
        self.mag_qual: Optional[float] = None
        self.updated_ts: float = 0.0
        self.setMinimumSize(170, 165)

    def set_attitude(
        self,
        roll_deg: float,
        pitch_deg: float,
        yaw_deg: Optional[float],
        *,
        mode: str = "-",
        mag_qual: Optional[float] = None,
    ):
        self.roll_deg = float(roll_deg)
        self.pitch_deg = float(pitch_deg)
        self.yaw_deg = None if yaw_deg is None else float(yaw_deg)
        self.mode_text = str(mode)
        self.mag_qual = None if mag_qual is None else float(mag_qual)
        self.updated_ts = time.time()
        self.update()

    @staticmethod
    def _clamp(value: float, lo: float, hi: float) -> float:
        return max(lo, min(hi, value))

    @staticmethod
    def _point_on_circle(cx: float, cy: float, radius: float, angle_deg: float) -> QPointF:
        angle_rad = math.radians(angle_deg - 90.0)
        return QPointF(cx + math.cos(angle_rad) * radius, cy + math.sin(angle_rad) * radius)

    @staticmethod
    def _draw_value_cell(p: QPainter, rect: QRectF, label: str, value_text: str, accent: QColor) -> None:
        p.setPen(QPen(QColor(54, 59, 68), 1))
        p.setBrush(QColor(17, 20, 26))
        p.drawRoundedRect(rect, 8.0, 8.0)

        label_rect = rect.adjusted(0.0, 5.0, 0.0, -rect.height() * 0.45)
        value_rect = rect.adjusted(0.0, rect.height() * 0.30, 0.0, -4.0)

        label_font = QFont(p.font())
        label_font.setPointSize(max(8, label_font.pointSize() - 1))
        p.setFont(label_font)
        p.setPen(QColor(160, 171, 188))
        p.drawText(label_rect, Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop, label)

        value_font = QFont(label_font)
        value_font.setBold(True)
        value_font.setPointSize(max(9, value_font.pointSize() + 2))
        p.setFont(value_font)
        p.setPen(accent)
        p.drawText(value_rect, Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter, value_text)

    @staticmethod
    def _format_angle(value: Optional[float]) -> str:
        if value is None or not math.isfinite(value):
            return "-"
        return f"{value:+.1f} deg"

    def paintEvent(self, _event):  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        outer = QRectF(self.rect()).adjusted(6.0, 6.0, -6.0, -6.0)
        if outer.width() <= 8.0 or outer.height() <= 8.0:
            return

        p.setPen(QPen(QColor(42, 42, 50), 1))
        p.setBrush(QColor(12, 14, 18))
        p.drawRoundedRect(outer, 14.0, 14.0)

        header_h = 24.0
        footer_h = max(44.0, min(60.0, outer.height() * 0.26))
        sphere_zone = outer.adjusted(10.0, header_h + 6.0, -10.0, -(footer_h + 10.0))
        sphere_side = max(0.0, min(sphere_zone.width(), sphere_zone.height()))
        sphere = QRectF(
            sphere_zone.center().x() - sphere_side * 0.5,
            sphere_zone.center().y() - sphere_side * 0.5,
            sphere_side,
            sphere_side,
        )

        cx = sphere.center().x()
        cy = sphere.center().y()
        radius = sphere.width() * 0.5

        info_font = QFont(self.font())
        info_font.setPointSize(max(8, info_font.pointSize()))
        p.setFont(info_font)
        p.setPen(QColor(220, 226, 236))
        p.drawText(
            outer.adjusted(10.0, 6.0, -10.0, 0.0),
            Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft,
            f"Mode {self.mode_text}",
        )
        mag_text = "-" if self.mag_qual is None else f"{self.mag_qual:.2f}"
        p.drawText(
            outer.adjusted(10.0, 6.0, -10.0, 0.0),
            Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignRight,
            f"Mag {mag_text}",
        )

        if sphere_side > 10.0:
            sphere_path = QPainterPath()
            sphere_path.addEllipse(sphere)

            p.save()
            p.setClipPath(sphere_path)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(8, 11, 16))
            p.drawEllipse(sphere)

            p.save()
            p.translate(cx, cy)
            p.rotate(-self.roll_deg)

            px_per_deg = max(1.5, radius / 30.0)
            y_off = self._clamp(self.pitch_deg * px_per_deg, -radius * 0.78, radius * 0.78)

            p.setBrush(QColor(54, 112, 184))
            p.drawRect(QRectF(-radius * 2.2, -radius * 2.2 + y_off, radius * 4.4, radius * 2.2))
            p.setBrush(QColor(146, 97, 56))
            p.drawRect(QRectF(-radius * 2.2, y_off, radius * 4.4, radius * 2.2))

            p.setPen(QPen(QColor(238, 242, 248), max(1.6, radius * 0.018)))
            p.drawLine(QPointF(-radius * 1.7, y_off), QPointF(radius * 1.7, y_off))

            ladder_font = QFont(info_font)
            ladder_font.setPointSize(max(7, info_font.pointSize() - 1))
            p.setFont(ladder_font)
            for deg in range(-40, 45, 5):
                if deg == 0:
                    continue
                yy = y_off + deg * px_per_deg
                if yy < -radius * 1.10 or yy > radius * 1.10:
                    continue
                is_major = (deg % 10) == 0
                half_width = radius * (0.18 if not is_major else 0.32)
                tick_pen = QPen(QColor(245, 245, 245), 1.4 if is_major else 1.0)
                p.setPen(tick_pen)
                p.drawLine(QPointF(-half_width, yy), QPointF(half_width, yy))
                if is_major:
                    label = str(abs(deg))
                    p.drawText(
                        QRectF(-radius * 0.64, yy - 8.0, 26.0, 16.0),
                        Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                        label,
                    )
                    p.drawText(
                        QRectF(radius * 0.38, yy - 8.0, 26.0, 16.0),
                        Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                        label,
                    )

            p.restore()
            p.restore()

            arc_rect = sphere.adjusted(-radius * 0.12, -radius * 0.12, radius * 0.12, radius * 0.12)
            p.setPen(QPen(QColor(88, 96, 109), 2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawArc(arc_rect, 30 * 16, 120 * 16)

            p.setPen(QPen(QColor(228, 232, 239), 2))
            for bank_deg in (-60, -45, -30, -20, -10, 10, 20, 30, 45, 60):
                outer_pt = self._point_on_circle(cx, cy, radius * 1.12, bank_deg)
                inner_len = radius * (0.10 if abs(bank_deg) in (30, 60) else 0.07)
                inner_pt = self._point_on_circle(cx, cy, radius * 1.12 - inner_len, bank_deg)
                p.drawLine(outer_pt, inner_pt)

            p.setPen(QPen(QColor(250, 250, 250), 2))
            p.drawLine(
                QPointF(cx, cy - radius * 1.16),
                QPointF(cx - radius * 0.05, cy - radius * 1.06),
            )
            p.drawLine(
                QPointF(cx, cy - radius * 1.16),
                QPointF(cx + radius * 0.05, cy - radius * 1.06),
            )

            p.setPen(QPen(QColor(226, 182, 72), max(2.0, radius * 0.035)))
            wing_y = cy + radius * 0.03
            p.drawLine(QPointF(cx - radius * 0.42, wing_y), QPointF(cx - radius * 0.10, wing_y))
            p.drawLine(QPointF(cx + radius * 0.10, wing_y), QPointF(cx + radius * 0.42, wing_y))
            p.drawLine(QPointF(cx - radius * 0.10, wing_y), QPointF(cx, cy + radius * 0.13))
            p.drawLine(QPointF(cx + radius * 0.10, wing_y), QPointF(cx, cy + radius * 0.13))
            p.drawLine(QPointF(cx, cy - radius * 0.03), QPointF(cx, cy + radius * 0.18))

            p.setPen(QPen(QColor(196, 202, 212), 2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(sphere)

        readout_area = QRectF(
            outer.left() + 8.0,
            outer.bottom() - footer_h - 4.0,
            max(0.0, outer.width() - 16.0),
            max(0.0, footer_h),
        )
        gap = 6.0
        cell_w = max(0.0, (readout_area.width() - 2.0 * gap) / 3.0)
        cells = [
            QRectF(readout_area.left(), readout_area.top(), cell_w, readout_area.height()),
            QRectF(readout_area.left() + cell_w + gap, readout_area.top(), cell_w, readout_area.height()),
            QRectF(readout_area.left() + (cell_w + gap) * 2.0, readout_area.top(), cell_w, readout_area.height()),
        ]
        self._draw_value_cell(p, cells[0], "ROLL", self._format_angle(self.roll_deg), QColor(247, 198, 84))
        self._draw_value_cell(p, cells[1], "PITCH", self._format_angle(self.pitch_deg), QColor(112, 194, 255))
        self._draw_value_cell(p, cells[2], "YAW", self._format_angle(self.yaw_deg), QColor(156, 226, 156))

        age = (time.time() - self.updated_ts) if self.updated_ts else None
        if age is None or age > 2.0:
            stale_rect = QRectF(outer.right() - 66.0, outer.top() + 28.0, 56.0, 18.0)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(120, 66, 34))
            p.drawRoundedRect(stale_rect, 9.0, 9.0)
            stale_font = QFont(info_font)
            stale_font.setBold(True)
            stale_font.setPointSize(max(7, stale_font.pointSize() - 1))
            p.setFont(stale_font)
            p.setPen(QColor(255, 214, 168))
            p.drawText(stale_rect, Qt.AlignmentFlag.AlignCenter, "STALE")


class AttitudeHistoryChartWidget(QWidget):
    """Simple rolling history plot for recent attitude telemetry."""

    def __init__(self, *, window_seconds: float = 20.0, parent=None):
        super().__init__(parent)
        self.window_seconds = max(5.0, float(window_seconds))
        self._samples: deque[tuple[float, float, float, Optional[float]]] = deque(maxlen=2400)
        self.setMinimumHeight(220)

    @staticmethod
    def _normalize_yaw(value: Optional[float]) -> Optional[float]:
        if value is None or not math.isfinite(value):
            return None
        wrapped = (float(value) + 180.0) % 360.0 - 180.0
        return wrapped

    def add_sample(self, timestamp_s: float, roll_deg: float, pitch_deg: float, yaw_deg: Optional[float]) -> None:
        self._samples.append((float(timestamp_s), float(roll_deg), float(pitch_deg), yaw_deg))
        self._trim_old(float(timestamp_s))
        self.update()

    def _trim_old(self, newest_ts: float) -> None:
        cutoff = float(newest_ts) - self.window_seconds - 1.0
        while self._samples and self._samples[0][0] < cutoff:
            self._samples.popleft()

    def _visible_samples(self) -> list[tuple[float, float, float, Optional[float]]]:
        if not self._samples:
            return []
        newest_ts = self._samples[-1][0]
        cutoff = newest_ts - self.window_seconds
        return [sample for sample in self._samples if sample[0] >= cutoff]

    def _draw_series(
        self,
        painter: QPainter,
        plot_rect: QRectF,
        samples: list[tuple[float, float, float, Optional[float]]],
        newest_ts: float,
        angle_range: float,
        getter,
        color: QColor,
        *,
        break_on_wrap: bool = False,
    ) -> None:
        painter.setPen(QPen(color, 2))
        path = QPainterPath()
        path_started = False
        prev_value: Optional[float] = None
        for sample in samples:
            ts = sample[0]
            value = getter(sample)
            if value is None or not math.isfinite(value):
                path_started = False
                prev_value = None
                continue
            if break_on_wrap and prev_value is not None and abs(value - prev_value) > 180.0:
                path_started = False
            prev_value = value
            age = newest_ts - ts
            x = plot_rect.right() - (age / self.window_seconds) * plot_rect.width()
            frac = (value + angle_range) / (2.0 * angle_range)
            y = plot_rect.bottom() - frac * plot_rect.height()
            if not path_started:
                path.moveTo(x, y)
                path_started = True
            else:
                path.lineTo(x, y)
        painter.drawPath(path)

    def paintEvent(self, _event):  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        outer = QRectF(self.rect()).adjusted(6.0, 6.0, -6.0, -6.0)
        if outer.width() <= 8.0 or outer.height() <= 8.0:
            return

        painter.setPen(QPen(QColor(42, 42, 50), 1))
        painter.setBrush(QColor(12, 14, 18))
        painter.drawRoundedRect(outer, 14.0, 14.0)

        title_font = QFont(self.font())
        title_font.setBold(True)
        painter.setFont(title_font)
        painter.setPen(QColor(232, 237, 245))
        painter.drawText(
            outer.adjusted(12.0, 8.0, -12.0, 0.0),
            Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft,
            "Attitude History",
        )

        legend_font = QFont(self.font())
        legend_font.setPointSize(max(8, legend_font.pointSize() - 1))
        painter.setFont(legend_font)
        legend = [("ROLL", QColor(247, 198, 84)), ("PITCH", QColor(112, 194, 255)), ("YAW", QColor(156, 226, 156))]
        legend_x = outer.left() + 12.0
        legend_y = outer.top() + 28.0
        for label, color in legend:
            painter.setPen(QPen(color, 3))
            painter.drawLine(QPointF(legend_x, legend_y), QPointF(legend_x + 14.0, legend_y))
            painter.setPen(QColor(190, 198, 210))
            painter.drawText(QRectF(legend_x + 18.0, legend_y - 8.0, 56.0, 16.0), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, label)
            legend_x += 74.0

        samples = self._visible_samples()
        plot_rect = outer.adjusted(48.0, 50.0, -12.0, -28.0)
        if plot_rect.width() <= 20.0 or plot_rect.height() <= 20.0:
            return

        painter.setPen(QPen(QColor(30, 34, 42), 1))
        painter.setBrush(QColor(8, 10, 14))
        painter.drawRoundedRect(plot_rect, 10.0, 10.0)

        if not samples:
            painter.setPen(QColor(150, 158, 172))
            painter.drawText(plot_rect, Qt.AlignmentFlag.AlignCenter, "Waiting for attitude telemetry")
            return

        newest_ts = samples[-1][0]
        values: list[float] = []
        for _ts, roll, pitch, yaw in samples:
            if math.isfinite(roll):
                values.append(abs(float(roll)))
            if math.isfinite(pitch):
                values.append(abs(float(pitch)))
            norm_yaw = self._normalize_yaw(yaw)
            if norm_yaw is not None:
                values.append(abs(norm_yaw))
        peak = max(values) if values else 1.0
        angle_range = min(180.0, max(20.0, math.ceil(peak / 10.0) * 10.0))

        grid_pen = QPen(QColor(34, 40, 48), 1)
        painter.setPen(grid_pen)
        for i in range(6):
            frac = i / 5.0
            y = plot_rect.top() + frac * plot_rect.height()
            painter.drawLine(QPointF(plot_rect.left(), y), QPointF(plot_rect.right(), y))
            value = angle_range - frac * (2.0 * angle_range)
            painter.setPen(QColor(132, 140, 154))
            painter.drawText(
                QRectF(outer.left() + 6.0, y - 8.0, 36.0, 16.0),
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                f"{value:.0f}",
            )
            painter.setPen(grid_pen)
        for i in range(5):
            frac = i / 4.0
            x = plot_rect.left() + frac * plot_rect.width()
            painter.drawLine(QPointF(x, plot_rect.top()), QPointF(x, plot_rect.bottom()))
            secs = self.window_seconds * (1.0 - frac)
            painter.setPen(QColor(132, 140, 154))
            painter.drawText(
                QRectF(x - 20.0, plot_rect.bottom() + 4.0, 40.0, 16.0),
                Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop,
                "now" if i == 4 else f"-{secs:.0f}s",
            )
            painter.setPen(grid_pen)

        self._draw_series(
            painter,
            plot_rect,
            samples,
            newest_ts,
            angle_range,
            lambda sample: sample[1],
            QColor(247, 198, 84),
        )
        self._draw_series(
            painter,
            plot_rect,
            samples,
            newest_ts,
            angle_range,
            lambda sample: sample[2],
            QColor(112, 194, 255),
        )
        self._draw_series(
            painter,
            plot_rect,
            samples,
            newest_ts,
            angle_range,
            lambda sample: self._normalize_yaw(sample[3]),
            QColor(156, 226, 156),
            break_on_wrap=True,
        )

        painter.setPen(QColor(132, 140, 154))
        painter.drawText(
            QRectF(plot_rect.left(), outer.bottom() - 18.0, plot_rect.width(), 14.0),
            Qt.AlignmentFlag.AlignCenter,
            f"Window {self.window_seconds:.0f}s   |   Scale +/-{angle_range:.0f} deg",
        )


class AttitudeInspectorPage(QWidget):
    """Dedicated page for attitude inspection, charting, and CSV capture."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._record_fh = None
        self._record_path: Optional[Path] = None
        self._record_started_ts: Optional[float] = None

        title = QLabel("Attitude Inspector")
        title_font = QFont(title.font())
        title_font.setBold(True)
        title_font.setPointSize(max(13, title_font.pointSize() + 2))
        title.setFont(title_font)

        subtitle = QLabel("Focused attitude page with live roll, pitch, yaw history and quick CSV capture.")
        subtitle.setWordWrap(True)
        subtitle.setStyleSheet("color: #b6bac8;")

        self.att_card = _Card("Attitude")
        self.attitude = AttitudeHorizonWidget()
        self.attitude.setMinimumHeight(320)
        self.att_card.body.addWidget(self.attitude)

        self.history_card = _Card("Last 20 Seconds")
        self.history_chart = AttitudeHistoryChartWidget(window_seconds=20.0)
        self.history_card.body.addWidget(self.history_chart)

        self.record_card = _Card("Recorder")
        controls = QHBoxLayout()
        controls.setContentsMargins(0, 0, 0, 0)
        controls.setSpacing(8)
        self.record_btn = QPushButton("Start Attitude Recording")
        self.record_btn.clicked.connect(self._toggle_recording)
        self.record_state = QLabel("Recording: idle")
        self.record_state.setAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft)
        controls.addWidget(self.record_btn, 0)
        controls.addWidget(self.record_state, 1)

        self.record_path_lbl = QLabel("File: -")
        self.record_path_lbl.setWordWrap(True)
        self.record_path_lbl.setStyleSheet("color: #b6bac8;")

        self.record_card.body.addLayout(controls)
        self.record_card.body.addWidget(self.record_path_lbl)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        lay.addWidget(title)
        lay.addWidget(subtitle)
        lay.addWidget(self.record_card)
        lay.addWidget(self.att_card)
        lay.addWidget(self.history_card, 1)

    def _toggle_recording(self) -> None:
        if self._record_fh is None:
            self._start_recording()
        else:
            self._stop_recording()

    def _start_recording(self) -> None:
        if self._record_fh is not None:
            return
        try:
            session_dir = StreamRecorder.make_session_dir("recordings")
            target = Path(session_dir) / "attitude_timeseries.csv"
            fh = open(target, "a", buffering=1, newline="")
            fh.write("unix_time_s,elapsed_s,roll_deg,pitch_deg,yaw_deg,mode,mag_qual\n")
            self._record_fh = fh
            self._record_path = target
            self._record_started_ts = time.time()
            self.record_btn.setText("Stop Attitude Recording")
            self.record_state.setText("Recording: active")
            self.record_path_lbl.setText(f"File: {target}")
        except Exception as exc:
            self._record_fh = None
            self._record_path = None
            self._record_started_ts = None
            self.record_state.setText(f"Recording: error ({exc})")

    def _stop_recording(self) -> None:
        fh = self._record_fh
        self._record_fh = None
        self._record_started_ts = None
        if fh is not None:
            try:
                fh.flush()
                fh.close()
            except Exception:
                pass
        self.record_btn.setText("Start Attitude Recording")
        self.record_state.setText("Recording: idle")

    def shutdown(self) -> None:
        self._stop_recording()

    def update_from_sensor(self, msg: dict) -> None:
        if (msg or {}).get("type") != "attitude":
            return
        try:
            rpy = (msg or {}).get("rpy_deg") or {}
            health = (msg or {}).get("health") or {}
            roll = float(rpy.get("roll", 0.0) or 0.0)
            pitch = float(rpy.get("pitch", 0.0) or 0.0)
            yaw = None if rpy.get("yaw") is None else float(rpy.get("yaw") or 0.0)
            mode = str(health.get("mode", "-"))
            mag_qual = None if health.get("mag_qual") is None else float(health.get("mag_qual"))
            now = time.time()

            self.attitude.set_attitude(roll, pitch, yaw, mode=mode, mag_qual=mag_qual)
            self.history_chart.add_sample(now, roll, pitch, yaw)

            if self._record_fh is not None:
                elapsed = 0.0 if self._record_started_ts is None else max(0.0, now - self._record_started_ts)
                yaw_text = "" if yaw is None else f"{yaw:.3f}"
                mag_text = "" if mag_qual is None else f"{mag_qual:.4f}"
                self._record_fh.write(f"{now:.6f},{elapsed:.3f},{roll:.3f},{pitch:.3f},{yaw_text},{mode},{mag_text}\n")
                self.record_state.setText(f"Recording: active ({elapsed:.1f}s)")
        except Exception:
            pass


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

        self.env_card = _Card("Temp / Leak")
        self.temp_bar = QProgressBar()
        self.temp_bar.setRange(0, 1000)  # map -10..90 C internally
        self.temp_bar.setFormat("Temp: -")
        self.leak_lbl = QLabel("Leak: unknown")
        self.leak_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        for w in (self.temp_bar, self.leak_lbl):
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
                    pressure = (msg or {}).get("pressure_mbar")
                    self.depth_gauge.set_value(None if depth is None else float(depth), secondary=(f"{float(temp):.1f} C" if temp is not None else ""))
                    meta = []
                    if pressure is not None:
                        meta.append(f"{float(pressure):.0f} mbar")
                    if temp is not None:
                        meta.append(f"{float(temp):.1f} C")
                    self.depth_meta.setText(" | ".join(meta) if meta else "-")
                self._last_depth_ts = time.time()
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
                self._last_env_ts = time.time()
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
                self._last_leak_ts = time.time()
            except Exception:
                pass
            return


class HoldTestPanel(QWidget):
    """Focused attitude/depth panel for stabilization and hold testing."""

    def __init__(self, parent=None):
        super().__init__(parent)

        title = QLabel("Hold Test")
        title_font = QFont(title.font())
        title_font.setBold(True)
        title_font.setPointSize(max(12, title_font.pointSize() + 1))
        title.setFont(title_font)

        subtitle = QLabel("Single-camera piloting page with live attitude and depth telemetry.")
        subtitle.setWordWrap(True)
        subtitle.setStyleSheet("color: #b6bac8;")

        self.att_card = _Card("Attitude")
        self.attitude = AttitudeHorizonWidget()
        self.attitude.setMinimumHeight(220)
        self.att_card.body.addWidget(self.attitude)

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

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        lay.addWidget(title)
        lay.addWidget(subtitle)
        lay.addWidget(self.att_card)
        lay.addWidget(self.depth_card)
        lay.addStretch(1)

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
            except Exception:
                pass
            return

        if typ != "external_depth":
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
