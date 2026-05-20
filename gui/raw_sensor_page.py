from __future__ import annotations

import math
import threading
import time
from collections import deque
from pathlib import Path
from typing import Callable, Optional

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
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
        max_update_hz: float = 30.0,
        parent=None,
    ):
        super().__init__(parent)
        self.title = str(title)
        self.series = list(series)
        self.window_s = max(2.0, float(window_s))
        self._min_update_interval_s = 1.0 / max(1.0, float(max_update_hz))
        self._last_update_s = 0.0
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
        }
        self.setMinimumHeight(190)

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
        scale = max(1.0, math.ceil(peak * 1.1))

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
            for ts, sample in visible:
                if name not in sample:
                    started = False
                    continue
                age = newest - ts
                x = plot.right() - (age / self.window_s) * plot.width()
                frac = (sample[name] + scale) / (2.0 * scale)
                y = plot.bottom() - frac * plot.height()
                if not started:
                    path.moveTo(x, y)
                    started = True
                else:
                    path.lineTo(x, y)
            painter.setPen(QPen(self.colors.get(name, QColor(220, 220, 220)), 2))
            painter.drawPath(path)

        painter.setPen(QColor(132, 140, 154))
        painter.drawText(
            QRectF(plot.left(), outer.bottom() - 18, plot.width(), 14),
            Qt.AlignmentFlag.AlignCenter,
            f"Window {self.window_s:.0f}s | scale +/-{scale:.0f}",
        )


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
        self.mag_plot = RollingVectorPlot("Primary Magnetometer", ["x", "y", "z", "norm"], window_s=20.0)
        self.ak_plot = RollingVectorPlot("AK09915 Magnetometer", ["x", "y", "z", "norm"], window_s=20.0)
        self.mmc_plot = RollingVectorPlot("MMC5983 Magnetometer", ["x", "y", "z", "norm"], window_s=20.0)

        plot_card = _Card("Rolling Raw Vectors")
        plot_card.body.addWidget(self.accel_plot)
        plot_card.body.addWidget(self.gyro_plot)
        plot_card.body.addWidget(self.attitude_plot)
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
            self.record_state.setText(f"Recording: error ({exc})")
            return
        with self._logger_lock:
            self._logger = logger
            self._record_started_ts = time.time()
        self.record_btn.setText("Stop Raw CSV")
        self.record_state.setText("Recording: active")
        self.record_path_lbl.setText(f"File: {target}")

    def stop_recording(self) -> None:
        with self._logger_lock:
            logger = self._logger
            self._logger = None
            self._record_started_ts = None
        if logger is not None:
            logger.stop()
        self.record_btn.setText("Start Raw CSV")
        self.record_state.setText("Recording: idle")

    def shutdown(self) -> None:
        self.stop_recording()

    def _reset_attitude_reference(self) -> None:
        self._attitude_estimator.reset()
        if self._using_onboard_attitude():
            self._labels["attitude_ref"].setText("local fallback rest reset | onboard attitude active")
            return
        self._latest_attitude = None
        self.attitude_plot.samples.clear()
        self.attitude_plot.update()
        self._refresh_attitude_status()

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
            self.record_state.setText(f"Recording: active ({max(0.0, now - started):.1f}s)")

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
        self._labels["rates"].setText(" | ".join(parts))

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

    def _refresh_attitude_status(self) -> None:
        status = self._attitude_estimator.status()
        target = int(status.get("calibration_target_samples") or 0)
        count = int(status.get("calibration_samples") or 0)
        if status.get("calibration_state") != "calibrated":
            self._labels["attitude"].setText(f"calibrating {count}/{target}")
            self._labels["attitude_ref"].setText("-")
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
            self._labels["attitude"].setText(text)
        else:
            self._labels["attitude"].setText("calibrated")
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
        self._labels["attitude_ref"].setText(f"{ref_txt} | {bias_txt}")

    def _update_imu(self, msg: dict, now: float) -> None:
        ts = msg.get("ts")
        try:
            age = now - float(ts)
        except Exception:
            age = None
        self._labels["imu_age"].setText(_num(age, decimals=2, unit="s"))

        accel = msg.get("accel") or {}
        gyro = msg.get("gyro") or {}
        mag = msg.get("mag") or msg.get("magnetometer") or {}
        self._labels["accel"].setText(_fmt_vec(accel, decimals=3))
        self._labels["gyro"].setText(_fmt_vec(gyro, decimals=4))
        source = str(msg.get("mag_source") or "-")
        self._labels["mag"].setText(f"{source} | {_fmt_vec(mag, decimals=2, unit='uT')}")

        mag_sources = msg.get("mag_sources") or {}
        if isinstance(mag_sources, dict):
            self._labels["ak"].setText(_fmt_vec(mag_sources.get("ak09915"), decimals=2, unit="uT"))
            self._labels["mmc"].setText(_fmt_vec(mag_sources.get("mmc5983"), decimals=2, unit="uT"))
            self.ak_plot.add_sample(now, self._plot_values(mag_sources.get("ak09915")))
            self.mmc_plot.add_sample(now, self._plot_values(mag_sources.get("mmc5983")))

        self.accel_plot.add_sample(now, self._plot_values(accel))
        self.gyro_plot.add_sample(now, self._plot_values(gyro))
        self.mag_plot.add_sample(now, self._plot_values(mag))
        if not self._using_onboard_attitude(now):
            self._refresh_attitude_status()

    def _update_mag(self, msg: dict, now: float) -> None:
        ts = msg.get("ts")
        try:
            age = now - float(ts)
        except Exception:
            age = None
        self._labels["mag_age"].setText(_num(age, decimals=2, unit="s"))

        mag = msg.get("mag") or msg.get("magnetometer") or {}
        source = str(msg.get("mag_source") or "-")
        self._labels["mag"].setText(f"{source} | {_fmt_vec(mag, decimals=2, unit='uT')}")
        self.mag_plot.add_sample(now, self._plot_values(mag))

        mag_sources = msg.get("mag_sources") or {}
        if isinstance(mag_sources, dict):
            ak = mag_sources.get("ak09915")
            mmc = mag_sources.get("mmc5983")
            self._labels["ak"].setText(_fmt_vec(ak, decimals=2, unit="uT"))
            self._labels["mmc"].setText(_fmt_vec(mmc, decimals=2, unit="uT"))
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
        self._labels["attitude"].setText(text)
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
        self._labels["attitude_ref"].setText(
            f"g0 x {_num(ref.get('x'), decimals=3)} | y {_num(ref.get('y'), decimals=3)} | "
            f"z {_num(ref.get('z'), decimals=3)} | bias "
            f"{_deg_value('x')}, {_deg_value('y')}, {_deg_value('z')} deg/s"
            f"{quality}"
        )
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
            self._labels["depth"].setText(f"ERR | {msg.get('error')}")
            return
        parts = [
            f"depth {_num(msg.get('depth_m'), decimals=3, unit='m')}",
            f"sensor {_num(msg.get('depth_sensor_m'), decimals=3, unit='m')}",
            f"pressure {_num(msg.get('pressure_mbar'), decimals=1, unit='mbar')}",
            f"temp {_num(msg.get('temperature_c'), decimals=2, unit='C')}",
        ]
        self._labels["depth"].setText(" | ".join(parts))

    def _update_env(self, msg: dict) -> None:
        self._labels["env"].setText(
            f"temp {_num(msg.get('temperature_c'), decimals=2, unit='C')} | "
            f"pressure {_num(msg.get('pressure_kpa'), decimals=2, unit='kPa')}"
        )

    def _update_adc(self, msg: dict) -> None:
        channels = msg.get("channels")
        if isinstance(channels, list):
            text = " | ".join(f"ch{i} {_num(v, decimals=3, unit='V')}" for i, v in enumerate(channels))
        else:
            text = str(channels or "-")
        self._labels["adc"].setText(text)

    def _update_power(self, msg: dict) -> None:
        if msg.get("error"):
            self._labels["power"].setText(f"ERR | {msg.get('error')}")
            return
        self._labels["power"].setText(
            f"{_num(msg.get('voltage_v'), decimals=2, unit='V')} | "
            f"{_num(msg.get('current_a'), decimals=2, unit='A')} | "
            f"{_num(msg.get('power_w'), decimals=1, unit='W')}"
        )

    def _update_leak(self, msg: dict) -> None:
        if msg.get("error"):
            self._labels["leak"].setText(f"ERR | {msg.get('error')}")
        else:
            self._labels["leak"].setText("DETECTED" if msg.get("leak") else "OK")
