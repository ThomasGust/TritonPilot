import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt6")

from PyQt6.QtWidgets import QApplication

from gui.ssh_page import SshConsolePage, SshPreset, default_pilot_ssh_presets


def _app() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def test_ssh_console_applies_presets_and_cleans_output():
    app = _app()
    page = SshConsolePage(presets=[SshPreset("ROV", "192.168.1.4", "triton")])
    try:
        app.processEvents()

        assert page.host_edit.text() == "192.168.1.4"
        assert page.user_edit.text() == "triton"
        assert page.port_spin.value() == 22
        assert page._clean_output("\x1b[31mred\x1b[0m\r\nnext\rline") == "red\nnext\nline"
    finally:
        page.close()
        page.deleteLater()
        app.processEvents()


def test_ssh_console_requires_host_and_user():
    app = _app()
    page = SshConsolePage()
    try:
        page.connect_to_host()

        assert "required" in page.status_label.text()
        assert page.connect_btn.isEnabled() is True
        assert page.send_btn.isEnabled() is False
    finally:
        page.close()
        page.deleteLater()
        app.processEvents()


def test_default_pilot_ssh_presets_include_rov_and_analysis_link():
    presets = default_pilot_ssh_presets("192.168.1.4", local_user="pilot-user")

    assert [(preset.name, preset.host, preset.username) for preset in presets[:2]] == [
        ("ROV", "192.168.1.4", "triton"),
        ("Analysis Laptop", "10.77.0.2", "pilot-user"),
    ]
