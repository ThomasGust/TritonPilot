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
        self.table.setWordWrap(True)
        # Avoid silent truncation: wrap in-cell and keep full text in tooltips.
        try:
            self.table.setTextElideMode(Qt.TextElideMode.ElideNone)
        except Exception:
            pass
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)

        lay = QVBoxLayout(self)
        lay.addWidget(self.title)
        lay.addWidget(self.table)

        self._rows: dict[str, int] = {}

    def upsert_sensor(self, msg: dict):
        sensor = msg.get("sensor", "unknown")
        typ = msg.get("type", "-")

        if typ == "imu":
            # Display accel + (if present) gyro + mag. Many IMUs publish all three.
            def _vec(d: dict | None):
                d = d or {}
                try:
                    x = float(d.get("x", 0.0))
                    y = float(d.get("y", 0.0))
                    z = float(d.get("z", 0.0))
                except Exception:
                    x = y = z = 0.0
                return x, y, z

            lines: list[str] = []
            if "accel" in msg:
                ax, ay, az = _vec(msg.get("accel"))
                lines.append(f"acc=({ax:.2f},{ay:.2f},{az:.2f})")
            if "gyro" in msg:
                gx, gy, gz = _vec(msg.get("gyro"))
                lines.append(f"gyro=({gx:.2f},{gy:.2f},{gz:.2f})")
            # Some stacks use 'mag', others 'magnetometer'
            if "mag" in msg or "magnetometer" in msg:
                mx, my, mz = _vec(msg.get("mag") or msg.get("magnetometer"))
                lines.append(f"mag=({mx:.2f},{my:.2f},{mz:.2f})")

            val = "\n".join(lines) if lines else str(msg)
        elif typ == "attitude":
            try:
                rpy = msg.get("rpy_deg") or {}
                r = float(rpy.get("roll", 0.0))
                pch = float(rpy.get("pitch", 0.0))
                y = float(rpy.get("yaw", 0.0))
                health = msg.get("health") or {}
                mode = health.get("mode", "-")
                mag_src = msg.get("mag_source") or "-"
                mq = health.get("mag_qual")
                mq_s = f"{float(mq):.2f}" if mq is not None else "-"
                st = int(bool(health.get("stationary", False)))
                val = f"rpy=({r:.1f},{pch:.1f},{y:.1f})\nmode={mode} mag={mag_src} qual={mq_s} stat={st}"
            except Exception:
                val = str(msg)
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
        elif typ == "power":
            # Converted from Blue Robotics Power Sense Module (PSM)
            if msg.get("error"):
                val = f"ERR: {msg.get('error')}"
            else:
                try:
                    v = float(msg.get("voltage_v", 0.0))
                    a = float(msg.get("current_a", 0.0))
                    w = float(msg.get("power_w", v * a))
                    vch = msg.get("voltage_ch")
                    ich = msg.get("current_ch")
                    ok = msg.get("ok", True)
                    held = bool(msg.get("held", False))
                    ch_s = ""
                    if vch is not None and ich is not None:
                        ch_s = f" (Vch={vch},Ich={ich})"
                    if held:
                        warn = " [HOLD]"
                    else:
                        warn = "" if ok else " [CHECK]"
                    val = f"{v:.2f} V, {a:.2f} A, {w:.1f} W{ch_s}{warn}"
                except Exception:
                    val = str(msg)
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
            try:
                item.setToolTip(item.text())
            except Exception:
                pass

        # Keep rows tall enough for wrapped text in the Value column.
        try:
            self.table.resizeRowToContents(row)
        except Exception:
            pass
