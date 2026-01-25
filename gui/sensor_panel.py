# gui/sensor_panel.py
from __future__ import annotations

from PyQt6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QLabel,
    QTableWidget,
    QTableWidgetItem,
    QHeaderView,
)
from PyQt6.QtCore import Qt
import math

def quaternion_to_euler(q, degrees=False):
    """
    Convert a quaternion dict {x, y, z, w} to Euler angles dict
    {roll, pitch, yaw} using the XYZ (roll-pitch-yaw) convention.

    :param q: dict with keys 'x', 'y', 'z', 'w'
    :param degrees: if True, return angles in degrees; otherwise radians
    :return: dict { 'roll': ..., 'pitch': ..., 'yaw': ... }
    """
    x = float(q["x"])
    y = float(q["y"])
    z = float(q["z"])
    w = float(q["w"])

    # (optional) normalize to be safe
    norm = math.sqrt(x*x + y*y + z*z + w*w)
    if norm == 0:
        raise ValueError("Quaternion has zero length")
    x /= norm
    y /= norm
    z /= norm
    w /= norm

    # roll (x-axis rotation)
    sinr_cosp = 2 * (w * x + y * z)
    cosr_cosp = 1 - 2 * (x * x + y * y)
    roll = math.atan2(sinr_cosp, cosr_cosp)

    # pitch (y-axis rotation)
    sinp = 2 * (w * y - z * x)
    if abs(sinp) >= 1:
        # use 90 degrees if out of range
        pitch = math.copysign(math.pi / 2, sinp)
    else:
        pitch = math.asin(sinp)

    # yaw (z-axis rotation)
    siny_cosp = 2 * (w * z + x * y)
    cosy_cosp = 1 - 2 * (y * y + z * z)
    yaw = math.atan2(siny_cosp, cosy_cosp)

    if degrees:
        roll = math.degrees(roll)
        pitch = math.degrees(pitch)
        yaw = math.degrees(yaw)

    return {"roll": float(roll), "pitch": float(pitch), "yaw": float(yaw)}

class SensorPanel(QWidget):
    """
    Very simple table:
      sensor | type | value
    """
    def __init__(self, parent=None):
        super().__init__(parent)

        self.title = QLabel("Sensors")
        self.title.setStyleSheet("font-weight: bold")

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Sensor", "Type", "Value"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)

        lay = QVBoxLayout(self)
        lay.addWidget(self.title)
        lay.addWidget(self.table)

        self._rows: dict[str, int] = {}

    def upsert_sensor(self, msg: dict):
        sensor = msg.get("sensor", "unknown")
        typ = msg.get("type", "-")

        if typ == "imu":
            ax, ay, az = msg["accel"]["x"], msg["accel"]["y"], msg["accel"]["z"]
            val = f"acc=({ax:.2f},{ay:.2f},{az:.2f})"
        elif typ == "env":
            val = f"{msg.get('temperature_c', 0):.1f} C, {msg.get('pressure_kpa', 0):.1f} kPa"
        elif typ == "leak":
            val = "LEAK!" if msg.get("leak") else "ok"
        elif typ == "adc":
            chans = msg.get("channels", [])
            val = ", ".join(f"{c:.2f}" for c in chans)
        elif typ == "external_depth":
            val = f"{msg.get('depth_m', 0):.2f} m, {msg.get('temperature_c', 0):.1f} C"
        elif typ == "att":
            quat = msg.get("quat") or {}
            try:
                eul = quaternion_to_euler(quat, degrees=True)
                val = f"roll={eul['roll']:.1f} pitch={eul['pitch']:.1f} yaw={eul['yaw']:.1f} deg"
            except Exception:
                val = f"quat={quat}"

            mag_used = msg.get("mag_used")
            if mag_used:
                val += f" | mag={mag_used}"

            # optional: show field magnitudes if present
            try:
                m1 = msg.get("mag_ak09915")
                if isinstance(m1, dict) and all(k in m1 for k in ("x","y","z")):
                    n1 = math.sqrt(float(m1["x"])**2 + float(m1["y"])**2 + float(m1["z"])**2)
                    val += f" | |ak|={n1:.1f}"
                m2 = msg.get("mag_mmc5983")
                if isinstance(m2, dict) and all(k in m2 for k in ("x","y","z")):
                    n2 = math.sqrt(float(m2["x"])**2 + float(m2["y"])**2 + float(m2["z"])**2)
                    val += f" | |mmc|={n2:.1f}"
            except Exception:
                pass
        elif typ == "heartbeat":
            armed = msg.get("armed")
            pa = msg.get("pilot_age")
            seq = msg.get("pilot_seq")
            try:
                pa_s = f"{float(pa):.2f}s" if pa is not None else "-"
            except Exception:
                pa_s = str(pa)
            val = f"armed={armed} pilot_age={pa_s} seq={seq}"
        else:
            val = str(msg)

        if sensor in self._rows:
            row = self._rows[sensor]
        else:
            row = self.table.rowCount()
            self.table.insertRow(row)
            self._rows[sensor] = row

        self.table.setItem(row, 0, QTableWidgetItem(sensor))
        self.table.setItem(row, 1, QTableWidgetItem(typ))
        self.table.setItem(row, 2, QTableWidgetItem(val))

        for col in range(3):
            item = self.table.item(row, col)
            if not item:
                continue
            align = Qt.AlignmentFlag.AlignVCenter
            if col == 0:
                align |= Qt.AlignmentFlag.AlignLeft
            elif col == 1:
                align |= Qt.AlignmentFlag.AlignCenter
            else:
                align |= Qt.AlignmentFlag.AlignLeft
            item.setTextAlignment(align)
