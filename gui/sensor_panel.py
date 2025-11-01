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
