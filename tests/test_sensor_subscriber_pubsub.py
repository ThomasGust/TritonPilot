import json
import socket
import time
import zmq

from telemetry.sensor_service import SensorSubscriberService


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_sensor_subscriber_receives_messages():
    port = _free_port()
    ep = f"tcp://127.0.0.1:{port}"

    # publisher binds
    ctx = zmq.Context.instance()
    pub = ctx.socket(zmq.PUB)
    pub.bind(ep)

    received = []

    def on_msg(m: dict):
        received.append(m)

    sub = SensorSubscriberService(endpoint=ep, on_message=on_msg, debug=False)
    sub.start()

    time.sleep(0.15)  # slow-joiner

    pub.send_string(json.dumps({"type": "heartbeat", "sensor": "heartbeat", "armed": False, "pilot_age": 0.0}))
    t0 = time.time()
    while time.time() - t0 < 1.0 and not received:
        time.sleep(0.02)

    sub.stop()
    pub.close(0)

    assert received
    assert received[0]["type"] == "heartbeat"
