"""Vehicle setup page backed by the TritonOS management RPC service."""

from __future__ import annotations

import time

from PyQt6.QtCore import QTimer, Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox,
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
    {"key": "DEPTH_HOLD_I_LIMIT", "label": "Depth Hold I Limit", "min": 0.0, "max": 1000.0, "step": 0.01, "decimals": 4},
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
    {
        "key": "DEPTH_HOLD_WALK_DEADBAND",
        "label": "Depth Hold Walk Deadband",
        "min": 0.0,
        "max": 1.0,
        "step": 0.01,
        "decimals": 4,
    },
    {
        "key": "DEPTH_HOLD_WALK_RATE_MPS",
        "label": "Depth Hold Walk Rate (m/s)",
        "min": 0.0,
        "max": 10.0,
        "step": 0.05,
        "decimals": 4,
    },
    {"key": "EXTERNAL_DEPTH_RATE_HZ", "label": "External Depth Rate (Hz)", "min": 0.0, "max": 1000.0, "step": 0.1, "decimals": 2},
    {"key": "AUTOPILOT_ROLL_KP", "label": "Roll Level Kp", "min": -10.0, "max": 10.0, "step": 0.001, "decimals": 5},
    {"key": "AUTOPILOT_ROLL_KD", "label": "Roll Level Kd", "min": -10.0, "max": 10.0, "step": 0.001, "decimals": 5},
    {"key": "AUTOPILOT_ROLL_OUT_LIMIT", "label": "Roll Output Limit", "min": 0.0, "max": 1.0, "step": 0.01, "decimals": 4},
    {"key": "AUTOPILOT_ROLL_SIGN", "label": "Roll Sign", "min": -1.0, "max": 1.0, "step": 2.0, "decimals": 0},
    {"key": "AUTOPILOT_PITCH_KP", "label": "Pitch Level Kp", "min": -10.0, "max": 10.0, "step": 0.001, "decimals": 5},
    {"key": "AUTOPILOT_PITCH_KD", "label": "Pitch Level Kd", "min": -10.0, "max": 10.0, "step": 0.001, "decimals": 5},
    {"key": "AUTOPILOT_PITCH_OUT_LIMIT", "label": "Pitch Output Limit", "min": 0.0, "max": 1.0, "step": 0.01, "decimals": 4},
    {"key": "AUTOPILOT_PITCH_SIGN", "label": "Pitch Sign", "min": -1.0, "max": 1.0, "step": 2.0, "decimals": 0},
    {"key": "AUTOPILOT_YAW_KP", "label": "Yaw Hold Kp", "min": -10.0, "max": 10.0, "step": 0.001, "decimals": 5},
    {"key": "AUTOPILOT_YAW_KD", "label": "Yaw Hold Kd", "min": -10.0, "max": 10.0, "step": 0.001, "decimals": 5},
    {"key": "AUTOPILOT_YAW_OUT_LIMIT", "label": "Yaw Output Limit", "min": 0.0, "max": 1.0, "step": 0.01, "decimals": 4},
    {"key": "AUTOPILOT_YAW_SIGN", "label": "Yaw Sign", "min": -1.0, "max": 1.0, "step": 2.0, "decimals": 0},
    {"key": "AUTOPILOT_MIX_DEADBAND", "label": "Autopilot Mix Deadband", "min": 0.0, "max": 1.0, "step": 0.01, "decimals": 4},
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
    """Operator-facing controls for live ROV configuration and calibration."""

    rpc_result_sig = pyqtSignal(dict)

    def __init__(self, endpoint: str = MANAGEMENT_RPC_ENDPOINT, parent=None, *, pilot_svc=None):
        super().__init__(parent)
        self.endpoint = str(endpoint)
        # Topside pilot publisher: used to stream LIVE arm-tuning overrides (the
        # persistent save still goes through the management RPC).
        self._pilot_svc = pilot_svc
        self._svc = ManagementRpcService(
            endpoint=self.endpoint,
            on_result=self._on_rpc_result_from_thread,
            timeout_ms=180_000,
        )
        self._svc.start()

        self._available_commands: set[str] = set()
        self._config_keys_present: set[str] = set()
        self._pending_requests: dict[int, dict] = {}
        self._last_state: dict = {}
        self._last_refresh_ts: float = 0.0

        self._config_spins: dict[str, QDoubleSpinBox] = {}
        self._status_labels: dict[str, QLabel] = {}
        self._arm_tune_checks: dict[str, QCheckBox] = {}
        self._arm_tune_spins: dict[str, QDoubleSpinBox] = {}
        self._arm_park_spins: dict[str, QDoubleSpinBox] = {}
        self._arm_alignment_buttons: dict[str, QPushButton] = {}
        self._arm_tune_config_defaults = {
            "left_invert": 1.0,
            "right_invert": -1.0,
            "pitch_invert": 1.0,
            "yaw_invert": 1.0,
            "servo_range_deg": 100.0,
            "pitch_span_deg": 90.0,
            "pitch_neutral_deg": 45.0,
            "wrist_span_deg": 90.0,
            "wrist_neutral_deg": 45.0,
            "servo_center_us": 1500.0,
            "servo_pulse_halfspan_us": 800.0,
            "park_pitch": -1.0,
            "park_wrist": 1.0,
        }

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
            ("Attitude Reference", "attitude_reference"),
            ("Surface Pressure", "surface_pressure"),
            ("Sensor To Top", "sensor_to_top"),
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
        self.capture_local_rest_btn = QPushButton("Capture Local Rest")
        self.capture_local_rest_btn.clicked.connect(self._capture_local_rest)
        self.save_surface_btn = QPushButton("Save Manual Surface Pressure")
        self.save_surface_btn.clicked.connect(self._save_manual_surface_reference)
        depth_buttons.addWidget(self.save_sensor_offset_btn)
        depth_buttons.addWidget(self.capture_surface_btn)
        depth_buttons.addWidget(self.capture_local_rest_btn)
        depth_buttons.addWidget(self.save_surface_btn)
        depth_card.body.addLayout(depth_buttons)
        root.addWidget(depth_card)

        service_card = _SectionCard(
            "ROV Code",
            "Update the Pi checkout from GitHub and restart TritonOS without opening an SSH session.",
        )
        service_buttons = QHBoxLayout()
        service_buttons.setSpacing(8)
        self.update_code_btn = QPushButton("Force Update From GitHub")
        self.update_code_btn.clicked.connect(lambda: self._confirm_update_code(restart=False))
        self.update_restart_btn = QPushButton("Update + Restart")
        self.update_restart_btn.clicked.connect(lambda: self._confirm_update_code(restart=True))
        self.restart_service_btn = QPushButton("Restart TritonOS")
        self.restart_service_btn.clicked.connect(self._confirm_restart_service)
        service_buttons.addWidget(self.update_code_btn)
        service_buttons.addWidget(self.update_restart_btn)
        service_buttons.addWidget(self.restart_service_btn)
        service_card.body.addLayout(service_buttons)
        root.addWidget(service_card)

        root.addWidget(self._build_arm_tuning_card())

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

    def _build_arm_tuning_card(self) -> "_SectionCard":
        card = _SectionCard(
            "Arm Tuning (Live)",
            "Differential-wrist inverts and neutral, streamed to the ROV instantly over "
            "the control link -- no restart. Use these to un-swap pitch/roll and fix "
            "direction. 'Reset' clears the live overrides; 'Save to ROV config' writes "
            "them to rov_config.py (TritonOS restart required to persist).",
        )
        check_specs = [
            ("right_invert", "Invert RIGHT servo (un-swap pitch / roll)"),
            ("left_invert", "Invert LEFT servo"),
            ("pitch_invert", "Invert pitch direction"),
            ("yaw_invert", "Invert wrist direction"),
        ]
        for key, label in check_specs:
            cb = QCheckBox(label)
            cb.toggled.connect(lambda checked, k=key: self._on_arm_tune_invert(k, checked))
            card.body.addWidget(cb)
            self._arm_tune_checks[key] = cb

        form = QFormLayout()
        form.setContentsMargins(0, 6, 0, 0)
        form.setSpacing(8)
        spin_specs = [
            ("servo_range_deg", "Servo range", 30.0, 120.0, 100.0),
            ("pitch_span_deg", "Pitch span", 30.0, 140.0, 90.0),
            ("pitch_neutral_deg", "Pitch neutral", 0.0, 140.0, 45.0),
        ]
        for key, label, minimum, maximum, default in spin_specs:
            spin = self._make_spinbox(minimum, maximum, 1.0, 1, suffix=" deg")
            self._set_spin_value(spin, default)
            spin.valueChanged.connect(lambda v, k=key: self._on_arm_tune_value(k, v))
            self._arm_tune_spins[key] = spin
            if key == "servo_range_deg":
                self._arm_tune_servo_range_spin = spin
            if key == "pitch_neutral_deg":
                self._arm_tune_neutral_spin = spin
            if key == "pitch_span_deg":
                self._arm_tune_pitch_span_spin = spin
            form.addRow(label, spin)
        card.body.addLayout(form)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        self.arm_tune_reset_btn = QPushButton("Reset (use config)")
        self.arm_tune_reset_btn.clicked.connect(self._reset_arm_tune)
        self.arm_tune_save_btn = QPushButton("Save to ROV config")
        self.arm_tune_save_btn.clicked.connect(self._save_arm_tune_config)
        buttons.addWidget(self.arm_tune_reset_btn)
        buttons.addWidget(self.arm_tune_save_btn)
        buttons.addStretch(1)
        card.body.addLayout(buttons)

        align_buttons = QHBoxLayout()
        align_buttons.setSpacing(8)
        for pose_key, label in (
            ("center", "Servo Center"),
            ("flat_wrist_90", "Flat / Wrist 90"),
            ("flat_wrist_0", "Flat / Wrist 0"),
        ):
            btn = QPushButton(label)
            btn.clicked.connect(lambda _checked=False, k=pose_key: self._confirm_arm_alignment_pose(k))
            align_buttons.addWidget(btn)
            self._arm_alignment_buttons[pose_key] = btn
        align_buttons.addStretch(1)
        card.body.addLayout(align_buttons)

        park_form = QFormLayout()
        park_form.setContentsMargins(0, 8, 0, 0)
        park_form.setSpacing(8)
        for key, label, default in (
            ("park_pitch", "Park pitch", -1.0),
            ("park_wrist", "Park wrist", 1.0),
        ):
            spin = self._make_spinbox(-1.0, 1.0, 0.05, 2)
            self._set_spin_value(spin, default)
            spin.valueChanged.connect(lambda _v, k=key: self._on_arm_park_value(k))
            self._arm_park_spins[key] = spin
            park_form.addRow(label, spin)
        card.body.addLayout(park_form)

        park_buttons = QHBoxLayout()
        park_buttons.setSpacing(8)
        self.arm_park_command_btn = QPushButton("Command Park Pose")
        self.arm_park_command_btn.clicked.connect(self._confirm_arm_park_pose)
        self.arm_park_save_btn = QPushButton("Save Park Pose")
        self.arm_park_save_btn.clicked.connect(self._save_arm_park_config)
        park_buttons.addWidget(self.arm_park_command_btn)
        park_buttons.addWidget(self.arm_park_save_btn)
        park_buttons.addStretch(1)
        card.body.addLayout(park_buttons)

        self.arm_alignment_status_label = QLabel("-")
        self.arm_alignment_status_label.setWordWrap(True)
        self.arm_alignment_status_label.setObjectName("managementMetaValue")
        self.arm_alignment_status_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        card.body.addWidget(self.arm_alignment_status_label)

        if self._pilot_svc is None:
            for cb in self._arm_tune_checks.values():
                cb.setEnabled(False)
            for spin in self._arm_tune_spins.values():
                spin.setEnabled(False)
            for spin in self._arm_park_spins.values():
                spin.setEnabled(False)
            self.arm_tune_reset_btn.setEnabled(False)
            self.arm_park_command_btn.setEnabled(False)
            for btn in self._arm_alignment_buttons.values():
                btn.setEnabled(False)
        return card

    def _on_arm_tune_invert(self, key: str, checked: bool) -> None:
        if self._pilot_svc is None:
            return
        try:
            self._pilot_svc.set_arm_tune(key, -1.0 if checked else 1.0)
        except Exception:
            pass

    def _on_arm_tune_value(self, key: str, value) -> None:
        if self._pilot_svc is None:
            return
        try:
            self._pilot_svc.set_arm_tune(key, float(value))
        except Exception:
            pass

    def _on_arm_park_value(self, _key: str) -> None:
        self._sync_pilot_arm_park_pose()

    def _reset_arm_tune(self) -> None:
        if self._pilot_svc is not None:
            try:
                self._pilot_svc.clear_arm_tune()
            except Exception:
                pass
        self._apply_arm_tune_config(dict((self._last_state or {}).get("config") or {}))
        self._set_feedback("Arm tuning overrides cleared; controls restored to ROV config values.", tone="info")

    def _save_arm_tune_config(self) -> None:
        updates = {
            "GRIPPER_LEFT_INVERT": -1.0 if self._arm_tune_checks["left_invert"].isChecked() else 1.0,
            "GRIPPER_RIGHT_INVERT": -1.0 if self._arm_tune_checks["right_invert"].isChecked() else 1.0,
            "GRIPPER_PITCH_INVERT": -1.0 if self._arm_tune_checks["pitch_invert"].isChecked() else 1.0,
            "GRIPPER_YAW_INVERT": -1.0 if self._arm_tune_checks["yaw_invert"].isChecked() else 1.0,
            "GRIPPER_SERVO_RANGE_DEG": float(self._arm_tune_spins["servo_range_deg"].value()),
            "GRIPPER_PITCH_SPAN_DEG": float(self._arm_tune_spins["pitch_span_deg"].value()),
            "GRIPPER_PITCH_NEUTRAL_DEG": float(self._arm_tune_spins["pitch_neutral_deg"].value()),
        }
        self._queue_request("set_config", {"updates": updates}, request_meta={"refresh_after_success": True})

    def _current_arm_park_norm(self) -> tuple[float, float]:
        defaults = dict(self._arm_tune_config_defaults)
        pitch_spin = self._arm_park_spins.get("park_pitch")
        wrist_spin = self._arm_park_spins.get("park_wrist")
        pitch = float(pitch_spin.value()) if pitch_spin is not None else float(defaults["park_pitch"])
        wrist = float(wrist_spin.value()) if wrist_spin is not None else float(defaults["park_wrist"])
        return max(-1.0, min(1.0, pitch)), max(-1.0, min(1.0, wrist))

    def _arm_park_command_values(self) -> tuple[float, float, float, float]:
        park_pitch, park_wrist = self._current_arm_park_norm()
        geom = self._current_arm_geometry()
        pitch_cmd = self._axis_command_for_invert(park_pitch, geom["pitch_invert"])
        wrist_cmd = self._axis_command_for_invert(park_wrist, geom["yaw_invert"])
        return park_pitch, park_wrist, pitch_cmd, wrist_cmd

    def _sync_pilot_arm_park_pose(self) -> tuple[float, float] | None:
        if self._pilot_svc is None or not hasattr(self._pilot_svc, "set_arm_park_position"):
            return None
        _park_pitch, _park_wrist, pitch_cmd, wrist_cmd = self._arm_park_command_values()
        try:
            return self._pilot_svc.set_arm_park_position(pitch_cmd, wrist_cmd)
        except Exception:
            return None

    def _confirm_arm_park_pose(self) -> None:
        park_pitch, park_wrist, pitch_cmd, wrist_cmd = self._arm_park_command_values()
        answer = QMessageBox.question(
            self,
            "Arm Park Pose",
            (
                "Command the arm park pose now?\n\n"
                f"Config target: pitch {park_pitch:+.2f}, wrist {park_wrist:+.2f}.\n"
                f"Pilot target: pitch {pitch_cmd:+.2f}, wrist {wrist_cmd:+.2f}.\n"
                "Keep the arm clear and only arm the ROV when it is safe for the servos to move."
            ),
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._send_arm_park_pose()

    def _send_arm_park_pose(self) -> bool:
        if self._pilot_svc is None or not hasattr(self._pilot_svc, "set_arm_position"):
            self._set_feedback("Arm park pose needs the live pilot publisher.", tone="error")
            return False

        park_pitch, park_wrist, pitch_cmd, wrist_cmd = self._arm_park_command_values()
        self._sync_pilot_arm_park_pose()
        try:
            if hasattr(self._pilot_svc, "park_arm"):
                sent_pitch, sent_wrist = self._pilot_svc.park_arm()
            else:
                sent_pitch, sent_wrist = self._pilot_svc.set_arm_position(pitch_cmd, wrist_cmd)
        except Exception as exc:
            self._set_feedback(f"Could not set arm park target: {exc}", tone="error")
            return False

        status = (
            f"Park Pose: config pitch {park_pitch:+.2f}, wrist {park_wrist:+.2f} | "
            f"pilot target {float(sent_pitch):+.2f}, {float(sent_wrist):+.2f}"
        )
        self.arm_alignment_status_label.setText(status)
        self._set_feedback(status, tone="info")
        return True

    def _save_arm_park_config(self) -> None:
        park_pitch, park_wrist = self._current_arm_park_norm()
        self._sync_pilot_arm_park_pose()
        updates = {
            "GRIPPER_DISARM_PITCH": float(park_pitch),
            "GRIPPER_DISARM_YAW": float(park_wrist),
            "GRIPPER_ARM_PITCH": float(park_pitch),
            "GRIPPER_ARM_YAW": float(park_wrist),
        }
        self._queue_request("set_config", {"updates": updates}, request_meta={"refresh_after_success": True})

    def _confirm_arm_alignment_pose(self, pose_key: str) -> None:
        pose = self._arm_alignment_pose_degrees(pose_key)
        if pose is None:
            return
        label, pitch_deg, wrist_deg = pose
        answer = QMessageBox.question(
            self,
            "Arm Alignment Pose",
            (
                f"Command {label}?\n\n"
                f"Target: pitch {pitch_deg:.1f} deg, wrist {wrist_deg:.1f} deg.\n"
                "This moves the differential arm through the normal pilot command stream. "
                "Keep the arm clear and only arm the ROV when it is safe for the servos to move."
            ),
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._send_arm_alignment_pose(pose_key)

    def _send_arm_alignment_pose(self, pose_key: str) -> bool:
        if self._pilot_svc is None or not hasattr(self._pilot_svc, "set_arm_position"):
            self._set_feedback("Arm alignment poses need the live pilot publisher.", tone="error")
            return False

        pose = self._arm_alignment_pose_degrees(pose_key)
        if pose is None:
            self._set_feedback(f"Unknown arm alignment pose: {pose_key}", tone="error")
            return False

        label, pitch_deg, wrist_deg = pose
        geom = self._current_arm_geometry()
        pitch_norm, wrist_norm = self._arm_pose_norms(pitch_deg, wrist_deg, geom)
        pitch_cmd = self._axis_command_for_invert(pitch_norm, geom["pitch_invert"])
        wrist_cmd = self._axis_command_for_invert(wrist_norm, geom["yaw_invert"])

        try:
            self._pilot_svc.set_arm_position(pitch_cmd, wrist_cmd)
        except Exception as exc:
            self._set_feedback(f"Could not set arm alignment target: {exc}", tone="error")
            return False

        left_norm, right_norm = self._diff_mix_norm_for_geometry(pitch_norm, wrist_norm, geom)
        left_us = geom["servo_center_us"] + left_norm * geom["servo_pulse_halfspan_us"]
        right_us = geom["servo_center_us"] + right_norm * geom["servo_pulse_halfspan_us"]
        status = (
            f"{label}: pitch {pitch_deg:.1f} deg, wrist {wrist_deg:.1f} deg | "
            f"pilot target {pitch_cmd:+.3f}, {wrist_cmd:+.3f} | "
            f"servos left {left_norm:+.3f} ({left_us:.0f} us), "
            f"right {right_norm:+.3f} ({right_us:.0f} us)"
        )
        self.arm_alignment_status_label.setText(status)

        runtime = dict((self._last_state or {}).get("runtime") or {})
        if runtime.get("armed") is False:
            status += " | ROV is reported disarmed; the target will apply when armed."
        self._set_feedback(status, tone="info")
        return True

    def _arm_alignment_pose_degrees(self, pose_key: str) -> tuple[str, float, float] | None:
        geom = self._current_arm_geometry()
        key = str(pose_key or "").strip().lower()
        if key == "center":
            return ("Servo Center", float(geom["pitch_neutral_deg"]), float(geom["wrist_neutral_deg"]))
        if key == "flat_wrist_90":
            return ("Flat / Wrist 90", 0.0, float(geom["wrist_span_deg"]))
        if key == "flat_wrist_0":
            return ("Flat / Wrist 0", 0.0, 0.0)
        return None

    def _current_arm_geometry(self) -> dict[str, float]:
        config = dict((self._last_state or {}).get("config") or {})
        defaults = dict(self._arm_tune_config_defaults)

        def cfg_float(cfg_key: str, default_key: str) -> float:
            return self._float_or_default(config.get(cfg_key), defaults[default_key])

        center_us = cfg_float("GRIPPER_SERVO_CENTER_US", "servo_center_us")
        pulse_halfspan = config.get("GRIPPER_SERVO_PULSE_HALFSPAN_US")
        if pulse_halfspan is None:
            min_us = config.get("GRIPPER_SERVO_MIN_US")
            max_us = config.get("GRIPPER_SERVO_MAX_US")
            try:
                pulse_halfspan = max(abs(float(center_us) - float(min_us)), abs(float(max_us) - float(center_us)))
            except Exception:
                pulse_halfspan = defaults["servo_pulse_halfspan_us"]

        return {
            "left_invert": -1.0 if self._arm_tune_checks["left_invert"].isChecked() else 1.0,
            "right_invert": -1.0 if self._arm_tune_checks["right_invert"].isChecked() else 1.0,
            "pitch_invert": -1.0 if self._arm_tune_checks["pitch_invert"].isChecked() else 1.0,
            "yaw_invert": -1.0 if self._arm_tune_checks["yaw_invert"].isChecked() else 1.0,
            "servo_range_deg": float(self._arm_tune_spins["servo_range_deg"].value()),
            "pitch_span_deg": float(self._arm_tune_spins["pitch_span_deg"].value()),
            "pitch_neutral_deg": float(self._arm_tune_spins["pitch_neutral_deg"].value()),
            "wrist_span_deg": cfg_float("GRIPPER_WRIST_SPAN_DEG", "wrist_span_deg"),
            "wrist_neutral_deg": cfg_float("GRIPPER_WRIST_NEUTRAL_DEG", "wrist_neutral_deg"),
            "servo_center_us": float(center_us),
            "servo_pulse_halfspan_us": self._float_or_default(pulse_halfspan, defaults["servo_pulse_halfspan_us"]),
        }

    @staticmethod
    def _arm_pose_norms(pitch_deg: float, wrist_deg: float, geom: dict[str, float]) -> tuple[float, float]:
        def norm(deg: float, span: float) -> float:
            span = max(1e-6, float(span))
            value = (float(deg) / span) * 2.0 - 1.0
            return max(-1.0, min(1.0, value))

        return norm(pitch_deg, geom["pitch_span_deg"]), norm(wrist_deg, geom["wrist_span_deg"])

    @staticmethod
    def _axis_command_for_invert(position_norm: float, invert: float) -> float:
        try:
            inv = float(invert)
        except Exception:
            inv = 1.0
        if abs(inv) < 1e-6:
            inv = 1.0
        return max(-1.0, min(1.0, float(position_norm) / inv))

    @staticmethod
    def _diff_mix_norm_for_geometry(pitch_norm: float, wrist_norm: float, geom: dict[str, float]) -> tuple[float, float]:
        rng = max(1.0, float(geom["servo_range_deg"]))
        pitch_deg = (max(-1.0, min(1.0, float(pitch_norm))) + 1.0) * 0.5 * float(geom["pitch_span_deg"])
        wrist_deg = (max(-1.0, min(1.0, float(wrist_norm))) + 1.0) * 0.5 * float(geom["wrist_span_deg"])
        d_pitch = max(-rng, min(rng, pitch_deg - float(geom["pitch_neutral_deg"])))
        room = max(0.0, rng - abs(d_pitch))
        d_wrist = max(-room, min(room, wrist_deg - float(geom["wrist_neutral_deg"])))
        left = float(geom["left_invert"]) * (d_pitch + d_wrist) / rng
        right = float(geom["right_invert"]) * (d_pitch - d_wrist) / rng
        return max(-1.0, min(1.0, left)), max(-1.0, min(1.0, right))

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
        self._status_labels["attitude_reference"].setText(
            self._format_path_state(
                bool(refs.get("attitude_reference_exists")),
                refs.get("attitude_reference_path"),
            )
        )
        self._status_labels["surface_pressure"].setText(self._fmt_num(refs.get("surface_pressure_mbar"), "mbar", decimals=2))
        self._status_labels["sensor_to_top"].setText(self._fmt_num(refs.get("depth_sensor_to_top_m"), "m", decimals=4))
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

        for spec in CONFIG_FIELD_SPECS:
            key = str(spec["key"])
            spin = self._config_spins[key]
            present = key in config
            spin.setEnabled(bool(present))
            spin.setToolTip("" if present else f"{key} is not present in rov_config.py on the ROV.")
            if present:
                self._set_spin_value(spin, config.get(key))

        self._apply_arm_tune_config(config)
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
        elif cmd == "capture_local_rest":
            depth = dict(data.get("depth") or {})
            attitude = dict(data.get("attitude") or {})
            bits = []
            if attitude:
                bits.append(f"attitude -> {str(attitude.get('attitude_reference_path') or 'attitude reference file')}")
            if depth:
                bits.append(
                    f"surface {self._fmt_num(depth.get('surface_pressure_mbar'), 'mbar', decimals=2)} "
                    f"-> {str(depth.get('path') or 'depth reference file')}"
                )
            msg = "Captured local rest: " + ("; ".join(bits) if bits else "no reference data reported")
            errors = dict(data.get("errors") or {})
            if errors:
                msg += " | warnings: " + "; ".join(f"{k}: {v}" for k, v in errors.items())
        elif cmd == "update_code":
            stdout = str(data.get("stdout") or "").strip()
            revision = ""
            for line in reversed(stdout.splitlines()):
                if line.strip():
                    revision = line.strip()
                    break
            msg = "ROV code updated from GitHub."
            if revision:
                msg += f" Latest: {revision}"
            if data.get("restart_scheduled"):
                msg += " TritonOS restart scheduled."
        elif cmd == "restart_service":
            msg = "TritonOS restart scheduled. The management RPC will disconnect briefly."
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
        self.capture_local_rest_btn.setEnabled((not busy) and ("capture_local_rest" in self._available_commands))
        self.update_code_btn.setEnabled((not busy) and ("update_code" in self._available_commands))
        self.update_restart_btn.setEnabled((not busy) and ("update_code" in self._available_commands))
        self.restart_service_btn.setEnabled((not busy) and ("restart_service" in self._available_commands))

        has_enabled_config_field = any(spin.isEnabled() for spin in self._config_spins.values())
        self.save_config_btn.setEnabled((not busy) and can_set_config and has_enabled_config_field)
        if hasattr(self, "arm_tune_save_btn"):
            self.arm_tune_save_btn.setEnabled((not busy) and can_set_config)
        if hasattr(self, "arm_park_save_btn"):
            self.arm_park_save_btn.setEnabled((not busy) and can_set_config)
        if hasattr(self, "arm_park_command_btn"):
            self.arm_park_command_btn.setEnabled((not busy) and self._pilot_svc is not None)

        if not has_state:
            self.save_sensor_offset_btn.setEnabled(False)
            self.save_surface_btn.setEnabled(False)
            self.capture_surface_btn.setEnabled(False)
            self.capture_local_rest_btn.setEnabled(False)
            self.update_code_btn.setEnabled(False)
            self.update_restart_btn.setEnabled(False)
            self.restart_service_btn.setEnabled(False)
            self.save_config_btn.setEnabled(False)
            if hasattr(self, "arm_tune_save_btn"):
                self.arm_tune_save_btn.setEnabled(False)
            if hasattr(self, "arm_park_save_btn"):
                self.arm_park_save_btn.setEnabled(False)

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

    def _capture_local_rest(self) -> None:
        answer = QMessageBox.question(
            self,
            "Capture Local Rest",
            "Capture the current ROV pose and surface pressure as the persistent local rest reference?\n\nKeep the vehicle still in its chosen rest pose, with the top of the ROV at the water surface.",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._queue_request(
            "capture_local_rest",
            {"samples": 20, "delay_s": 0.02, "include_depth": True},
            request_meta={"refresh_after_success": True},
        )

    def _confirm_update_code(self, *, restart: bool) -> None:
        action = "update code from GitHub and restart TritonOS" if restart else "update code from GitHub"
        answer = QMessageBox.question(
            self,
            "Force Update From GitHub",
            (
                f"Force {action}?\n\n"
                "This resets the Pi checkout to origin/main and removes untracked code files. "
                "Runtime data directories such as calibration/ and recordings/ are preserved."
            ),
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._queue_request(
            "update_code",
            {"branch": "main", "force": True, "restart": bool(restart), "timeout_s": 180.0},
            request_meta={"refresh_after_success": not restart},
        )

    def _confirm_restart_service(self) -> None:
        answer = QMessageBox.question(
            self,
            "Restart TritonOS",
            "Restart the TritonOS ROV service now?\n\nThe management RPC and telemetry will disconnect briefly.",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._queue_request("restart_service", {"delay_s": 1.0})

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
    def _float_or_default(value, default: float) -> float:
        try:
            return float(value)
        except Exception:
            return float(default)

    @staticmethod
    def _set_check_value(check: QCheckBox, checked: bool) -> None:
        prev = check.blockSignals(True)
        try:
            check.setChecked(bool(checked))
        finally:
            check.blockSignals(prev)

    def _apply_arm_tune_config(self, config: dict) -> None:
        cfg = dict(config or {})
        defaults = dict(self._arm_tune_config_defaults)
        key_map = {
            "left_invert": "GRIPPER_LEFT_INVERT",
            "right_invert": "GRIPPER_RIGHT_INVERT",
            "pitch_invert": "GRIPPER_PITCH_INVERT",
            "yaw_invert": "GRIPPER_YAW_INVERT",
        }
        for tune_key, cfg_key in key_map.items():
            check = self._arm_tune_checks.get(tune_key)
            if check is None:
                continue
            value = self._float_or_default(cfg.get(cfg_key), defaults[tune_key])
            self._set_check_value(check, value < 0.0)

        spin_map = {
            "servo_range_deg": "GRIPPER_SERVO_RANGE_DEG",
            "pitch_span_deg": "GRIPPER_PITCH_SPAN_DEG",
            "pitch_neutral_deg": "GRIPPER_PITCH_NEUTRAL_DEG",
        }
        for tune_key, cfg_key in spin_map.items():
            spin = self._arm_tune_spins.get(tune_key)
            if spin is None:
                continue
            value = self._float_or_default(cfg.get(cfg_key), defaults[tune_key])
            self._set_spin_value(spin, value)

        park_pitch = self._float_or_default(
            cfg.get("GRIPPER_ARM_PITCH", cfg.get("GRIPPER_DISARM_PITCH")),
            defaults["park_pitch"],
        )
        park_wrist = self._float_or_default(
            cfg.get("GRIPPER_ARM_YAW", cfg.get("GRIPPER_DISARM_YAW")),
            defaults["park_wrist"],
        )
        if "park_pitch" in self._arm_park_spins:
            self._set_spin_value(self._arm_park_spins["park_pitch"], park_pitch)
        if "park_wrist" in self._arm_park_spins:
            self._set_spin_value(self._arm_park_spins["park_wrist"], park_wrist)
        self._sync_pilot_arm_park_pose()

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

    @staticmethod
    def _command_label(cmd: str) -> str:
        labels = {
            "get_state": "Refresh State",
            "get_hold_status": "Refresh Hold Status",
            "set_config": "Save Config",
            "set_surface_reference": "Save Manual Surface Pressure",
            "capture_surface_reference": "Capture Surface Reference",
            "capture_local_rest": "Capture Local Rest",
            "update_code": "Force Update From GitHub",
            "restart_service": "Restart TritonOS",
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
