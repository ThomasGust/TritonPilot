import os
import json
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest

pytest.importorskip("PyQt6")

from PyQt6.QtCore import QEvent, Qt, pyqtSignal
from PyQt6.QtGui import QImage, QKeyEvent
from PyQt6.QtWidgets import QApplication, QComboBox, QWidget

import gui.main_window as main_window
import gui.competition_clock as competition_clock
import gui.transect_page as transect_page
from video.cam import SnapshotImagePacket, StereoImagePairPacket


class _FakeSettings:
    def __init__(self):
        self.values = {}

    def value(self, key, default=None):
        return self.values.get(key, default)

    def setValue(self, key, value):
        self.values[key] = value

    def remove(self, key):
        self.values.pop(key, None)


class _FakePilotService:
    def __init__(self, *args, **kwargs):
        self.on_send = kwargs.get("on_send")
        self._reverse = False
        self.queued_edges = []
        self.axis_target_calls = []
        self.aux_axis_calls = []
        self.aux_axes = {}
        self.max_gain = 0.4
        self.back_gripper_gain = 0.5
        self.arm_gain = 0.5
        self.arm_kb_pitch_dir = 0.0
        self.arm_kb_wrist_dir = 0.0
        self.arm_kb_intent_calls = []
        self.arm_inputs_enabled_calls = []
        self.arm_park_pitch = -1.0
        self.arm_park_wrist = 1.0
        self.arm_position_calls = []
        self.arm_snap_to_park_calls = []
        self.arm_pitch = -1.0
        self.arm_wrist = 0.0

    def start(self):
        return None

    def stop(self):
        return None

    def is_reverse_enabled(self):
        return self._reverse

    def set_reverse_enabled(self, enabled):
        changed = self._reverse != bool(enabled)
        self._reverse = bool(enabled)
        return changed

    def set_aux_axis(self, name, value):
        self.aux_axis_calls.append((str(name), float(value)))
        self.aux_axes[str(name)] = float(value)
        return None

    def queue_edge(self, *_args, **_kwargs):
        name = _args[0] if _args else ""
        state = _args[1] if len(_args) > 1 else _kwargs.get("state", "down")
        self.queued_edges.append((name, state))
        return None

    def set_autopilot_axis_target(self, axis, target_deg, *, mode="hold"):
        self.axis_target_calls.append((axis, float(target_deg), mode))
        return True

    def current_modes(self):
        return {
            "max_gain": self.max_gain,
            "back_gripper_gain": self.back_gripper_gain,
            "t200_wrist_gain": self.back_gripper_gain,
            "arm_gain": self.arm_gain,
            "reverse": self._reverse,
        }

    def max_gain_step(self):
        return 0.05

    def max_gain_min(self):
        return 0.05

    def max_gain_max(self):
        return 0.8

    def set_max_gain(self, value):
        prev = self.max_gain
        self.max_gain = max(0.05, min(0.8, round(float(value), 2)))
        return self.max_gain != prev

    def adjust_max_gain(self, delta):
        prev = self.max_gain
        self.max_gain = max(0.05, min(0.8, round(self.max_gain + float(delta), 2)))
        return self.max_gain != prev

    def current_max_gain(self):
        return self.max_gain

    def back_gripper_gain_step(self):
        return 0.05

    def adjust_back_gripper_gain(self, delta):
        prev = self.back_gripper_gain
        self.back_gripper_gain = max(0.1, min(1.0, round(self.back_gripper_gain + float(delta), 2)))
        return self.back_gripper_gain != prev

    def current_back_gripper_gain(self):
        return self.back_gripper_gain

    def arm_gain_step(self):
        return 0.05

    def adjust_arm_gain(self, delta):
        prev = self.arm_gain
        self.arm_gain = max(0.1, min(1.0, round(self.arm_gain + float(delta), 2)))
        return self.arm_gain != prev

    def current_arm_gain(self):
        return self.arm_gain

    def set_arm_keyboard_intent(self, pitch_dir, wrist_dir):
        self.arm_kb_intent_calls.append((float(pitch_dir), float(wrist_dir)))
        self.arm_kb_pitch_dir = float(pitch_dir)
        self.arm_kb_wrist_dir = float(wrist_dir)
        return None

    def clear_arm_keyboard_intent(self):
        self.arm_kb_pitch_dir = 0.0
        self.arm_kb_wrist_dir = 0.0
        return None

    def set_arm_inputs_enabled(self, enabled):
        self.arm_inputs_enabled_calls.append(bool(enabled))
        return None

    def arm_position(self):
        return (self.arm_pitch, self.arm_wrist)

    def arm_park_position(self):
        return (self.arm_park_pitch, self.arm_park_wrist)

    def set_arm_position(self, pitch, wrist):
        self.arm_pitch = float(pitch)
        self.arm_wrist = float(wrist)
        self.arm_position_calls.append((self.arm_pitch, self.arm_wrist))
        return self.arm_pitch, self.arm_wrist

    def set_arm_park_position(self, pitch, wrist):
        self.arm_park_pitch = float(pitch)
        self.arm_park_wrist = float(wrist)
        return self.arm_park_pitch, self.arm_park_wrist

    def park_arm(self):
        return self.set_arm_position(self.arm_park_pitch, self.arm_park_wrist)

    def snap_arm_to_park(self):
        self.arm_snap_to_park_calls.append((self.arm_park_pitch, self.arm_park_wrist))
        return self.set_arm_position(self.arm_park_pitch, self.arm_park_wrist)

    def t200_wrist_gain_step(self):
        return self.back_gripper_gain_step()

    def adjust_t200_wrist_gain(self, delta):
        return self.adjust_back_gripper_gain(delta)

    def current_t200_wrist_gain(self):
        return self.current_back_gripper_gain()


class _FakeSensorService:
    def __init__(self, *args, **kwargs):
        pass

    def start(self):
        return None

    def stop(self):
        return None


class _FakeRemoteCameraManager:
    default_pane_order = [
        "Primary Camera",
        "Aux Camera",
        "Arm Camera",
        "Back Gripper Camera",
    ]

    def __init__(self, _path):
        self.stream_defs = {
            "Primary Camera": {"name": "Primary Camera", "width": 1920, "height": 1080, "fps": 30, "video_format": "h264", "port": 5000},
            "Aux Camera": {"name": "Aux Camera", "width": 1920, "height": 1080, "fps": 30, "video_format": "h264", "port": 5002},
            "Arm Camera": {"name": "Arm Camera", "width": 1920, "height": 1080, "fps": 30, "video_format": "h264", "port": 5001},
            "Back Gripper Camera": {"name": "Back Gripper Camera", "width": 1920, "height": 1080, "fps": 30, "video_format": "h264", "port": 5003},
        }

    def list_available(self):
        return list(self.default_pane_order)


class _FakeVideoWidget:
    def __init__(self):
        self.packet = None
        self.snapshot = None

    def status(self):
        return {"state": "playing", "age_s": 0.0}

    def latest_frame_packet(self):
        return self.packet

    def recent_frame_packets(self, *, max_age_s=0.5):
        return [] if self.packet is None else [self.packet]

    def snapshot_image(self):
        return self.snapshot


class _FakeVideoPanel(QWidget):
    selectionChanged = pyqtSignal()

    def __init__(self, _manager, stream_names, parent=None):
        super().__init__(parent)
        self.stream_names = list(stream_names)
        self._pane_count = 4
        self._active_pane_index = 0
        self._pane_streams = list(stream_names[:4])
        self.controls_visible = True
        self.controls_enabled = True
        self.apply_temporary_layout_calls = []
        self.restore_layout_snapshot_calls = []
        self.set_current_stream_calls = []
        self.rov_link_statuses = []
        self.tether_statuses = []
        self.square_display_enabled = False
        self.refresh_layout_count = 0
        self.snapshot_badges = []
        self.stop_hidden_streams_calls = []
        self._widgets = {name: _FakeVideoWidget() for name in self.stream_names}

    def _visible_count(self):
        return min(len(self.stream_names), int(self._pane_count))

    def set_layout_count(self, count):
        self._pane_count = int(count)

    def layout_count(self):
        return int(self._pane_count)

    def visible_stream_names(self):
        return list(self._pane_streams[: self._visible_count()])

    def current_stream_name(self):
        visible = self.visible_stream_names()
        if not visible:
            return None
        idx = max(0, min(self._active_pane_index, len(visible) - 1))
        return visible[idx]

    def current_video_widget(self):
        name = self.current_stream_name()
        return self._widgets.get(name) if name else None

    def video_widget_for_stream(self, name):
        return self._widgets.get(str(name))

    def set_layout_controls_visible(self, visible):
        self.controls_visible = bool(visible)

    def set_layout_controls_enabled(self, enabled):
        self.controls_enabled = bool(enabled)

    def has_stream(self, name):
        return name in self.stream_names

    def set_current_stream(self, name, *, save=True, emit=True):
        self.set_current_stream_calls.append((name, save, emit))
        if name in self.visible_stream_names():
            self._active_pane_index = self.visible_stream_names().index(name)
            return True
        return False

    def layout_snapshot(self):
        return {
            "pane_count": self._pane_count,
            "active_pane_index": self._active_pane_index,
            "pane_streams": list(self._pane_streams),
        }

    def apply_temporary_layout(self, *args, **kwargs):
        self.apply_temporary_layout_calls.append((args, kwargs))

    def restore_layout_snapshot(self, *args, **kwargs):
        self.restore_layout_snapshot_calls.append((args, kwargs))

    def set_square_display_enabled(self, enabled):
        self.square_display_enabled = bool(enabled)

    def refresh_layout_geometry(self):
        self.refresh_layout_count += 1

    def flash_snapshot_badge(self, stream_name=None):
        self.snapshot_badges.append(stream_name)

    def set_water_correction(self, *_args, **_kwargs):
        return None

    def set_rov_link_status(self, status):
        self.rov_link_statuses.append(str(status))

    def set_tether_status(self, ready, message=""):
        self.tether_statuses.append((bool(ready), str(message)))

    def is_stop_hidden_streams_enabled(self):
        return False

    def set_stop_hidden_streams(self, enabled):
        self.stop_hidden_streams_calls.append(bool(enabled))

    def stop_all(self):
        return None


class _SimplePage(QWidget):
    def __init__(self, *args, **kwargs):
        super().__init__()

    def refresh_state(self):
        return None

    def shutdown(self):
        return None


class _NoopThread:
    def __init__(self, *args, **kwargs):
        pass

    def start(self):
        return None


def _app():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.fixture(autouse=True)
def _disable_transfer_autostart(monkeypatch):
    monkeypatch.setenv("TRITON_PILOT_TRANSFER_AUTOSTART", "0")


def test_reverse_drive_page_keeps_pilot_video_layout(monkeypatch, tmp_path):
    app = _app()
    streams_path = tmp_path / "streams.json"
    streams_path.write_text("{}", encoding="utf-8")

    monkeypatch.setattr(main_window, "QSettings", lambda *args, **kwargs: _FakeSettings())
    monkeypatch.setattr(main_window, "PilotPublisherService", _FakePilotService)
    monkeypatch.setattr(main_window, "SensorSubscriberService", _FakeSensorService)
    monkeypatch.setattr(main_window, "RemoteCameraManager", _FakeRemoteCameraManager)
    monkeypatch.setattr(main_window, "VideoTabs", _FakeVideoPanel)
    monkeypatch.setattr(main_window, "HoldTestPanel", _SimplePage)
    monkeypatch.setattr(main_window, "ManagementPage", _SimplePage)
    monkeypatch.setattr(main_window.threading, "Thread", _NoopThread)

    win = main_window.MainWindow(str(streams_path))
    try:
        app.processEvents()
        panel = win.video_panel
        assert panel is not None
        assert [win._page_tabs.tabText(i) for i in range(win._page_tabs.count())] == [
            "Pilot",
            "Transect",
            "Hold Test",
            "Raw Sensors",
            "Vehicle Setup",
            "SSH",
        ]
        assert panel.layout_count() == 4
        assert panel.visible_stream_names() == [
            "Primary Camera",
            "Aux Camera",
            "Arm Camera",
            "Back Gripper Camera",
        ]

        win._set_center_page("reverse_drive", announce=False)
        app.processEvents()

        assert win._reverse_enabled is True
        assert panel.layout_count() == 4
        assert panel.visible_stream_names() == [
            "Primary Camera",
            "Aux Camera",
            "Arm Camera",
            "Back Gripper Camera",
        ]
        assert panel.apply_temporary_layout_calls == []
        assert panel.restore_layout_snapshot_calls == []
        assert panel.set_current_stream_calls == []
        assert panel.controls_visible is True
        assert panel.controls_enabled is True

        win._set_video_layout(2)
        assert panel.layout_count() == 2
        win._set_center_page("pilot", announce=False)
        app.processEvents()
        assert panel.layout_count() == 2
    finally:
        win.close()
        app.processEvents()


def test_transect_page_applies_square_single_camera_layout(monkeypatch, tmp_path):
    app = _app()
    streams_path = tmp_path / "streams.json"
    streams_path.write_text("{}", encoding="utf-8")

    monkeypatch.setattr(main_window, "QSettings", lambda *args, **kwargs: _FakeSettings())
    monkeypatch.setattr(main_window, "PilotPublisherService", _FakePilotService)
    monkeypatch.setattr(main_window, "SensorSubscriberService", _FakeSensorService)
    monkeypatch.setattr(main_window, "RemoteCameraManager", _FakeRemoteCameraManager)
    monkeypatch.setattr(main_window, "VideoTabs", _FakeVideoPanel)
    monkeypatch.setattr(main_window, "HoldTestPanel", _SimplePage)
    monkeypatch.setattr(main_window, "ManagementPage", _SimplePage)
    monkeypatch.setattr(main_window.threading, "Thread", _NoopThread)

    win = main_window.MainWindow(str(streams_path))
    try:
        app.processEvents()
        panel = win.video_panel
        assert panel is not None

        win._set_center_page("transect", announce=False)
        app.processEvents()

        assert win._active_page_name == "transect"
        assert win._page_tabs.tabText(win._page_tabs.currentIndex()) == "Transect"
        assert win._transect_page.camera_combo.focusPolicy() == Qt.FocusPolicy.NoFocus
        assert panel.controls_visible is False
        assert panel.controls_enabled is False
        assert panel.square_display_enabled is True
        assert win._transect_page.square_host.current_widget() is panel
        # Transect defaults to the arm camera (the square-aspect task feed).
        assert panel.apply_temporary_layout_calls[-1][0] == (1, ["Arm Camera"])
        assert panel.apply_temporary_layout_calls[-1][1]["active_name"] == "Arm Camera"
        assert panel.stop_hidden_streams_calls == []

        win._transect_page.set_current_stream("Aux Camera", emit=True)
        app.processEvents()
        assert panel.apply_temporary_layout_calls[-1][0] == (1, ["Aux Camera"])
        assert panel.apply_temporary_layout_calls[-1][1]["active_name"] == "Aux Camera"

        before_calls = len(panel.apply_temporary_layout_calls)
        win._transect_page.camera_combo.setFocus()
        key_event = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_A, Qt.KeyboardModifier.NoModifier, "a")
        assert win.eventFilter(win._transect_page.camera_combo, key_event) is True
        assert win._servo_wrist_keys_down == set()
        assert win.pilot_svc.arm_position_calls[-1] == pytest.approx((-1.0, 1.0))
        assert len(panel.apply_temporary_layout_calls) == before_calls
        assert win._transect_page.current_stream_name() == "Aux Camera"

        win._set_center_page("pilot", announce=False)
        app.processEvents()

        assert win._active_page_name == "pilot"
        assert panel.square_display_enabled is False
        assert panel.restore_layout_snapshot_calls
        assert panel.controls_visible is True
        assert panel.controls_enabled is True
        assert panel.isHidden() is False
        assert panel.refresh_layout_count > 0
    finally:
        win.close()
        app.processEvents()


def test_r_shortcut_toggles_reverse_without_leaving_pilot_page(monkeypatch, tmp_path):
    app = _app()
    streams_path = tmp_path / "streams.json"
    streams_path.write_text("{}", encoding="utf-8")

    monkeypatch.setattr(main_window, "QSettings", lambda *args, **kwargs: _FakeSettings())
    monkeypatch.setattr(main_window, "PilotPublisherService", _FakePilotService)
    monkeypatch.setattr(main_window, "SensorSubscriberService", _FakeSensorService)
    monkeypatch.setattr(main_window, "RemoteCameraManager", _FakeRemoteCameraManager)
    monkeypatch.setattr(main_window, "VideoTabs", _FakeVideoPanel)
    monkeypatch.setattr(main_window, "HoldTestPanel", _SimplePage)
    monkeypatch.setattr(main_window, "ManagementPage", _SimplePage)
    monkeypatch.setattr(main_window.threading, "Thread", _NoopThread)

    win = main_window.MainWindow(str(streams_path))
    try:
        app.processEvents()
        assert win._active_page_name == "pilot"
        assert win._page_tabs.tabText(win._page_tabs.currentIndex()) == "Pilot"
        before_size = win.size()
        before_video_rect = win._pilot_video_host.geometry()

        key_event = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_R, Qt.KeyboardModifier.NoModifier, "r")
        assert win.eventFilter(win, key_event) is True
        app.processEvents()

        assert win._reverse_enabled is True
        assert win.pilot_svc.is_reverse_enabled() is True
        assert win._active_page_name == "pilot"
        assert win._page_tabs.tabText(win._page_tabs.currentIndex()) == "Pilot"
        assert win.size() == before_size
        assert win._pilot_video_host.geometry() == before_video_rect
        assert "Mode: REVERSE" in win._mode_lbl.text()

        key_event = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_R, Qt.KeyboardModifier.NoModifier, "r")
        assert win.eventFilter(win, key_event) is True
        assert win._reverse_enabled is False
        assert win._active_page_name == "pilot"
    finally:
        win.close()
        app.processEvents()


def test_m_shortcut_starts_global_competition_clock_without_toggling_pause(monkeypatch, tmp_path):
    app = _app()
    streams_path = tmp_path / "streams.json"
    streams_path.write_text("{}", encoding="utf-8")
    now = [1000.0]

    monkeypatch.setattr(competition_clock, "monotonic", lambda: now[0])
    monkeypatch.setattr(main_window, "QSettings", lambda *args, **kwargs: _FakeSettings())
    monkeypatch.setattr(main_window, "PilotPublisherService", _FakePilotService)
    monkeypatch.setattr(main_window, "SensorSubscriberService", _FakeSensorService)
    monkeypatch.setattr(main_window, "RemoteCameraManager", _FakeRemoteCameraManager)
    monkeypatch.setattr(main_window, "VideoTabs", _FakeVideoPanel)
    monkeypatch.setattr(main_window, "HoldTestPanel", _SimplePage)
    monkeypatch.setattr(main_window, "ManagementPage", _SimplePage)
    monkeypatch.setattr(main_window.threading, "Thread", _NoopThread)

    win = main_window.MainWindow(str(streams_path))
    try:
        app.processEvents()
        clock = win._competition_clock
        assert clock.clock_label.text() == "15:00"
        assert clock.is_running() is False

        key_event = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_M, Qt.KeyboardModifier.NoModifier, "m")
        assert win.eventFilter(win, key_event) is True
        assert clock.is_running() is True

        now[0] = 1060.0
        clock._refresh_display()
        assert clock.clock_label.text() == "14:00"

        key_event = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_M, Qt.KeyboardModifier.NoModifier, "m")
        assert win.eventFilter(win, key_event) is True
        assert clock.is_running() is True
        assert clock.remaining_seconds() == pytest.approx(14 * 60)

        clock.toggle_btn.click()
        app.processEvents()
        assert clock.is_running() is False
        assert clock.clock_label.text() == "PAUSED 14:00"

        win._set_center_page("raw_sensors", announce=False)
        now[0] = 1080.0
        key_event = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_M, Qt.KeyboardModifier.NoModifier, "m")
        assert win.eventFilter(win, key_event) is True
        assert clock.is_running() is True
        assert win._active_page_name == "raw_sensors"
    finally:
        win.close()
        app.processEvents()


def test_transect_stopwatch_hotkeys_override_reverse_shortcut(monkeypatch, tmp_path):
    app = _app()
    streams_path = tmp_path / "streams.json"
    streams_path.write_text("{}", encoding="utf-8")
    now = [500.0]

    monkeypatch.setattr(transect_page, "monotonic", lambda: now[0])
    monkeypatch.setattr(main_window, "QSettings", lambda *args, **kwargs: _FakeSettings())
    monkeypatch.setattr(main_window, "PilotPublisherService", _FakePilotService)
    monkeypatch.setattr(main_window, "SensorSubscriberService", _FakeSensorService)
    monkeypatch.setattr(main_window, "RemoteCameraManager", _FakeRemoteCameraManager)
    monkeypatch.setattr(main_window, "VideoTabs", _FakeVideoPanel)
    monkeypatch.setattr(main_window, "HoldTestPanel", _SimplePage)
    monkeypatch.setattr(main_window, "ManagementPage", _SimplePage)
    monkeypatch.setattr(main_window.threading, "Thread", _NoopThread)

    win = main_window.MainWindow(str(streams_path))
    try:
        app.processEvents()
        win._set_center_page("transect", announce=False)
        app.processEvents()
        assert win._active_page_name == "transect"
        assert win._reverse_enabled is False

        assert win.eventFilter(win, QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_T, Qt.KeyboardModifier.NoModifier, "t")) is True
        assert win._transect_page.stopwatch_running() is True

        now[0] = 510.0
        assert win.eventFilter(win, QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_T, Qt.KeyboardModifier.NoModifier, "t")) is True
        assert win._transect_page.stopwatch_running() is False
        assert win._transect_page.stopwatch_elapsed_seconds() == pytest.approx(10.0)

        assert win.eventFilter(win, QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_R, Qt.KeyboardModifier.NoModifier, "r")) is True
        assert win._transect_page.stopwatch_running() is False
        assert win._transect_page.stopwatch_elapsed_seconds() == pytest.approx(0.0)
        assert "00:00.0" in win._transect_page.stopwatch_label.text()
        assert win._reverse_enabled is False
        assert win.pilot_svc.is_reverse_enabled() is False

        now[0] = 520.0
        assert win.eventFilter(win, QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_T, Qt.KeyboardModifier.NoModifier, "t")) is True
        assert win._transect_page.stopwatch_running() is True
        assert win._transect_page.stopwatch_elapsed_seconds() == pytest.approx(0.0)
    finally:
        win.close()
        app.processEvents()


def test_x_button_snapshots_selected_stream_into_app_session(monkeypatch, tmp_path):
    app = _app()
    streams_path = tmp_path / "streams.json"
    streams_path.write_text("{}", encoding="utf-8")
    save_root = tmp_path / "recordings"
    saved = []

    def _save_snapshot(self, image, target, stream_name):
        saved.append((image, Path(target), stream_name))

    monkeypatch.setattr(main_window, "QSettings", lambda *args, **kwargs: _FakeSettings())
    monkeypatch.setattr(main_window, "PilotPublisherService", _FakePilotService)
    monkeypatch.setattr(main_window, "SensorSubscriberService", _FakeSensorService)
    monkeypatch.setattr(main_window, "RemoteCameraManager", _FakeRemoteCameraManager)
    monkeypatch.setattr(main_window, "VideoTabs", _FakeVideoPanel)
    monkeypatch.setattr(main_window, "HoldTestPanel", _SimplePage)
    monkeypatch.setattr(main_window, "ManagementPage", _SimplePage)
    monkeypatch.setattr(main_window.threading, "Thread", _NoopThread)
    monkeypatch.setattr(main_window, "resolve_recordings_dir", lambda _preferred: main_window.SaveLocation(save_root))
    monkeypatch.setattr(main_window.MainWindow, "_save_snapshot_image_async", _save_snapshot)

    win = main_window.MainWindow(str(streams_path))
    try:
        app.processEvents()
        panel = win.video_panel
        assert panel is not None
        panel._active_pane_index = 2
        image = QImage(8, 4, QImage.Format.Format_RGB32)
        image.fill(0xFF336699)
        panel._widgets["Arm Camera"].snapshot = image

        win._handle_pilot_msg_on_ui({"edges": {"x": "down"}, "modes": {}})

        assert len(saved) == 1
        saved_image, target, stream_name = saved[0]
        assert saved_image.size() == image.size()
        assert stream_name == "Arm Camera"
        assert target.parent == win._app_session_dir
        assert target.parent.parent.resolve() == save_root.resolve()
        assert target.name.startswith("Arm_Camera_")
        assert target.suffix == ".png"
        assert panel.snapshot_badges == ["Arm Camera"]
    finally:
        win.close()
        app.processEvents()


def test_x_button_prefers_source_capture_over_widget_snapshot(monkeypatch, tmp_path):
    app = _app()
    streams_path = tmp_path / "streams.json"
    streams_path.write_text("{}", encoding="utf-8")
    save_root = tmp_path / "recordings"
    capture_jobs = []

    def _queue_source_capture(self, stream_name, target):
        capture_jobs.append((stream_name, Path(target)))

    def _unexpected_widget_save(*_args, **_kwargs):
        raise AssertionError("widget snapshot fallback should not be used when source capture exists")

    monkeypatch.setattr(main_window, "QSettings", lambda *args, **kwargs: _FakeSettings())
    monkeypatch.setattr(main_window, "PilotPublisherService", _FakePilotService)
    monkeypatch.setattr(main_window, "SensorSubscriberService", _FakeSensorService)
    monkeypatch.setattr(main_window, "RemoteCameraManager", _FakeRemoteCameraManager)
    monkeypatch.setattr(main_window, "VideoTabs", _FakeVideoPanel)
    monkeypatch.setattr(main_window, "HoldTestPanel", _SimplePage)
    monkeypatch.setattr(main_window, "ManagementPage", _SimplePage)
    monkeypatch.setattr(main_window.threading, "Thread", _NoopThread)
    monkeypatch.setattr(main_window, "resolve_recordings_dir", lambda _preferred: main_window.SaveLocation(save_root))
    monkeypatch.setattr(main_window.MainWindow, "_capture_and_save_snapshot_async", _queue_source_capture)
    monkeypatch.setattr(main_window.MainWindow, "_save_snapshot_image_async", _unexpected_widget_save)

    win = main_window.MainWindow(str(streams_path))
    try:
        app.processEvents()
        panel = win.video_panel
        assert panel is not None
        panel._widgets["Primary Camera"].snapshot = QImage(8, 4, QImage.Format.Format_RGB32)
        win.cam_mgr.capture_snapshot_frame = lambda *_args, **_kwargs: None

        win._handle_pilot_msg_on_ui({"edges": {"x": "down"}, "modes": {}})

        assert len(capture_jobs) == 1
        stream_name, target = capture_jobs[0]
        assert stream_name == "Primary Camera"
        assert target.parent == win._app_session_dir
        assert target.name.startswith("Primary_Camera_")
        assert panel.snapshot_badges == ["Primary Camera"]
    finally:
        win.close()
        app.processEvents()


def test_x_button_prefers_onboard_snapshot_bytes(monkeypatch, tmp_path):
    app = _app()
    streams_path = tmp_path / "streams.json"
    streams_path.write_text("{}", encoding="utf-8")
    save_root = tmp_path / "recordings"
    jpeg_bytes = b"\xff\xd8onboard snapshot\xff\xd9"
    calls = []

    class _ImmediateThread:
        def __init__(self, *, target, name=None, daemon=None):
            self.target = target

        def start(self):
            self.target()

    monkeypatch.setattr(main_window, "QSettings", lambda *args, **kwargs: _FakeSettings())
    monkeypatch.setattr(main_window, "PilotPublisherService", _FakePilotService)
    monkeypatch.setattr(main_window, "SensorSubscriberService", _FakeSensorService)
    monkeypatch.setattr(main_window, "RemoteCameraManager", _FakeRemoteCameraManager)
    monkeypatch.setattr(main_window, "VideoTabs", _FakeVideoPanel)
    monkeypatch.setattr(main_window, "HoldTestPanel", _SimplePage)
    monkeypatch.setattr(main_window, "ManagementPage", _SimplePage)
    monkeypatch.setattr(main_window.threading, "Thread", _NoopThread)
    monkeypatch.setattr(main_window, "resolve_recordings_dir", lambda _preferred: main_window.SaveLocation(save_root))

    win = main_window.MainWindow(str(streams_path))
    try:
        app.processEvents()
        panel = win.video_panel
        assert panel is not None

        def _capture_onboard_snapshot(name, *, timeout_s=2.0):
            calls.append((name, timeout_s))
            return SimpleNamespace(
                source_name=name,
                image_bytes=jpeg_bytes,
                mime_type="image/jpeg",
                extension="jpg",
                byte_count=len(jpeg_bytes),
            )

        def _unexpected_frame_capture(*_args, **_kwargs):
            raise AssertionError("frame fallback should not run when onboard snapshot succeeds")

        win.cam_mgr.capture_onboard_snapshot = _capture_onboard_snapshot
        win.cam_mgr.capture_snapshot_frame = _unexpected_frame_capture
        panel._active_pane_index = 1
        panel._widgets["Aux Camera"].snapshot = QImage(8, 4, QImage.Format.Format_RGB32)

        monkeypatch.setattr(main_window.threading, "Thread", _ImmediateThread)
        win._handle_pilot_msg_on_ui({"edges": {"x": "down"}, "modes": {}})
        monkeypatch.setattr(main_window.threading, "Thread", _NoopThread)
        app.processEvents()

        files = list(win._app_session_dir.glob("Aux_Camera_*.jpg"))
        assert calls == [("Aux Camera", 4.0)]
        assert len(files) == 1
        assert files[0].read_bytes() == jpeg_bytes
        assert files[0].parent.parent.resolve() == save_root.resolve()
        assert panel.snapshot_badges == ["Aux Camera"]
    finally:
        win.close()
        app.processEvents()


def test_keyboard_c_toggles_capture_mode_when_stereo_pair_exists(monkeypatch, tmp_path):
    app = _app()
    streams_path = tmp_path / "streams.json"
    streams_path.write_text(
        json.dumps(
            {
                "streams": [{"name": "Primary Camera"}, {"name": "Aux Camera"}],
                "stereo_pairs": [
                    {
                        "name": "Forward Stereo",
                        "left": "Primary Camera",
                        "right": "Aux Camera",
                        "rig_id": "rig-a",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(main_window, "QSettings", lambda *args, **kwargs: _FakeSettings())
    monkeypatch.setattr(main_window, "PilotPublisherService", _FakePilotService)
    monkeypatch.setattr(main_window, "SensorSubscriberService", _FakeSensorService)
    monkeypatch.setattr(main_window, "RemoteCameraManager", _FakeRemoteCameraManager)
    monkeypatch.setattr(main_window, "VideoTabs", _FakeVideoPanel)
    monkeypatch.setattr(main_window, "HoldTestPanel", _SimplePage)
    monkeypatch.setattr(main_window, "ManagementPage", _SimplePage)
    monkeypatch.setattr(main_window.threading, "Thread", _NoopThread)

    win = main_window.MainWindow(str(streams_path))
    try:
        app.processEvents()
        key_event = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_C, Qt.KeyboardModifier.NoModifier, "c")
        assert win.eventFilter(win, key_event) is True
        assert win._capture_mode == "stereo"

        key_event = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_C, Qt.KeyboardModifier.NoModifier, "c")
        assert win.eventFilter(win, key_event) is True
        assert win._capture_mode == "standard"
    finally:
        win.close()
        app.processEvents()


def test_keyboard_n_creates_stereo_session_and_x_saves_pair(monkeypatch, tmp_path):
    app = _app()
    streams_path = tmp_path / "streams.json"
    streams_path.write_text(
        json.dumps(
            {
                "streams": [{"name": "Primary Camera"}, {"name": "Aux Camera"}],
                "stereo_pairs": [
                    {
                        "name": "Forward Stereo",
                        "left": "Primary Camera",
                        "right": "Aux Camera",
                        "rig_id": "rig-a",
                        "max_pair_delta_ms": 50,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    save_root = tmp_path / "recordings"
    calls = []

    class _ImmediateThread:
        def __init__(self, *, target, name=None, daemon=None):
            self.target = target

        def start(self):
            self.target()

    monkeypatch.setattr(main_window, "QSettings", lambda *args, **kwargs: _FakeSettings())
    monkeypatch.setattr(main_window, "PilotPublisherService", _FakePilotService)
    monkeypatch.setattr(main_window, "SensorSubscriberService", _FakeSensorService)
    monkeypatch.setattr(main_window, "RemoteCameraManager", _FakeRemoteCameraManager)
    monkeypatch.setattr(main_window, "VideoTabs", _FakeVideoPanel)
    monkeypatch.setattr(main_window, "HoldTestPanel", _SimplePage)
    monkeypatch.setattr(main_window, "ManagementPage", _SimplePage)
    monkeypatch.setattr(main_window.threading, "Thread", _NoopThread)
    monkeypatch.setattr(main_window, "resolve_recordings_dir", lambda _preferred: main_window.SaveLocation(save_root))

    win = main_window.MainWindow(str(streams_path))
    try:
        app.processEvents()

        def _capture_onboard_stereo_pair(left, right, *, timeout_s=2.0, max_pair_delta_ms=50.0):
            calls.append((left, right, timeout_s, max_pair_delta_ms))
            return StereoImagePairPacket(
                left=SnapshotImagePacket(
                    source_name=left,
                    image_bytes=b"left-jpeg",
                    mime_type="image/jpeg",
                    extension="jpg",
                    wall_ts=1000.0,
                    monotonic_ts=50.0,
                    byte_count=len(b"left-jpeg"),
                    seq=1,
                    shape=(1080, 1920, 3),
                ),
                right=SnapshotImagePacket(
                    source_name=right,
                    image_bytes=b"right-jpeg",
                    mime_type="image/jpeg",
                    extension="jpg",
                    wall_ts=1000.008,
                    monotonic_ts=50.008,
                    byte_count=len(b"right-jpeg"),
                    seq=2,
                    shape=(1080, 1920, 3),
                ),
                pair_delta_ms=8.0,
                timestamp_source="rov_snapshot_appsink_fresh_monotonic",
            )

        win.cam_mgr.capture_onboard_stereo_pair = _capture_onboard_stereo_pair
        key_event = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_N, Qt.KeyboardModifier.NoModifier, "n")
        assert win.eventFilter(win, key_event) is True
        assert win._capture_mode == "stereo"
        assert win._stereo_capture_session is not None
        session_dir = win._stereo_capture_session.session_dir

        monkeypatch.setattr(main_window.threading, "Thread", _ImmediateThread)
        win._handle_pilot_msg_on_ui({"edges": {"x": "down"}, "modes": {}})
        monkeypatch.setattr(main_window.threading, "Thread", _NoopThread)
        app.processEvents()

        assert calls == [("Primary Camera", "Aux Camera", 5.0, 50.0)]
        assert (session_dir / "left" / "pair_000001_left.jpg").read_bytes() == b"left-jpeg"
        assert (session_dir / "right" / "pair_000001_right.jpg").read_bytes() == b"right-jpeg"
        manifest = json.loads((session_dir / "manifest.json").read_text(encoding="utf-8"))
        assert manifest["frames"][0]["pair_delta_ms"] == pytest.approx(8.0)
        assert manifest["frames"][0]["left_path"] == "left\\pair_000001_left.jpg"
        assert manifest["frames"][0]["right_path"] == "right\\pair_000001_right.jpg"
        assert session_dir.parent.name == "stereo_sessions"
        assert session_dir.parent.parent.parent.resolve() == save_root.resolve()
        assert win.video_panel.snapshot_badges == ["Primary Camera", "Aux Camera"]
    finally:
        win.close()
        app.processEvents()


def test_non_down_x_edge_does_not_snapshot(monkeypatch, tmp_path):
    app = _app()
    streams_path = tmp_path / "streams.json"
    streams_path.write_text("{}", encoding="utf-8")
    saved = []

    monkeypatch.setattr(main_window, "QSettings", lambda *args, **kwargs: _FakeSettings())
    monkeypatch.setattr(main_window, "PilotPublisherService", _FakePilotService)
    monkeypatch.setattr(main_window, "SensorSubscriberService", _FakeSensorService)
    monkeypatch.setattr(main_window, "RemoteCameraManager", _FakeRemoteCameraManager)
    monkeypatch.setattr(main_window, "VideoTabs", _FakeVideoPanel)
    monkeypatch.setattr(main_window, "HoldTestPanel", _SimplePage)
    monkeypatch.setattr(main_window, "ManagementPage", _SimplePage)
    monkeypatch.setattr(main_window.threading, "Thread", _NoopThread)
    monkeypatch.setattr(main_window.MainWindow, "_save_snapshot_image_async", lambda *args, **kwargs: saved.append(args))

    win = main_window.MainWindow(str(streams_path))
    try:
        app.processEvents()
        panel = win.video_panel
        assert panel is not None
        image = QImage(2, 2, QImage.Format.Format_RGB32)
        image.fill(0xFFFFFFFF)
        panel._widgets["Primary Camera"].snapshot = image

        win._handle_pilot_msg_on_ui({"edges": {"x": "up"}, "modes": {}})

        assert saved == []
        assert panel.snapshot_badges == []
    finally:
        win.close()
        app.processEvents()


def test_engaging_optical_hold_auto_records_the_arm_camera(monkeypatch, tmp_path):
    app = _app()
    streams_path = tmp_path / "streams.json"
    streams_path.write_text("{}", encoding="utf-8")
    save_root = tmp_path / "recordings"

    monkeypatch.setattr(main_window, "QSettings", lambda *args, **kwargs: _FakeSettings())
    monkeypatch.setattr(main_window, "PilotPublisherService", _FakePilotService)
    monkeypatch.setattr(main_window, "SensorSubscriberService", _FakeSensorService)
    monkeypatch.setattr(main_window, "RemoteCameraManager", _FakeRemoteCameraManager)
    monkeypatch.setattr(main_window, "VideoTabs", _FakeVideoPanel)
    monkeypatch.setattr(main_window, "HoldTestPanel", _SimplePage)
    monkeypatch.setattr(main_window, "ManagementPage", _SimplePage)
    monkeypatch.setattr(main_window.threading, "Thread", _NoopThread)
    monkeypatch.setattr(main_window, "resolve_recordings_dir", lambda _preferred: main_window.SaveLocation(save_root))

    win = main_window.MainWindow(str(streams_path))
    try:
        app.processEvents()
        # Skip the synchronized-log machinery for this unit test.
        win._stream_recorder = object()

        # Records the arm/transect camera regardless of which pane is selected.
        win.video_panel._active_pane_index = 0  # Primary Camera selected
        assert win._resolve_hold_recording_stream() == "Arm Camera"

        # Engage -> auto-start a recording, owned by the hold.
        win._auto_record_hold(True)
        assert win._hold_owns_recording is True
        assert win._video_recording_stream == "Arm Camera"
        assert win._video_recording_busy is True

        # Engaging again while already recording does not start a second / take over.
        win._video_recording = True
        win._video_recording_busy = False
        win._hold_owns_recording = False
        win._video_recording_stream = "Arm Camera"
        win._auto_record_hold(True)
        assert win._hold_owns_recording is False
    finally:
        win.close()
        app.processEvents()


def test_disengaging_optical_hold_stops_the_hold_owned_recording(monkeypatch, tmp_path):
    app = _app()
    streams_path = tmp_path / "streams.json"
    streams_path.write_text("{}", encoding="utf-8")

    monkeypatch.setattr(main_window, "QSettings", lambda *args, **kwargs: _FakeSettings())
    monkeypatch.setattr(main_window, "PilotPublisherService", _FakePilotService)
    monkeypatch.setattr(main_window, "SensorSubscriberService", _FakeSensorService)
    monkeypatch.setattr(main_window, "RemoteCameraManager", _FakeRemoteCameraManager)
    monkeypatch.setattr(main_window, "VideoTabs", _FakeVideoPanel)
    monkeypatch.setattr(main_window, "HoldTestPanel", _SimplePage)
    monkeypatch.setattr(main_window, "ManagementPage", _SimplePage)
    monkeypatch.setattr(main_window.threading, "Thread", _NoopThread)

    win = main_window.MainWindow(str(streams_path))
    try:
        app.processEvents()
        stops = []
        monkeypatch.setattr(win, "_stop_video_recording", lambda: stops.append(True))

        # A hold-owned recording that is live -> disengage stops it.
        win._hold_owns_recording = True
        win._video_recording = True
        win._auto_record_hold(False)
        assert stops == [True]
        assert win._hold_owns_recording is False

        # A manually-started recording (not hold-owned) is left running.
        stops.clear()
        win._hold_owns_recording = False
        win._video_recording = True
        win._auto_record_hold(False)
        assert stops == []

        # Disengage before the start worker finished -> defer the stop.
        win._hold_owns_recording = True
        win._video_recording = False
        win._video_recording_busy = True
        win._auto_record_hold(False)
        assert win._hold_recording_stop_pending is True
    finally:
        win.close()
        app.processEvents()


def test_snapshot_path_uses_stream_name_timestamp_and_collision_suffix(tmp_path):
    session = tmp_path / "20260617-120000"
    session.mkdir()

    first = main_window.MainWindow._snapshot_path(session, "Port / Aux: Camera?", now=1_800_000_000.125)
    first.write_bytes(b"existing")
    second = main_window.MainWindow._snapshot_path(session, "Port / Aux: Camera?", now=1_800_000_000.125)

    assert first.name.startswith("Port_Aux_Camera_")
    assert first.name.endswith("-125.png")
    assert second.name == first.with_name(first.stem + "_02.png").name


def test_qimage_from_bgr_frame_uses_camera_pixels_not_overlay():
    frame = np.array([[[1, 2, 3], [10, 20, 30]]], dtype=np.uint8)

    image = main_window.MainWindow._qimage_from_bgr_frame(frame)

    assert image.width() == 2
    assert image.height() == 1
    first = image.pixelColor(0, 0)
    assert (first.red(), first.green(), first.blue()) == (3, 2, 1)


def test_analysis_transfer_status_bar_shows_served_root(monkeypatch, tmp_path):
    app = _app()
    streams_path = tmp_path / "streams.json"
    streams_path.write_text("{}", encoding="utf-8")
    save_root = tmp_path / "recordings"

    class _FakeTransferServer:
        server_address = ("127.0.0.1", 49123)

        def __init__(self, root):
            self.root = Path(root)
            self.shutdown_called = False

        def request_snapshot(self):
            return {"request_count": 1, "last_request_ts": main_window.time.time(), "last_request_path": "/events"}

        def shutdown(self):
            self.shutdown_called = True

        def server_close(self):
            return None

    class _JoinableThread:
        def join(self, timeout=None):
            return None

    servers = []

    def _create_server(**kwargs):
        server = _FakeTransferServer(kwargs["root"])
        servers.append((server, kwargs))
        return server

    monkeypatch.setenv("TRITON_PILOT_TRANSFER_AUTOSTART", "1")
    monkeypatch.setenv("TRITON_PILOT_TRANSFER_HOST", "127.0.0.1")
    monkeypatch.setenv("TRITON_PILOT_TRANSFER_PORT", "49123")
    monkeypatch.setattr(main_window, "QSettings", lambda *args, **kwargs: _FakeSettings())
    monkeypatch.setattr(main_window, "PilotPublisherService", _FakePilotService)
    monkeypatch.setattr(main_window, "SensorSubscriberService", _FakeSensorService)
    monkeypatch.setattr(main_window, "RemoteCameraManager", _FakeRemoteCameraManager)
    monkeypatch.setattr(main_window, "VideoTabs", _FakeVideoPanel)
    monkeypatch.setattr(main_window, "HoldTestPanel", _SimplePage)
    monkeypatch.setattr(main_window, "ManagementPage", _SimplePage)
    monkeypatch.setattr(main_window.threading, "Thread", _NoopThread)
    monkeypatch.setattr(main_window, "create_server", _create_server)
    monkeypatch.setattr(main_window, "start_server_in_thread", lambda _server: _JoinableThread())
    monkeypatch.setattr(main_window, "build_index", lambda *_args, **_kwargs: {"file_count": 3, "total_bytes": 5_242_880})
    monkeypatch.setattr(main_window, "resolve_recordings_dir", lambda _preferred: main_window.SaveLocation(save_root))

    win = main_window.MainWindow(str(streams_path))
    try:
        app.processEvents()
        assert servers
        assert servers[0][1]["root"] == save_root.resolve()
        deadline = main_window.time.time() + 2.0
        while "3 files/5.0 MB" not in win._analysis_transfer_lbl.text() and main_window.time.time() < deadline:
            app.processEvents()
            main_window.time.sleep(0.01)
        text = win._analysis_transfer_lbl.text()
        assert "Analysis Share: ON http://127.0.0.1:49123" in text
        assert "recordings" in text
        assert "3 files/5.0 MB" in text
        assert "Analysis listening" in text
        assert win._analysis_transfer_line.text() == text
        assert win._analysis_transfer_line.isHidden() is True
        assert win._analysis_transfer_line.wordWrap() is True
        assert win._analysis_transfer_line.toolTip() == text
        assert win.pilot_telemetry_column.analysis_text.text() == text
    finally:
        win.close()
        app.processEvents()
        assert servers[0][0].shutdown_called is True


def test_analysis_transfer_advertise_host_prefers_configured_link(monkeypatch):
    monkeypatch.setattr(
        main_window,
        "list_local_ipv4_addrs",
        lambda: [
            main_window.LocalAddr(ip="172.16.111.159", iface="Wi-Fi", is_wifi=True),
            main_window.LocalAddr(ip="192.168.1.1", iface="Ethernet 8", is_wifi=False),
            main_window.LocalAddr(ip="10.77.0.1", iface="Ethernet 8", is_wifi=False),
        ],
    )

    assert main_window.MainWindow._default_analysis_transfer_advertise_host() == "10.77.0.1"


def test_analysis_transfer_display_url_caches_auto_host(monkeypatch):
    calls = []

    def _fake_default_host():
        calls.append("called")
        return "10.77.0.1"

    monkeypatch.setattr(main_window.MainWindow, "_default_analysis_transfer_advertise_host", staticmethod(_fake_default_host))

    win = main_window.MainWindow.__new__(main_window.MainWindow)
    win._analysis_transfer_server = None
    win._analysis_transfer_port = 8765
    win._analysis_transfer_advertise_host = ""
    win._analysis_transfer_host = "0.0.0.0"
    win._analysis_transfer_resolved_advertise_host = ""

    assert win._analysis_transfer_display_url() == "http://10.77.0.1:8765"
    assert win._analysis_transfer_display_url() == "http://10.77.0.1:8765"
    assert calls == ["called"]


def test_pilot_page_adds_compact_telemetry_column_and_frees_status_bar(monkeypatch, tmp_path):
    app = _app()
    streams_path = tmp_path / "streams.json"
    streams_path.write_text("{}", encoding="utf-8")

    monkeypatch.setattr(main_window, "QSettings", lambda *args, **kwargs: _FakeSettings())
    monkeypatch.setattr(main_window, "PilotPublisherService", _FakePilotService)
    monkeypatch.setattr(main_window, "SensorSubscriberService", _FakeSensorService)
    monkeypatch.setattr(main_window, "RemoteCameraManager", _FakeRemoteCameraManager)
    monkeypatch.setattr(main_window, "VideoTabs", _FakeVideoPanel)
    monkeypatch.setattr(main_window, "HoldTestPanel", _SimplePage)
    monkeypatch.setattr(main_window, "ManagementPage", _SimplePage)
    monkeypatch.setattr(main_window.threading, "Thread", _NoopThread)

    win = main_window.MainWindow(str(streams_path))
    try:
        app.processEvents()
        pilot_layout = win._pilot_page.layout()
        assert pilot_layout.indexOf(win._pilot_video_host) < pilot_layout.indexOf(win.pilot_telemetry_scroll)
        assert win.pilot_telemetry_column.minimumWidth() >= 200
        assert win.pilot_telemetry_scroll.widget() is win.pilot_telemetry_column
        assert win._analysis_transfer_lbl.parent() is win
        assert win._depth_lbl.parent() is win
        assert win._analysis_transfer_line.isHidden() is True

        win._handle_sensor_msg_on_ui(
            {
                "type": "attitude",
                "sensor": "roll_pitch_estimator",
                "roll_deg": 4.0,
                "pitch_deg": -2.0,
                "yaw_deg": 91.0,
            }
        )
        win._handle_sensor_msg_on_ui(
            {
                "type": "external_depth",
                "sensor": "external_depth",
                "depth_m": 1.25,
                "pressure_mbar": 1125.0,
                "temperature_c": 12.5,
            }
        )
        win._flush_sensor_ui()
        assert win.pilot_telemetry_column.attitude_indicator.roll_deg == pytest.approx(4.0)
        assert win.pilot_telemetry_column.attitude_indicator.pitch_deg == pytest.approx(-2.0)
        assert win.pilot_telemetry_column.attitude_indicator.yaw_deg == pytest.approx(91.0)
        assert win.pilot_telemetry_column.depth_gauge.value == pytest.approx(1.25)
        assert "1.25 m" in win.pilot_telemetry_column.depth_text.text()
        assert "Analysis Share: OFF" in win.pilot_telemetry_column.analysis_text.text()
    finally:
        win.close()
        app.processEvents()


def test_heartbeat_loss_and_recovery_notify_video_panel(monkeypatch, tmp_path):
    app = _app()
    streams_path = tmp_path / "streams.json"
    streams_path.write_text("{}", encoding="utf-8")

    now = {"value": 1_000.0}
    monkeypatch.setattr(main_window.time, "time", lambda: now["value"])
    monkeypatch.setattr(main_window, "QSettings", lambda *args, **kwargs: _FakeSettings())
    monkeypatch.setattr(main_window, "PilotPublisherService", _FakePilotService)
    monkeypatch.setattr(main_window, "SensorSubscriberService", _FakeSensorService)
    monkeypatch.setattr(main_window, "RemoteCameraManager", _FakeRemoteCameraManager)
    monkeypatch.setattr(main_window, "VideoTabs", _FakeVideoPanel)
    monkeypatch.setattr(main_window, "HoldTestPanel", _SimplePage)
    monkeypatch.setattr(main_window, "ManagementPage", _SimplePage)
    monkeypatch.setattr(main_window.threading, "Thread", _NoopThread)

    win = main_window.MainWindow(str(streams_path))
    try:
        app.processEvents()
        panel = win.video_panel
        assert panel is not None

        win._handle_sensor_msg_on_ui({"type": "heartbeat", "sensor": "heartbeat", "armed": False})
        win._update_link_status()
        assert panel.rov_link_statuses[-1] == "OK"

        now["value"] += 6.0
        win._update_link_status()
        assert panel.rov_link_statuses[-1] == "LOST"
        assert "Heartbeat: LOST" in win._link_lbl.text()

        now["value"] += 0.1
        win._handle_sensor_msg_on_ui({"type": "heartbeat", "sensor": "heartbeat", "armed": False})
        win._update_link_status()
        assert panel.rov_link_statuses[-1] == "OK"
    finally:
        win.close()
        app.processEvents()


def test_tether_banner_blocks_video_until_tether_recovers(monkeypatch, tmp_path):
    app = _app()
    streams_path = tmp_path / "streams.json"
    streams_path.write_text("{}", encoding="utf-8")

    monkeypatch.setattr(main_window, "QSettings", lambda *args, **kwargs: _FakeSettings())
    monkeypatch.setattr(main_window, "PilotPublisherService", _FakePilotService)
    monkeypatch.setattr(main_window, "SensorSubscriberService", _FakeSensorService)
    monkeypatch.setattr(main_window, "RemoteCameraManager", _FakeRemoteCameraManager)
    monkeypatch.setattr(main_window, "VideoTabs", _FakeVideoPanel)
    monkeypatch.setattr(main_window, "HoldTestPanel", _SimplePage)
    monkeypatch.setattr(main_window, "ManagementPage", _SimplePage)
    monkeypatch.setattr(main_window.threading, "Thread", _NoopThread)
    audio_cues = []
    monkeypatch.setattr(
        main_window.MainWindow,
        "_play_tether_audio_cue",
        lambda _self, ready: audio_cues.append(bool(ready)),
    )

    win = main_window.MainWindow(str(streams_path))
    try:
        app.processEvents()
        panel = win.video_panel
        assert panel is not None

        win._set_tether_status_snapshot(
            {
                "ts": 1.0,
                "ready": False,
                "host": "192.168.1.4",
                "local_ip": None,
                "iface": None,
                "port": None,
                "reason": "local tether IP 192.168.1.1 missing",
            }
        )
        win._refresh_tether_status_ui()

        assert "TETHER NETWORK UNREACHABLE" in win._tether_banner.text()
        assert win._tether_banner.isHidden() is False
        assert win._tether_top_lbl.property("tone") == "alert"
        assert panel.tether_statuses[-1][0] is False

        win._set_tether_status_snapshot(
            {
                "ts": 2.0,
                "ready": True,
                "host": "192.168.1.4",
                "local_ip": "192.168.1.1",
                "iface": "Ethernet 8",
                "port": 5555,
                "reason": "",
            }
        )
        win._refresh_tether_status_ui()

        assert "Tether: OK" in win._tether_top_lbl.text()
        assert win._tether_banner.isHidden() is True
        assert win._tether_top_lbl.property("tone") == "ok"
        assert panel.tether_statuses[-1][0] is True
        assert audio_cues == [True]

        win._set_tether_status_snapshot(
            {
                "ts": 3.0,
                "ready": False,
                "host": "192.168.1.4",
                "local_ip": None,
                "iface": None,
                "port": None,
                "reason": "192.168.1.4 not reachable on ports 5555",
            }
        )
        win._refresh_tether_status_ui()

        assert "TETHER NETWORK UNREACHABLE" in win._tether_banner.text()
        assert win._tether_banner.isHidden() is False
        assert panel.tether_statuses[-1][0] is False
        assert audio_cues == [True, False]
    finally:
        win.close()
        app.processEvents()



def test_arm_disarm_backup_controls_queue_menu_edge(monkeypatch, tmp_path):
    app = _app()
    streams_path = tmp_path / "streams.json"
    streams_path.write_text("{}", encoding="utf-8")

    monkeypatch.setattr(main_window, "QSettings", lambda *args, **kwargs: _FakeSettings())
    monkeypatch.setattr(main_window, "PilotPublisherService", _FakePilotService)
    monkeypatch.setattr(main_window, "SensorSubscriberService", _FakeSensorService)
    monkeypatch.setattr(main_window, "RemoteCameraManager", _FakeRemoteCameraManager)
    monkeypatch.setattr(main_window, "VideoTabs", _FakeVideoPanel)
    monkeypatch.setattr(main_window, "HoldTestPanel", _SimplePage)
    monkeypatch.setattr(main_window, "ManagementPage", _SimplePage)
    monkeypatch.setattr(main_window.threading, "Thread", _NoopThread)

    win = main_window.MainWindow(str(streams_path))
    try:
        app.processEvents()
        assert win._arm_disarm_btn.text() == "Arm/Disarm (O)"

        key_event = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_O, Qt.KeyboardModifier.NoModifier, "o")
        win.eventFilter(win, key_event)
        assert win.pilot_svc.queued_edges[-1] == ("menu", "down")

        win._arm_disarm_btn.click()
        assert win.pilot_svc.queued_edges[-1] == ("menu", "down")

        win._handle_sensor_msg_on_ui({"type": "heartbeat", "sensor": "heartbeat", "armed": False})
        assert win._arm_disarm_btn.text() == "Arm (O)"
        assert win.pilot_svc.arm_inputs_enabled_calls[-1] is False
        assert win.pilot_svc.arm_snap_to_park_calls[-1] == pytest.approx((-1.0, 1.0))
        snap_count = len(win.pilot_svc.arm_snap_to_park_calls)
        win._handle_sensor_msg_on_ui({"type": "heartbeat", "sensor": "heartbeat", "armed": True})
        assert win._arm_disarm_btn.text() == "Disarm (O)"
        assert win.pilot_svc.arm_inputs_enabled_calls[-1] is True
        assert len(win.pilot_svc.arm_snap_to_park_calls) == snap_count
    finally:
        win.close()
        app.processEvents()


def test_keyboard_vehicle_shortcuts_are_suppressed_for_ssh_and_text_input(monkeypatch, tmp_path):
    app = _app()
    streams_path = tmp_path / "streams.json"
    streams_path.write_text("{}", encoding="utf-8")

    monkeypatch.setattr(main_window, "QSettings", lambda *args, **kwargs: _FakeSettings())
    monkeypatch.setattr(main_window, "PilotPublisherService", _FakePilotService)
    monkeypatch.setattr(main_window, "SensorSubscriberService", _FakeSensorService)
    monkeypatch.setattr(main_window, "RemoteCameraManager", _FakeRemoteCameraManager)
    monkeypatch.setattr(main_window, "VideoTabs", _FakeVideoPanel)
    monkeypatch.setattr(main_window, "HoldTestPanel", _SimplePage)
    monkeypatch.setattr(main_window, "ManagementPage", _SimplePage)
    monkeypatch.setattr(main_window.threading, "Thread", _NoopThread)

    win = main_window.MainWindow(str(streams_path))
    try:
        app.processEvents()

        win._set_center_page("ssh", announce=False)
        assert win._active_page_name == "ssh"

        ssh_input = win._ssh_page.command_edit
        win.eventFilter(ssh_input, QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_O, Qt.KeyboardModifier.NoModifier, "o"))
        win.eventFilter(ssh_input, QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_L, Qt.KeyboardModifier.NoModifier, "l"))
        win.eventFilter(ssh_input, QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_W, Qt.KeyboardModifier.NoModifier, "w"))

        assert win.pilot_svc.queued_edges == []
        assert "W" not in win._servo_wrist_keys_down

        win._set_center_page("pilot", announce=False)
        win.eventFilter(ssh_input, QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_O, Qt.KeyboardModifier.NoModifier, "o"))
        assert win.pilot_svc.queued_edges == []

        win.eventFilter(win, QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_O, Qt.KeyboardModifier.NoModifier, "o"))
        assert win.pilot_svc.queued_edges[-1] == ("menu", "down")
    finally:
        win.close()
        app.processEvents()


def test_switching_to_ssh_releases_keyboard_wrist_controls(monkeypatch, tmp_path):
    app = _app()
    streams_path = tmp_path / "streams.json"
    streams_path.write_text("{}", encoding="utf-8")

    monkeypatch.setattr(main_window, "QSettings", lambda *args, **kwargs: _FakeSettings())
    monkeypatch.setattr(main_window, "PilotPublisherService", _FakePilotService)
    monkeypatch.setattr(main_window, "SensorSubscriberService", _FakeSensorService)
    monkeypatch.setattr(main_window, "RemoteCameraManager", _FakeRemoteCameraManager)
    monkeypatch.setattr(main_window, "VideoTabs", _FakeVideoPanel)
    monkeypatch.setattr(main_window, "HoldTestPanel", _SimplePage)
    monkeypatch.setattr(main_window, "ManagementPage", _SimplePage)
    monkeypatch.setattr(main_window.threading, "Thread", _NoopThread)

    win = main_window.MainWindow(str(streams_path))
    try:
        app.processEvents()

        win._servo_wrist_keys_down = {"W", "D"}
        win.pilot_svc.set_arm_keyboard_intent(1.0, 1.0)

        win._set_center_page("ssh", announce=False)

        assert win._servo_wrist_keys_down == set()
        assert win.pilot_svc.arm_kb_pitch_dir == pytest.approx(0.0)
        assert win.pilot_svc.arm_kb_wrist_dir == pytest.approx(0.0)
    finally:
        win.close()
        app.processEvents()


def test_a_key_parks_arm_without_reenabling_wasd_jog(monkeypatch, tmp_path):
    app = _app()
    streams_path = tmp_path / "streams.json"
    streams_path.write_text("{}", encoding="utf-8")

    monkeypatch.setattr(main_window, "QSettings", lambda *args, **kwargs: _FakeSettings())
    monkeypatch.setattr(main_window, "PilotPublisherService", _FakePilotService)
    monkeypatch.setattr(main_window, "SensorSubscriberService", _FakeSensorService)
    monkeypatch.setattr(main_window, "RemoteCameraManager", _FakeRemoteCameraManager)
    monkeypatch.setattr(main_window, "VideoTabs", _FakeVideoPanel)
    monkeypatch.setattr(main_window, "HoldTestPanel", _SimplePage)
    monkeypatch.setattr(main_window, "ManagementPage", _SimplePage)
    monkeypatch.setattr(main_window.threading, "Thread", _NoopThread)

    win = main_window.MainWindow(str(streams_path))
    try:
        app.processEvents()

        for key, text in (
            (Qt.Key.Key_W, "w"),
            (Qt.Key.Key_S, "s"),
            (Qt.Key.Key_D, "d"),
        ):
            handled = win.eventFilter(
                win,
                QKeyEvent(QEvent.Type.KeyPress, key, Qt.KeyboardModifier.NoModifier, text),
            )
            assert handled is False

        win.pilot_svc.set_arm_park_position(-0.25, 0.75)
        handled = win.eventFilter(
            win,
            QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_A, Qt.KeyboardModifier.NoModifier, "a"),
        )
        assert handled is True

        assert win._servo_wrist_keys_down == set()
        assert win.pilot_svc.arm_kb_intent_calls == []
        assert win.pilot_svc.arm_kb_pitch_dir == pytest.approx(0.0)
        assert win.pilot_svc.arm_kb_wrist_dir == pytest.approx(0.0)
        assert win.pilot_svc.arm_position_calls[-1] == pytest.approx((-0.25, 0.75))
    finally:
        win.close()
        app.processEvents()


def test_keyboard_gain_shortcuts_update_pilot_modes_and_indicators(monkeypatch, tmp_path):
    app = _app()
    streams_path = tmp_path / "streams.json"
    streams_path.write_text("{}", encoding="utf-8")

    monkeypatch.setattr(main_window, "QSettings", lambda *args, **kwargs: _FakeSettings())
    monkeypatch.setattr(main_window, "PilotPublisherService", _FakePilotService)
    monkeypatch.setattr(main_window, "SensorSubscriberService", _FakeSensorService)
    monkeypatch.setattr(main_window, "RemoteCameraManager", _FakeRemoteCameraManager)
    monkeypatch.setattr(main_window, "VideoTabs", _FakeVideoPanel)
    monkeypatch.setattr(main_window, "HoldTestPanel", _SimplePage)
    monkeypatch.setattr(main_window, "ManagementPage", _SimplePage)
    monkeypatch.setattr(main_window.threading, "Thread", _NoopThread)

    win = main_window.MainWindow(str(streams_path))
    try:
        app.processEvents()

        win.eventFilter(win, QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_2, Qt.KeyboardModifier.NoModifier, "2"))
        assert win.pilot_svc.back_gripper_gain == pytest.approx(0.55)
        assert win.pilot_svc.back_gripper_gain - 0.50 == pytest.approx(0.05)
        assert win.pilot_telemetry_column.back_gain_indicator.value == pytest.approx(0.55)

        win.eventFilter(win, QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_6, Qt.KeyboardModifier.NoModifier, "6"))
        assert win.pilot_svc.arm_gain == pytest.approx(0.45)
        assert 0.50 - win.pilot_svc.arm_gain == pytest.approx(0.05)
        assert win.pilot_telemetry_column.arm_gain_indicator.value == pytest.approx(0.45)

        win.eventFilter(win, QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Minus, Qt.KeyboardModifier.NoModifier, "-"))
        assert win.pilot_svc.max_gain == pytest.approx(0.35)
        assert 0.40 - win.pilot_svc.max_gain == pytest.approx(0.05)
        assert win.pilot_telemetry_column.rov_gain_indicator.value == pytest.approx(0.35)
    finally:
        win.close()
        app.processEvents()


def test_top_bar_gain_button_sets_pilot_max_gain(monkeypatch, tmp_path):
    app = _app()
    streams_path = tmp_path / "streams.json"
    streams_path.write_text("{}", encoding="utf-8")

    monkeypatch.setattr(main_window, "QSettings", lambda *args, **kwargs: _FakeSettings())
    monkeypatch.setattr(main_window, "PilotPublisherService", _FakePilotService)
    monkeypatch.setattr(main_window, "SensorSubscriberService", _FakeSensorService)
    monkeypatch.setattr(main_window, "RemoteCameraManager", _FakeRemoteCameraManager)
    monkeypatch.setattr(main_window, "VideoTabs", _FakeVideoPanel)
    monkeypatch.setattr(main_window, "HoldTestPanel", _SimplePage)
    monkeypatch.setattr(main_window, "ManagementPage", _SimplePage)
    monkeypatch.setattr(main_window.threading, "Thread", _NoopThread)

    win = main_window.MainWindow(str(streams_path))
    try:
        app.processEvents()

        assert win._max_gain_btn.text() == "Gain 40%"
        assert win._max_gain_spin.value() == 40

        win._max_gain_spin.setValue(55)
        app.processEvents()

        assert win.pilot_svc.max_gain == pytest.approx(0.55)
        assert win.pilot_svc.current_modes()["max_gain"] == pytest.approx(0.55)
        assert win.pilot_telemetry_column.rov_gain_indicator.value == pytest.approx(0.55)
        assert win._max_gain_btn.text() == "Gain 55%"
        assert win._gain_lbl.text() == "Max Gain: 55%"
    finally:
        win.close()
        app.processEvents()


def test_yaw_hold_status_uses_rov_runtime_target(monkeypatch, tmp_path):
    app = _app()
    streams_path = tmp_path / "streams.json"
    streams_path.write_text("{}", encoding="utf-8")

    monkeypatch.setattr(main_window, "QSettings", lambda *args, **kwargs: _FakeSettings())
    monkeypatch.setattr(main_window, "PilotPublisherService", _FakePilotService)
    monkeypatch.setattr(main_window, "SensorSubscriberService", _FakeSensorService)
    monkeypatch.setattr(main_window, "RemoteCameraManager", _FakeRemoteCameraManager)
    monkeypatch.setattr(main_window, "VideoTabs", _FakeVideoPanel)
    monkeypatch.setattr(main_window, "HoldTestPanel", _SimplePage)
    monkeypatch.setattr(main_window, "ManagementPage", _SimplePage)
    monkeypatch.setattr(main_window.threading, "Thread", _NoopThread)

    win = main_window.MainWindow(str(streams_path))
    try:
        app.processEvents()
        win._handle_sensor_msg_on_ui(
            {
                "type": "attitude",
                "sensor": "roll_pitch_estimator",
                "roll_deg": 0.0,
                "pitch_deg": 0.0,
                "yaw_deg": 37.5,
            }
        )
        win._handle_sensor_msg_on_ui(
            {
                "type": "autopilot_status",
                "sensor": "autopilot_status",
                "attitude": {
                    "axes": {
                        "yaw": {
                            "active": False,
                            "reason": "manual_override",
                            "target_deg": 42.0,
                        },
                    },
                },
            }
        )
        win._handle_pilot_msg_on_ui(
            {
                "ts": 1.0,
                "axes": {"rx": 0.5, "ry": 0.0},
                "modes": {"yaw_hold": True, "autopilot": {"yaw": "hold", "targets": {}}},
            }
        )

        assert win.pilot_svc.axis_target_calls == []
        assert "37.5deg" in win._yaw_hold_status_text
        assert "42.0deg" in win._yaw_hold_status_text
        assert "[manual]" in win._yaw_hold_status_text
    finally:
        win.close()
        app.processEvents()


def test_transect_cv_inert_without_rov(monkeypatch, tmp_path):
    app = _app()
    streams_path = tmp_path / "streams.json"
    streams_path.write_text("{}", encoding="utf-8")

    monkeypatch.setattr(main_window, "QSettings", lambda *args, **kwargs: _FakeSettings())
    monkeypatch.setattr(main_window, "PilotPublisherService", _FakePilotService)
    monkeypatch.setattr(main_window, "SensorSubscriberService", _FakeSensorService)
    monkeypatch.setattr(main_window, "RemoteCameraManager", _FakeRemoteCameraManager)
    monkeypatch.setattr(main_window, "VideoTabs", _FakeVideoPanel)
    monkeypatch.setattr(main_window, "HoldTestPanel", _SimplePage)
    monkeypatch.setattr(main_window, "ManagementPage", _SimplePage)
    monkeypatch.setattr(main_window.threading, "Thread", _NoopThread)

    win = main_window.MainWindow(str(streams_path))
    try:
        app.processEvents()
        # The fake cam manager has no .rov, so the CV source must NOT start
        # (no GStreamer spawn / mirror); the transect tab just shows the normal
        # Direct3D video and the chip reports unavailable.
        win._set_center_page("transect", announce=False)
        app.processEvents()
        assert win._transect_cv_source is None
        assert win._start_transect_cv() is False
        win._update_transect_cv_status()
        assert "waiting for tether" in win._transect_page.cv_status_label.text().lower()

        win._tether_ui_ready_last = True
        assert win._start_transect_cv() is False
        win._update_transect_cv_status()
        assert "waiting for rov link" in win._transect_page.cv_status_label.text().lower()

        win._link_state_last = "OK"
        assert win._start_transect_cv() is False
        win._update_transect_cv_status()
        assert "unavailable" in win._transect_page.cv_status_label.text().lower()
    finally:
        win.close()
        app.processEvents()


def test_transect_estimate_records_state_for_chip(monkeypatch, tmp_path):
    app = _app()
    streams_path = tmp_path / "streams.json"
    streams_path.write_text("{}", encoding="utf-8")

    monkeypatch.setattr(main_window, "QSettings", lambda *args, **kwargs: _FakeSettings())
    monkeypatch.setattr(main_window, "PilotPublisherService", _FakePilotService)
    monkeypatch.setattr(main_window, "SensorSubscriberService", _FakeSensorService)
    monkeypatch.setattr(main_window, "RemoteCameraManager", _FakeRemoteCameraManager)
    monkeypatch.setattr(main_window, "VideoTabs", _FakeVideoPanel)
    monkeypatch.setattr(main_window, "HoldTestPanel", _SimplePage)
    monkeypatch.setattr(main_window, "ManagementPage", _SimplePage)
    monkeypatch.setattr(main_window.threading, "Thread", _NoopThread)

    from tracking import TransectObservation, TransectPolicy

    win = main_window.MainWindow(str(streams_path))
    try:
        app.processEvents()
        model = win._transect_model
        obs = TransectObservation(
            blue_found=True, blue_cx=model.target_cx, blue_cy=model.target_cy,
            blue_fraction=model.nominal_blue_fraction, fit_quality=0.95,
            blue_rotation_deg=22.5,
        )
        est = TransectPolicy(model).evaluate(obs)
        frame = np.zeros((48, 64, 3), np.uint8)
        # The CV no longer drives the video; the estimate is just recorded (for the
        # non-covering lock/error chip) and published when engaged.
        win._on_transect_estimate(est, obs, frame)
        app.processEvents()
        assert win._transect_last_lock == est.lock_state
        ex, ey, es, er, viol = win._transect_last_err
        assert er == pytest.approx(0.5, abs=0.05)   # 22.5deg / 45 rot_norm
    finally:
        win.close()
        app.processEvents()


def test_set_stream_mirror_skips_noop_updates():
    class _Rov:
        def __init__(self):
            self.extra = {"udp_mirror_ports": [53111]}
            self.updates = []

        def list_status(self):
            return {"Arm Camera": {"extra": dict(self.extra)}}

        def update_stream(self, **kwargs):
            self.updates.append(kwargs)
            self.extra = dict(kwargs["extra"])

    rov = _Rov()
    win = main_window.MainWindow.__new__(main_window.MainWindow)
    win.cam_mgr = SimpleNamespace(rov=rov)

    win._set_stream_mirror("Arm Camera", 53111, add=True)
    assert rov.updates == []

    win._set_stream_mirror("Arm Camera", 53112, add=False)
    assert rov.updates == []

    win._set_stream_mirror("Arm Camera", 53112, add=True)
    assert rov.updates[-1]["extra"]["udp_mirror_ports"] == [53111, 53112]

    win._set_stream_mirror("Arm Camera", 53111, add=False)
    assert rov.updates[-1]["extra"]["udp_mirror_ports"] == [53112]


def test_depth_hold_status_uses_rov_runtime_target(monkeypatch, tmp_path):
    app = _app()
    streams_path = tmp_path / "streams.json"
    streams_path.write_text("{}", encoding="utf-8")

    monkeypatch.setattr(main_window, "QSettings", lambda *args, **kwargs: _FakeSettings())
    monkeypatch.setattr(main_window, "PilotPublisherService", _FakePilotService)
    monkeypatch.setattr(main_window, "SensorSubscriberService", _FakeSensorService)
    monkeypatch.setattr(main_window, "RemoteCameraManager", _FakeRemoteCameraManager)
    monkeypatch.setattr(main_window, "VideoTabs", _FakeVideoPanel)
    monkeypatch.setattr(main_window, "HoldTestPanel", _SimplePage)
    monkeypatch.setattr(main_window, "ManagementPage", _SimplePage)
    monkeypatch.setattr(main_window.threading, "Thread", _NoopThread)

    win = main_window.MainWindow(str(streams_path))
    try:
        app.processEvents()
        win._handle_sensor_msg_on_ui({"type": "external_depth", "sensor": "external_depth", "depth_m": 1.23})
        win._handle_sensor_msg_on_ui(
            {
                "type": "autopilot_status",
                "sensor": "autopilot_status",
                "depth_hold": {
                    "active": False,
                    "reason": "manual_override",
                    "target_m": 1.40,
                },
            }
        )
        win._handle_pilot_msg_on_ui(
            {
                "ts": 1.0,
                "axes": {"ry": 0.5, "rx": 0.0},
                "modes": {"depth_hold": True, "autopilot": {"depth": True, "targets": {}}},
            }
        )

        assert "1.23m" in win._depth_hold_status_text
        assert "1.40m" in win._depth_hold_status_text
        assert "[manual]" in win._depth_hold_status_text
    finally:
        win.close()
        app.processEvents()
