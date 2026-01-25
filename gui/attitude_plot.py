# gui/attitude_plot.py
from __future__ import annotations

import time
import math
from collections import deque
from typing import Deque, Optional, Tuple

from PyQt6.QtCore import Qt, QTimer, QRectF
from PyQt6.QtGui import QPainter, QPen, QFont
from PyQt6.QtWidgets import QWidget

from gui.sensor_panel import quaternion_to_euler


class AttitudePlotWidget(QWidget):
    """
    Lightweight live plot (no external deps): roll/pitch/yaw vs time.

    Intended for EKF tuning:
      - watch for jitter/oscillation while stationary
      - correlate with accel_norm/gyro_norm and EKF innovations if published
    """

    def __init__(self, parent=None, window_s: float = 20.0, refresh_hz: float = 20.0):
        super().__init__(parent)
        self.window_s = float(window_s)
        self._t: Deque[float] = deque()
        self._r: Deque[float] = deque()
        self._p: Deque[float] = deque()
        self._y: Deque[float] = deque()

        # optional debug streams
        self._accel_norm: Deque[float] = deque()
        self._gyro_norm: Deque[float] = deque()
        self._acc_innov: Deque[float] = deque()
        self._mag_innov: Deque[float] = deque()

        self._last_vals: Tuple[float, float, float] = (0.0, 0.0, 0.0)

        self.setMinimumHeight(220)
        self.setAutoFillBackground(True)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self.update)
        self._timer.start(int(1000.0 / max(1.0, refresh_hz)))

    def ingest(self, msg: dict) -> None:
        if not isinstance(msg, dict):
            return
        if msg.get("type") != "att":
            return
        quat = msg.get("quat") or {}
        if not isinstance(quat, dict):
            return

        try:
            e = quaternion_to_euler(quat, degrees=True)
            roll = float(e["roll"])
            pitch = float(e["pitch"])
            yaw = float(e["yaw"])
        except Exception:
            return

        ts = msg.get("ts")
        try:
            t = float(ts) if ts is not None else time.time()
        except Exception:
            t = time.time()

        self._t.append(t)
        self._r.append(roll)
        self._p.append(pitch)
        self._y.append(yaw)
        self._last_vals = (roll, pitch, yaw)

        # optional extra telemetry
        try:
            self._accel_norm.append(float(msg.get("accel_norm", float("nan"))))
            self._gyro_norm.append(float(msg.get("gyro_norm", float("nan"))))
        except Exception:
            pass

        dbg = msg.get("att_debug") or {}
        if isinstance(dbg, dict):
            try:
                self._acc_innov.append(float(dbg.get("accel_innov_norm", float("nan"))))
                self._mag_innov.append(float(dbg.get("mag_innov_norm", float("nan"))))
            except Exception:
                pass

        # prune old
        cutoff = t - self.window_s
        while self._t and self._t[0] < cutoff:
            self._t.popleft(); self._r.popleft(); self._p.popleft(); self._y.popleft()
            if self._accel_norm: self._accel_norm.popleft()
            if self._gyro_norm: self._gyro_norm.popleft()
            if self._acc_innov: self._acc_innov.popleft()
            if self._mag_innov: self._mag_innov.popleft()

    def _series_bounds(self) -> Tuple[float, float]:
        if not self._r:
            return (-10.0, 10.0)
        vals = list(self._r) + list(self._p) + list(self._y)
        vmin = min(vals); vmax = max(vals)
        if not math.isfinite(vmin) or not math.isfinite(vmax):
            return (-10.0, 10.0)
        if vmax - vmin < 5.0:
            mid = 0.5 * (vmax + vmin)
            return (mid - 5.0, mid + 5.0)
        pad = 0.10 * (vmax - vmin)
        return (vmin - pad, vmax + pad)

    def _draw_series(self, p: QPainter, rect: QRectF, xs: list[float], ys: list[float], pen: QPen) -> None:
        if len(xs) < 2:
            return
        p.setPen(pen)
        # polyline
        for i in range(1, len(xs)):
            p.drawLine(int(xs[i-1]), int(ys[i-1]), int(xs[i]), int(ys[i]))

    def paintEvent(self, _ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        w = self.width()
        h = self.height()

        # background
        p.fillRect(0, 0, w, h, self.palette().window())

        # plot area
        margin = 10
        top = 22
        rect = QRectF(margin, top, w - 2 * margin, h - top - margin)

        # axes box
        p.setPen(QPen(Qt.GlobalColor.gray, 1))
        p.drawRect(rect)

        if len(self._t) < 2:
            p.setPen(QPen(Qt.GlobalColor.darkGray))
            p.drawText(int(rect.left()) + 6, int(rect.top()) + 18, "Attitude plot (waiting for data...)")
            return

        t0 = self._t[0]
        t1 = self._t[-1]
        if t1 <= t0:
            return

        ymin, ymax = self._series_bounds()

        # map time/value to pixels
        def xpix(t: float) -> float:
            return rect.left() + (t - t0) / (t1 - t0) * rect.width()

        def ypix(v: float) -> float:
            # invert y
            return rect.bottom() - (v - ymin) / (ymax - ymin) * rect.height()

        xs = [xpix(t) for t in self._t]
        yr = [ypix(v) for v in self._r]
        yp = [ypix(v) for v in self._p]
        yy = [ypix(v) for v in self._y]

        # draw grid lines
        p.setPen(QPen(Qt.GlobalColor.lightGray, 1))
        for frac in (0.25, 0.5, 0.75):
            y = rect.top() + frac * rect.height()
            p.drawLine(int(rect.left()), int(y), int(rect.right()), int(y))

        # series pens
        pen_r = QPen(Qt.GlobalColor.red, 2)
        pen_p = QPen(Qt.GlobalColor.green, 2)
        pen_y = QPen(Qt.GlobalColor.blue, 2)

        self._draw_series(p, rect, xs, yr, pen_r)
        self._draw_series(p, rect, xs, yp, pen_p)
        self._draw_series(p, rect, xs, yy, pen_y)

        # labels
        p.setPen(QPen(Qt.GlobalColor.black))
        p.setFont(QFont("Sans", 9))
        roll, pitch, yaw = self._last_vals
        p.drawText(margin, 16, f"Roll/Pitch/Yaw (deg): {roll:+6.1f}  {pitch:+6.1f}  {yaw:+6.1f}    range=({ymin:+.1f},{ymax:+.1f})")

        # quick stability stats (std-dev over last ~5s)
        try:
            now = self._t[-1]
            cutoff = now - 5.0
            idx = 0
            for i, tt in enumerate(self._t):
                if tt >= cutoff:
                    idx = i
                    break
            rwin = list(self._r)[idx:]
            pwin = list(self._p)[idx:]
            ywin = list(self._y)[idx:]
            def std(vs):
                if len(vs) < 2:
                    return float("nan")
                m = sum(vs)/len(vs)
                return math.sqrt(sum((x-m)**2 for x in vs)/ (len(vs)-1))
            sr, sp, sy = std(rwin), std(pwin), std(ywin)
            p.drawText(margin, h - 6, f"~5s std: roll={sr:.2f}  pitch={sp:.2f}  yaw={sy:.2f}")
        except Exception:
            pass
