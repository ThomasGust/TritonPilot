import json
import time
import uuid

import zmq

from input.pilot_service import PilotPublisherService
from input.controller import ControllerSnapshot


class FakeController:
    def __init__(self):
        self.i = 0

    def read_once(self) -> ControllerSnapshot:
        self.i += 1
        return ControllerSnapshot(
            lx=0.1 * self.i, ly=0.0, rx=0.0, ry=0.0, lt=0.0, rt=0.0,
            dpad=(0, 0),
            a=False, b=False, x=False, y=False, lb=False, rb=False,
            win=False, menu=(self.i == 2), lstick=False, rstick=False,
        )


def test_pilot_publisher_sends_frames(monkeypatch):
    ep = f"inproc://pilot_pub_{uuid.uuid4().hex}"

    ctx = zmq.Context.instance()
    sub = ctx.socket(zmq.SUB)
    sub.setsockopt_string(zmq.SUBSCRIBE, "")
    sub.bind(ep)  # PilotPublisherService will connect()

    # monkeypatch controller open to avoid pygame
    def _fake_open(self):
        return FakeController()

    monkeypatch.setattr(PilotPublisherService, "_open_controller", _fake_open, raising=True)

    pubsvc = PilotPublisherService(endpoint=ep, rate_hz=100.0, deadzone=0.0, debug=False)
    pubsvc.start()

    poller = zmq.Poller()
    poller.register(sub, zmq.POLLIN)

    msgs = []
    deadline = time.time() + 1.5
    while time.time() < deadline and len(msgs) < 3:
        events = dict(poller.poll(timeout=200))
        if sub in events:
            raw = sub.recv_string()
            msgs.append(json.loads(raw))

    pubsvc.stop()
    sub.close(0)

    assert len(msgs) >= 2
    assert msgs[0]["schema"] == 1
    assert msgs[1]["seq"] > msgs[0]["seq"]
