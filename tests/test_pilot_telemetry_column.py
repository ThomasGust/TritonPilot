import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt6")

from PyQt6.QtWidgets import QApplication

from gui import instruments
from gui.instruments import PilotTelemetryColumn


def _app() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def test_pilot_telemetry_column_shows_stereo_capture_activity(monkeypatch):
    app = _app()
    monkeypatch.setattr(instruments.time, "time", lambda: 110.0)
    column = PilotTelemetryColumn()
    column.show()
    try:
        app.processEvents()

        column.set_capture_mode("camera")
        assert column.capture_activity_text.text() == ""

        column.set_capture_mode("stereo")
        assert column.capture_activity_text.text() == "STEREO READY"

        column.set_capture_activity({"state": "single", "mode": "single", "count": 0})
        assert column.capture_activity_text.text() == "STEREO CAPTURE | 0 pairs"

        column.set_capture_activity(
            {"state": "recording", "mode": "recording", "count": 3, "started_ts": 45.0}
        )
        assert column.capture_activity_text.text() == "STEREO REC 01:05 | 3 pairs"

        column.set_capture_activity({"state": "stopping", "mode": "recording", "count": 3})
        assert column.capture_activity_text.text() == "STEREO FINALIZING | 3 pairs"

        column.set_capture_activity({"state": "completed", "count": 1, "manifest_path": "manifest.json"})
        assert column.capture_activity_text.text() == "STEREO SAVED | 1 pair"
        assert column.capture_activity_text.toolTip() == "manifest.json"
    finally:
        column.close()
        column.deleteLater()
        app.processEvents()


def test_pilot_telemetry_column_updates_gain_indicators():
    app = _app()
    column = PilotTelemetryColumn()
    column.show()
    try:
        app.processEvents()
        column.set_gains(back=0.25, rov=0.8, arm=0.45)
        assert column.back_gain_indicator.value == pytest.approx(0.25)
        assert column.rov_gain_indicator.value == pytest.approx(0.8)
        assert column.arm_gain_indicator.value == pytest.approx(0.45)
        assert "25%" in column.back_gain_indicator.toolTip()
    finally:
        column.close()
        column.deleteLater()
        app.processEvents()
