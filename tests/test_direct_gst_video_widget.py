import os

import numpy as np

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from gui.direct_gst_video_widget import DirectReceiverConfig, build_direct_receiver_cmd
from gui.video_tabs import VideoTabs
from video.frame_quality import live_frame_rejection_reason


def test_direct_h264_receiver_renders_with_direct3d_without_media_tees():
    cmd = build_direct_receiver_cmd(
        "gst-launch-1.0",
        DirectReceiverConfig(
            name="Primary Camera",
            codec="h264",
            port=5000,
            bind_address="192.168.1.1",
            latency_ms=5,
            h264_decoder="decodebin",
        ),
    )

    assert "rtph264depay" in cmd
    assert "decodebin" in cmd
    assert "d3d11videosink" in cmd
    assert "tee" not in cmd
    assert "udpsink" not in cmd
    assert "fdsink" not in cmd
    assert "video/x-raw,format=BGR" not in cmd
    assert "sync=false" in cmd
    assert "async=false" in cmd
    assert "leaky=downstream" in cmd


def test_direct_h264_receiver_defaults_to_software_decoder():
    cmd = build_direct_receiver_cmd(
        "gst-launch-1.0",
        DirectReceiverConfig(
            name="Primary Camera",
            codec="h264",
            port=5000,
            bind_address="192.168.1.1",
        ),
    )

    assert "openh264dec" in cmd
    assert "decodebin" not in cmd


def test_direct_receiver_fans_rtp_to_loopback_for_recording():
    cmd = build_direct_receiver_cmd(
        "gst-launch-1.0",
        DirectReceiverConfig(
            name="Primary Camera",
            codec="h264",
            port=5000,
            bind_address="192.168.1.1",
            record_fanout_port=5200,
        ),
    )

    # A tee splits the raw RTP: one branch drives the live display exactly as
    # before, the other forwards the same packets to a loopback recording port.
    assert "tee" in cmd
    assert "name=rtptee" in cmd
    assert "udpsink" in cmd
    assert "host=127.0.0.1" in cmd
    assert "port=5200" in cmd  # fan-out target (display udpsrc still on port=5000)
    assert "port=5000" in cmd
    # Display path stays intact downstream of the tee.
    assert "rtph264depay" in cmd
    assert "d3d11videosink" in cmd
    # Fan-out is teed off the raw RTP, before the display jitter buffer.
    assert cmd.index("tee") < cmd.index("rtpjitterbuffer")
    assert cmd.index("udpsink") > cmd.index("tee")
    # The recording branch must be leaky so an absent/slow recorder can never
    # back up the tee and stall the live display.
    assert "leaky=downstream" in cmd


def test_direct_jpeg_receiver_uses_direct3d_sink():
    cmd = build_direct_receiver_cmd(
        "gst-launch-1.0",
        DirectReceiverConfig(
            name="Primary Camera",
            codec="jpeg",
            port=5000,
            bind_address="192.168.1.1",
        ),
    )

    assert "rtpjpegdepay" in cmd
    assert "jpegdec" in cmd
    assert "d3d11videosink" in cmd
    assert "fdsink" not in cmd


def test_direct_receiver_can_center_crop_to_square():
    cmd = build_direct_receiver_cmd(
        "gst-launch-1.0",
        DirectReceiverConfig(
            name="Primary Camera",
            codec="h264",
            port=5000,
            bind_address="192.168.1.1",
            width=1920,
            height=1080,
            square_crop=True,
        ),
    )

    assert "videocrop" in cmd
    assert "left=420" in cmd
    assert "right=420" in cmd
    assert cmd.index("videocrop") > cmd.index("videoconvert")
    assert cmd.index("videocrop") < cmd.index("d3d11videosink")


def test_video_tabs_selects_direct_widget_for_direct3d_stream():
    class _Manager:
        stream_defs = {
            "Primary Camera": {"render_mode": "direct3d"},
            "Aux Camera": {},
        }

    tabs = VideoTabs.__new__(VideoTabs)
    tabs.manager = _Manager()

    assert tabs._widget_class_for_stream("Primary Camera").__name__ == "DirectGstVideoWidget"
    assert tabs._widget_class_for_stream("Aux Camera").__name__ == "VideoWidget"


def test_live_frame_quality_rejects_green_and_blank_startup_artifacts():
    green = np.zeros((24, 32, 3), dtype=np.uint8)
    green[:, :, 1] = 120
    assert live_frame_rejection_reason(green) == "green_startup_artifact"

    blank = np.zeros((24, 32, 3), dtype=np.uint8)
    assert live_frame_rejection_reason(blank) == "blank_startup_artifact"

    usable = np.zeros((24, 32, 3), dtype=np.uint8)
    usable[:, :, 0] = 70
    usable[:, :, 1] = 90
    usable[:, :, 2] = 110
    usable[::2, ::2, :] = 150
    assert live_frame_rejection_reason(usable) is None
