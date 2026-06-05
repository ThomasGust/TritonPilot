import os
import json
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt6")

from PyQt6.QtCore import QEvent, Qt, pyqtSignal
from PyQt6.QtGui import QKeyEvent
from PyQt6.QtWidgets import QApplication, QLabel, QWidget

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
        self.aux_axis_calls = []
        self.aux_axes = {}

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
        assert win._analysis_transfer_line.text() == text
        assert win._analysis_transfer_line.isHidden() is True
        assert win._analysis_transfer_line.wordWrap() is True
        assert win._analysis_transfer_line.toolTip() == text
        assert win.pilot_telemetry_column.analysis_text.text() == text
    finally:
        win.close()
        app.processEvents()
        assert servers[0][0].shutdown_called is True


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
        assert pilot_layout.indexOf(win._pilot_video_host) < pilot_layout.indexOf(win.pilot_telemetry_column)
        assert win.pilot_telemetry_column.minimumWidth() >= 200
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


def test_stereo_page_controller_x_b_route_to_stereo_capture(monkeypatch, tmp_path):
    app = _app()
    streams_path = tmp_path / "streams.json"
    streams_path.write_text(
        """
        {
          "streams": [
            {"name": "Primary Camera"},
            {"name": "Aux Camera"}
          ],
          "stereo_pairs": [
            {
              "name": "Forward Stereo",
              "left": "Primary Camera",
              "right": "Aux Camera",
              "rig_id": "rig-a"
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
        stereo_actions = []
        camera_actions = []
        win._stereo_page.capture_pair = lambda: stereo_actions.append("pair")
        win._stereo_page.toggle_recording = lambda: stereo_actions.append("record")
        win._save_snapshot = lambda: camera_actions.append("snapshot")
        win._toggle_video_recording = lambda: camera_actions.append("record")

        win._set_center_page("stereo", announce=False)
        win._handle_pilot_msg_on_ui({"edges": {"x": "down", "b": "down"}, "modes": {}})

        assert stereo_actions == ["pair", "record"]
        assert camera_actions == []

        win._set_center_page("pilot", announce=False)
        win._handle_pilot_msg_on_ui({"edges": {"x": "down", "b": "down"}, "modes": {}})

        assert stereo_actions == ["pair", "record"]
        assert camera_actions == ["snapshot", "record"]

        key_event = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_R, Qt.KeyboardModifier.NoModifier, "r")
        win.eventFilter(win, key_event)
        assert win._capture_route_mode == "stereo"
        assert "Stereo pairs" in win.pilot_telemetry_column.capture_mode_text.text()
        assert win.pilot_telemetry_column.capture_activity_text.text() == "STEREO READY"

        win._on_stereo_capture_state_changed({"state": "recording", "mode": "recording", "count": 2, "started_ts": main_window.time.time()})
        assert "STEREO REC" in win.pilot_telemetry_column.capture_activity_text.text()
        assert "2 pairs" in win.pilot_telemetry_column.capture_activity_text.text()

        win._handle_pilot_msg_on_ui({"edges": {"x": "down", "b": "down"}, "modes": {}})

        assert stereo_actions == ["pair", "record", "pair", "record"]
        assert camera_actions == ["snapshot", "record"]
    finally:
        win.close()
        app.processEvents()


def test_capture_outputs_share_app_session_directory(monkeypatch, tmp_path):
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

        first_dir, first_location = win._capture_output_dir()
        second_dir, second_location = win._capture_output_dir()
        raw_dir, raw_location = win._make_recording_session_dir()

        assert first_dir == second_dir == raw_dir
        assert first_dir.parent == save_root
        assert first_dir.exists()
        assert first_dir != save_root
        assert first_location.path == save_root
        assert second_location.path == save_root
        assert raw_location.path == save_root
        assert win._analysis_transfer_configured_root() == save_root.resolve()
    finally:
        win.close()
        app.processEvents()


def test_stereo_page_rolls_sessions_on_mode_changes(monkeypatch, tmp_path):
    app = _app()
    from gui import stereo_page as stereo_module
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
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(stereo_module.time, "strftime", lambda _fmt: "20260605-120000")
    monkeypatch.setattr(stereo_module.time, "time", lambda: 1000.456)

    page = StereoPage(
        streams_path=str(streams_path),
        manager=_FakeRemoteCameraManager(str(streams_path)),
        output_root_provider=lambda: tmp_path / "recordings",
    )
    try:
        app.processEvents()
        sessions = []

        def _fake_start_capture(**kwargs):
            sessions.append((kwargs["mode"], page._resolve_session_name()))

        page._start_capture = _fake_start_capture

        page.capture_pair()
        page.capture_pair()
        assert sessions[0] == ("single", "20260605-120000-456")
        assert sessions[1] == ("single", "20260605-120000-456")

        page.prepare_next_still_session()
        page.capture_pair()
        assert sessions[2] == ("single", "20260605-120000-456-01")

        page.start_recording()
        assert sessions[3] == ("recording", "20260605-120000-456-02")

        page.capture_pair()
        assert sessions[4] == ("single", "20260605-120000-456-03")
    finally:
        page.close()
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


def test_stereo_page_omits_disparity_preview_controls(tmp_path):
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
        section_titles = [label.text() for label in page.findChildren(QLabel, "stereoSectionTitle")]
        assert "Disparity" not in section_titles
        assert page.findChildren(QWidget, "stereoDisparityPreview") == []
        assert page.findChildren(QWidget, "stereoDisparityFrame") == []
        assert not hasattr(page, "disparity_toggle")
    finally:
        page.close()
        app.processEvents()


def test_stereo_page_scrolls_control_column_independently(tmp_path):
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
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    page = StereoPage(
        streams_path=str(streams_path),
        manager=_FakeRemoteCameraManager(str(streams_path)),
    )
    try:
        app.processEvents()
        assert page.side_scroll.widget() is page.side_panel
        assert page.side_scroll.widgetResizable() is True
        assert page.side_scroll.horizontalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        assert page.layout().indexOf(page.video_host) < page.layout().indexOf(page.side_scroll)
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


def test_keyboard_wrist_controls_swap_ws_and_ad_axes(monkeypatch, tmp_path):
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
        assert win._servo_wrist_keymap[Qt.Key.Key_W][0] == "gripper_yaw"
        assert win._servo_wrist_keymap[Qt.Key.Key_S][0] == "gripper_yaw"
        assert win._servo_wrist_keymap[Qt.Key.Key_D][0] == "gripper_pitch"
        assert win._servo_wrist_keymap[Qt.Key.Key_A][0] == "gripper_pitch"

        win._servo_wrist_pitch = 0.0
        win._servo_wrist_yaw = 0.0
        win._servo_wrist_keys_down = {"W"}
        win._servo_wrist_last_update = main_window.time.monotonic() - 0.1
        win._update_servo_wrist_keyboard_axes()
        assert win.pilot_svc.aux_axes["gripper_pitch"] == pytest.approx(0.0)
        assert win.pilot_svc.aux_axes["gripper_yaw"] > 0.0

        win._servo_wrist_pitch = 0.0
        win._servo_wrist_yaw = 0.0
        win._servo_wrist_keys_down = {"D"}
        win._servo_wrist_last_update = main_window.time.monotonic() - 0.1
        win._update_servo_wrist_keyboard_axes()
        assert win.pilot_svc.aux_axes["gripper_pitch"] > 0.0
        assert win.pilot_svc.aux_axes["gripper_yaw"] == pytest.approx(0.0)

        win._servo_wrist_keys_down = {"S"}
        assert win._servo_wrist_keyboard_targets()[1] == pytest.approx(-1.0)
        win._servo_wrist_keys_down = {"A"}
        assert win._servo_wrist_keyboard_targets()[0] == pytest.approx(-1.0)
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
