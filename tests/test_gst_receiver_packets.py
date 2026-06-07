from video.gst_receiver import ReceiverProcess, RxConfig, _StoredRawFrame


def _receiver(monkeypatch, *, channel_order: str = "BGR") -> ReceiverProcess:
    monkeypatch.setattr("video.gst_receiver._find_gst_launch", lambda: "gst-launch-1.0")
    return ReceiverProcess(
        RxConfig(
            name="test",
            width=2,
            height=1,
            mode="raw",
            channel_order=channel_order,
        )
    )


def _seed_frame(receiver: ReceiverProcess, data: bytes = b"\x01\x02\x03\x04\x05\x06") -> None:
    with receiver._raw_buffer_lock:
        receiver._latest_frame = data
        receiver._latest_seq = 7
        receiver._latest_frame_ts = 123.5
        receiver._latest_frame_monotonic_ts = 45.25
        receiver._frame_history.append(
            _StoredRawFrame(
                data=data,
                seq=7,
                monotonic_ts=45.25,
                wall_ts=123.5,
            )
        )


def test_latest_frame_packet_does_not_consume_delivery_state(monkeypatch):
    receiver = _receiver(monkeypatch)
    _seed_frame(receiver)

    latest = receiver.latest_frame_packet()
    consumed = receiver.read_frame_packet()
    second_consumed = receiver.read_frame_packet()

    assert latest is not None
    assert consumed is not None
    assert second_consumed is None
    assert latest.seq == 7
    assert latest.wall_ts == 123.5
    assert latest.monotonic_ts == 45.25
    assert latest.data == consumed.data


def test_latest_frame_packet_remains_available_after_consuming_read(monkeypatch):
    receiver = _receiver(monkeypatch)
    _seed_frame(receiver)

    assert receiver.read_frame_packet() is not None
    latest = receiver.latest_frame_packet()

    assert latest is not None
    assert latest.seq == 7


def test_frame_packet_applies_channel_order(monkeypatch):
    receiver = _receiver(monkeypatch, channel_order="RGB")
    _seed_frame(receiver, b"\x01\x02\x03\x04\x05\x06")

    packet = receiver.latest_frame_packet()

    assert packet is not None
    assert packet.data == b"\x03\x02\x01\x06\x05\x04"


def test_receiver_pipeline_uses_configured_udp_buffer_and_jitter(monkeypatch):
    receiver = _receiver(monkeypatch)

    cmd = receiver._build_cmd(
        RxConfig(
            name="test",
            codec="h264",
            port=5000,
            mode="raw",
            width=2,
            height=1,
            latency_ms=60,
            udp_buffer_size=1234567,
            drop_on_latency=False,
        )
    )

    assert "buffer-size=1234567" in cmd
    assert "latency=60" in cmd
    assert "drop-on-latency=false" in cmd
