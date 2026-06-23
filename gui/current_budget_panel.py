"""Top-bar panel for the optional intelligent current (fuse) limiter.

Two glanceable controls that sit next to the competition clock:

  * a checkbox to enable/disable the ROV-side feed-forward current limiter live
    (a pilot kill switch -- the ROV still has its own config master switch), and
  * a readout of the live *estimated* total thruster draw (amps) reported by the
    ROV, color-coded against the configured budget.

The estimate is the ROV's feed-forward prediction from commanded PWM + the
BlueRobotics T200 data; it does not depend on the (untrusted) Power Sense
Module. This widget is display/intent only -- all limiting happens on the ROV.
"""

from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import QCheckBox, QHBoxLayout, QLabel, QSizePolicy, QSpinBox, QWidget

_TONE_COLORS = {
    "off": "#888888",
    "green": "#3fb950",
    "amber": "#d29922",
    "red": "#f85149",
}


class CurrentBudgetPanel(QWidget):
    """Enable/disable + live estimated thruster draw, for the pilot top bar."""

    toggled = pyqtSignal(bool)
    budget_changed = pyqtSignal(float)

    def __init__(
        self,
        parent=None,
        *,
        enabled: bool = True,
        budget_a: float = 22.0,
        budget_min: float = 5.0,
        budget_max: float = 40.0,
    ):
        super().__init__(parent)
        self.setObjectName("currentBudgetPanel")
        self.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 2, 6, 2)
        layout.setSpacing(4)

        self.check = QCheckBox("Smart Limit")
        self.check.setObjectName("currentBudgetCheck")
        self.check.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.check.setToolTip(
            "Enable the ROV's intelligent thruster current limiter (fuse protection).\n"
            "Uncheck instantly if it ever feels wrong; the ROV reverts to raw thrust."
        )
        self.check.setChecked(bool(enabled))
        self.check.toggled.connect(self._on_toggled)

        self.readout = QLabel("— A")
        self.readout.setObjectName("currentBudgetReadout")
        self.readout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.readout.setMinimumWidth(78)
        self.readout.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.readout.setToolTip("Estimated total thruster current draw (feed-forward).")

        # Live current cap. Keyboard focus is disabled (it stays free for piloting);
        # adjust with the mouse wheel or the spin arrows, matching the clock duration.
        lo, hi = (budget_min, budget_max) if budget_min <= budget_max else (budget_max, budget_min)
        self.budget_spin = QSpinBox()
        self.budget_spin.setObjectName("currentBudgetCap")
        self.budget_spin.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.budget_spin.setRange(int(round(lo)), int(round(hi)))
        self.budget_spin.setSuffix(" A")
        self.budget_spin.setPrefix("≤ ")  # "<= 22 A": this is the cap
        self.budget_spin.setValue(int(round(max(lo, min(hi, float(budget_a))))))
        self.budget_spin.setToolTip(
            "Total thruster-current cap (amps) the ROV limits to.\n"
            "Keep it below the fuse rating with margin."
        )
        try:
            self.budget_spin.lineEdit().setReadOnly(True)
        except Exception:
            pass
        self.budget_spin.valueChanged.connect(self._on_budget_changed)

        layout.addWidget(self.check, 0)
        layout.addWidget(self.readout, 0)
        layout.addWidget(self.budget_spin, 0)

        self._apply_tone("off")

    # ---- enable/disable state ------------------------------------------
    def _on_toggled(self, state: bool) -> None:
        self.toggled.emit(bool(state))

    def is_enabled_state(self) -> bool:
        return bool(self.check.isChecked())

    def set_enabled_state(self, enabled: bool) -> None:
        """Reflect external state without re-emitting ``toggled``."""
        enabled = bool(enabled)
        if self.check.isChecked() == enabled:
            return
        previous = self.check.blockSignals(True)
        try:
            self.check.setChecked(enabled)
        finally:
            self.check.blockSignals(previous)

    # ---- current cap ---------------------------------------------------
    def _on_budget_changed(self, value: int) -> None:
        self.budget_changed.emit(float(value))

    def budget_value(self) -> float:
        return float(self.budget_spin.value())

    def set_budget_value(self, amps: float) -> None:
        """Reflect external cap value without re-emitting ``budget_changed``."""
        try:
            value = int(round(float(amps)))
        except Exception:
            return
        value = max(self.budget_spin.minimum(), min(self.budget_spin.maximum(), value))
        if self.budget_spin.value() == value:
            return
        previous = self.budget_spin.blockSignals(True)
        try:
            self.budget_spin.setValue(value)
        finally:
            self.budget_spin.blockSignals(previous)

    # ---- live estimate -------------------------------------------------
    def clear_estimate(self) -> None:
        """Show the no-data placeholder (e.g. disarmed or link stale)."""
        self.readout.setText("— A")
        self.readout.setToolTip("Estimated total thruster current draw (feed-forward).")
        self._apply_tone("off")

    def update_estimate(
        self,
        predicted_a: Optional[float],
        *,
        active: Optional[bool] = None,
        applied: bool = False,
        budget_a: Optional[float] = None,
    ) -> None:
        """Update the draw readout. ``predicted_a is None`` clears it."""
        if predicted_a is None:
            self.clear_estimate()
            return

        amps = float(predicted_a)
        text = f"{amps:.0f} A"
        if applied:
            text += "  LIM"
        self.readout.setText(text)

        if budget_a is None or budget_a <= 0.0:
            tone = "green"
        elif amps <= float(budget_a):
            tone = "green"
        elif amps <= 1.25 * float(budget_a):
            tone = "amber"
        else:
            tone = "red"
        self._apply_tone(tone)

        tip = f"Estimated thruster draw: {amps:.1f} A"
        if budget_a:
            tip += f" / budget {float(budget_a):.0f} A"
        if active is not None:
            tip += f"\nLimiter: {'ACTIVE' if active else 'monitoring only'}"
        if applied:
            tip += "\nClamping thrust to stay under budget."
        self.readout.setToolTip(tip)

    def _apply_tone(self, tone: str) -> None:
        color = _TONE_COLORS.get(tone, _TONE_COLORS["off"])
        self.readout.setStyleSheet(
            f"QLabel#currentBudgetReadout {{ color: {color}; font-weight: 600; }}"
        )
