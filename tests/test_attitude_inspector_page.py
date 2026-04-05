import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt6")

from PyQt6.QtWidgets import QApplication

from gui.instruments import AttitudeInspectorPage


def _app() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def test_attitude_inspector_page_shows_mag_debug_values():
    app = _app()

    page = AttitudeInspectorPage()
    try:
        page.update_from_sensor(
            {
                "type": "attitude",
                "rpy_deg": {"roll": 1.5, "pitch": -2.5, "yaw": 42.0},
                "health": {"mode": "yc6", "mag_qual": 0.8, "yaw_source": "direct_mag"},
                "mag_debug": {
                    "selected_source": "mmc5983",
                    "heading_delta_deg": 87.5,
                    "body_angle_deg": 92.0,
                    "ak09915": {"heading_deg": -40.0, "norm_uT": 48.1},
                    "mmc5983": {"heading_deg": -127.5, "norm_uT": 50.2},
                },
            }
        )
        app.processEvents()

        assert page._mag_labels["selected_source"].text() == "mmc5983"
        assert page._mag_labels["yaw_source"].text() == "direct_mag"
        assert page._mag_labels["ak_heading"].text() == "-40.0 deg"
        assert page._mag_labels["mmc_heading"].text() == "-127.5 deg"
        assert page._mag_labels["heading_delta"].text() == "87.5 deg"
        assert page._mag_labels["body_angle"].text() == "92.0 deg"
        assert page._mag_labels["ak_norm"].text() == "48.1 uT"
        assert page._mag_labels["mmc_norm"].text() == "50.2 uT"
    finally:
        page.shutdown()
        page.close()
        page.deleteLater()
        app.processEvents()
