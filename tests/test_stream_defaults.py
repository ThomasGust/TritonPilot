import json
from pathlib import Path


def test_checked_in_streams_use_direct3d_display_profile_with_onboard_snapshots():
    streams_path = Path(__file__).resolve().parents[1] / "data" / "streams.json"
    config = json.loads(streams_path.read_text(encoding="utf-8"))

    streams = [stream for stream in config["streams"] if stream.get("enabled", True)]
    assert streams
    assert config["stereo_pairs"][0]["name"] == "Forward Stereo"
    assert config["stereo_pairs"][0]["left"] == "Primary Camera"
    assert config["stereo_pairs"][0]["right"] == "Aux Camera"
    assert config["stereo_pairs"][0]["max_pair_delta_ms"] == 50
    assert config["snapshot_prewarm_count"] == 0
    assert "receiver_snapshot_output_fps" not in config

    for stream in streams:
        is_stereo_stream = stream["name"] in {"Primary Camera", "Aux Camera"}
        assert str(stream.get("render_mode", "")).lower() == "direct3d"
        assert stream["video_format"] == "h264"
        assert stream["h264_bitrate"] == 8_000_000
        assert stream["latency_ms"] == 50
        assert stream["receiver_h264_decoder"] == "d3d11h264dec"
        assert stream["extra"].get("rov_snapshot_ondemand") == (True if is_stereo_stream else None)
        assert stream["extra"].get("rov_snapshot_ring_aus") == (150 if is_stereo_stream else None)
        assert stream["extra"].get("rov_snapshot_jpeg_quality") == (98 if is_stereo_stream else None)
        assert stream["extra"].get("rov_snapshot_cache_enabled") == (True if is_stereo_stream else None)
        assert stream["extra"].get("rov_snapshot_cache_frames") == (24 if is_stereo_stream else None)
        if is_stereo_stream:
            assert not any(str(key).startswith("rov_still_") for key in stream["extra"])
        assert stream["extra"]["sender_leaky_queues"] is True
        assert stream["extra"]["sender_queue_max_buffers"] == 8
        assert stream["extra"]["sender_queue_max_time_ms"] == 0
        assert "capture_port" not in stream
        assert "receiver_capture_latency_ms" not in stream
        assert "receiver_capture_drop_on_latency" not in stream
        assert "receiver_capture_udp_buffer_size" not in stream
