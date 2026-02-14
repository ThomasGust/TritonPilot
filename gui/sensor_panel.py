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
            p = msg.get("pressure_mbar")
            p_s = f", {float(p):.1f} mbar" if p is not None else ""
            val = f"{msg.get('depth_m', 0):.2f} m, {msg.get('temperature_c', 0):.1f} C{p_s}"
        elif typ == "heartbeat":
            armed = msg.get("armed")
            pa = msg.get("pilot_age")
            seq = msg.get("pilot_seq")
            try:
                pa_s = f"{float(pa):.2f}s" if pa is not None else "-"
            except Exception:
                pa_s = str(pa)
            val = f"armed={armed} pilot_age={pa_s} seq={seq}"
        elif typ == "net":
            iface = msg.get("iface") or "-"
            ip = msg.get("ip") or "-"
            link = msg.get("link") or {}
            kind = link.get("kind") or "-"
            state = link.get("state") or "-"
            sp = link.get("speed_mbps")
            sp_s = f"{int(sp)}Mbps" if isinstance(sp, (int, float)) and sp and sp > 0 else "-"

            def _bps_to_str(bps):
                try:
                    bps = float(bps)
                except Exception:
                    return "-"
                if bps < 0:
                    return "-"
                # show in bits/s
                b = bps * 8.0
                if b >= 1e9:
                    return f"{b/1e9:.2f}Gb/s"
                if b >= 1e6:
                    return f"{b/1e6:.2f}Mb/s"
                if b >= 1e3:
                    return f"{b/1e3:.1f}Kb/s"
                return f"{b:.0f}b/s"

            rx_s = _bps_to_str(msg.get("rx_bps"))
            tx_s = _bps_to_str(msg.get("tx_bps"))
            c = msg.get("counters") or {}
            drop = f"{c.get('rx_drop','-')}/{c.get('tx_drop','-')}"
            errs = f"{c.get('rx_errs','-')}/{c.get('tx_errs','-')}"
            tether = msg.get("is_tether")
            tether_s = "tether" if tether else "wifi/other"
            val = f"{iface} {kind} {state} {sp_s} ip={ip} rx={rx_s} tx={tx_s} drop={drop} err={errs} ({tether_s})"
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
