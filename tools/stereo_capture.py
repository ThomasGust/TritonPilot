"""Command-line stereo pair capture helper.

This is intended for calibration days: start TritonOS, point both cameras at
the calibration board, and save a manifest plus left/right image pairs for
TritonAnalysis.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from recording.save_location import DEFAULT_RECORDINGS_DIR
from stereo.capture import StereoCaptureSession
from stereo.pairs import StereoPairConfig, load_stereo_pairs
from video.cam import RemoteCameraManager


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Capture timestamped stereo image pairs from ROV streams.")
    parser.add_argument("--streams", default="data/streams.json", help="TritonPilot streams.json path.")
    parser.add_argument("--pair", default="", help="Stereo pair name. Defaults to the first enabled pair.")
    parser.add_argument("--list-pairs", action="store_true", help="List configured stereo pairs and exit.")
    parser.add_argument("--count", type=int, default=1, help="Number of stereo pairs to capture.")
    parser.add_argument("--interval-s", type=float, default=0.35, help="Delay between burst captures.")
    parser.add_argument("--wait-s", type=float, default=2.0, help="Per-pair wait timeout.")
    parser.add_argument("--output-root", default=str(DEFAULT_RECORDINGS_DIR), help="Root for stereo_sessions output.")
    parser.add_argument("--session-name", default="", help="Optional session folder name.")
    return parser


def _select_pair(pairs: list[StereoPairConfig], name: str) -> StereoPairConfig:
    if not pairs:
        raise SystemExit("No enabled stereo pairs are configured.")
    if not name:
        return pairs[0]
    for pair in pairs:
        if pair.name.lower() == name.lower():
            return pair
    raise SystemExit(f"Unknown enabled stereo pair: {name}")


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    all_pairs = load_stereo_pairs(args.streams, include_disabled=True)
    if args.list_pairs:
        if not all_pairs:
            print("No stereo pairs configured.")
            return 0
        for pair in all_pairs:
            state = "enabled" if pair.enabled else "disabled"
            print(f"{pair.name} ({state}): {pair.left} + {pair.right}, rig_id={pair.rig_id}")
        return 0

    pair = _select_pair([pair for pair in all_pairs if pair.enabled], args.pair)
    manager = RemoteCameraManager(args.streams)
    session = StereoCaptureSession(
        manager,
        pair,
        output_root=Path(args.output_root),
        session_name=args.session_name or None,
        close_on_stop=True,
    )

    try:
        session_dir = session.start()
        print(f"Stereo session: {session_dir}")
        captures = session.capture_burst(args.count, interval_s=args.interval_s, wait_s=args.wait_s)
        for item in captures:
            print(
                f"pair {item['index']:06d}: delta={item['pair_delta_ms']:.1f} ms "
                f"left={item['left_path']} right={item['right_path']}"
            )
        print(f"Manifest: {session.manifest_path}")
        return 0
    finally:
        session.stop()


if __name__ == "__main__":
    raise SystemExit(main())
