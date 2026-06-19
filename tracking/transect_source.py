"""Live frame source for the transect autopilot.

Owns a topside *raw* video receiver on a UDP mirror port for the transect/arm
camera, pulls decoded BGR frames on a worker thread, runs them through a
``TransectDetector`` + :class:`~tracking.transect_policy.TransectPolicy`, and
hands each :class:`~tracking.transect_policy.TransectEstimate` to a callback
(the UI overlay + ``publish_visual_target``). This is what gives the CV its own
pixel access -- the live display path is gst-launch -> Direct3D with no Python
frames, so the tracker needs a dedicated raw pull (the ROV simply duplicates the
existing H.264 RTP to one extra UDP port via a live ``multiudpsink`` client, so
there is **no** added decode load on the Pi).

Decoupled for testing: the GStreamer receiver and the ROV mirror-port toggle are
injected (``receiver_factory`` / ``mirror_setter``), so the worker loop runs in
tests against a fake receiver with synthetic frames -- no GStreamer, no ROV, no
Qt. The default factory builds the same ``ReceiverProcess`` the app already uses
for raw snapshots.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional, Protocol

import numpy as np

from tracking.transect_detector import StubTransectDetector, TransectDetector
from tracking.transect_policy import TransectEstimate, TransectModel, TransectObservation, TransectPolicy

logger = logging.getLogger(__name__)


class _Receiver(Protocol):
    def start(self) -> None: ...
    def stop(self, *args, **kwargs) -> None: ...
    def latest_frame_packet(self): ...


# (estimate, observation, frame_bgr) -> None
EstimateCallback = Callable[[TransectEstimate, TransectObservation, "np.ndarray"], None]


def default_receiver_factory(
    *, port: int, codec: str, width: int, height: int,
    latency_ms: int = 60, channel_order: str = "BGR", bind_address: str = "0.0.0.0",
) -> Callable[[], _Receiver]:
    """Build a factory that creates the app's raw ``ReceiverProcess`` on ``port``."""

    def _make() -> _Receiver:
        from video.gst_receiver import ReceiverProcess, RxConfig

        return ReceiverProcess(RxConfig(
            name="transect-cv", codec=codec, port=port, mode="raw",
            width=width, height=height, latency_ms=latency_ms,
            channel_order=channel_order, bind_address=bind_address,
        ))

    return _make


class TransectVisionSource:
    """Pull frames, detect+evaluate, and emit estimates on a worker thread."""

    def __init__(
        self,
        *,
        width: int,
        height: int,
        on_estimate: EstimateCallback,
        receiver_factory: Callable[[], _Receiver],
        detector: Optional[TransectDetector] = None,
        policy: Optional[TransectPolicy] = None,
        model: Optional[TransectModel] = None,
        mirror_setter: Optional[Callable[[bool], None]] = None,
        target_fps: float = 15.0,
        name: str = "transect-cv",
    ):
        self.width = int(width)
        self.height = int(height)
        self._frame_bytes = self.width * self.height * 3
        self._on_estimate = on_estimate
        self._receiver_factory = receiver_factory
        self.detector: TransectDetector = detector or StubTransectDetector()
        self.policy = policy or TransectPolicy(model or TransectModel())
        self._mirror_setter = mirror_setter
        self._period = 1.0 / max(1e-3, float(target_fps))
        self._name = name

        self._receiver: Optional[_Receiver] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._last_seq: Optional[int] = None
        self._warned_size = False

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        with self._lock:
            if self.is_running():
                return
            self._stop.clear()
            self._last_seq = None
            self.policy.reset()
            try:
                self.detector.reset()
            except Exception:
                pass
            if self._mirror_setter is not None:
                try:
                    self._mirror_setter(True)
                except Exception as exc:
                    logger.warning("[%s] mirror enable failed: %s", self._name, exc)
            self._receiver = self._receiver_factory()
            self._receiver.start()
            self._thread = threading.Thread(target=self._run, name=self._name, daemon=True)
            self._thread.start()
            logger.info("[%s] started (%dx%d)", self._name, self.width, self.height)

    def stop(self) -> None:
        with self._lock:
            self._stop.set()
            thread, self._thread = self._thread, None
            receiver, self._receiver = self._receiver, None
        if thread is not None:
            thread.join(timeout=2.0)
        if receiver is not None:
            try:
                receiver.stop()
            except Exception as exc:
                logger.debug("[%s] receiver stop: %s", self._name, exc)
        if self._mirror_setter is not None:
            try:
                self._mirror_setter(False)
            except Exception as exc:
                logger.debug("[%s] mirror disable: %s", self._name, exc)
        logger.info("[%s] stopped", self._name)

    def _run(self) -> None:
        receiver = self._receiver
        if receiver is None:
            return
        while not self._stop.is_set():
            t0 = time.monotonic()
            try:
                pkt = receiver.latest_frame_packet()
            except Exception as exc:
                logger.debug("[%s] read failed: %s", self._name, exc)
                pkt = None
            if pkt is not None and pkt.seq != self._last_seq:
                self._last_seq = pkt.seq
                self._process(pkt.data)
            elapsed = time.monotonic() - t0
            if elapsed < self._period:
                self._stop.wait(self._period - elapsed)

    def _process(self, data: bytes) -> None:
        if len(data) != self._frame_bytes:
            if not self._warned_size:
                logger.warning(
                    "[%s] frame size %d != expected %d (%dx%d); check stream dims",
                    self._name, len(data), self._frame_bytes, self.width, self.height,
                )
                self._warned_size = True
            return
        try:
            frame = np.frombuffer(data, np.uint8).reshape((self.height, self.width, 3)).copy()
            obs = self.detector.detect(frame)
            est = self.policy.evaluate(obs)
            self._on_estimate(est, obs, frame)
        except Exception as exc:
            logger.debug("[%s] process failed: %s", self._name, exc)
