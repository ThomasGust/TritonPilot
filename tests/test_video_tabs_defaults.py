import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt6")

from PyQt6.QtWidgets import QApplication, QWidget

from gui.video_tabs import VideoTabs


class _DummyManager:
    def __init__(self, default_pane_order=None):
        self.default_pane_order = list(default_pane_order or [])


class _FakeSettings:
    def __init__(self, values=None):
        self.values = dict(values or {})

    def value(self, key, default=None):
        return self.values.get(key, default)

    def setValue(self, key, value):
        self.values[key] = value


class _DummyVideoWidget(QWidget):
    def __init__(self, _manager, stream_name: str, parent=None):
        super().__init__(parent)
        self.stream_name = stream_name

    def set_water_correction(self, enabled: bool) -> None:
        return

    def shutdown(self, release_only: bool = True):
        return


def _app() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def test_video_tabs_prefers_configured_default_pane_order_over_saved_assignments(monkeypatch):
    app = _app()
    fake_settings = _FakeSettings(
        {
            "video/layout_count": 4,
            "video/pane_stream_0": "Primary Camera",
            "video/pane_stream_1": "Back Gripper Camera",
            "video/pane_stream_2": "Arm Camera",
            "video/pane_stream_3": "Downward Camera",
        }
    )
    monkeypatch.setattr("gui.video_tabs.QSettings", lambda *args, **kwargs: fake_settings)
    monkeypatch.setattr("gui.video_tabs.VideoWidget", _DummyVideoWidget)

    tabs = VideoTabs(
        _DummyManager(
            default_pane_order=[
                "Primary Camera",
                "Back Gripper Camera",
                "Downward Camera",
                "Arm Camera",
            ]
        ),
        stream_names=[
            "Primary Camera",
            "Back Gripper Camera",
            "Downward Camera",
            "Arm Camera",
        ],
    )
    try:
        app.processEvents()
        assert tabs.visible_stream_names() == [
            "Primary Camera",
            "Back Gripper Camera",
            "Downward Camera",
            "Arm Camera",
        ]
    finally:
        tabs.close()
        tabs.deleteLater()
        app.processEvents()
