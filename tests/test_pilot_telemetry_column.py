import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt6")

from PyQt6.QtWidgets import QApplication

from gui.instruments import PilotTelemetryColumn


def _app() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def test_pilot_telemetry_column_shows_analysis_share_status():
    app = _app()
    column = PilotTelemetryColumn()
    column.show()
    try:
        app.processEvents()

        column.set_analysis_share("Analysis Share: ON http://10.77.0.1:8765", "warn")
        assert "10.77.0.1" in column.analysis_text.text()
        assert column.analysis_text.toolTip() == column.analysis_text.text()
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
