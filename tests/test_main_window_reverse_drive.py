import os
import json
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt6")

from PyQt6.QtCore import QEvent, Qt, pyqtSignal
from PyQt6.QtGui import QKeyEvent
from PyQt6.QtWidgets import QApplication, QWidget

import gui.main_window as main_window


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
        self._reverse = False
        self.queued_edges = []
        self.axis_target_calls = []

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

    def set_aux_axis(self, *_args, **_kwargs):
        return None

    def queue_edge(self, *_args, **_kwargs):
        name = _args[0] if _args else ""
        state = _args[1] if len(_args) > 1 else _kwargs.get("state", "down")
        self.queued_edges.append((name, state))
        return None

    def set_autopilot_axis_target(self, axis, target_deg, *, mode="hold"):
        self.axis_target_calls.append((axis, float(target_deg), mode))
        return True

    def t200_wrist_gain_step(self):
        return 0.05

    def adjust_t200_wrist_gain(self, *_args, **_kwargs):
        return False

    def current_t200_wrist_gain(self):
        return 1.0


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
    def status(self):
        return {"state": "playing", "age_s": 0.0}


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
        return _FakeVideoWidget()

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

    def set_water_correction(self, *_args, **_kwargs):
        return None

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
            return {"request_count": 1, "last_request_ts": main_window.time.time(), "last_request_path": "/index.json"}

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
        text = win._analysis_transfer_lbl.text()
        assert "Analysis Share: ON http://127.0.0.1:49123" in text
        assert "recordings" in text
        assert "3 files/5.0 MB" in text
    finally:
        win.close()
        app.processEvents()
        assert servers[0][0].shutdown_called is True


def test_stereo_page_applies_configured_pair_layout(monkeypatch, tmp_path):
    app = _app()
    streams_path = tmp_path / "streams.json"
    streams_path.write_text(
        """
        {
          "streams": [
            {"name": "Primary Camera"},
            {"name": "Aux Camera"},
            {"name": "Arm Camera"},
            {"name": "Back Gripper Camera"}
          ],
          "stereo_pairs": [
            {
              "name": "Forward Stereo",
              "left": "Primary Camera",
              "right": "Aux Camera",
              "rig_id": "rig-a",
              "max_pair_delta_ms": 25,
              "metadata": {"baseline_mm": 120, "sync_notes": "software paired"}
            }
          ]
        }
        """,
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
        panel = win.video_panel
        assert panel is not None

        win._set_center_page("stereo", announce=False)
        app.processEvents()

        assert win._page_tabs.tabText(win._page_tabs.currentIndex()) == "Stereo"
        assert panel.apply_temporary_layout_calls[-1][0] == (
            2,
            ["Primary Camera", "Aux Camera"],
        )
        assert panel.apply_temporary_layout_calls[-1][1]["active_name"] == "Primary Camera"
        assert panel.controls_enabled is False
        assert win._stereo_page.rig_lbl.text() == "rig-a"
        assert win._stereo_page.baseline_lbl.text() == "120 mm"

        win._set_center_page("pilot", announce=False)
        app.processEvents()

        assert panel.restore_layout_snapshot_calls
    finally:
        win.close()
        app.processEvents()


def test_stereo_page_resumes_existing_capture_session(tmp_path):
    app = _app()
    from gui.stereo_page import StereoPage

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
                        "max_pair_delta_ms": 25,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    output_root = tmp_path / "recordings"
    session_dir = output_root / "stereo_sessions" / "pool-session"
    session_dir.mkdir(parents=True)
    manifest_path = session_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema": "tritonpilot.stereo_capture_manifest",
                "session_name": "pool-session",
                "pair": {
                    "name": "Forward Stereo",
                    "left": "Primary Camera",
                    "right": "Aux Camera",
                    "rig_id": "rig-a",
                },
                "frames": [
                    {
                        "index": 1,
                        "stem": "pair_000001",
                        "pair_delta_ms": 12.5,
                        "left": {"seq": 10},
                        "right": {"seq": 11},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    page = StereoPage(
        streams_path=str(streams_path),
        manager=_FakeRemoteCameraManager(str(streams_path)),
        output_root_provider=lambda: output_root,
    )
    try:
        app.processEvents()

        assert page._load_session_manifest(manifest_path) is True

        assert page.session_edit.text() == "pool-session"
        assert page.output_lbl.text() == str(manifest_path)
        assert page.frames_table.rowCount() == 1
        assert page.frames_table.item(0, 0).text() == "1"
        assert page.frames_table.item(0, 1).text() == "12.5 ms"
        assert page._active_output_root == output_root
    finally:
        page.close()
        app.processEvents()


def test_stereo_page_resolves_disparity_calibration_from_metadata(tmp_path):
    app = _app()
    from gui.stereo_page import StereoPage

    data_dir = tmp_path / "data"
    calibration_path = data_dir / "calibrations" / "rig-a.json"
    calibration_path.parent.mkdir(parents=True)
    calibration_path.write_text("{}", encoding="utf-8")
    streams_path = data_dir / "streams.json"
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
                        "metadata": {"calibration_path": "calibrations/rig-a.json"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    page = StereoPage(
        streams_path=str(streams_path),
        manager=_FakeRemoteCameraManager(str(streams_path)),
        packet_provider=lambda _name: None,
    )
    try:
        app.processEvents()
        assert page._resolve_disparity_calibration_path() == calibration_path.resolve()
        assert page.disparity_calibration_lbl.text() == str(calibration_path.resolve())
    finally:
        page.close()
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
        win._handle_sensor_msg_on_ui({"type": "heartbeat", "sensor": "heartbeat", "armed": True})
        assert win._arm_disarm_btn.text() == "Disarm (O)"
    finally:
        win.close()
        app.processEvents()


def test_yaw_hold_latches_current_attitude_target(monkeypatch, tmp_path):
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
        win._handle_pilot_msg_on_ui(
            {
                "ts": 1.0,
                "axes": {"rx": 0.0, "ry": 0.0},
                "modes": {"yaw_hold": True, "autopilot": {"yaw": "hold", "targets": {}}},
            }
        )

        assert win.pilot_svc.axis_target_calls[-1] == ("yaw", 37.5, "hold")
        assert "37.5deg" in win._yaw_hold_status_text
    finally:
        win.close()
        app.processEvents()


def test_yaw_hold_latches_target_after_manual_yaw_release(monkeypatch, tmp_path):
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
    monkeypatch.setattr(main_window, "YAW_HOLD_RELEASE_SETTLE_S", 0.0)

    win = main_window.MainWindow(str(streams_path))
    try:
        app.processEvents()
        win._handle_sensor_msg_on_ui({"type": "attitude", "sensor": "roll_pitch_estimator", "yaw_deg": 10.0})
        win._handle_pilot_msg_on_ui(
            {
                "ts": 1.0,
                "axes": {"rx": 0.5, "ry": 0.0},
                "modes": {"yaw_hold": True, "autopilot": {"yaw": "hold", "targets": {}}},
            }
        )
        assert win.pilot_svc.axis_target_calls[-1] == ("yaw", 10.0, "hold")

        win._handle_sensor_msg_on_ui({"type": "attitude", "sensor": "roll_pitch_estimator", "yaw_deg": 22.0})
        win._handle_pilot_msg_on_ui(
            {
                "ts": 1.1,
                "axes": {"rx": 0.0, "ry": 0.0},
                "modes": {"yaw_hold": True, "autopilot": {"yaw": "hold", "targets": {}}},
            }
        )
        assert win.pilot_svc.axis_target_calls[-1] == ("yaw", 10.0, "hold")
        assert win._yh_release_ts is not None

        win._handle_sensor_msg_on_ui({"type": "attitude", "sensor": "roll_pitch_estimator", "yaw_deg": 23.0})
        win._handle_pilot_msg_on_ui(
            {
                "ts": 1.2,
                "axes": {"rx": 0.0, "ry": 0.0},
                "modes": {"yaw_hold": True, "autopilot": {"yaw": "hold", "targets": {}}},
            }
        )

        assert win.pilot_svc.axis_target_calls[-1] == ("yaw", 23.0, "hold")
    finally:
        win.close()
        app.processEvents()
