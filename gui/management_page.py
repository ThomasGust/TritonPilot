from __future__ import annotations

import time

from PyQt6.QtCore import QTimer, Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from config import MANAGEMENT_RPC_ENDPOINT
from network.management_rpc import ManagementRpcService


CONFIG_FIELD_SPECS = [
    {"key": "DEPTH_HOLD_KP", "label": "Depth Hold Kp", "min": -1000.0, "max": 1000.0, "step": 0.01, "decimals": 4},
    {"key": "DEPTH_HOLD_KI", "label": "Depth Hold Ki", "min": -1000.0, "max": 1000.0, "step": 0.01, "decimals": 4},
    {"key": "DEPTH_HOLD_KD", "label": "Depth Hold Kd", "min": -1000.0, "max": 1000.0, "step": 0.01, "decimals": 4},
    {
        "key": "DEPTH_HOLD_LPF_TAU_S",
        "label": "Depth Hold LPF Tau (s)",
        "min": 0.0,
        "max": 1000.0,
        "step": 0.01,
        "decimals": 4,
    },
    {
        "key": "DEPTH_HOLD_ERROR_DEADBAND_M",
        "label": "Depth Hold Deadband (m)",
        "min": 0.0,
        "max": 1000.0,
        "step": 0.01,
        "decimals": 4,
    },
    {"key": "DEPTH_HOLD_OUT_LIMIT", "label": "Depth Hold Out Limit", "min": 0.0, "max": 1000.0, "step": 0.01, "decimals": 4},
    {"key": "ATTITUDE_HOLD_KP", "label": "Attitude Hold Kp", "min": -1000.0, "max": 1000.0, "step": 0.001, "decimals": 4},
    {"key": "ATTITUDE_HOLD_KI", "label": "Attitude Hold Ki", "min": -1000.0, "max": 1000.0, "step": 0.001, "decimals": 4},
    {"key": "ATTITUDE_HOLD_KD", "label": "Attitude Hold Kd", "min": -1000.0, "max": 1000.0, "step": 0.001, "decimals": 4},
    {
        "key": "ATTITUDE_HOLD_LPF_TAU_S",
        "label": "Attitude Hold LPF Tau (s)",
        "min": 0.0,
        "max": 1000.0,
        "step": 0.01,
        "decimals": 4,
    },
    {
        "key": "ATTITUDE_HOLD_ERROR_DEADBAND_DEG",
        "label": "Attitude Hold Deadband (deg)",
        "min": 0.0,
        "max": 1000.0,
        "step": 0.1,
        "decimals": 3,
    },
    {"key": "ATTITUDE_HOLD_OUT_LIMIT", "label": "Attitude Hold Out Limit", "min": 0.0, "max": 1000.0, "step": 0.01, "decimals": 4},
    {"key": "ATTITUDE_HOLD_WALK_RATE_DPS", "label": "Attitude Hold Walk Rate (deg/s)", "min": 0.0, "max": 1000.0, "step": 0.5, "decimals": 2},
    {"key": "ATTITUDE_HOLD_TARGET_MIN_DEG", "label": "Attitude Hold Target Min (deg)", "min": -360.0, "max": 360.0, "step": 1.0, "decimals": 2},
    {"key": "ATTITUDE_HOLD_TARGET_MAX_DEG", "label": "Attitude Hold Target Max (deg)", "min": -360.0, "max": 360.0, "step": 1.0, "decimals": 2},
    {"key": "EXTERNAL_DEPTH_RATE_HZ", "label": "External Depth Rate (Hz)", "min": 0.0, "max": 1000.0, "step": 0.1, "decimals": 2},
]


class _SectionCard(QFrame):
    def __init__(self, title: str, subtitle: str = "", parent=None):
        super().__init__(parent)
        self.setObjectName("managementSectionCard")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        title_label = QLabel(title)
        title_label.setObjectName("managementSectionTitle")
        layout.addWidget(title_label)

        if subtitle:
            subtitle_label = QLabel(subtitle)
            subtitle_label.setWordWrap(True)
            subtitle_label.setObjectName("managementSectionSubtitle")
            layout.addWidget(subtitle_label)

        self.body = QVBoxLayout()
        self.body.setContentsMargins(0, 0, 0, 0)
        self.body.setSpacing(10)
        layout.addLayout(self.body)


class ManagementPage(QWidget):
    rpc_result_sig = pyqtSignal(dict)

    def __init__(self, endpoint: str = MANAGEMENT_RPC_ENDPOINT, parent=None):
        super().__init__(parent)
        self.endpoint = str(endpoint)
        self._svc = ManagementRpcService(endpoint=self.endpoint, on_result=self._on_rpc_result_from_thread)
        self._svc.start()

        self._available_commands: set[str] = set()
        self._config_keys_present: set[str] = set()
        self._pending_requests: dict[int, dict] = {}
        self._last_state: dict = {}
        self._last_refresh_ts: float = 0.0

        self._config_spins: dict[str, QDoubleSpinBox] = {}
        self._status_labels: dict[str, QLabel] = {}

        self._build_ui()
        self.rpc_result_sig.connect(self._handle_rpc_result)
        QTimer.singleShot(0, self.refresh_state)

    def shutdown(self) -> None:
        self._svc.stop()

    def refresh_state(self, *, allow_busy: bool = False, show_feedback: bool = False) -> None:
        self._queue_request(
            "get_state",
            {},
            allow_busy=allow_busy,
            request_meta={"show_refresh_feedback": bool(show_feedback)},
        )

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        outer.addWidget(scroll, 1)

        content = QWidget()
        scroll.setWidget(content)

        root = QVBoxLayout(content)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(10)

        header_card = _SectionCard(
            "Vehicle Setup",
            "Manage persistent ROV references and selected config values through the dedicated management RPC service.",
        )
        header_row = QHBoxLayout()
        header_row.setSpacing(8)

        self.connection_label = QLabel("Disconnected")
        self.connection_label.setObjectName("managementPill")
        self.connection_label.setProperty("tone", "error")

        self.endpoint_label = QLabel(self.endpoint)
        self.endpoint_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.endpoint_label.setObjectName("managementMetaValue")

        self.refresh_btn = QPushButton("Refresh State")
        self.refresh_btn.clicked.connect(lambda: self.refresh_state(show_feedback=True))

        header_row.addWidget(QLabel("RPC"))
        header_row.addWidget(self.connection_label, 0)
        header_row.addSpacing(8)
        header_row.addWidget(QLabel("Endpoint"))
        header_row.addWidget(self.endpoint_label, 1)
        header_row.addWidget(self.refresh_btn, 0)
        header_card.body.addLayout(header_row)

        self.feedback_label = QLabel("Open this page to read the current ROV state before making changes.")
        self.feedback_label.setWordWrap(True)
        self.feedback_label.setObjectName("managementFeedback")
        header_card.body.addWidget(self.feedback_label)

        self.restart_label = QLabel("Saved on ROV. TritonOS restart required to fully apply.")
        self.restart_label.setWordWrap(True)
        self.restart_label.setObjectName("managementRestartBanner")
        self.restart_label.hide()
        header_card.body.addWidget(self.restart_label)
        root.addWidget(header_card)

        status_card = _SectionCard(
            "Reference Status",
            "This is the currently loaded persistent state reported by the ROV management service.",
        )
        status_grid = QGridLayout()
        status_grid.setHorizontalSpacing(12)
        status_grid.setVerticalSpacing(8)
        status_items = [
            ("Config Path", "config_path"),
            ("Last Refresh", "last_refresh"),
            ("Depth Reference", "depth_reference"),
            ("Surface Pressure", "surface_pressure"),
            ("Sensor To Top", "sensor_to_top"),
            ("Flat Mount", "flat_mount"),
            ("Flat Mount Loaded", "flat_mount_loaded"),
            ("Available Commands", "commands"),
        ]
        for row, (label_text, key) in enumerate(status_items):
            label = QLabel(label_text)
            value = QLabel("-")
            value.setObjectName("managementMetaValue")
            value.setWordWrap(True)
            value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            status_grid.addWidget(label, row, 0)
            status_grid.addWidget(value, row, 1)
            self._status_labels[key] = value
        status_card.body.addLayout(status_grid)
        root.addWidget(status_card)

        depth_card = _SectionCard(
            "Depth Reference",
            "Capture with the top of the ROV at the water surface. Published depth is measured from the top of the vehicle, not the sensor body.",
        )
        depth_form = QFormLayout()
        depth_form.setContentsMargins(0, 0, 0, 0)
        depth_form.setSpacing(8)

        self.depth_sensor_to_top_spin = self._make_spinbox(-10.0, 10.0, 0.01, 4, suffix=" m")
        self.manual_surface_pressure_spin = self._make_spinbox(800.0, 1300.0, 0.01, 2, suffix=" mbar")
        depth_form.addRow("Sensor to Top Offset", self.depth_sensor_to_top_spin)
        depth_form.addRow("Manual Surface Pressure", self.manual_surface_pressure_spin)
        depth_card.body.addLayout(depth_form)

        depth_buttons = QHBoxLayout()
        depth_buttons.setSpacing(8)
        self.save_sensor_offset_btn = QPushButton("Save Sensor Offset")
        self.save_sensor_offset_btn.clicked.connect(self._save_sensor_offset)
        self.capture_surface_btn = QPushButton("Capture Surface Reference")
        self.capture_surface_btn.clicked.connect(self._capture_surface_reference)
        self.save_surface_btn = QPushButton("Save Manual Surface Pressure")
        self.save_surface_btn.clicked.connect(self._save_manual_surface_reference)
        depth_buttons.addWidget(self.save_sensor_offset_btn)
        depth_buttons.addWidget(self.capture_surface_btn)
        depth_buttons.addWidget(self.save_surface_btn)
        depth_card.body.addLayout(depth_buttons)
        root.addWidget(depth_card)

        flat_card = _SectionCard(
            "Flat Pose",
            "Hold the ROV in the pose that should count as flat, then capture the mount reference. Yaw usually matches ATTITUDE_AUTO_MOUNT_YAW_DEG.",
        )
        flat_form = QFormLayout()
        flat_form.setContentsMargins(0, 0, 0, 0)
        flat_form.setSpacing(8)
        self.flat_yaw_spin = self._make_spinbox(-360.0, 360.0, 1.0, 2, suffix=" deg")
        flat_form.addRow("Reference Yaw", self.flat_yaw_spin)
        flat_card.body.addLayout(flat_form)

        flat_buttons = QHBoxLayout()
        flat_buttons.setSpacing(8)
        self.capture_flat_btn = QPushButton("Capture Flat Pose")
        self.capture_flat_btn.clicked.connect(self._capture_flat_reference)
        flat_buttons.addWidget(self.capture_flat_btn)
        flat_buttons.addStretch(1)
        flat_card.body.addLayout(flat_buttons)
        root.addWidget(flat_card)

        config_card = _SectionCard(
            "Config Values",
            "Only a selected set of safe fields is exposed here. Values are saved immediately on the ROV and typically require a TritonOS restart to fully apply.",
        )
        config_form = QFormLayout()
        config_form.setContentsMargins(0, 0, 0, 0)
        config_form.setSpacing(8)
        for spec in CONFIG_FIELD_SPECS:
            spin = self._make_spinbox(spec["min"], spec["max"], spec["step"], spec["decimals"])
            spin.setEnabled(False)
            config_form.addRow(spec["label"], spin)
            self._config_spins[str(spec["key"])] = spin
        config_card.body.addLayout(config_form)

        config_buttons = QHBoxLayout()
        config_buttons.setSpacing(8)
        self.save_config_btn = QPushButton("Save Config Values")
        self.save_config_btn.clicked.connect(self._save_config_values)
        config_buttons.addWidget(self.save_config_btn)
        config_buttons.addStretch(1)
        config_card.body.addLayout(config_buttons)
        root.addWidget(config_card)

        root.addStretch(1)
        self._sync_action_state()

    @staticmethod
    def _make_spinbox(minimum: float, maximum: float, step: float, decimals: int, *, suffix: str = "") -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setRange(float(minimum), float(maximum))
        spin.setDecimals(int(decimals))
        spin.setSingleStep(float(step))
        spin.setKeyboardTracking(False)
        spin.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        if suffix:
            spin.setSuffix(suffix)
        return spin

    def _on_rpc_result_from_thread(self, result: dict) -> None:
        self.rpc_result_sig.emit(result)

    def _queue_request(self, cmd: str, args: dict, *, allow_busy: bool = False, request_meta: dict | None = None) -> None:
        if self._pending_requests and not allow_busy:
            return
        request_id = self._svc.request(cmd, args)
        self._pending_requests[request_id] = dict(request_meta or {})
        self._sync_action_state()

    def _handle_rpc_result(self, result: dict) -> None:
        request_id = int(result.get("request_id", 0) or 0)
        request_meta = self._pending_requests.pop(request_id, {})
        cmd = str(result.get("cmd") or "")

        if result.get("ok"):
            self._set_connection(True)
            if cmd == "get_state":
                self._apply_state(dict(result.get("data") or {}))
                if request_meta.get("show_refresh_feedback"):
                    self._set_feedback("Management state refreshed from ROV.", tone="info")
            else:
                self._handle_mutation_success(cmd, dict(result.get("data") or {}))
                if request_meta.get("refresh_after_success"):
                    self.refresh_state(allow_busy=True)
        else:
            self._set_connection(False)
            err = str(result.get("error") or "unknown error")
            if cmd == "get_state":
                self._set_feedback(f"Could not refresh management state: {err}", tone="error")
            else:
                self._set_feedback(f"{self._command_label(cmd)} failed: {err}", tone="error")

        self._sync_action_state()

    def _apply_state(self, state: dict) -> None:
        self._last_state = dict(state or {})
        self._last_refresh_ts = time.time()
        config = dict(self._last_state.get("config") or {})
        refs = dict(self._last_state.get("references") or {})
        self._config_keys_present = {str(key) for key in config.keys()}

        self._available_commands = {str(cmd) for cmd in (self._last_state.get("commands") or [])}

        self._status_labels["config_path"].setText(str(self._last_state.get("config_path") or "-"))
        self._status_labels["last_refresh"].setText(time.strftime("%H:%M:%S", time.localtime(self._last_refresh_ts)))
        self._status_labels["depth_reference"].setText(
            self._format_path_state(
                bool(refs.get("depth_reference_exists")),
                refs.get("depth_reference_path"),
            )
        )
        self._status_labels["surface_pressure"].setText(self._fmt_num(refs.get("surface_pressure_mbar"), "mbar", decimals=2))
        self._status_labels["sensor_to_top"].setText(self._fmt_num(refs.get("depth_sensor_to_top_m"), "m", decimals=4))
        self._status_labels["flat_mount"].setText(
            self._format_path_state(
                bool(refs.get("mount_exists")),
                refs.get("mount_path"),
            )
        )
        self._status_labels["flat_mount_loaded"].setText("yes" if refs.get("mount_loaded") else "no")
        commands_text = ", ".join(sorted(self._available_commands)) if self._available_commands else "-"
        self._status_labels["commands"].setText(commands_text)

        sensor_offset_present = "EXTERNAL_DEPTH_SENSOR_TO_TOP_M" in self._config_keys_present
        self.depth_sensor_to_top_spin.setToolTip(
            "" if sensor_offset_present else "EXTERNAL_DEPTH_SENSOR_TO_TOP_M is not present in rov_config.py on the ROV."
        )
        self.save_sensor_offset_btn.setToolTip(
            "" if sensor_offset_present else "This ROV config does not expose EXTERNAL_DEPTH_SENSOR_TO_TOP_M for persistent updates."
        )

        self._set_spin_value(
            self.depth_sensor_to_top_spin,
            config.get("EXTERNAL_DEPTH_SENSOR_TO_TOP_M", refs.get("depth_sensor_to_top_m", 0.0)),
        )
        if refs.get("surface_pressure_mbar") is not None:
            self._set_spin_value(self.manual_surface_pressure_spin, refs.get("surface_pressure_mbar"))
        if config.get("ATTITUDE_AUTO_MOUNT_YAW_DEG") is not None:
            self._set_spin_value(self.flat_yaw_spin, config.get("ATTITUDE_AUTO_MOUNT_YAW_DEG"))

        for spec in CONFIG_FIELD_SPECS:
            key = str(spec["key"])
            spin = self._config_spins[key]
            present = key in config
            spin.setEnabled(bool(present))
            spin.setToolTip("" if present else f"{key} is not present in rov_config.py on the ROV.")
            if present:
                self._set_spin_value(spin, config.get(key))

        self._sync_action_state()

    def _handle_mutation_success(self, cmd: str, data: dict) -> None:
        if data.get("restart_required"):
            self.restart_label.show()

        if cmd == "set_config":
            updated = dict(data.get("updated") or {})
            if not updated:
                msg = "ROV accepted the config save request, but it did not report any updated values."
            elif len(updated) == 1:
                key, value = next(iter(updated.items()))
                msg = f"Saved {key}={self._fmt_num(value, decimals=4)} to rov_config.py."
            else:
                keys = ", ".join(sorted(updated.keys()))
                msg = f"Saved {len(updated)} config values to rov_config.py: {keys}."
        elif cmd == "set_surface_reference":
            msg = (
                f"Saved surface pressure {self._fmt_num(data.get('surface_pressure_mbar'), 'mbar', decimals=2)} "
                f"to {str(data.get('path') or 'the depth reference file')}."
            )
        elif cmd == "capture_surface_reference":
            msg = (
                f"Captured surface pressure {self._fmt_num(data.get('surface_pressure_mbar'), 'mbar', decimals=2)} "
                f"and saved it to {str(data.get('path') or 'the depth reference file')}."
            )
        elif cmd == "capture_flat_reference":
            msg = (
                f"Captured flat pose with yaw {self._fmt_num(data.get('yaw_deg'), 'deg', decimals=2)} "
                f"and saved it to {str(data.get('path') or 'the mount reference file')}."
            )
        else:
            msg = f"{self._command_label(cmd)} succeeded."

        if data.get("restart_required"):
            msg += " TritonOS restart required to fully apply."
        self._set_feedback(msg, tone="ok")

    def _sync_action_state(self) -> None:
        busy = bool(self._pending_requests)
        has_state = bool(self._last_state)
        can_set_config = "set_config" in self._available_commands
        sensor_offset_present = "EXTERNAL_DEPTH_SENSOR_TO_TOP_M" in self._config_keys_present

        self.refresh_btn.setEnabled(not busy)
        self.save_sensor_offset_btn.setEnabled((not busy) and can_set_config and sensor_offset_present)
        self.save_surface_btn.setEnabled((not busy) and ("set_surface_reference" in self._available_commands))
        self.capture_surface_btn.setEnabled((not busy) and ("capture_surface_reference" in self._available_commands))
        self.capture_flat_btn.setEnabled((not busy) and ("capture_flat_reference" in self._available_commands))

        has_enabled_config_field = any(spin.isEnabled() for spin in self._config_spins.values())
        self.save_config_btn.setEnabled((not busy) and can_set_config and has_enabled_config_field)

        if not has_state:
            self.save_sensor_offset_btn.setEnabled(False)
            self.save_surface_btn.setEnabled(False)
            self.capture_surface_btn.setEnabled(False)
            self.capture_flat_btn.setEnabled(False)
            self.save_config_btn.setEnabled(False)

    def _save_sensor_offset(self) -> None:
        self._queue_request(
            "set_config",
            {"updates": {"EXTERNAL_DEPTH_SENSOR_TO_TOP_M": float(self.depth_sensor_to_top_spin.value())}},
            request_meta={"refresh_after_success": True},
        )

    def _save_manual_surface_reference(self) -> None:
        self._queue_request(
            "set_surface_reference",
            {"surface_pressure_mbar": float(self.manual_surface_pressure_spin.value())},
            request_meta={"refresh_after_success": True},
        )

    def _capture_surface_reference(self) -> None:
        answer = QMessageBox.question(
            self,
            "Capture Surface Reference",
            "Capture surface pressure from the ROV's depth sensor now?\n\nMake sure the top of the ROV is at the water surface.",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._queue_request(
            "capture_surface_reference",
            {"samples": 20, "delay_s": 0.02},
            request_meta={"refresh_after_success": True},
        )

    def _capture_flat_reference(self) -> None:
        answer = QMessageBox.question(
            self,
            "Capture Flat Pose",
            "Capture the current vehicle pose as the flat mount reference now?\n\nHold the ROV in the pose that should count as flat.",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._queue_request(
            "capture_flat_reference",
            {"samples": 200, "delay_s": 0.02, "yaw_deg": float(self.flat_yaw_spin.value())},
            request_meta={"refresh_after_success": True},
        )

    def _save_config_values(self) -> None:
        updates = {}
        for spec in CONFIG_FIELD_SPECS:
            key = str(spec["key"])
            spin = self._config_spins[key]
            if spin.isEnabled():
                updates[key] = float(spin.value())

        if "EXTERNAL_DEPTH_SENSOR_TO_TOP_M" in self._config_keys_present:
            updates["EXTERNAL_DEPTH_SENSOR_TO_TOP_M"] = float(self.depth_sensor_to_top_spin.value())

        self._queue_request(
            "set_config",
            {"updates": updates},
            request_meta={"refresh_after_success": True},
        )

    def _set_connection(self, connected: bool) -> None:
        self.connection_label.setText("Connected" if connected else "Disconnected")
        self.connection_label.setProperty("tone", "ok" if connected else "error")
        self.style().unpolish(self.connection_label)
        self.style().polish(self.connection_label)
        self.connection_label.update()

    def _set_feedback(self, text: str, *, tone: str = "info") -> None:
        self.feedback_label.setText(str(text))
        self.feedback_label.setProperty("tone", str(tone))
        self.style().unpolish(self.feedback_label)
        self.style().polish(self.feedback_label)
        self.feedback_label.update()

    @staticmethod
    def _set_spin_value(spin: QDoubleSpinBox, value) -> None:
        try:
            numeric = float(value)
        except Exception:
            return
        prev = spin.blockSignals(True)
        try:
            spin.setValue(numeric)
        finally:
            spin.blockSignals(prev)

    @staticmethod
    def _format_path_state(exists: bool, path) -> str:
        if path:
            return f"{'yes' if exists else 'no'} | {path}"
        return "yes" if exists else "no"

    @staticmethod
    def _fmt_bool(value) -> str:
        if value is None:
            return "-"
        return "yes" if bool(value) else "no"

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

    def _format_attitude_sensor(self, sensor: dict) -> str:
        parts: list[str] = []
        for label, key in (("pitch", "pitch_deg"), ("roll", "roll_deg"), ("yaw", "yaw_deg")):
            text = self._fmt_num(sensor.get(key), "deg", decimals=1)
            if text != "-":
                parts.append(f"{label} {text}")
        sample_age = sensor.get("sample_age_s")
        if sample_age is not None:
            parts.append(f"sample age {self._fmt_num(sample_age, 's', decimals=2)}")
        return " | ".join(parts) if parts else "-"

    def _format_attitude_debug(self, status: dict) -> str:
        pitch = dict(status.get("pitch") or {})
        roll = dict(status.get("roll") or {})
        parts: list[str] = []
        for axis_name, axis_state in (("pitch", pitch), ("roll", roll)):
            axis_parts: list[str] = []
            for label, key, unit, decimals in (
                ("angle", "angle_f_deg", "deg", 1),
                ("target", "target_deg", "deg", 1),
                ("error", "error_deg", "deg", 1),
                ("out", "u_out", "", 3),
            ):
                text = self._fmt_num(axis_state.get(key), unit, decimals=decimals)
                if text != "-":
                    axis_parts.append(f"{label} {text}")
            if axis_parts:
                parts.append(f"{axis_name}: " + ", ".join(axis_parts))
        return " | ".join(parts) if parts else "-"

    def _format_attitude_targets(self, hold_state: dict) -> str:
        pitch_text = self._fmt_num(hold_state.get("target_pitch_deg"), "deg", decimals=1)
        roll_text = self._fmt_num(hold_state.get("target_roll_deg"), "deg", decimals=1)
        parts: list[str] = []
        if pitch_text != "-":
            parts.append(f"p {pitch_text}")
        if roll_text != "-":
            parts.append(f"r {roll_text}")
        return " | ".join(parts)

    @staticmethod
    def _command_label(cmd: str) -> str:
        labels = {
            "get_state": "Refresh State",
            "get_hold_status": "Refresh Hold Status",
            "set_config": "Save Config",
            "set_surface_reference": "Save Manual Surface Pressure",
            "capture_surface_reference": "Capture Surface Reference",
            "capture_flat_reference": "Capture Flat Pose",
        }
        return labels.get(str(cmd), str(cmd) or "Request")

    @staticmethod
    def _fmt_num(value, unit: str = "", *, decimals: int = 3) -> str:
        try:
            text = f"{float(value):.{int(decimals)}f}"
        except Exception:
            return "-"
        if unit:
            return f"{text} {unit}"
        return text
