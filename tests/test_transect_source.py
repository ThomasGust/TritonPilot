"""Tests for TransectVisionSource worker loop (fake receiver, no GStreamer/Qt)."""

import threading
import time
from dataclasses import dataclass

import numpy as np
import pytest

from tracking.transect_policy import TransectModel, TransectObservation, TransectPolicy
from tracking.transect_source import TransectVisionSource

W, H = 64, 48


@dataclass
class _Pkt:
    data: bytes
    seq: int
    monotonic_ts: float = 0.0
    wall_ts: float = 0.0


class _FakeReceiver:
    """Hands out a fresh BGR frame (incrementing seq) on each poll."""

    def __init__(self, *, frame_bytes: int, broken: bool = False):
        self.started = False
        self.stopped = False
        self._seq = 0
        self._n = frame_bytes
        self._broken = broken

    def start(self):
        self.started = True

    def stop(self, *a, **k):
        self.stopped = True

    def latest_frame_packet(self):
        self._seq += 1
        n = self._n - 7 if self._broken else self._n
        return _Pkt(data=bytes(n), seq=self._seq)


class _NoFrameReceiver:
    def start(self):
        return None

    def stop(self, *a, **k):
        return None

    def latest_frame_packet(self):
        return None


class _OnStationDetector:
    def __init__(self, model):
        self.m = model
        self.reset_called = False

    def detect(self, frame_bgr):
        assert frame_bgr.shape == (H, W, 3)
        return TransectObservation(
            blue_found=True, blue_cx=self.m.target_cx, blue_cy=self.m.target_cy,
            blue_fraction=self.m.nominal_blue_fraction, fit_quality=0.95,
        )

    def reset(self):
        self.reset_called = True


def _make(detector, on_estimate, **kw):
    model = TransectModel()
    return TransectVisionSource(
        width=W, height=H, on_estimate=on_estimate,
        receiver_factory=lambda: _FakeReceiver(frame_bytes=W * H * 3, **kw),
        detector=detector, policy=TransectPolicy(model), target_fps=200.0,
        # These tests feed all-zero synthetic frames to exercise the loop; disable
        # the corruption gate (which would reject all-zero frames as "blank").
        frame_quality_check=None,
    )


def _collect(n_target):
    got = []
    done = threading.Event()

    def cb(est, obs, frame):
        got.append((est, obs, frame))
        if len(got) >= n_target:
            done.set()

    return got, done, cb


def test_source_runs_detect_evaluate_and_emits_estimates():
    model = TransectModel()
    det = _OnStationDetector(model)
    got, done, cb = _collect(model.min_lock_frames + 2)
    src = _make(det, cb)

    mirror_calls = []
    src._mirror_setter = mirror_calls.append  # observe lifecycle

    src.start()
    assert done.wait(2.0), "did not receive enough estimates"
    src.stop()

    assert det.reset_called is True
    assert mirror_calls and mirror_calls[0] is True and mirror_calls[-1] is False
    # Lock should be achieved once enough consecutive good frames are seen.
    assert any(est.lock_state == "lock" and est.error.valid for est, _, _ in got)
    last_est, last_obs, last_frame = got[-1]
    assert last_frame.shape == (H, W, 3)
    assert last_obs.blue_found is True


def test_source_skips_wrong_sized_frames_without_crashing():
    got, done, cb = _collect(1)
    src = _make(_OnStationDetector(TransectModel()), cb, broken=True)
    src.start()
    fired = done.wait(0.4)
    src.stop()
    assert fired is False  # mismatched frames are dropped, never delivered
    assert got == []


def test_source_rejects_corrupt_frames_before_detector():
    # Gate ON (default). All-zero frames trip the blank-artifact check, so the
    # detector must never run and the policy must get an explicit no-target -- a
    # corrupt frame can't become an autopilot command.
    model = TransectModel()
    det = _OnStationDetector(model)  # would report blue_found=True if ever called
    got, done, cb = _collect(2)
    src = TransectVisionSource(
        width=W, height=H, on_estimate=cb,
        receiver_factory=lambda: _FakeReceiver(frame_bytes=W * H * 3),
        detector=det, policy=TransectPolicy(model), target_fps=200.0,
    )  # frame_quality_check defaults to the real gate
    src.start()
    assert done.wait(2.0), "no estimates emitted for rejected frames"
    src.stop()

    # Detector never produced a target; every emitted estimate is an invalid no-lock.
    assert all(obs.blue_found is False for _, obs, _ in got)
    assert all(est.error.valid is False for est, _, _ in got)
    assert src.stats()["rejected"] >= 1


def test_source_retries_mirror_while_waiting_for_frames():
    calls = []
    src = TransectVisionSource(
        width=W,
        height=H,
        on_estimate=lambda *a: None,
        receiver_factory=_NoFrameReceiver,
        detector=_OnStationDetector(TransectModel()),
        target_fps=200.0,
        mirror_retry_interval_s=0.03,
        mirror_setter=calls.append,
    )

    src.start()
    deadline = time.monotonic() + 0.4
    while time.monotonic() < deadline and calls.count(True) < 3:
        time.sleep(0.01)
    src.stop()

    assert calls.count(True) >= 3
    assert calls[-1] is False


def test_source_restarts_receiver_while_waiting_for_frames():
    factories = []

    def factory():
        rx = _NoFrameReceiver()
        factories.append(rx)
        return rx

    src = TransectVisionSource(
        width=W,
        height=H,
        on_estimate=lambda *a: None,
        receiver_factory=factory,
        detector=_OnStationDetector(TransectModel()),
        target_fps=200.0,
        mirror_retry_interval_s=0.03,
        receiver_restart_interval_s=0.06,
    )

    src.start()
    deadline = time.monotonic() + 0.5
    while time.monotonic() < deadline and len(factories) < 3:
        time.sleep(0.01)
    src.stop()

    assert len(factories) >= 3


def test_start_stop_is_idempotent_and_manages_receiver():
    holder = {}

    def factory():
        rx = _FakeReceiver(frame_bytes=W * H * 3)
        holder["rx"] = rx
        return rx

    src = TransectVisionSource(
        width=W, height=H, on_estimate=lambda *a: None,
        receiver_factory=factory, detector=_OnStationDetector(TransectModel()),
        target_fps=120.0,
    )
    src.start()
    assert src.is_running()
    src.start()  # second start is a no-op while running
    assert holder["rx"].started is True
    src.stop()
    assert holder["rx"].stopped is True
    assert not src.is_running()
