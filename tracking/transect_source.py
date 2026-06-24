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
from video.frame_quality import live_frame_rejection_reason

logger = logging.getLogger(__name__)

# (frame_bgr) -> reason str if the frame is unusable, else None
FrameQualityCheck = Callable[["np.ndarray"], Optional[str]]


class _Receiver(Protocol):
    def start(self) -> None: ...
    def stop(self, *args, **kwargs) -> None: ...
    def latest_frame_packet(self): ...


# (estimate, observation, frame_bgr) -> None
EstimateCallback = Callable[[TransectEstimate, TransectObservation, "np.ndarray"], None]


def default_receiver_factory(
    *, port: int, codec: str, width: int, height: int,
    latency_ms: int = 60, channel_order: str = "BGR", bind_address: str = "0.0.0.0",
    kill_port_users: bool = False,
) -> Callable[[], _Receiver]:
    """Build a factory that creates the app's raw ``ReceiverProcess`` on ``port``.

    ``kill_port_users`` defaults False: the caller is expected to hand us a
    dedicated (freshly allocated) mirror port, so we must NOT kill whatever holds
    it (killing would target the display/snapshot receivers if a port collided).
    """

    def _make() -> _Receiver:
        from video.gst_receiver import ReceiverProcess, RxConfig

        return ReceiverProcess(RxConfig(
            name="transect-cv", codec=codec, port=port, mode="raw",
            width=width, height=height, latency_ms=latency_ms,
            channel_order=channel_order, bind_address=bind_address,
            extra={
                "receiver_kill_port_users": bool(kill_port_users),
                # Negotiate against any decoded colorimetry + rescale if needed, so
                # the CV pull doesn't "no-frames" on strict raw caps.
                "raw_caps_loose": True,
            },
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
        frame_quality_check: Optional[FrameQualityCheck] = live_frame_rejection_reason,
        target_fps: float = 15.0,
        mirror_retry_interval_s: float = 1.0,
        receiver_restart_interval_s: float = 4.0,
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
        # Gross corruption guard: a packet-loss / decode artifact frame must never
        # reach the detector, or a phantom blob becomes a real autopilot command.
        # None disables the gate (used by tests feeding synthetic frames).
        self._frame_quality_check = frame_quality_check
        self._period = 1.0 / max(1e-3, float(target_fps))
        self._mirror_retry_interval_s = max(0.0, float(mirror_retry_interval_s))
        self._last_mirror_attempt_mono = 0.0
        self._receiver_restart_interval_s = max(0.0, float(receiver_restart_interval_s))
        self._last_receiver_restart_mono = 0.0
        self._name = name

        self._receiver: Optional[_Receiver] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._last_seq: Optional[int] = None
        self._warned_size = False
        # Health stats (read from the GUI thread for the status line).
        self._stats_lock = threading.Lock()
        self._frames = 0
        self._rejected = 0
        self._fps = 0.0
        self._last_frame_mono: Optional[float] = None
        self._started_mono: Optional[float] = None

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def stats(self) -> dict:
        """Thread-safe snapshot for a UI status line."""
        now = time.monotonic()
        with self._stats_lock:
            last = self._last_frame_mono
            return {
                "running": self.is_running(),
                "frames": self._frames,
                "rejected": self._rejected,
                "fps": self._fps,
                "last_frame_age_s": (now - last) if last is not None else None,
                "age_since_start_s": (now - self._started_mono) if self._started_mono else None,
            }

    def set_policy(self, policy: TransectPolicy) -> None:
        """Swap the live policy after a runtime model/target change."""
        with self._lock:
            self.policy = policy
            self.policy.reset()

    def start(self) -> None:
        with self._lock:
            if self.is_running():
                return
            self._stop.clear()
            self._last_seq = None
            with self._stats_lock:
                self._frames = 0
                self._rejected = 0
                self._fps = 0.0
                self._last_frame_mono = None
                self._started_mono = time.monotonic()
            self.policy.reset()
            try:
                self.detector.reset()
            except Exception:
                pass
            self._ensure_mirror(force=True)
            self._receiver = self._receiver_factory()
            self._receiver.start()
            self._last_receiver_restart_mono = time.monotonic()
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

    def _ensure_mirror(self, *, force: bool = False) -> None:
        if self._mirror_setter is None:
            return
        now = time.monotonic()
        if not force:
            if self._mirror_retry_interval_s <= 0.0:
                return
            if now - self._last_mirror_attempt_mono < self._mirror_retry_interval_s:
                return
        self._last_mirror_attempt_mono = now
        try:
            self._mirror_setter(True)
        except Exception as exc:
            logger.warning("[%s] mirror enable failed: %s", self._name, exc)

    def _mirror_needs_refresh(self, now: float) -> bool:
        with self._stats_lock:
            frames = self._frames
            last = self._last_frame_mono
        if frames <= 0:
            return True
        return last is not None and now - last > 2.0

    def _receiver_needs_restart(self, now: float) -> bool:
        if self._receiver_restart_interval_s <= 0.0:
            return False
        if now - self._last_receiver_restart_mono < self._receiver_restart_interval_s:
            return False
        with self._stats_lock:
            frames = self._frames
            last = self._last_frame_mono
            started = self._started_mono
        if frames <= 0 and started is not None:
            return now - started >= self._receiver_restart_interval_s
        return last is not None and now - last > max(2.0, self._receiver_restart_interval_s)

    def _restart_receiver(self) -> None:
        with self._lock:
            if self._stop.is_set():
                return
            old = self._receiver
            if old is not None:
                try:
                    old.stop()
                except Exception as exc:
                    logger.debug("[%s] receiver restart stop: %s", self._name, exc)
            self._last_seq = None
            self._receiver = self._receiver_factory()
            self._receiver.start()
            self._last_receiver_restart_mono = time.monotonic()
        logger.info("[%s] restarted raw receiver while waiting for frames", self._name)

    def _run(self) -> None:
        while not self._stop.is_set():
            t0 = time.monotonic()
            if self._mirror_needs_refresh(t0):
                self._ensure_mirror(force=False)
            if self._receiver_needs_restart(t0):
                self._restart_receiver()
            try:
                receiver = self._receiver
                if receiver is None:
                    return
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
        now = time.monotonic()
        with self._stats_lock:
            self._frames += 1
            if self._last_frame_mono is not None:
                dt = now - self._last_frame_mono
                if dt > 0:
                    inst = 1.0 / dt
                    self._fps = inst if self._fps <= 0 else 0.3 * inst + 0.7 * self._fps
            self._last_frame_mono = now
        try:
            frame = np.frombuffer(data, np.uint8).reshape((self.height, self.width, 3)).copy()
        except Exception as exc:
            logger.debug("[%s] frame reshape failed: %s", self._name, exc)
            return

        # Reject gross corruption (H.264 loss / startup artifacts) BEFORE the
        # detector. A smeared/green frame can yield a confident-looking phantom
        # blob, and -- once Optical Hold is engaged -- that becomes a real thrust
        # command. Feeding the policy an explicit no-target instead makes it coast
        # a few frames and then drop the lock on sustained corruption, exactly like
        # a real detection dropout (the ROV also falls back to manual on stale lock).
        if self._frame_quality_check is not None:
            try:
                reason = self._frame_quality_check(frame)
            except Exception:
                reason = None
            if reason is not None:
                with self._stats_lock:
                    self._rejected += 1
                try:
                    obs = TransectObservation.no_target(ts=now)
                    est = self.policy.evaluate(obs)
                    self._on_estimate(est, obs, frame)
                except Exception as exc:
                    logger.debug("[%s] rejected-frame process failed: %s", self._name, exc)
                return

        try:
            obs = self.detector.detect(frame)
            est = self.policy.evaluate(obs)
            self._on_estimate(est, obs, frame)
        except Exception as exc:
            logger.debug("[%s] process failed: %s", self._name, exc)
