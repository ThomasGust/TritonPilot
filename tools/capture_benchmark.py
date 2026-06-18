#!/usr/bin/env python3
"""Media-capture benchmark harness for TritonPilot <-> TritonOS.

Fires repeated still and/or stereo captures against a running ROV and reports
artifact rate, capture latency, and (for stereo) the pair-delta distribution.
Run it before and after each capture-rework phase to get before/after numbers
instead of impressions.

Prerequisite: the target ROV streams must already be running (the GUI is up, or
they were started manually). Captures go over the existing video RPC and do not
open a local UDP receiver, so this is safe to run alongside the live GUI.

Examples (PowerShell):
    python .\tools\capture_benchmark.py --mode both --count 30
    python .\tools\capture_benchmark.py --mode snapshot --stream "Primary Camera" --count 50
    python .\tools\capture_benchmark.py --mode stereo --count 40 --max-pair-delta-ms 1000
    python .\tools\capture_benchmark.py --endpoint tcp://192.168.1.4:5555 --no-save

A `--max-pair-delta-ms` larger than the configured gate is useful for baseline
runs: it lets every pair succeed so you can see the *real* delta distribution
rather than only counting gate failures.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

# Allow running as a plain script: python tools/capture_benchmark.py
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
from recording.capture_benchmark import (
    CaptureSample,
    classify_error,
    classify_image_bytes,
    format_report,
    summarize,
)
from recording.save_location import DEFAULT_RECORDINGS_DIR
from stereo.pairs import load_stereo_pairs
from video.cam import RemoteCameraManager


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="TritonPilot media-capture benchmark")
    ap.add_argument("--streams", default=str(config.STREAMS_FILE), help="streams.json path")
    ap.add_argument("--endpoint", default=None, help="override ROV video RPC endpoint")
    ap.add_argument("--mode", choices=["snapshot", "stereo", "both"], default="both")
    ap.add_argument("--count", type=int, default=20, help="captures per mode")
    ap.add_argument("--stream", default=None, help="snapshot stream name (default: first pane)")
    ap.add_argument("--pair", default=None, help="stereo pair name (default: first configured)")
    ap.add_argument("--interval", type=float, default=0.5, help="seconds between captures")
    ap.add_argument("--timeout", type=float, default=2.0, help="per-capture RPC timeout (s)")
    ap.add_argument(
        "--max-pair-delta-ms",
        type=float,
        default=None,
        help="stereo gate override (default: pair config). Use a large value for honest baselines.",
    )
    ap.add_argument("--output", default=None, help="output directory (default: recordings/capture_benchmarks/<ts>)")
    ap.add_argument("--no-save", action="store_true", help="do not write captured images to disk")
    return ap.parse_args(argv)


def _run_snapshot_loop(manager: RemoteCameraManager, args, stream: str, out_dir: Path | None) -> list[CaptureSample]:
    samples: list[CaptureSample] = []
    print(f"[snapshot] stream={stream!r} count={args.count}")
    for i in range(int(args.count)):
        t0 = time.perf_counter()
        try:
            packet = manager.capture_onboard_snapshot(stream, timeout_s=float(args.timeout))
            latency_ms = (time.perf_counter() - t0) * 1000.0
            data = bytes(getattr(packet, "image_bytes", b"") or b"")
            quality = classify_image_bytes(data)
            sample = CaptureSample(
                kind="snapshot",
                ok=True,
                latency_ms=latency_ms,
                images=[quality],
            )
            if out_dir is not None:
                ext = str(getattr(packet, "extension", "jpg") or "jpg")
                prefix = "BAD_" if quality.flagged else ""
                (out_dir / f"{prefix}snapshot_{i:04d}.{ext}").write_bytes(data)
        except Exception as exc:
            latency_ms = (time.perf_counter() - t0) * 1000.0
            sample = CaptureSample(
                kind="snapshot",
                ok=False,
                latency_ms=latency_ms,
                error=str(exc),
                error_kind=classify_error(str(exc)),
            )
        samples.append(sample)
        _print_progress(i, sample)
        time.sleep(max(0.0, float(args.interval)))
    return samples


def _run_stereo_loop(manager: RemoteCameraManager, args, pair, out_dir: Path | None) -> list[CaptureSample]:
    samples: list[CaptureSample] = []
    gate = float(args.max_pair_delta_ms) if args.max_pair_delta_ms is not None else float(pair.max_pair_delta_ms)
    print(f"[stereo] pair={pair.name!r} left={pair.left!r} right={pair.right!r} gate={gate:.0f}ms count={args.count}")
    for i in range(int(args.count)):
        t0 = time.perf_counter()
        try:
            packet = manager.capture_onboard_stereo_pair(
                pair.left,
                pair.right,
                timeout_s=float(args.timeout),
                max_pair_delta_ms=gate,
            )
            latency_ms = (time.perf_counter() - t0) * 1000.0
            left_bytes = bytes(getattr(packet.left, "image_bytes", b"") or b"")
            right_bytes = bytes(getattr(packet.right, "image_bytes", b"") or b"")
            left_q = classify_image_bytes(left_bytes, label="left")
            right_q = classify_image_bytes(right_bytes, label="right")
            sample = CaptureSample(
                kind="stereo",
                ok=True,
                latency_ms=latency_ms,
                images=[left_q, right_q],
                pair_delta_ms=float(getattr(packet, "pair_delta_ms", 0.0) or 0.0),
                attempts=int(getattr(packet, "attempts", 1) or 1),
                timestamp_source=str(getattr(packet, "timestamp_source", "") or ""),
            )
            if out_dir is not None:
                flagged = sample.flagged
                prefix = "BAD_" if flagged else ""
                (out_dir / f"{prefix}pair_{i:04d}_left.{getattr(packet.left, 'extension', 'jpg')}").write_bytes(left_bytes)
                (out_dir / f"{prefix}pair_{i:04d}_right.{getattr(packet.right, 'extension', 'jpg')}").write_bytes(right_bytes)
        except Exception as exc:
            latency_ms = (time.perf_counter() - t0) * 1000.0
            sample = CaptureSample(
                kind="stereo",
                ok=False,
                latency_ms=latency_ms,
                error=str(exc),
                error_kind=classify_error(str(exc)),
            )
        samples.append(sample)
        _print_progress(i, sample)
        time.sleep(max(0.0, float(args.interval)))
    return samples


def _print_progress(i: int, sample: CaptureSample) -> None:
    if not sample.ok:
        print(f"  [{i:04d}] FAIL ({sample.error_kind}): {sample.error}")
        return
    extra = ""
    if sample.pair_delta_ms is not None:
        extra = f" delta={sample.pair_delta_ms:.1f}ms attempts={sample.attempts}"
    flag = " FLAGGED" if sample.flagged else ""
    print(f"  [{i:04d}] ok {sample.latency_ms:.0f}ms{extra}{flag}")


def _sample_to_dict(sample: CaptureSample) -> dict:
    return {
        "kind": sample.kind,
        "ok": sample.ok,
        "latency_ms": sample.latency_ms,
        "error": sample.error,
        "error_kind": sample.error_kind,
        "pair_delta_ms": sample.pair_delta_ms,
        "attempts": sample.attempts,
        "timestamp_source": sample.timestamp_source,
        "images": [
            {
                "label": img.label,
                "reasons": img.reasons,
                "blockiness": img.blockiness,
                "byte_count": img.byte_count,
                "width": img.width,
                "height": img.height,
            }
            for img in sample.images
        ],
    }


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    manager = RemoteCameraManager(args.streams)
    if args.endpoint:
        manager.set_rpc_endpoint(args.endpoint)
    endpoint = str(getattr(manager.rov, "endpoint", config.VIDEO_RPC_ENDPOINT))
    print(f"ROV video RPC: {endpoint}")

    out_dir: Path | None = None
    if not args.no_save:
        if args.output:
            out_dir = Path(args.output)
        else:
            stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
            out_dir = Path(DEFAULT_RECORDINGS_DIR) / "capture_benchmarks" / stamp
        out_dir.mkdir(parents=True, exist_ok=True)
        print(f"Saving images to: {out_dir}")

    runs: dict[str, list[CaptureSample]] = {}

    if args.mode in ("snapshot", "both"):
        stream = args.stream or (manager.list_available()[0] if manager.list_available() else None)
        if not stream:
            print("No streams available for snapshot mode", file=sys.stderr)
        else:
            runs["snapshot"] = _run_snapshot_loop(manager, args, stream, out_dir)

    if args.mode in ("stereo", "both"):
        pairs = load_stereo_pairs(args.streams)
        pair = None
        if args.pair:
            pair = next((p for p in pairs if p.name == args.pair), None)
            if pair is None:
                print(f"Stereo pair {args.pair!r} not found in {args.streams}", file=sys.stderr)
        elif pairs:
            pair = pairs[0]
        if pair is None:
            print("No stereo pair available for stereo mode", file=sys.stderr)
        else:
            runs["stereo"] = _run_stereo_loop(manager, args, pair, out_dir)

    print()
    report: dict[str, dict] = {}
    for kind, samples in runs.items():
        summary = summarize(samples)
        report[kind] = summary
        print(format_report(kind, summary))
        print()

    if out_dir is not None:
        payload = {
            "endpoint": endpoint,
            "args": vars(args),
            "summary": report,
            "samples": {kind: [_sample_to_dict(s) for s in samples] for kind, samples in runs.items()},
        }
        (out_dir / "summary.json").write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        print(f"Wrote {out_dir / 'summary.json'}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
