"""Square single-camera view for the MATEROV transect task."""

from __future__ import annotations

from PyQt6.QtCore import QRect, Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)


class SquareVideoHost(QFrame):
    """Center one child in the largest square that fits this host."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("transectSquareHost")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._widget: QWidget | None = None
        # Optional autopilot overlay layered above the video (shown only while the
        # transect CV is running); kept square-aligned with the video below it.
        self._overlay: QWidget | None = None
        self._placeholder = QLabel("Video unavailable.", self)
        self._placeholder.setObjectName("videoPanePlaceholder")
        self._placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._placeholder.setWordWrap(True)

    def set_overlay(self, widget: QWidget | None) -> None:
        self._overlay = widget
        if widget is not None:
            anchor_setter = getattr(widget, "set_anchor_widget", None)
            if callable(anchor_setter):
                anchor_setter(self)
            else:
                widget.setParent(self)
            widget.hide()
        self._layout_child()

    def show_overlay(self) -> None:
        if self._overlay is not None:
            self._layout_child()
            show_for_anchor = getattr(self._overlay, "show_for_anchor", None)
            if callable(show_for_anchor):
                show_for_anchor()
            else:
                self._overlay.show()
                self._overlay.raise_()

    def hide_overlay(self) -> None:
        if self._overlay is not None:
            self._overlay.hide()

    def current_widget(self) -> QWidget | None:
        return self._widget

    def set_placeholder_text(self, text: str) -> None:
        self._placeholder.setText(str(text or "Video unavailable."))
        self._layout_child()

    def set_widget(self, widget: QWidget | None) -> None:
        if widget is self._widget:
            self._layout_child()
            return
        if self._widget is not None and self._widget.parent() is self:
            self._widget.hide()
            self._widget.setParent(None)
        self._widget = widget
        if widget is None:
            self._placeholder.setParent(self)
            self._placeholder.show()
        else:
            self._placeholder.hide()
            widget.setParent(self)
            widget.show()
            widget.raise_()
        self._layout_child()

    def _square_rect(self) -> QRect:
        rect = self.contentsRect()
        side = max(1, min(rect.width(), rect.height()))
        x = rect.x() + max(0, (rect.width() - side) // 2)
        y = rect.y() + max(0, (rect.height() - side) // 2)
        return QRect(x, y, side, side)

    def _layout_child(self) -> None:
        rect = self._square_rect()
        widget = self._widget
        if widget is not None:
            widget.setGeometry(rect)
            refresher = getattr(widget, "refresh_layout_geometry", None)
            if callable(refresher):
                try:
                    refresher()
                except Exception:
                    pass
        self._placeholder.setGeometry(rect)
        if self._overlay is not None:
            sync_to_anchor = getattr(self._overlay, "sync_to_anchor", None)
            if callable(sync_to_anchor):
                sync_to_anchor()
            else:
                self._overlay.setGeometry(rect)
            if self._overlay.isVisible() and not callable(sync_to_anchor):
                self._overlay.raise_()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._layout_child()


class TransectPage(QWidget):
    """Operator page for a square, single-camera transect view."""

    cameraSelectionChanged = pyqtSignal(str)
    engageToggled = pyqtSignal(bool)   # operator pressed the Engage Optical Hold button
    rotationServoToggled = pyqtSignal(bool)
    targetBlueWidthChanged = pyqtSignal(float)

    def __init__(
        self,
        stream_names: list[str],
        parent=None,
        *,
        rotation_servo_enabled: bool = False,
        target_blue_width_percent: float = 55.6,
        target_blue_width_min_percent: float = 25.0,
        target_blue_width_max_percent: float = 95.0,
    ):
        super().__init__(parent)
        self.stream_names = list(stream_names)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(6)

        controls = QWidget()
        controls.setObjectName("transectControls")
        controls_lay = QHBoxLayout(controls)
        controls_lay.setContentsMargins(0, 0, 0, 0)
        controls_lay.setSpacing(6)

        label = QLabel("Camera")
        self.camera_combo = QComboBox()
        self.camera_combo.setObjectName("transectCameraSelector")
        self.camera_combo.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.camera_combo.addItems(self.stream_names)
        self.camera_combo.currentTextChanged.connect(self.cameraSelectionChanged.emit)

        self.cv_status_label = QLabel("Autopilot CV: off")
        self.cv_status_label.setObjectName("transectCvStatus")
        self.cv_status_label.setProperty("tone", "off")

        target_label = QLabel("Blue width")
        target_label.setObjectName("transectTargetLabel")
        self.target_blue_width_spin = QDoubleSpinBox()
        self.target_blue_width_spin.setObjectName("transectTargetBlueWidthSpin")
        self.target_blue_width_spin.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self.target_blue_width_spin.setKeyboardTracking(False)
        lo = float(target_blue_width_min_percent)
        hi = max(lo, float(target_blue_width_max_percent))
        self.target_blue_width_spin.setRange(lo, hi)
        self.target_blue_width_spin.setDecimals(1)
        self.target_blue_width_spin.setSingleStep(1.0)
        self.target_blue_width_spin.setSuffix(" %")
        self.target_blue_width_spin.setValue(max(lo, min(hi, float(target_blue_width_percent))))
        self.target_blue_width_spin.setMinimumWidth(92)
        self.target_blue_width_spin.setToolTip(
            "Target apparent blue-square width as a percent of frame width."
        )
        self.target_blue_width_spin.valueChanged.connect(
            lambda value: self.targetBlueWidthChanged.emit(float(value))
        )

        self.rotation_servo_check = QCheckBox("Yaw/er")
        self.rotation_servo_check.setObjectName("transectRotationServoToggle")
        self.rotation_servo_check.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.rotation_servo_check.setChecked(bool(rotation_servo_enabled))
        self.rotation_servo_check.setToolTip("Allow transect rotation error to drive yaw.")
        self.rotation_servo_check.toggled.connect(self.rotationServoToggled.emit)

        # Big, obvious engage control (also bound to the K key elsewhere).
        self.engage_btn = QPushButton("Engage Optical Hold  (K)")
        self.engage_btn.setObjectName("transectEngageButton")
        self.engage_btn.setCheckable(True)
        self.engage_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.engage_btn.setMinimumHeight(34)
        self.engage_btn.setProperty("tone", "idle")
        self.engage_btn.toggled.connect(self.engageToggled.emit)

        controls_lay.addWidget(label, 0)
        controls_lay.addWidget(self.camera_combo, 0)
        controls_lay.addWidget(target_label, 0)
        controls_lay.addWidget(self.target_blue_width_spin, 0)
        controls_lay.addWidget(self.rotation_servo_check, 0)
        controls_lay.addStretch(1)
        controls_lay.addWidget(self.cv_status_label, 0)
        controls_lay.addWidget(self.engage_btn, 0)

        self.square_host = SquareVideoHost()

        outer.addWidget(controls, 0)
        outer.addWidget(self.square_host, 1)

    def current_stream_name(self) -> str | None:
        name = self.camera_combo.currentText().strip()
        if name:
            return name
        return self.stream_names[0] if self.stream_names else None

    def set_current_stream(self, name: str | None, *, emit: bool = False) -> None:
        if name not in self.stream_names:
            return
        prev = self.camera_combo.blockSignals(not emit)
        try:
            self.camera_combo.setCurrentText(str(name))
        finally:
            self.camera_combo.blockSignals(prev)

    def attach_video_panel(self, panel: QWidget) -> None:
        self.square_host.set_widget(panel)

    def detach_video_panel(self, panel: QWidget | None = None) -> None:
        if panel is None or self.square_host.current_widget() is panel:
            self.square_host.set_widget(None)

    def attach_video_placeholder(self, text: str) -> None:
        self.square_host.set_placeholder_text(text)
        self.square_host.set_widget(None)

    def set_cv_status(self, text: str, tone: str = "off") -> None:
        """Update the small non-covering autopilot CV status chip."""
        self.cv_status_label.setText(str(text))
        self._set_tone(self.cv_status_label, tone)

    def set_rotation_servo_enabled(self, enabled: bool, *, emit: bool = False) -> None:
        prev = self.rotation_servo_check.blockSignals(not emit)
        try:
            self.rotation_servo_check.setChecked(bool(enabled))
        finally:
            self.rotation_servo_check.blockSignals(prev)

    def set_target_blue_width_percent(self, value: float, *, emit: bool = False) -> None:
        prev = self.target_blue_width_spin.blockSignals(not emit)
        try:
            self.target_blue_width_spin.setValue(float(value))
        finally:
            self.target_blue_width_spin.blockSignals(prev)

    def update_engage_state(self, *, engaged: bool, lock_ready: bool) -> None:
        """Reflect the hold state on the engage button (also driven by the K key).

        ``lock_ready`` highlights the button green when a clean lock is available
        so the operator knows it's a good moment to engage.
        """
        b = self.engage_btn
        blocked = b.blockSignals(True)
        b.setChecked(bool(engaged))
        b.blockSignals(blocked)
        if engaged:
            b.setText("● HOLDING — click or press K to release")
            tone = "engaged"
        elif lock_ready:
            b.setText("Engage Optical Hold  ✓ lock  (K)")
            tone = "ready"
        else:
            b.setText("Engage Optical Hold  (K)")
            tone = "idle"
        self._set_tone(b, tone)

    @staticmethod
    def _set_tone(widget, tone: str) -> None:
        if widget.property("tone") != tone:
            widget.setProperty("tone", tone)
            style = widget.style()
            style.unpolish(widget)
            style.polish(widget)

    def set_overlay_widget(self, widget: QWidget | None) -> None:
        """Install the autopilot overlay view (layered above the video)."""
        self.square_host.set_overlay(widget)

    def show_overlay(self) -> None:
        self.square_host.show_overlay()

    def hide_overlay(self) -> None:
        self.square_host.hide_overlay()
