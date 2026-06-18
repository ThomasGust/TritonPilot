#!/usr/bin/env python3
"""Headless validation of the stereo burst-recording loop.

Mimics the GUI recording worker (StereoCaptureSession.capture_once back-to-back)
without Qt, so we can confirm the recording mode runs smoothly and reliably and
writes a clean, synced session. Streams must already be running (use
tools/rov_streams_ctl.py start "Primary Camera" "Aux Camera").

    python .\tools\stereo_record_test.py --count 25
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
from recording.capture_benchmark import classify_image_bytes, percentile, stats_block
from stereo.capture import StereoCaptureSession, default_stereo_session_name
from stereo.pairs import load_stereo_pairs
from video.cam import RemoteCameraManager


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Stereo burst recording loop test")
    ap.add_argument("--streams", default=str(config.STREAMS_FILE))
    ap.add_argument("--endpoint", default=None)
    ap.add_argument("--count", type=int, default=25)
    ap.add_argument("--pair-timeout", type=float, default=6.0)
    ap.add_argument("--output", default=None)
    args = ap.parse_args(argv)

    manager = RemoteCameraManager(args.streams)
    if args.endpoint:
        manager.set_rpc_endpoint(args.endpoint)

    pairs = load_stereo_pairs(args.streams)
    if not pairs:
        print("No stereo pair configured", file=sys.stderr)
        return 2
    pair = pairs[0]

    output_root = Path(args.output) if args.output else (Path("recordings") / "stereo_record_tests")
    session = StereoCaptureSession(
        manager, pair, output_root=output_root, session_name=default_stereo_session_name()
    )
    session.start()
    print(f"recording -> {session.session_dir}  pair={pair.name} gate={pair.max_pair_delta_ms}ms")

    latencies: list[float] = []
    deltas: list[float] = []
    failures = 0
    t_start = time.monotonic()
    for i in range(int(args.count)):
        t0 = time.perf_counter()
        try:
            record = session.capture_once(wait_s=float(args.pair_timeout))
        except Exception as exc:
            failures += 1
            print(f"  [{i:03d}] FAIL: {exc}")
            continue
        dt_ms = (time.perf_counter() - t0) * 1000.0
        latencies.append(dt_ms)
        deltas.append(float(record.get("pair_delta_ms", 0.0) or 0.0))
        print(f"  [{i:03d}] idx={record.get('index')} delta={record.get('pair_delta_ms'):.2f}ms {dt_ms:.0f}ms")
    elapsed = time.monotonic() - t_start
    session.stop()

    # Verify saved images are clean.
    flagged = 0
    checked = 0
    speckle: list[float] = []
    for img_path in sorted(session.session_dir.rglob("*.jpg")):
        data = img_path.read_bytes()
        q = classify_image_bytes(data)
        checked += 1
        if q.chroma_speckle is not None:
            speckle.append(q.chroma_speckle)
        if q.flagged:
            flagged += 1

    manifest = json.loads(session.manifest_path.read_text(encoding="utf-8"))
    n_frames = len(manifest.get("frames", []))

    print()
    print(f"== stereo recording: {int(args.count)} requested ==")
    print(f"ok={len(latencies)} failed={failures} elapsed={elapsed:.1f}s rate={len(latencies)/elapsed:.2f} pairs/s")
    print(f"per-pair latency ms: {stats_block(latencies)}")
    print(f"pair_delta ms: {stats_block(deltas)}")
    print(f"manifest frames={n_frames}  images_checked={checked}  flagged(corrupt)={flagged}")
    print(f"chroma_speckle: {stats_block(speckle)}")
    ok = failures == 0 and flagged == 0 and n_frames == len(latencies)
    print("RESULT:", "PASS" if ok else "CHECK")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
