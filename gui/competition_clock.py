"""Always-visible competition countdown clock for the pilot shell."""

from __future__ import annotations

from math import ceil
from time import monotonic

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QAction
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QMenu, QPushButton, QSizePolicy, QSpinBox, QToolButton, QWidget, QWidgetAction


class CompetitionClock(QWidget):
    """Mouse-guarded 15 minute countdown with a keyboard-only start path."""

    DEFAULT_DURATION_SECONDS = 15 * 60
    MIN_DURATION_MINUTES = 1
    MAX_DURATION_MINUTES = 99

    def __init__(self, parent=None, *, duration_seconds: int = DEFAULT_DURATION_SECONDS):
        super().__init__(parent)
        self.setObjectName("competitionClock")
        self.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)

        self._duration_seconds = self._coerce_duration_seconds(duration_seconds)
        self._remaining_before_run = float(self._duration_seconds)
        self._started_at: float | None = None
        self._clock_enabled = True

        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._timer.setInterval(100)
        self._timer.timeout.connect(self._refresh_display)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 2, 6, 2)
        layout.setSpacing(4)

        self.clock_label = QLabel()
        self.clock_label.setObjectName("competitionClockLabel")
        self.clock_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.clock_label.setMinimumWidth(108)
        self.clock_label.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        self.toggle_btn = QPushButton("Start")
        self.toggle_btn.setObjectName("competitionClockButton")
        self.toggle_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.toggle_btn.setToolTip("Start, resume, or pause the run clock. The M key only starts/resumes.")
        self.toggle_btn.clicked.connect(self._toggle_from_mouse)

        self.options_menu = QMenu(self)
        self.enable_act = QAction("Clock Enabled", self)
        self.enable_act.setCheckable(True)
        self.enable_act.setChecked(True)
        self.enable_act.toggled.connect(self.set_clock_enabled)
        self.options_menu.addAction(self.enable_act)

        self.reset_act = QAction("Reset to Duration", self)
        self.reset_act.triggered.connect(self.reset)
        self.options_menu.addAction(self.reset_act)
        self.options_menu.addSeparator()

        duration_widget = QWidget()
        duration_layout = QHBoxLayout(duration_widget)
        duration_layout.setContentsMargins(8, 4, 8, 4)
        duration_layout.setSpacing(6)
        duration_label = QLabel("Duration")
        duration_label.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        self.duration_spin = QSpinBox()
        self.duration_spin.setObjectName("competitionClockDuration")
        self.duration_spin.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.duration_spin.setRange(self.MIN_DURATION_MINUTES, self.MAX_DURATION_MINUTES)
        self.duration_spin.setSuffix(" min")
        self.duration_spin.setValue(max(self.MIN_DURATION_MINUTES, int(round(self._duration_seconds / 60.0))))
        self.duration_spin.setToolTip("Run duration. Use the mouse controls to change it.")
        try:
            self.duration_spin.lineEdit().setReadOnly(True)
        except Exception:
            pass
        self.duration_spin.valueChanged.connect(self.set_duration_minutes)

        duration_layout.addWidget(duration_label, 0)
        duration_layout.addWidget(self.duration_spin, 0)
        duration_action = QWidgetAction(self)
        duration_action.setDefaultWidget(duration_widget)
        self.options_menu.addAction(duration_action)

        self.options_btn = QToolButton()
        self.options_btn.setObjectName("competitionClockButton")
        self.options_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.options_btn.setText("Set")
        self.options_btn.setToolTip("Open clock controls.")
        self.options_btn.setMenu(self.options_menu)
        self.options_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)

        layout.addWidget(self.clock_label, 0)
        layout.addWidget(self.toggle_btn, 0)
        layout.addWidget(self.options_btn, 0)

        self._refresh_display()

    @classmethod
    def _coerce_duration_seconds(cls, seconds: int | float) -> int:
        try:
            value = int(round(float(seconds)))
        except Exception:
            value = cls.DEFAULT_DURATION_SECONDS
        lo = cls.MIN_DURATION_MINUTES * 60
        hi = cls.MAX_DURATION_MINUTES * 60
        return max(lo, min(hi, value))

    def clock_enabled(self) -> bool:
        return bool(self._clock_enabled)

    def is_running(self) -> bool:
        return self._started_at is not None

    def duration_seconds(self) -> int:
        return int(self._duration_seconds)

    def remaining_seconds(self) -> float:
        remaining = float(self._remaining_before_run)
        started = self._started_at
        if started is not None:
            remaining -= max(0.0, monotonic() - started)
        return max(0.0, remaining)

    def start(self) -> bool:
        if not self._clock_enabled or self.is_running() or self.remaining_seconds() <= 0.0:
            self._refresh_display()
            return False
        self._started_at = monotonic()
        self._timer.start()
        self._refresh_display()
        return True

    def start_from_keyboard(self) -> str:
        """Start/resume from the keyboard without ever pausing or resetting."""
        if not self._clock_enabled:
            self._refresh_display()
            return "disabled"
        if self.is_running():
            self._refresh_display()
            return "running"
        if self.remaining_seconds() <= 0.0:
            self._refresh_display()
            return "complete"
        self.start()
        return "started"

    def _toggle_from_mouse(self) -> None:
        if self.is_running():
            self.pause()
        else:
            self.start()

    def pause(self) -> bool:
        if not self.is_running():
            self._refresh_display()
            return False
        self._remaining_before_run = self.remaining_seconds()
        self._started_at = None
        self._timer.stop()
        self._refresh_display()
        return True

    def reset(self) -> None:
        self._remaining_before_run = float(self._duration_seconds)
        self._started_at = None
        self._timer.stop()
        self._refresh_display()

    def set_clock_enabled(self, enabled: bool) -> None:
        self._clock_enabled = bool(enabled)
        if not self._clock_enabled:
            self.pause()
        if self.enable_act.isChecked() != self._clock_enabled:
            previous = self.enable_act.blockSignals(True)
            try:
                self.enable_act.setChecked(self._clock_enabled)
            finally:
                self.enable_act.blockSignals(previous)
        self._refresh_display()

    def set_duration_minutes(self, minutes: int) -> None:
        try:
            value = int(minutes)
        except Exception:
            value = int(round(self._duration_seconds / 60.0))
        value = max(self.MIN_DURATION_MINUTES, min(self.MAX_DURATION_MINUTES, value))
        if self.duration_spin.value() != value:
            previous = self.duration_spin.blockSignals(True)
            try:
                self.duration_spin.setValue(value)
            finally:
                self.duration_spin.blockSignals(previous)
        self._duration_seconds = value * 60
        self.reset()

    @staticmethod
    def _format_seconds(seconds: float) -> str:
        total_seconds = max(0, int(ceil(float(seconds))))
        minutes, remainder = divmod(total_seconds, 60)
        return f"{minutes:02d}:{remainder:02d}"

    @staticmethod
    def _tone_for_remaining(seconds: float) -> str:
        if seconds <= 0.0:
            return "red"
        if seconds > 12 * 60:
            return "green"
        if seconds > 9 * 60:
            return "blue"
        if seconds > 6 * 60:
            return "yellow"
        if seconds > 3 * 60:
            return "orange"
        return "red"

    @staticmethod
    def _set_property(widget, name: str, value: str) -> None:
        if widget.property(name) == value:
            return
        widget.setProperty(name, value)
        style = widget.style()
        style.unpolish(widget)
        style.polish(widget)
        widget.update()

    def _refresh_display(self) -> None:
        remaining = self.remaining_seconds()
        if self.is_running() and remaining <= 0.0:
            self._remaining_before_run = 0.0
            self._started_at = None
            self._timer.stop()

        running = self.is_running()
        enabled = self.clock_enabled()
        complete = enabled and remaining <= 0.0
        ready = enabled and not running and remaining >= float(self._duration_seconds)

        if not enabled:
            text = "CLOCK OFF"
            tone = "off"
            state = "off"
        else:
            time_text = self._format_seconds(remaining)
            if running:
                text = time_text
                state = "running"
            elif complete:
                text = "00:00"
                state = "complete"
            elif ready:
                text = time_text
                state = "ready"
            else:
                text = f"PAUSED {time_text}"
                state = "paused"
            tone = self._tone_for_remaining(remaining)

        self.clock_label.setText(text)
        self.clock_label.setToolTip(f"Competition run clock: {text}")
        self._set_property(self.clock_label, "tone", tone)
        self._set_property(self.clock_label, "state", state)

        if running:
            self.toggle_btn.setEnabled(True)
            self.toggle_btn.setText("Pause")
        elif complete:
            self.toggle_btn.setEnabled(False)
            self.toggle_btn.setText("Done")
        elif ready:
            self.toggle_btn.setEnabled(enabled and remaining > 0.0)
            self.toggle_btn.setText("Start")
        else:
            self.toggle_btn.setEnabled(enabled and remaining > 0.0)
            self.toggle_btn.setText("Resume" if enabled else "Off")
        self.reset_act.setEnabled(enabled and (running or not ready))
        self.duration_spin.setEnabled(enabled and not running)
