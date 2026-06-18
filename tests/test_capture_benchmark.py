import numpy as np
import pytest

from recording.capture_benchmark import (
    CaptureSample,
    ImageQuality,
    blockiness_score,
    chroma_speckle_score,
    classify_error,
    classify_image_bytes,
    percentile,
    stats_block,
    summarize,
)


def test_percentile_interpolates():
    values = [0.0, 10.0, 20.0, 30.0, 40.0]
    assert percentile(values, 0.0) == 0.0
    assert percentile(values, 1.0) == 40.0
    assert percentile(values, 0.5) == 20.0
    assert percentile(values, 0.95) == pytest.approx(38.0)


def test_percentile_handles_empty_and_single():
    assert percentile([], 0.5) is None
    assert percentile([7.0], 0.95) == 7.0


def test_stats_block_summary():
    block = stats_block([1.0, 2.0, 3.0, 4.0])
    assert block["count"] == 4
    assert block["min"] == 1.0
    assert block["max"] == 4.0
    assert block["mean"] == pytest.approx(2.5)
    assert block["p50"] == pytest.approx(2.5)


def test_stats_block_empty():
    assert stats_block([]) == {"count": 0}


def test_classify_error_buckets():
    assert classify_error("Could not capture stereo pair within 20.0 ms (best delta 180.0 ms ...)") == "pair_gate_exceeded"
    assert classify_error("No such stream: Primary Camera") == "stream_not_running"
    assert classify_error("No onboard snapshot frame available for 'Aux Camera'") == "timeout"
    assert classify_error("Stream 'Aux Camera' is disabled in config") == "stream_disabled"
    assert classify_error("connection refused") == "rpc_error"
    assert classify_error("") == "unknown"


def test_blockiness_flat_image_is_low():
    flat = np.full((64, 64, 3), 120, dtype=np.uint8)
    assert blockiness_score(flat) == pytest.approx(0.0, abs=0.05)


def test_blockiness_detects_block_grid():
    # Build an image whose only discontinuities land on 8-pixel boundaries.
    luma = np.zeros((64, 64), dtype=np.float32)
    for k in range(0, 64, 8):
        luma[k:k + 8, :] += (k // 8) * 20
        luma[:, k:k + 8] += (k // 8) * 20
    frame = np.repeat(luma[:, :, None], 3, axis=2).astype(np.uint8)
    blocky = blockiness_score(frame)

    # Random noise spreads discontinuity everywhere, so relative blockiness is low.
    rng = np.random.default_rng(0)
    noise = rng.integers(0, 255, size=(64, 64, 3), dtype=np.uint8)
    assert blocky > blockiness_score(noise)
    assert blocky > 1.0


def test_chroma_speckle_clean_vs_speckled():
    pytest.importorskip("cv2")
    rng = np.random.default_rng(1)
    # Smooth natural-ish image: low chroma speckle.
    clean = np.zeros((128, 128, 3), dtype=np.uint8)
    clean[:, :, 0] = np.linspace(40, 200, 128, dtype=np.uint8)[None, :]
    clean[:, :, 2] = np.linspace(200, 40, 128, dtype=np.uint8)[None, :]
    # Isolated high-saturation chroma speckle on ~8% of pixels.
    speckled = clean.copy()
    mask = rng.random((128, 128)) < 0.08
    speckled[mask] = rng.integers(0, 255, size=(int(mask.sum()), 3), dtype=np.uint8)

    assert chroma_speckle_score(speckled) > chroma_speckle_score(clean)
    assert chroma_speckle_score(clean) < 0.6


def test_classify_image_bytes_flags_chroma_corruption():
    cv2 = pytest.importorskip("cv2")
    rng = np.random.default_rng(2)
    frame = np.full((96, 96, 3), 110, dtype=np.uint8)
    mask = rng.random((96, 96)) < 0.25
    frame[mask] = rng.integers(0, 255, size=(int(mask.sum()), 3), dtype=np.uint8)
    ok, buf = cv2.imencode(".png", frame)
    assert ok
    quality = classify_image_bytes(buf.tobytes())
    assert "chroma_corruption" in quality.reasons
    assert quality.chroma_speckle is not None and quality.chroma_speckle > 0.6


def test_classify_image_bytes_decode_failure():
    quality = classify_image_bytes(b"not-an-image")
    assert "decode_failed" in quality.reasons


def test_classify_image_bytes_empty():
    quality = classify_image_bytes(b"")
    assert "empty" in quality.reasons


def test_classify_image_bytes_roundtrip():
    cv2 = pytest.importorskip("cv2")
    frame = np.full((48, 64, 3), 90, dtype=np.uint8)
    ok, buf = cv2.imencode(".jpg", frame)
    assert ok
    quality = classify_image_bytes(buf.tobytes())
    assert quality.reasons == []
    assert quality.width == 64 and quality.height == 48
    assert quality.blockiness is not None


def test_summarize_snapshot_run():
    samples = [
        CaptureSample(kind="snapshot", ok=True, latency_ms=40.0, images=[ImageQuality(blockiness=0.2)]),
        CaptureSample(
            kind="snapshot",
            ok=True,
            latency_ms=60.0,
            images=[ImageQuality(reasons=["decode_failed"], blockiness=5.0)],
        ),
        CaptureSample(kind="snapshot", ok=False, error="timeout", error_kind="timeout"),
    ]
    summary = summarize(samples)
    assert summary["captures"] == 3
    assert summary["ok"] == 2
    assert summary["failed"] == 1
    assert summary["success_rate"] == pytest.approx(2 / 3)
    assert summary["errors"] == {"timeout": 1}
    assert summary["images"]["total"] == 2
    assert summary["images"]["clean"] == 1
    assert summary["images"]["flagged"] == 1
    assert summary["images"]["reasons"] == {"decode_failed": 1}
    assert summary["latency_ms"]["p50"] == pytest.approx(50.0)


def test_summarize_stereo_includes_delta_and_attempts():
    samples = [
        CaptureSample(
            kind="stereo",
            ok=True,
            latency_ms=30.0,
            images=[ImageQuality(label="left", blockiness=0.1), ImageQuality(label="right", blockiness=0.1)],
            pair_delta_ms=12.0,
            attempts=1,
        ),
        CaptureSample(
            kind="stereo",
            ok=True,
            latency_ms=35.0,
            images=[ImageQuality(label="left", blockiness=0.1), ImageQuality(label="right", blockiness=0.1)],
            pair_delta_ms=180.0,
            attempts=3,
        ),
    ]
    summary = summarize(samples)
    assert summary["images"]["total"] == 4
    assert summary["pair_delta_ms"]["max"] == 180.0
    assert summary["pair_delta_ms"]["p50"] == pytest.approx(96.0)
    assert summary["attempts"]["max"] == 3
