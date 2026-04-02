import json
import time
import uuid

import zmq

from input.pilot_service import PilotPublisherService
from input.controller import ControllerSnapshot


class FakeController:
    def __init__(self):
        self.i = 0

    def healthcheck(self) -> None:
        return None

    def close(self) -> None:
        return None

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


def test_reverse_mode_flips_horizontal_axes(monkeypatch):
    monkeypatch.setattr("input.pilot_service.time.sleep", lambda *_args, **_kwargs: None)

    svc = PilotPublisherService(endpoint="inproc://reverse_test", rate_hz=30.0, deadzone=0.0, debug=False)
    snap = ControllerSnapshot(
        lx=0.25,
        ly=-0.75,
        rx=0.5,
        ry=-0.2,
        lt=0.1,
        rt=0.9,
        dpad=(0, 0),
        a=False,
        b=False,
        x=False,
        y=False,
        lb=False,
        rb=False,
        win=False,
        menu=False,
        lstick=False,
        rstick=False,
    )

    fwd = svc._build_frame(123.0, snap)
    assert fwd.axes.lx == 0.25
    assert fwd.axes.ly == -0.75
    assert fwd.axes.rx == 0.5
    assert fwd.axes.ry == -0.2

    svc.set_reverse_enabled(True)
    rev = svc._build_frame(124.0, snap)
    assert rev.axes.lx == -0.25
    assert rev.axes.ly == 0.75
    assert rev.axes.rx == -0.5
    assert rev.axes.ry == -0.2
    assert rev.axes.lt == 0.1
    assert rev.axes.rt == 0.9
    assert svc.current_modes()["reverse"] is True


def test_aux_axes_are_embedded_in_frame(monkeypatch):
    monkeypatch.setattr("input.pilot_service.time.sleep", lambda *_args, **_kwargs: None)

    svc = PilotPublisherService(endpoint="inproc://aux_test", rate_hz=30.0, deadzone=0.0, debug=False)
    svc.set_aux_axis("gripper_pitch", 1.0)
    svc.set_aux_axis("gripper_yaw", -1.0)

    snap = ControllerSnapshot(
        lx=0.0, ly=0.0, rx=0.0, ry=0.0, lt=0.0, rt=0.0,
        dpad=(0, 0),
        a=False, b=False, x=False, y=False, lb=False, rb=False,
        win=False, menu=False, lstick=False, rstick=False,
    )
    frame = svc._build_frame(10.0, snap)

    assert frame.aux["gripper_pitch"] == 1.0
    assert frame.aux["gripper_yaw"] == -1.0


def test_t200_wrist_gain_is_exposed_in_modes(monkeypatch):
    monkeypatch.setattr("input.pilot_service.time.sleep", lambda *_args, **_kwargs: None)

    svc = PilotPublisherService(endpoint="inproc://t200_gain_test", rate_hz=30.0, deadzone=0.0, debug=False)
    start_gain = svc.current_t200_wrist_gain()

    assert "t200_wrist_gain" in svc.current_modes()
    assert svc.adjust_t200_wrist_gain(-svc.t200_wrist_gain_step()) is True
    assert svc.current_t200_wrist_gain() < start_gain
