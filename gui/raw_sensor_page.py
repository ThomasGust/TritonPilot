from __future__ import annotations

import math
import threading
import time
from collections import deque
from pathlib import Path
from typing import Callable, Optional

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen, QPolygonF
from PyQt6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from recording.raw_sensor_csv import RawSensorCsvLogger
from recording.stream_recorder import StreamRecorder
from telemetry.roll_pitch_estimator import RollPitchEstimator


class _Card(QFrame):
    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        self.setObjectName("RawSensorCard")
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

        title_label = QLabel(title)
        title_label.setObjectName("RawSensorCardTitle")
        f = QFont(title_label.font())
        f.setBold(True)
        title_label.setFont(f)

        self.body = QVBoxLayout()
        self.body.setContentsMargins(0, 0, 0, 0)
        self.body.setSpacing(8)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)
        layout.addWidget(title_label)
        layout.addLayout(self.body)

        self.setStyleSheet(
            """
            QFrame#RawSensorCard {
                border: 1px solid #2a2a32;
                border-radius: 8px;
                background: #16161b;
            }
            QLabel#RawSensorCardTitle {
                color: #eef2f8;
            }
            """
        )


def _num(value, *, decimals: int = 3, unit: str = "") -> str:
    try:
        v = float(value)
    except Exception:
        return "-"
    if not math.isfinite(v):
        return "-"
    text = f"{v:.{int(decimals)}f}"
    return f"{text} {unit}".strip() if unit else text


def _finite_float(value) -> Optional[float]:
    try:
        v = float(value)
    except Exception:
        return None
    return v if math.isfinite(v) else None


def _vec(msg: dict | None) -> tuple[Optional[float], Optional[float], Optional[float]]:
    if not isinstance(msg, dict):
        return None, None, None
    out = []
    for key in ("x", "y", "z"):
        try:
            value = float(msg.get(key))
        except Exception:
            value = None
        out.append(value if value is not None and math.isfinite(value) else None)
    return out[0], out[1], out[2]


def _vec_norm(msg: dict | None) -> Optional[float]:
    x, y, z = _vec(msg)
    if x is None or y is None or z is None:
        return None
    return math.sqrt(x * x + y * y + z * z)


def _fmt_vec(msg: dict | None, *, decimals: int = 3, unit: str = "") -> str:
    x, y, z = _vec(msg)
    if x is None or y is None or z is None:
        return "-"
    norm = math.sqrt(x * x + y * y + z * z)
    suffix = f" {unit}" if unit else ""
    return (
        f"x {x:.{decimals}f} | y {y:.{decimals}f} | z {z:.{decimals}f} | "
        f"|v| {norm:.{decimals}f}{suffix}"
    )


class RollingVectorPlot(QWidget):
    def __init__(
        self,
        title: str,
        series: list[str],
        *,
        window_s: float = 20.0,
        max_update_hz: float = 15.0,
        parent=None,
    ):
        super().__init__(parent)
        self.title = str(title)
        self.series = list(series)
        self.window_s = max(2.0, float(window_s))
        self._min_update_interval_s = 1.0 / max(1.0, float(max_update_hz))
        self._last_update_s = 0.0
        self._display_scale = 1.0
        self._display_scale_update_s = time.monotonic()
        self.samples: deque[tuple[float, dict[str, float]]] = deque(maxlen=2400)
        self.colors = {
            "x": QColor(247, 198, 84),
            "y": QColor(112, 194, 255),
            "z": QColor(156, 226, 156),
            "norm": QColor(228, 142, 255),
            "roll": QColor(247, 198, 84),
            "pitch": QColor(112, 194, 255),
            "yaw": QColor(255, 170, 92),
            "yaw_mag": QColor(255, 118, 118),
            "tilt": QColor(156, 226, 156),
            "accel_roll": QColor(228, 142, 255),
            "accel_pitch": QColor(255, 160, 122),
            "depth": QColor(112, 194, 255),
            "sensor": QColor(247, 198, 84),
        }
        self.setMinimumHeight(190)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)

    def add_sample(self, ts: float, values: dict[str, float]) -> None:
        clean: dict[str, float] = {}
        for name in self.series:
            try:
                v = float(values.get(name))
            except Exception:
                continue
            if math.isfinite(v):
                clean[str(name)] = v
        if not clean:
            return
        self.samples.append((float(ts), clean))
        self._trim(float(ts))
        now = time.monotonic()
        if (now - self._last_update_s) >= self._min_update_interval_s:
            self._last_update_s = now
            self.update()

    def _trim(self, newest_ts: float) -> None:
        cutoff = float(newest_ts) - self.window_s - 1.0
        while self.samples and self.samples[0][0] < cutoff:
            self.samples.popleft()

    def _visible(self) -> list[tuple[float, dict[str, float]]]:
        if not self.samples:
            return []
        newest = self.samples[-1][0]
        cutoff = newest - self.window_s
        return [sample for sample in self.samples if sample[0] >= cutoff]

    def paintEvent(self, _event):  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.fillRect(self.rect(), QColor(18, 18, 22))
        outer = QRectF(self.rect()).adjusted(6, 6, -6, -6)
        if outer.width() <= 8 or outer.height() <= 8:
            return

        painter.setPen(QPen(QColor(42, 42, 50), 1))
        painter.setBrush(QColor(12, 14, 18))
        painter.drawRoundedRect(outer, 8.0, 8.0)

        title_font = QFont(self.font())
        title_font.setBold(True)
        painter.setFont(title_font)
        painter.setPen(QColor(232, 237, 245))
        painter.drawText(outer.adjusted(10, 6, -10, 0), Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft, self.title)

        legend_x = outer.left() + 10
        legend_y = outer.top() + 30
        legend_font = QFont(self.font())
        legend_font.setPointSize(max(8, legend_font.pointSize() - 1))
        painter.setFont(legend_font)
        for name in self.series:
            color = self.colors.get(name, QColor(220, 220, 220))
            painter.setPen(QPen(color, 3))
            painter.drawLine(QPointF(legend_x, legend_y), QPointF(legend_x + 14, legend_y))
            painter.setPen(QColor(190, 198, 210))
            text_w = painter.fontMetrics().horizontalAdvance(str(name))
            painter.drawText(QRectF(legend_x + 18, legend_y - 8, text_w + 4, 16), Qt.AlignmentFlag.AlignLeft, name)
            legend_x += max(64, 28 + text_w)

        plot = outer.adjusted(44, 52, -12, -24)
        if plot.width() <= 20 or plot.height() <= 20:
            return
        painter.setPen(QPen(QColor(30, 34, 42), 1))
        painter.setBrush(QColor(8, 10, 14))
        painter.drawRoundedRect(plot, 6.0, 6.0)

        visible = self._visible()
        if not visible:
            painter.setPen(QColor(150, 158, 172))
            painter.drawText(plot, Qt.AlignmentFlag.AlignCenter, "Waiting for raw telemetry")
            return

        values = [abs(v) for _ts, sample in visible for v in sample.values()]
        peak = max(values) if values else 1.0
        target_scale = max(1.0, math.ceil(peak * 1.1))
        now_s = time.monotonic()
        dt = max(0.0, min(1.0, now_s - self._display_scale_update_s))
        self._display_scale_update_s = now_s
        if target_scale >= self._display_scale:
            self._display_scale = target_scale
        else:
            # Let the axis relax downward gradually so one quiet sample does not
            # make active traces appear to jump or vanish after motion.
            self._display_scale -= (self._display_scale - target_scale) * min(1.0, dt / 2.0)
            self._display_scale = max(target_scale, self._display_scale)
        scale = max(1.0, self._display_scale)

        grid_pen = QPen(QColor(34, 40, 48), 1)
        for i in range(5):
            frac = i / 4.0
            y = plot.top() + frac * plot.height()
            painter.setPen(grid_pen)
            painter.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))
            value = scale - frac * (2.0 * scale)
            painter.setPen(QColor(132, 140, 154))
            painter.drawText(QRectF(outer.left() + 4, y - 8, 36, 16), Qt.AlignmentFlag.AlignRight, f"{value:.0f}")

        newest = visible[-1][0]
        for name in self.series:
            path = QPainterPath()
            started = False
            point_count = 0
            last_point: QPointF | None = None
            for ts, sample in visible:
                if name not in sample:
                    continue
                age = newest - ts
                x = plot.right() - (age / self.window_s) * plot.width()
                frac = (sample[name] + scale) / (2.0 * scale)
                y = plot.bottom() - frac * plot.height()
                point = QPointF(x, y)
                if not started:
                    path.moveTo(point)
                    started = True
                else:
                    path.lineTo(point)
                point_count += 1
                last_point = point
            color = self.colors.get(name, QColor(220, 220, 220))
            painter.setPen(QPen(color, 2))
            painter.drawPath(path)
            if point_count == 1 and last_point is not None:
                painter.setBrush(color)
                painter.drawEllipse(last_point, 2.25, 2.25)

        painter.setPen(QColor(132, 140, 154))
        painter.drawText(
            QRectF(plot.left(), outer.bottom() - 18, plot.width(), 14),
            Qt.AlignmentFlag.AlignCenter,
            f"Window {self.window_s:.0f}s | scale +/-{scale:.0f}",
        )


class Attitude3DWidget(QWidget):
    """Small software-rendered attitude view for the ROV body frame."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.roll_deg: float | None = None
        self.pitch_deg: float | None = None
        self.yaw_deg: float | None = None
        self.source: str = ""
        self.yaw_status: str = ""
        self.setMinimumHeight(260)
        self.setMinimumWidth(280)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

    @staticmethod
    def _finite(value) -> float | None:
        try:
            out = float(value)
        except Exception:
            return None
        return out if math.isfinite(out) else None

    def clear(self) -> None:
        self.roll_deg = None
        self.pitch_deg = None
        self.yaw_deg = None
        self.source = ""
        self.yaw_status = ""
        self.update()

    def set_attitude(self, msg: dict | None) -> None:
        msg = dict(msg or {})
        self.roll_deg = self._finite(msg.get("roll_deg"))
        self.pitch_deg = self._finite(msg.get("pitch_deg"))
        self.yaw_deg = self._finite(msg.get("yaw_deg"))
        self.source = str(msg.get("source") or "")
        self.yaw_status = str(msg.get("yaw_status") or "")
        self.update()

    @staticmethod
    def _dot(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
        return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]

    @staticmethod
    def _cross(a: tuple[float, float, float], b: tuple[float, float, float]) -> tuple[float, float, float]:
        return (
            a[1] * b[2] - a[2] * b[1],
            a[2] * b[0] - a[0] * b[2],
            a[0] * b[1] - a[1] * b[0],
        )

    @staticmethod
    def _normalize(v: tuple[float, float, float]) -> tuple[float, float, float]:
        n = math.sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2])
        if n <= 1e-9:
            return (0.0, 0.0, 0.0)
        return (v[0] / n, v[1] / n, v[2] / n)

    @staticmethod
    def _rotate(
        v: tuple[float, float, float],
        roll_rad: float,
        pitch_rad: float,
        yaw_rad: float,
    ) -> tuple[float, float, float]:
        x, y, z = v

        cr = math.cos(roll_rad)
        sr = math.sin(roll_rad)
        y, z = y * cr - z * sr, y * sr + z * cr

        cp = math.cos(pitch_rad)
        sp = math.sin(pitch_rad)
        x, z = x * cp - z * sp, x * sp + z * cp

        cy = math.cos(yaw_rad)
        sy = math.sin(yaw_rad)
        x, y = x * cy - y * sy, x * sy + y * cy
        return (x, y, z)

    def _projector(self, center: QPointF, scale: float):
        camera = self._normalize((3.2, -4.4, 2.6))
        right = self._normalize(self._cross((0.0, 0.0, 1.0), camera))
        up = self._normalize(self._cross(camera, right))

        def project(v: tuple[float, float, float]) -> tuple[QPointF, float]:
            sx = self._dot(v, right) * scale
            sy = self._dot(v, up) * scale
            depth = self._dot(v, camera)
            return QPointF(center.x() + sx, center.y() - sy), depth

        return project

    @staticmethod
    def _fmt_angle(value: float | None) -> str:
        return "-" if value is None else f"{value:+.1f}"

    def paintEvent(self, _event):  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        outer = QRectF(self.rect()).adjusted(6, 6, -6, -6)
        if outer.width() <= 8 or outer.height() <= 8:
            return

        painter.setPen(QPen(QColor(42, 42, 50), 1))
        painter.setBrush(QColor(12, 14, 18))
        painter.drawRoundedRect(outer, 8.0, 8.0)

        title_font = QFont(self.font())
        title_font.setBold(True)
        painter.setFont(title_font)
        painter.setPen(QColor(232, 237, 245))
        painter.drawText(outer.adjusted(10, 6, -10, 0), Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft, "3D Attitude")

        view = outer.adjusted(10, 34, -10, -34)
        if view.width() <= 40 or view.height() <= 40:
            return

        painter.setPen(QPen(QColor(30, 34, 42), 1))
        painter.setBrush(QColor(8, 10, 14))
        painter.drawRoundedRect(view, 6.0, 6.0)

        if self.roll_deg is None or self.pitch_deg is None:
            painter.setPen(QColor(150, 158, 172))
            painter.drawText(view, Qt.AlignmentFlag.AlignCenter, "Waiting for attitude")
            return

        roll = math.radians(float(self.roll_deg))
        pitch = math.radians(float(self.pitch_deg))
        yaw = math.radians(float(self.yaw_deg or 0.0))

        center = QPointF(view.center().x(), view.center().y() + 10.0)
        scale = max(28.0, min(view.width() / 4.4, view.height() / 3.2))
        project = self._projector(center, scale)

        grid_pen = QPen(QColor(30, 48, 58), 1)
        grid_pen.setStyle(Qt.PenStyle.DotLine)
        painter.setPen(grid_pen)
        for i in range(-2, 3):
            p0, _ = project((-2.0, float(i), -0.68))
            p1, _ = project((2.0, float(i), -0.68))
            painter.drawLine(p0, p1)
            p2, _ = project((float(i), -2.0, -0.68))
            p3, _ = project((float(i), 2.0, -0.68))
            painter.drawLine(p2, p3)

        half_l, half_w, half_h = 1.25, 0.62, 0.34
        vertices = [
            (-half_l, -half_w, -half_h),
            (half_l, -half_w, -half_h),
            (half_l, half_w, -half_h),
            (-half_l, half_w, -half_h),
            (-half_l, -half_w, half_h),
            (half_l, -half_w, half_h),
            (half_l, half_w, half_h),
            (-half_l, half_w, half_h),
        ]
        rotated = [self._rotate(v, roll, pitch, yaw) for v in vertices]
        projected = [project(v) for v in rotated]

        faces = [
            ([0, 1, 2, 3], QColor(35, 47, 60, 210)),
            ([4, 5, 6, 7], QColor(55, 133, 154, 215)),
            ([0, 1, 5, 4], QColor(47, 96, 126, 205)),
            ([3, 2, 6, 7], QColor(62, 111, 92, 205)),
            ([0, 3, 7, 4], QColor(46, 58, 76, 205)),
            ([1, 2, 6, 5], QColor(229, 151, 72, 220)),
        ]
        face_depths = []
        for indices, color in faces:
            depth = sum(projected[i][1] for i in indices) / len(indices)
            face_depths.append((depth, indices, color))

        for _depth, indices, color in sorted(face_depths, key=lambda item: item[0]):
            poly = QPolygonF([projected[i][0] for i in indices])
            painter.setPen(QPen(QColor(13, 18, 24), 1))
            painter.setBrush(color)
            painter.drawPolygon(poly)

        edge_pen = QPen(QColor(224, 232, 240), 1)
        painter.setPen(edge_pen)
        for a, b in ((0, 1), (1, 2), (2, 3), (3, 0), (4, 5), (5, 6), (6, 7), (7, 4), (0, 4), (1, 5), (2, 6), (3, 7)):
            painter.drawLine(projected[a][0], projected[b][0])

        nose_points = [
            self._rotate((half_l, 0.0, 0.0), roll, pitch, yaw),
            self._rotate((half_l + 0.52, 0.0, 0.0), roll, pitch, yaw),
            self._rotate((half_l + 0.30, -0.18, 0.0), roll, pitch, yaw),
            self._rotate((half_l + 0.30, 0.18, 0.0), roll, pitch, yaw),
        ]
        p_tail, _ = project(nose_points[0])
        p_tip, _ = project(nose_points[1])
        p_left, _ = project(nose_points[2])
        p_right, _ = project(nose_points[3])
        painter.setPen(QPen(QColor(255, 204, 99), 3))
        painter.drawLine(p_tail, p_tip)
        painter.setPen(QPen(QColor(255, 204, 99), 2))
        painter.drawLine(p_tip, p_left)
        painter.drawLine(p_tip, p_right)

        pods = [
            self._rotate((-0.70, -0.86, -0.05), roll, pitch, yaw),
            self._rotate((0.70, -0.86, -0.05), roll, pitch, yaw),
            self._rotate((-0.70, 0.86, -0.05), roll, pitch, yaw),
            self._rotate((0.70, 0.86, -0.05), roll, pitch, yaw),
        ]
        pod_points = [project(p) for p in pods]
        for point, _depth in sorted(pod_points, key=lambda item: item[1]):
            painter.setPen(QPen(QColor(13, 18, 24), 1))
            painter.setBrush(QColor(185, 196, 210, 220))
            painter.drawEllipse(point, 5.0, 5.0)

        painter.setPen(QColor(190, 198, 210))
        readout = (
            f"R {self._fmt_angle(self.roll_deg)} deg   "
            f"P {self._fmt_angle(self.pitch_deg)} deg   "
            f"Y {self._fmt_angle(self.yaw_deg)} deg"
        )
        painter.drawText(QRectF(outer.left() + 10, outer.bottom() - 24, outer.width() - 20, 18), Qt.AlignmentFlag.AlignCenter, readout)


class RawSensorPage(QWidget):
    """Raw ROV telemetry dashboard with focused CSV capture."""

    LOCAL_ATTITUDE_FALLBACK_STALE_S = 2.0

    def __init__(self, parent=None, recording_session_provider: Callable[[], Path] | None = None):
        super().__init__(parent)
        self._recording_session_provider = recording_session_provider
        self._logger: RawSensorCsvLogger | None = None
        self._logger_lock = threading.Lock()
        self._record_started_ts: float | None = None
        self._last_seen: dict[str, float] = {}
        self._last_messages: dict[str, dict] = {}
        self._type_counts: dict[str, int] = {}
        self._type_rates: dict[str, float] = {}
        self._rate_t0 = time.time()
        self._attitude_estimator = RollPitchEstimator()
        self._latest_attitude: dict | None = None
        self._last_onboard_attitude_recv_s: float | None = None
        self._latest_depth_msg: dict | None = None
        self._latest_depth_m: float | None = None
        self._depth_zero_m: float | None = None

        title = QLabel("Raw Sensors")
        title_font = QFont(title.font())
        title_font.setBold(True)
        title_font.setPointSize(max(13, title_font.pointSize() + 2))
        title.setFont(title_font)

        self.record_card = _Card("Capture")
        record_row = QHBoxLayout()
        record_row.setContentsMargins(0, 0, 0, 0)
        record_row.setSpacing(8)
        self.record_btn = QPushButton("Start Raw CSV")
        self.record_btn.clicked.connect(self._toggle_recording)
        self.attitude_rest_btn = QPushButton("Set Local Rest")
        self.attitude_rest_btn.clicked.connect(self._reset_attitude_reference)
        self.record_state = QLabel("Recording: idle")
        self.record_path_lbl = QLabel("File: -")
        self.record_path_lbl.setWordWrap(True)
        self.record_path_lbl.setStyleSheet("color: #b6bac8;")
        record_row.addWidget(self.record_btn, 0)
        record_row.addWidget(self.attitude_rest_btn, 0)
        record_row.addWidget(self.record_state, 1)
        self.record_card.body.addLayout(record_row)
        self.record_card.body.addWidget(self.record_path_lbl)

        self.summary_card = _Card("Live Values")
        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(6)
        self._labels: dict[str, QLabel] = {}
        for row, (label_text, key) in enumerate(
            [
                ("Rates", "rates"),
                ("IMU Age", "imu_age"),
                ("Mag Age", "mag_age"),
                ("Attitude", "attitude"),
                ("Rest Reference", "attitude_ref"),
                ("Accel", "accel"),
                ("Gyro", "gyro"),
                ("Primary Mag", "mag"),
                ("AK09915", "ak"),
                ("MMC5983", "mmc"),
                ("Depth", "depth"),
                ("Depth Reference", "depth_ref"),
                ("Environment", "env"),
                ("ADC", "adc"),
                ("Power", "power"),
                ("Leak", "leak"),
            ]
        ):
            label = QLabel(label_text)
            value = QLabel("-")
            value.setWordWrap(True)
            value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            grid.addWidget(label, row, 0)
            grid.addWidget(value, row, 1)
            self._labels[key] = value
        self.summary_card.body.addLayout(grid)

        self.accel_plot = RollingVectorPlot("Accelerometer", ["x", "y", "z", "norm"], window_s=20.0)
        self.gyro_plot = RollingVectorPlot("Gyroscope", ["x", "y", "z", "norm"], window_s=20.0)
        self.attitude_plot = RollingVectorPlot(
            "Final Attitude",
            ["roll", "pitch", "yaw"],
            window_s=20.0,
        )
        self.attitude_view = Attitude3DWidget()
        self.mag_plot = RollingVectorPlot("Primary Magnetometer", ["x", "y", "z", "norm"], window_s=20.0)
        self.ak_plot = RollingVectorPlot("AK09915 Magnetometer", ["x", "y", "z", "norm"], window_s=20.0)
        self.mmc_plot = RollingVectorPlot("MMC5983 Magnetometer", ["x", "y", "z", "norm"], window_s=20.0)
        self.depth_plot = RollingVectorPlot("Depth", ["depth", "sensor"], window_s=60.0, max_update_hz=10.0)

        attitude_card = _Card("Attitude Display")
        attitude_row = QHBoxLayout()
        attitude_row.setContentsMargins(0, 0, 0, 0)
        attitude_row.setSpacing(8)
        attitude_row.addWidget(self.attitude_view, 1)
        attitude_row.addWidget(self.attitude_plot, 1)
        attitude_card.body.addLayout(attitude_row)

        plot_card = _Card("Rolling Raw Vectors")
        plot_card.body.addWidget(self.depth_plot)
        plot_card.body.addWidget(self.accel_plot)
        plot_card.body.addWidget(self.gyro_plot)
        plot_card.body.addWidget(self.mag_plot)
        plot_card.body.addWidget(self.ak_plot)
        plot_card.body.addWidget(self.mmc_plot)

        content = QWidget()
        content_lay = QVBoxLayout(content)
        content_lay.setContentsMargins(8, 8, 8, 8)
        content_lay.setSpacing(10)
        content_lay.addWidget(title)
        content_lay.addWidget(self.record_card)
        content_lay.addWidget(self.summary_card)
        content_lay.addWidget(attitude_card)
        content_lay.addWidget(plot_card)
        content_lay.addStretch(1)

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(content)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.addWidget(scroll)

    def set_recording_session_provider(self, provider: Callable[[], Path] | None) -> None:
        self._recording_session_provider = provider

    def _make_recording_session_dir(self) -> Path:
        if self._recording_session_provider is not None:
            return Path(self._recording_session_provider())
        return StreamRecorder.make_session_dir()

    def _toggle_recording(self) -> None:
        with self._logger_lock:
            active = self._logger is not None
        if active:
            self.stop_recording()
        else:
            self.start_recording()

    def start_recording(self) -> None:
        with self._logger_lock:
            if self._logger is not None:
                return
        try:
            session_dir = self._make_recording_session_dir()
            target = Path(session_dir) / "raw_sensor_timeseries.csv"
            logger = RawSensorCsvLogger(target)
            logger.start()
        except Exception as exc:
            self._set_widget_text(self.record_state, f"Recording: error ({exc})")
            return
        with self._logger_lock:
            self._logger = logger
            self._record_started_ts = time.time()
        self._set_widget_text(self.record_btn, "Stop Raw CSV")
        self._set_widget_text(self.record_state, "Recording: active")
        self._set_widget_text(self.record_path_lbl, f"File: {target}")

    def stop_recording(self) -> None:
        with self._logger_lock:
            logger = self._logger
            self._logger = None
            self._record_started_ts = None
        if logger is not None:
            logger.stop()
        self._set_widget_text(self.record_btn, "Start Raw CSV")
        self._set_widget_text(self.record_state, "Recording: idle")

    def shutdown(self) -> None:
        self.stop_recording()

    def _reset_attitude_reference(self) -> None:
        self._reset_depth_reference()
        self._attitude_estimator.reset()
        if self._using_onboard_attitude():
            self._set_label_text("attitude_ref", "local fallback rest reset | onboard attitude active")
            return
        self._latest_attitude = None
        self.attitude_plot.samples.clear()
        self.attitude_view.clear()
        self.attitude_plot.update()
        self._refresh_attitude_status()

    def _reset_depth_reference(self) -> None:
        if self._latest_depth_m is None:
            self._set_label_text("depth_ref", "waiting for valid depth")
            return
        self._depth_zero_m = float(self._latest_depth_m)
        self.depth_plot.samples.clear()
        self.depth_plot.update()
        self._refresh_depth_reference_label()
        if self._latest_depth_msg is not None:
            self._update_depth(dict(self._latest_depth_msg), time.time())

    def _set_label_text(self, key: str, text: str) -> None:
        label = self._labels.get(key)
        if label is not None and label.text() != text:
            label.setText(text)

    @staticmethod
    def _set_widget_text(widget: QLabel | QPushButton, text: str) -> None:
        if widget.text() != text:
            widget.setText(text)

    def _using_onboard_attitude(self, now: float | None = None) -> bool:
        if self._last_onboard_attitude_recv_s is None:
            return False
        if now is None:
            now = time.time()
        return (float(now) - float(self._last_onboard_attitude_recv_s)) <= self.LOCAL_ATTITUDE_FALLBACK_STALE_S

    def record_message(self, msg: dict) -> list[dict]:
        derived: list[dict] = []
        with self._logger_lock:
            logger = self._logger
        if logger is not None:
            logger.record(dict(msg or {}))
        typ = str((msg or {}).get("type", ""))
        if typ == "attitude":
            source = str((msg or {}).get("source", ""))
            if not source.startswith("topside_"):
                self._last_onboard_attitude_recv_s = time.time()
            return derived
        if typ == "mag":
            try:
                self._attitude_estimator.update_mag(dict(msg or {}))
            except Exception:
                pass
        if typ == "imu":
            try:
                estimate = self._attitude_estimator.update(dict(msg or {}), recv_time_s=time.time())
            except Exception:
                estimate = None
            if isinstance(estimate, dict):
                if not self._using_onboard_attitude():
                    derived.append(estimate)
                    self._latest_attitude = estimate
                    if logger is not None:
                        logger.record(dict(estimate))
        return derived

    def update_from_sensor(self, msg: dict) -> None:
        msg = dict(msg or {})
        typ = str(msg.get("type", ""))
        sensor = str(msg.get("sensor", typ or "unknown"))
        now = time.time()
        self._last_seen[sensor] = now
        self._last_messages[sensor] = msg
        self._type_counts[typ] = self._type_counts.get(typ, 0) + 1
        if (now - self._rate_t0) >= 1.0:
            dt = max(1e-6, now - self._rate_t0)
            self._type_rates = {key: count / dt for key, count in self._type_counts.items() if key}
            self._type_counts.clear()
            self._rate_t0 = now
        self._refresh_rates()

        if typ == "imu":
            self._update_imu(msg, now)
        elif typ == "mag":
            self._update_mag(msg, now)
        elif typ == "attitude":
            self._update_attitude(msg)
        elif typ == "external_depth":
            self._update_depth(msg, now)
        elif typ == "env":
            self._update_env(msg)
        elif typ == "adc":
            self._update_adc(msg)
        elif typ == "power":
            self._update_power(msg)
        elif typ == "leak":
            self._update_leak(msg)

        with self._logger_lock:
            started = self._record_started_ts
        if started is not None:
            self._set_widget_text(self.record_state, f"Recording: active ({max(0.0, now - started):.1f}s)")

    def _refresh_rates(self) -> None:
        if not self._type_rates:
            return
        order = ["imu", "mag", "attitude", "external_depth", "env", "adc", "power", "leak", "heartbeat", "net"]
        parts = []
        for typ in order:
            if typ in self._type_rates:
                parts.append(f"{typ} {self._type_rates[typ]:.1f} Hz")
        for typ in sorted(self._type_rates):
            if typ not in order:
                parts.append(f"{typ} {self._type_rates[typ]:.1f} Hz")
        self._set_label_text("rates", " | ".join(parts))

    @staticmethod
    def _plot_values(vec: dict | None) -> dict[str, float]:
        x, y, z = _vec(vec)
        out = {}
        if x is not None:
            out["x"] = x
        if y is not None:
            out["y"] = y
        if z is not None:
            out["z"] = z
        norm = _vec_norm(vec)
        if norm is not None:
            out["norm"] = norm
        return out

    def _relative_depth(self, depth_m: float | None) -> float | None:
        if depth_m is None:
            return None
        if self._depth_zero_m is None:
            return float(depth_m)
        return float(depth_m) - float(self._depth_zero_m)

    def _refresh_depth_reference_label(self) -> None:
        if self._depth_zero_m is None:
            self._set_label_text("depth_ref", "raw")
        else:
            self._set_label_text("depth_ref", f"zero {_num(self._depth_zero_m, decimals=3, unit='m')}")

    def _refresh_attitude_status(self) -> None:
        status = self._attitude_estimator.status()
        target = int(status.get("calibration_target_samples") or 0)
        count = int(status.get("calibration_samples") or 0)
        if status.get("calibration_state") != "calibrated":
            self._set_label_text("attitude", f"calibrating {count}/{target}")
            self._set_label_text("attitude_ref", "-")
            self.attitude_view.clear()
            return
        ref = status.get("reference_accel")
        bias = status.get("gyro_bias")
        last = status.get("last_output") or self._latest_attitude or {}
        if last:
            text = (
                f"roll {_num(last.get('roll_deg'), decimals=2, unit='deg')} | "
                f"pitch {_num(last.get('pitch_deg'), decimals=2, unit='deg')} | "
                f"yaw {_num(last.get('yaw_deg'), decimals=2, unit='deg')}"
            )
            self._set_label_text("attitude", text)
            self.attitude_view.set_attitude(last)
        else:
            self._set_label_text("attitude", "calibrated")
        if isinstance(ref, tuple) and len(ref) == 3:
            ref_txt = f"g0 x {ref[0]:.3f} | y {ref[1]:.3f} | z {ref[2]:.3f}"
        else:
            ref_txt = "g0 -"
        if isinstance(bias, tuple) and len(bias) == 3:
            bias_txt = (
                f"bias {math.degrees(bias[0]):.2f}, "
                f"{math.degrees(bias[1]):.2f}, "
                f"{math.degrees(bias[2]):.2f} deg/s"
            )
        else:
            bias_txt = "bias -"
        self._set_label_text("attitude_ref", f"{ref_txt} | {bias_txt}")

    def _update_imu(self, msg: dict, now: float) -> None:
        ts = msg.get("ts")
        try:
            age = now - float(ts)
        except Exception:
            age = None
        self._set_label_text("imu_age", _num(age, decimals=2, unit="s"))

        accel = msg.get("accel") or {}
        gyro = msg.get("gyro") or {}
        mag = msg.get("mag") or msg.get("magnetometer")
        self._set_label_text("accel", _fmt_vec(accel, decimals=3))
        self._set_label_text("gyro", _fmt_vec(gyro, decimals=4))
        if _vec_norm(mag) is not None:
            source = str(msg.get("mag_source") or "-")
            self._set_label_text("mag", f"{source} | {_fmt_vec(mag, decimals=2, unit='uT')}")

        mag_sources = msg.get("mag_sources") or {}
        if isinstance(mag_sources, dict):
            ak = mag_sources.get("ak09915")
            mmc = mag_sources.get("mmc5983")
            if _vec_norm(ak) is not None:
                self._set_label_text("ak", _fmt_vec(ak, decimals=2, unit="uT"))
                self.ak_plot.add_sample(now, self._plot_values(ak))
            if _vec_norm(mmc) is not None:
                self._set_label_text("mmc", _fmt_vec(mmc, decimals=2, unit="uT"))
                self.mmc_plot.add_sample(now, self._plot_values(mmc))

        self.accel_plot.add_sample(now, self._plot_values(accel))
        self.gyro_plot.add_sample(now, self._plot_values(gyro))
        if _vec_norm(mag) is not None:
            self.mag_plot.add_sample(now, self._plot_values(mag))
        if not self._using_onboard_attitude(now):
            self._refresh_attitude_status()

    def _update_mag(self, msg: dict, now: float) -> None:
        ts = msg.get("ts")
        try:
            age = now - float(ts)
        except Exception:
            age = None
        self._set_label_text("mag_age", _num(age, decimals=2, unit="s"))

        mag = msg.get("mag") or msg.get("magnetometer") or {}
        source = str(msg.get("mag_source") or "-")
        if _vec_norm(mag) is not None:
            self._set_label_text("mag", f"{source} | {_fmt_vec(mag, decimals=2, unit='uT')}")
            self.mag_plot.add_sample(now, self._plot_values(mag))

        mag_sources = msg.get("mag_sources") or {}
        if isinstance(mag_sources, dict):
            ak = mag_sources.get("ak09915")
            mmc = mag_sources.get("mmc5983")
            self._set_label_text("ak", _fmt_vec(ak, decimals=2, unit="uT"))
            self._set_label_text("mmc", _fmt_vec(mmc, decimals=2, unit="uT"))
            self.ak_plot.add_sample(now, self._plot_values(ak))
            self.mmc_plot.add_sample(now, self._plot_values(mmc))

    def _update_attitude(self, msg: dict) -> None:
        self._latest_attitude = dict(msg or {})
        source = str(msg.get("source", ""))
        if not source.startswith("topside_"):
            self._last_onboard_attitude_recv_s = time.time()
        text = (
            f"roll {_num(msg.get('roll_deg'), decimals=2, unit='deg')} | "
            f"pitch {_num(msg.get('pitch_deg'), decimals=2, unit='deg')} | "
            f"yaw {_num(msg.get('yaw_deg'), decimals=2, unit='deg')}"
        )
        self._set_label_text("attitude", text)
        ref = msg.get("reference_accel") if isinstance(msg.get("reference_accel"), dict) else {}
        bias = msg.get("gyro_bias") if isinstance(msg.get("gyro_bias"), dict) else {}
        def _deg_value(key: str) -> str:
            try:
                return _num(math.degrees(float(bias.get(key))), decimals=2)
            except Exception:
                return "-"
        quality = ""
        if source:
            quality = f" | src {source}"
        if msg.get("calibration_tilt_std_deg") is not None:
            quality += f" | cal std {_num(msg.get('calibration_tilt_std_deg'), decimals=2, unit='deg')}"
        if msg.get("yaw_source"):
            quality += f" | yaw {msg.get('yaw_source')}"
        if msg.get("yaw_status"):
            quality += f" ({msg.get('yaw_status')})"
        self._set_label_text(
            "attitude_ref",
            f"g0 x {_num(ref.get('x'), decimals=3)} | y {_num(ref.get('y'), decimals=3)} | "
            f"z {_num(ref.get('z'), decimals=3)} | bias "
            f"{_deg_value('x')}, {_deg_value('y')}, {_deg_value('z')} deg/s"
            f"{quality}"
        )
        self.attitude_view.set_attitude(msg)
        self.attitude_plot.add_sample(
            float(msg.get("recv_time_s") or time.time()),
            {
                "roll": msg.get("roll_deg"),
                "pitch": msg.get("pitch_deg"),
                "yaw": msg.get("yaw_deg"),
            },
        )

    def _update_depth(self, msg: dict, _now: float) -> None:
        if msg.get("error"):
            self._set_label_text("depth", f"ERR | {msg.get('error')}")
            return
        depth_raw = _finite_float(msg.get("depth_m"))
        sensor_raw = _finite_float(msg.get("depth_sensor_m"))
        self._latest_depth_msg = dict(msg or {})
        self._latest_depth_m = depth_raw
        depth_display = self._relative_depth(depth_raw)
        sensor_display = self._relative_depth(sensor_raw)
        parts = [
            f"depth {_num(depth_display, decimals=3, unit='m')}",
            f"raw {_num(depth_raw, decimals=3, unit='m')}",
            f"sensor {_num(sensor_display, decimals=3, unit='m')}",
            f"pressure {_num(msg.get('pressure_mbar'), decimals=1, unit='mbar')}",
            f"temp {_num(msg.get('temperature_c'), decimals=2, unit='C')}",
        ]
        self._set_label_text("depth", " | ".join(parts))
        self._refresh_depth_reference_label()
        values: dict[str, float] = {}
        if depth_display is not None:
            values["depth"] = depth_display
        if sensor_display is not None:
            values["sensor"] = sensor_display
        self.depth_plot.add_sample(_now, values)

    def _update_env(self, msg: dict) -> None:
        self._set_label_text(
            "env",
            f"temp {_num(msg.get('temperature_c'), decimals=2, unit='C')} | "
            f"pressure {_num(msg.get('pressure_kpa'), decimals=2, unit='kPa')}"
        )

    def _update_adc(self, msg: dict) -> None:
        channels = msg.get("channels")
        if isinstance(channels, list):
            text = " | ".join(f"ch{i} {_num(v, decimals=3, unit='V')}" for i, v in enumerate(channels))
        else:
            text = str(channels or "-")
        self._set_label_text("adc", text)

    def _update_power(self, msg: dict) -> None:
        if msg.get("error"):
            self._set_label_text("power", f"ERR | {msg.get('error')}")
            return
        self._set_label_text(
            "power",
            f"{_num(msg.get('voltage_v'), decimals=2, unit='V')} | "
            f"{_num(msg.get('current_a'), decimals=2, unit='A')} | "
            f"{_num(msg.get('power_w'), decimals=1, unit='W')}"
        )

    def _update_leak(self, msg: dict) -> None:
        if msg.get("error"):
            self._set_label_text("leak", f"ERR | {msg.get('error')}")
        else:
            self._set_label_text("leak", "DETECTED" if msg.get("leak") else "OK")
