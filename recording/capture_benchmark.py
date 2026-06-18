"""Analysis helpers for the media-capture benchmark harness.

This module is intentionally free of any network, GStreamer, or Qt
dependencies so the statistics and image-quality scoring can be unit tested
without live ROV hardware. The hardware-driven driver lives in
``tools/capture_benchmark.py`` and feeds :class:`CaptureSample` records here.

Two kinds of signal are produced per captured image:

* hard rejection reasons reused from :mod:`video.frame_quality`
  (green/blank/collapse frames), plus ``decode_failed`` for unreadable bytes.
* a continuous ``chroma_speckle`` score. H.264 decode corruption from dropped
  reference frames shows up as isolated high-saturation chroma noise (rainbow
  speckle) and blocky garbage. The score is the mean absolute high-pass of the
  Cr/Cb chroma channels (chroma minus its 3x3 median). It cleanly separated
  clean vs corrupted bench frames (clean stills ~0.36-0.5; corrupted stereo
  frames ~0.9-1.3), so a frame is flagged ``chroma_corruption`` above
  :data:`CHROMA_SPECKLE_FLAG`.
* a continuous ``blockiness`` score (luminance block-boundary discontinuity),
  reported as a distribution for additional signal.

Both scores are reported as distributions, not just pass/fail, so thresholds
can be re-tuned from bench data.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Iterable

import numpy as np

from video.frame_quality import live_frame_rejection_reason


# Frames whose chroma-speckle score exceeds this are flagged as corrupted.
# Calibrated on bench data: the score is scene-dependent -- smooth scenes (e.g.
# underwater) sit ~0.3, busy natural scenes (foliage/wood) reach ~0.62 while
# still clean, and genuinely corrupted frames are ~0.9-1.8. The threshold sits
# in that gap so detail-rich-but-clean frames are not false-flagged.
CHROMA_SPECKLE_FLAG = 0.85


# --------------------------------------------------------------------------- #
# Sample records
# --------------------------------------------------------------------------- #
@dataclass
class ImageQuality:
    """Quality signal for one captured image (a snapshot, or one stereo side)."""

    label: str = "image"
    reasons: list[str] = field(default_factory=list)
    chroma_speckle: float | None = None
    blockiness: float | None = None
    byte_count: int = 0
    width: int = 0
    height: int = 0

    @property
    def flagged(self) -> bool:
        return bool(self.reasons)


@dataclass
class CaptureSample:
    """One capture attempt (a snapshot or a stereo pair) and its outcome."""

    kind: str  # "snapshot" | "stereo"
    ok: bool
    latency_ms: float | None = None
    error: str = ""
    error_kind: str = ""
    images: list[ImageQuality] = field(default_factory=list)
    pair_delta_ms: float | None = None
    attempts: int | None = None
    timestamp_source: str = ""

    @property
    def flagged(self) -> bool:
        return any(image.flagged for image in self.images)


# --------------------------------------------------------------------------- #
# Statistics
# --------------------------------------------------------------------------- #
def percentile(values: Iterable[float], p: float) -> float | None:
    """Linear-interpolated percentile. ``p`` is a fraction in ``[0, 1]``."""

    data = sorted(float(v) for v in values if v is not None)
    if not data:
        return None
    if len(data) == 1:
        return data[0]
    p = min(1.0, max(0.0, float(p)))
    k = (len(data) - 1) * p
    lo = math.floor(k)
    hi = math.ceil(k)
    if lo == hi:
        return data[int(k)]
    return data[lo] * (hi - k) + data[hi] * (k - lo)


def stats_block(values: Iterable[float]) -> dict[str, Any]:
    """Summarize a numeric sample with count/min/p50/p95/max/mean."""

    data = [float(v) for v in values if v is not None]
    if not data:
        return {"count": 0}
    return {
        "count": len(data),
        "min": min(data),
        "p50": percentile(data, 0.50),
        "p95": percentile(data, 0.95),
        "max": max(data),
        "mean": sum(data) / len(data),
    }


def classify_error(message: str) -> str:
    """Bucket an RPC/capture error string into a coarse kind for reporting."""

    text = str(message or "").lower()
    if not text:
        return "unknown"
    if "best delta" in text or "max_pair_delta" in text:
        return "pair_gate_exceeded"
    if "no such stream" in text or "unknown stream" in text or "not running" in text:
        return "stream_not_running"
    if "no onboard snapshot" in text or "no cached" in text or "timed out" in text or "timeout" in text:
        return "timeout"
    if "disabled" in text:
        return "stream_disabled"
    return "rpc_error"


# --------------------------------------------------------------------------- #
# Image-quality scoring
# --------------------------------------------------------------------------- #
def _luminance(frame_bgr: np.ndarray) -> np.ndarray:
    arr = np.asarray(frame_bgr)
    if arr.ndim == 2:
        return arr.astype(np.float32, copy=False)
    b = arr[:, :, 0].astype(np.float32)
    g = arr[:, :, 1].astype(np.float32)
    r = arr[:, :, 2].astype(np.float32)
    return 0.114 * b + 0.587 * g + 0.299 * r


def blockiness_score(frame_bgr: np.ndarray, *, block: int = 8) -> float:
    """Relative discontinuity at block boundaries vs. inside blocks.

    Returns ``(boundary_diff - within_diff) / (within_diff + eps)`` averaged
    over both axes. Clean natural images sit near zero; macroblock corruption
    and heavy ringing push it up.
    """

    luma = _luminance(frame_bgr)
    if luma.ndim != 2 or min(luma.shape) <= block:
        return 0.0
    scores: list[float] = []
    for axis in (0, 1):
        diff = np.abs(np.diff(luma, axis=axis))
        n = diff.shape[axis]
        if n <= 0:
            continue
        boundary_idx = np.arange(block - 1, n, block)
        if boundary_idx.size == 0:
            continue
        boundary = np.take(diff, boundary_idx, axis=axis)
        keep = np.ones(n, dtype=bool)
        keep[boundary_idx] = False
        within = np.compress(keep, diff, axis=axis)
        b = float(boundary.mean()) if boundary.size else 0.0
        a = float(within.mean()) if within.size else 0.0
        scores.append((b - a) / (a + 1e-3))
    return float(np.mean(scores)) if scores else 0.0


def chroma_speckle_score(frame_bgr: np.ndarray) -> float:
    """Mean absolute high-pass of the Cr/Cb chroma channels.

    H.264 decode corruption from dropped reference frames produces isolated
    high-saturation chroma noise; subtracting a 3x3 median isolates that speckle
    while leaving spatially-coherent natural color edges alone. Requires cv2.
    """

    import cv2

    ycc = cv2.cvtColor(np.ascontiguousarray(frame_bgr), cv2.COLOR_BGR2YCrCb)
    total = 0.0
    for index in (1, 2):  # Cr, Cb
        channel = ycc[:, :, index]
        median = cv2.medianBlur(channel, 3)
        total += float(np.abs(channel.astype(np.float32) - median.astype(np.float32)).mean())
    return total / 2.0


def classify_image_bytes(
    data: bytes,
    *,
    label: str = "image",
    chroma_flag: float = CHROMA_SPECKLE_FLAG,
) -> ImageQuality:
    """Decode encoded image bytes and score them. cv2 is imported lazily."""

    quality = ImageQuality(label=label, byte_count=len(data or b""))
    if not data:
        quality.reasons.append("empty")
        return quality
    try:
        import cv2
    except Exception:  # pragma: no cover - cv2 is a runtime dependency
        quality.reasons.append("cv2_unavailable")
        return quality
    buf = np.frombuffer(data, dtype=np.uint8)
    frame = cv2.imdecode(buf, cv2.IMREAD_COLOR)
    if frame is None or frame.size == 0:
        quality.reasons.append("decode_failed")
        return quality
    quality.height, quality.width = int(frame.shape[0]), int(frame.shape[1])
    reason = live_frame_rejection_reason(frame)
    if reason is not None:
        quality.reasons.append(reason)
    quality.chroma_speckle = chroma_speckle_score(frame)
    quality.blockiness = blockiness_score(frame)
    if quality.chroma_speckle > float(chroma_flag):
        quality.reasons.append("chroma_corruption")
    return quality


# --------------------------------------------------------------------------- #
# Aggregation + reporting
# --------------------------------------------------------------------------- #
def summarize(samples: list[CaptureSample]) -> dict[str, Any]:
    """Aggregate a list of same-kind capture samples into a report dict."""

    total = len(samples)
    ok = [s for s in samples if s.ok]
    failed = [s for s in samples if not s.ok]

    images = [img for s in ok for img in s.images]
    flagged_images = [img for img in images if img.flagged]
    reason_counts: Counter[str] = Counter()
    for img in images:
        reason_counts.update(img.reasons)

    summary: dict[str, Any] = {
        "captures": total,
        "ok": len(ok),
        "failed": len(failed),
        "success_rate": (len(ok) / total) if total else 0.0,
        "errors": dict(Counter(s.error_kind or "unknown" for s in failed)),
        "latency_ms": stats_block(s.latency_ms for s in ok),
        "images": {
            "total": len(images),
            "clean": len(images) - len(flagged_images),
            "flagged": len(flagged_images),
            "reasons": dict(reason_counts),
            "chroma_speckle": stats_block(img.chroma_speckle for img in images),
            "blockiness": stats_block(img.blockiness for img in images),
        },
    }

    deltas = [s.pair_delta_ms for s in ok if s.pair_delta_ms is not None]
    if deltas:
        summary["pair_delta_ms"] = stats_block(deltas)
    attempts = [s.attempts for s in ok if s.attempts is not None]
    if attempts:
        summary["attempts"] = stats_block(attempts)
    return summary


def _fmt_stats(stats: dict[str, Any], *, unit: str = "") -> str:
    if not stats or not stats.get("count"):
        return "n=0"
    suffix = unit
    return (
        f"n={stats['count']} "
        f"min={stats['min']:.1f}{suffix} "
        f"p50={stats['p50']:.1f}{suffix} "
        f"p95={stats['p95']:.1f}{suffix} "
        f"max={stats['max']:.1f}{suffix}"
    )


def format_report(title: str, summary: dict[str, Any]) -> str:
    """Render a human-readable report for one summarized capture run."""

    lines = [f"== {title} =="]
    lines.append(
        f"captures={summary['captures']} ok={summary['ok']} "
        f"failed={summary['failed']} success={summary['success_rate'] * 100:.1f}%"
    )
    if summary.get("errors"):
        errs = ", ".join(f"{k}={v}" for k, v in sorted(summary["errors"].items()))
        lines.append(f"errors: {errs}")
    lines.append(f"latency: {_fmt_stats(summary.get('latency_ms', {}), unit='ms')}")

    if "pair_delta_ms" in summary:
        lines.append(f"pair_delta: {_fmt_stats(summary['pair_delta_ms'], unit='ms')}")
    if "attempts" in summary:
        lines.append(f"attempts: {_fmt_stats(summary['attempts'])}")

    img = summary.get("images", {})
    lines.append(
        f"images: total={img.get('total', 0)} clean={img.get('clean', 0)} "
        f"flagged={img.get('flagged', 0)}"
    )
    if img.get("reasons"):
        reasons = ", ".join(f"{k}={v}" for k, v in sorted(img["reasons"].items()))
        lines.append(f"  reasons: {reasons}")
    lines.append(f"  chroma_speckle: {_fmt_stats(img.get('chroma_speckle', {}))}")
    lines.append(f"  blockiness: {_fmt_stats(img.get('blockiness', {}))}")
    return "\n".join(lines)
