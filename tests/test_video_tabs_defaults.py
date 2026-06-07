import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt6")

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import QApplication, QWidget

from gui.video_tabs import VideoTabs


class _DummyManager:
    def __init__(self, default_pane_order=None, default_layout_count=None, stop_hidden_streams=None):
        self.default_pane_order = list(default_pane_order or [])
        self.default_layout_count = default_layout_count
        self.stop_hidden_streams = stop_hidden_streams


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
        self._recording = False
        self.display_fps_value = None

    def set_water_correction(self, enabled: bool) -> None:
        return

    def set_display_fps(self, fps: float) -> None:
        self.display_fps_value = float(fps)

    def is_recording(self) -> bool:
        return bool(self._recording)

    def shutdown(self, release_only: bool = True):
        return


class _ActivatingDummyVideoWidget(_DummyVideoWidget):
    activated = pyqtSignal()


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


def test_video_tabs_uses_tight_pane_spacing(monkeypatch):
    app = _app()
    fake_settings = _FakeSettings({"video/layout_count": 4})
    monkeypatch.setattr("gui.video_tabs.QSettings", lambda *args, **kwargs: fake_settings)
    monkeypatch.setattr("gui.video_tabs.VideoWidget", _DummyVideoWidget)

    tabs = VideoTabs(
        _DummyManager(default_pane_order=["Primary Camera", "Aux Camera"]),
        stream_names=["Primary Camera", "Aux Camera"],
    )
    try:
        app.processEvents()
        assert tabs._grid.spacing() == 0
        assert tabs.layout().spacing() == 1
    finally:
        tabs.close()
        tabs.deleteLater()
        app.processEvents()


def test_video_tabs_widget_activation_selects_matching_pane(monkeypatch):
    app = _app()
    monkeypatch.setattr("gui.video_tabs.QSettings", lambda *args, **kwargs: _FakeSettings())
    monkeypatch.setattr("gui.video_tabs.VideoWidget", _ActivatingDummyVideoWidget)

    tabs = VideoTabs(
        _DummyManager(
            default_pane_order=["Primary Camera", "Aux Camera"],
            default_layout_count=2,
        ),
        stream_names=["Primary Camera", "Aux Camera"],
    )
    try:
        app.processEvents()
        assert tabs.current_stream_name() == "Primary Camera"

        attach_calls = []
        for pane in tabs._panes:
            original = pane.attach_widget

            def _spy(widget, placeholder, *, _original=original):
                attach_calls.append((widget, placeholder))
                return _original(widget, placeholder)

            monkeypatch.setattr(pane, "attach_widget", _spy)

        widget = tabs._widgets["Aux Camera"]
        assert isinstance(widget, _ActivatingDummyVideoWidget)
        widget.activated.emit()
        app.processEvents()

        assert tabs.current_stream_name() == "Aux Camera"
        assert attach_calls == []
    finally:
        tabs.close()
        tabs.deleteLater()
        app.processEvents()


def test_video_tabs_reverse_layout_spans_rear_camera_without_saving(monkeypatch):
    app = _app()
    fake_settings = _FakeSettings({"video/layout_count": 4})
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
        snapshot = tabs.layout_snapshot()

        tabs.apply_temporary_layout(
            3,
            ["Back Gripper Camera", "Downward Camera", "Arm Camera"],
            active_name="Back Gripper Camera",
        )
        app.processEvents()

        assert tabs.layout_count() == 3
        assert tabs.visible_stream_names() == [
            "Back Gripper Camera",
            "Downward Camera",
            "Arm Camera",
        ]
        assert tabs.current_stream_name() == "Back Gripper Camera"
        assert tabs._grid.getItemPosition(0) == (0, 0, 2, 1)
        assert fake_settings.value("video/layout_count") == 4

        tabs.restore_layout_snapshot(snapshot)
        app.processEvents()

        assert tabs.layout_count() == 4
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


def test_video_tabs_uses_configured_default_layout_count(monkeypatch):
    app = _app()
    fake_settings = _FakeSettings({"video/layout_count": 4})
    monkeypatch.setattr("gui.video_tabs.QSettings", lambda *args, **kwargs: fake_settings)
    monkeypatch.setattr("gui.video_tabs.VideoWidget", _DummyVideoWidget)

    tabs = VideoTabs(
        _DummyManager(
            default_pane_order=["Primary Camera", "Aux Camera", "Arm Camera"],
            default_layout_count=1,
        ),
        stream_names=["Primary Camera", "Aux Camera", "Arm Camera"],
    )
    try:
        app.processEvents()
        assert tabs.layout_count() == 1
        assert tabs.visible_stream_names() == ["Primary Camera"]
        assert tabs._widgets["Primary Camera"] is not None
        assert tabs._widgets["Aux Camera"] is None
        assert tabs._widgets["Arm Camera"] is None
    finally:
        tabs.close()
        tabs.deleteLater()
        app.processEvents()


def test_video_tabs_stops_hidden_streams_after_layout_shrinks(monkeypatch):
    app = _app()
    fake_settings = _FakeSettings({"video/layout_count": 4})
    monkeypatch.setattr("gui.video_tabs.QSettings", lambda *args, **kwargs: fake_settings)
    monkeypatch.setattr("gui.video_tabs.VideoWidget", _DummyVideoWidget)

    tabs = VideoTabs(
        _DummyManager(default_pane_order=["Primary Camera", "Aux Camera", "Arm Camera"], stop_hidden_streams=True),
        stream_names=["Primary Camera", "Aux Camera", "Arm Camera"],
    )
    try:
        app.processEvents()
        assert all(tabs._widgets[name] is not None for name in tabs.visible_stream_names())

        tabs.set_layout_count(1)
        app.processEvents()

        assert tabs.visible_stream_names() == ["Primary Camera"]
        assert tabs._widgets["Primary Camera"] is not None
        assert tabs._widgets["Aux Camera"] is None
        assert tabs._widgets["Arm Camera"] is None
    finally:
        tabs.close()
        tabs.deleteLater()
        app.processEvents()


def test_video_tabs_keeps_hidden_streams_warm_without_duplicate_widgets(monkeypatch):
    app = _app()
    fake_settings = _FakeSettings({"video/layout_count": 4})
    monkeypatch.setattr("gui.video_tabs.QSettings", lambda *args, **kwargs: fake_settings)
    monkeypatch.setattr("gui.video_tabs.VideoWidget", _DummyVideoWidget)

    tabs = VideoTabs(
        _DummyManager(default_pane_order=["Primary Camera", "Aux Camera", "Arm Camera"], stop_hidden_streams=False),
        stream_names=["Primary Camera", "Aux Camera", "Arm Camera"],
    )
    try:
        app.processEvents()
        original_widgets = dict(tabs._widgets)
        assert all(widget is not None for widget in original_widgets.values())

        tabs.set_layout_count(1)
        app.processEvents()

        assert tabs.visible_stream_names() == ["Primary Camera"]
        assert tabs._widgets == original_widgets

        tabs.set_layout_count(4)
        app.processEvents()

        assert tabs.visible_stream_names() == ["Primary Camera", "Aux Camera", "Arm Camera"]
        assert tabs._widgets == original_widgets
    finally:
        tabs.close()
        tabs.deleteLater()
        app.processEvents()


def test_video_tabs_lowers_display_fps_for_multi_camera_layouts(monkeypatch):
    app = _app()
    fake_settings = _FakeSettings({"video/layout_count": 4})
    monkeypatch.setattr("gui.video_tabs.QSettings", lambda *args, **kwargs: fake_settings)
    monkeypatch.setattr("gui.video_tabs.VideoWidget", _DummyVideoWidget)
    monkeypatch.setattr("gui.video_tabs.VIDEO_DISPLAY_FPS_SINGLE", 30.0)
    monkeypatch.setattr("gui.video_tabs.VIDEO_DISPLAY_FPS_DUAL", 28.0)
    monkeypatch.setattr("gui.video_tabs.VIDEO_DISPLAY_FPS_MULTI", 24.0)

    tabs = VideoTabs(
        _DummyManager(default_pane_order=["Primary Camera", "Aux Camera", "Arm Camera", "Back Gripper Camera"]),
        stream_names=["Primary Camera", "Aux Camera", "Arm Camera", "Back Gripper Camera"],
    )
    try:
        app.processEvents()
        assert {widget.display_fps_value for widget in tabs._widgets.values()} == {24.0}

        tabs.set_layout_count(2)
        app.processEvents()
        assert {widget.display_fps_value for widget in tabs._widgets.values()} == {28.0}

        tabs.set_layout_count(1)
        app.processEvents()
        assert {widget.display_fps_value for widget in tabs._widgets.values()} == {30.0}
    finally:
        tabs.close()
        tabs.deleteLater()
        app.processEvents()


def test_video_tabs_can_suspend_and_resume_visible_streams(monkeypatch):
    app = _app()
    fake_settings = _FakeSettings({"video/layout_count": 2})
    monkeypatch.setattr("gui.video_tabs.QSettings", lambda *args, **kwargs: fake_settings)
    monkeypatch.setattr("gui.video_tabs.VideoWidget", _DummyVideoWidget)

    tabs = VideoTabs(
        _DummyManager(default_pane_order=["Primary Camera", "Aux Camera", "Arm Camera"]),
        stream_names=["Primary Camera", "Aux Camera", "Arm Camera"],
    )
    try:
        app.processEvents()
        assert all(tabs._widgets[name] is not None for name in tabs.visible_stream_names())

        assert tabs.suspend_all() is True
        assert all(widget is None for widget in tabs._widgets.values())

        tabs.resume_visible_streams()
        app.processEvents()
        assert all(tabs._widgets[name] is not None for name in tabs.visible_stream_names())
    finally:
        tabs.close()
        tabs.deleteLater()
        app.processEvents()
