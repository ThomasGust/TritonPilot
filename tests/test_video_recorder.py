import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from gui.direct_gst_video_widget import (
    DirectReceiverConfig,
    build_direct_receiver_cmd,
    _stream_options,
)
from recording.video_recorder import (
    RECORD_FANOUT_HOST,
    RECORD_FANOUT_PORT_OFFSET,
    VideoRecorderConfig,
    build_video_recorder_cmd,
    record_fanout_port,
)


# --------------------------- loopback fan-out --------------------------- #

def test_record_fanout_port_matches_receiver_and_recorder():
    # The display receiver and the recorder both derive the loopback port from the
    # display port the same way, so they always agree without extra plumbing.
    assert RECORD_FANOUT_HOST == "127.0.0.1"
    for display_port in (5000, 5001, 5002, 5003):
        fanout = record_fanout_port(display_port)
        assert fanout == display_port + RECORD_FANOUT_PORT_OFFSET
        # No collision with any of the four display ports.
        assert fanout not in {5000, 5001, 5002, 5003}

    # The recorder binds the loopback fan-out port and host the receiver sends to.
    cmd = build_video_recorder_cmd(
        "gst-launch-1.0",
        VideoRecorderConfig(
            name="Primary Camera",
            out_path="/tmp/out.mp4",
            codec="h264",
            port=record_fanout_port(5000),
            bind_address=RECORD_FANOUT_HOST,
        ),
    )
    assert "port=5200" in cmd
    assert "address=127.0.0.1" in cmd


# --------------------------- recorder pipeline --------------------------- #

def test_video_recorder_cmd_records_h264_to_mp4_without_reencode():
    cmd = build_video_recorder_cmd(
        "gst-launch-1.0",
        VideoRecorderConfig(name="Primary Camera", out_path=r"C:\rec\out.mp4", codec="h264", port=5200),
    )
    # -e so Ctrl-Break flushes EOS and finalizes the moov.
    assert "-e" in cmd
    assert "udpsrc" in cmd and "port=5200" in cmd
    assert "rtph264depay" in cmd
    assert "h264parse" in cmd
    assert any(part.startswith("mp4mux") for part in cmd)
    # No decoder / re-encoder in the recording path.
    assert "openh264dec" not in cmd and "d3d11h264dec" not in cmd
    assert "x264enc" not in cmd
    # Recording favours completeness: never drop late packets.
    assert "drop-on-latency=false" in cmd


def test_video_recorder_cmd_uses_forward_slash_location_on_windows():
    # gst-launch escape-processes the pipeline string; backslash paths get
    # mangled and filesink silently fails. The location must use forward slashes.
    cmd = build_video_recorder_cmd(
        "gst-launch-1.0",
        VideoRecorderConfig(name="cam", out_path=r"C:\Users\me\rec\out.mp4", port=5200),
    )
    location = [c for c in cmd if c.startswith("location=")][0]
    assert "\\" not in location
    assert location == "location=C:/Users/me/rec/out.mp4"


def test_video_recorder_cmd_jpeg_path_uses_jpeg_depay():
    cmd = build_video_recorder_cmd(
        "gst-launch-1.0",
        VideoRecorderConfig(name="cam", out_path="/tmp/out.mp4", codec="jpeg", port=5201),
    )
    assert "rtpjpegdepay" in cmd
    assert "jpegparse" in cmd


# --------------------------- mirror survives reconnect --------------------------- #

class _FakeManager:
    _defaults: dict = {}

    def __init__(self):
        self.stream_defs = {
            "Primary Camera": {
                "name": "Primary Camera",
                "device": "/dev/video0",
                "width": 1920,
                "height": 1080,
                "fps": 30,
                "port": 5000,
                "video_format": "h264",
                "extra": {"udp_qos_dscp": 34},
            }
        }
        self.recording_mirror_ports: dict = {}


def test_stream_options_folds_active_recording_mirror_into_extra():
    mgr = _FakeManager()
    # No recording active -> no mirror.
    opts = _stream_options(mgr, "Primary Camera")
    assert "udp_mirror_ports" not in (opts.get("extra") or {})

    # Recording active -> mirror port appears so start_stream keeps it on reconnect.
    mgr.recording_mirror_ports["Primary Camera"] = [5200]
    opts = _stream_options(mgr, "Primary Camera")
    assert opts["extra"]["udp_mirror_ports"] == [5200]
    # Original stream def is not mutated.
    assert "udp_mirror_ports" not in mgr.stream_defs["Primary Camera"]["extra"]


# --------------------------- hardware decode pipeline --------------------------- #

def test_direct_receiver_uses_d3d11convert_for_hardware_decoder():
    cmd = build_direct_receiver_cmd(
        "gst-launch-1.0",
        DirectReceiverConfig(
            name="Primary Camera", codec="h264", port=5000,
            bind_address="192.168.1.1", h264_decoder="d3d11h264dec",
        ),
    )
    assert "d3d11h264dec" in cmd
    # GPU memory stays on the GPU: d3d11convert, not a CPU videoconvert download.
    assert "d3d11convert" in cmd
    assert "videoconvert" not in cmd
    assert "d3d11videosink" in cmd


def test_direct_receiver_software_decoder_keeps_cpu_convert():
    cmd = build_direct_receiver_cmd(
        "gst-launch-1.0",
        DirectReceiverConfig(
            name="Primary Camera", codec="h264", port=5000,
            bind_address="192.168.1.1", h264_decoder="openh264dec",
        ),
    )
    assert "openh264dec" in cmd
    assert "videoconvert" in cmd
    assert "d3d11convert" not in cmd


# --------------------------- mode-aware B dispatch --------------------------- #

def test_capture_manifest_creates_and_finalizes(tmp_path):
    import json
    from gui.main_window import MainWindow

    class _Stub:
        _stream_log_path = None

    stub = _Stub()
    session = tmp_path / "sess"
    (session / "video").mkdir(parents=True)
    stub._stream_log_path = str(session / "20260618_streams.jsonl")

    MainWindow._write_capture_manifest(
        stub, session,
        stream_name="Arm Camera",
        mp4_path=session / "video" / "Arm_Camera-x.mp4",
        codec="h264",
        opts={"width": 1920, "height": 1080, "fps": 30},
    )
    m = json.loads((session / "capture_manifest.json").read_text())
    assert m["schema"] == "tritonpilot.capture_manifest"
    assert m["video"]["stream"] == "Arm Camera"
    assert m["video"]["path"] == "video/Arm_Camera-x.mp4"   # relative, forward slashes
    assert m["video"]["width"] == 1920
    assert m["streams_log"] == "20260618_streams.jsonl"
    assert "tracking" in m["streams"]
    assert "started_wall_ts" in m and "ended_wall_ts" not in m

    # Finalize: adds end time, preserves the original video metadata.
    MainWindow._write_capture_manifest(
        stub, session, stream_name="Arm Camera",
        mp4_path=session / "video", codec="h264", opts={}, ended_wall=123.0,
    )
    m2 = json.loads((session / "capture_manifest.json").read_text())
    assert m2["ended_wall_ts"] == 123.0
    assert m2["video"]["width"] == 1920


def test_b_button_dispatches_video_in_standard_and_stereo_in_stereo_mode():
    from gui.main_window import MainWindow

    class _Stub:
        pass

    stub = _Stub()
    calls: list[str] = []
    stub._toggle_stereo_recording = lambda: calls.append("stereo")
    stub._toggle_video_recording = lambda: calls.append("video")

    stub._capture_mode = "standard"
    MainWindow._toggle_recording_for_mode(stub)
    assert calls == ["video"]

    stub._capture_mode = "stereo"
    MainWindow._toggle_recording_for_mode(stub)
    assert calls == ["video", "stereo"]
