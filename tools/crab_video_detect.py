from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

import cv2

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from analysis.crab_detector_cv import (  # noqa: E402
    detect_crabs_in_video,
    detection_summary_text,
    draw_crab_detections,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Find and count competition crab image copies in a video.",
    )
    parser.add_argument("video", help="Path to the video file to scan.")
    parser.add_argument("--start", type=float, default=0.0, help="First second to sample.")
    parser.add_argument("--end", type=float, default=None, help="Last second to sample.")
    parser.add_argument(
        "--interval",
        type=float,
        default=0.5,
        help="Seconds between sampled frames.",
    )
    parser.add_argument(
        "--max-count",
        type=int,
        default=12,
        help="Soft upper bound used to reject implausible artifact-heavy frames.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=REPO_ROOT / "results" / "crab_video_detection",
        help="Directory for annotated output images and CSV summary.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = detect_crabs_in_video(
        args.video,
        start_seconds=args.start,
        end_seconds=args.end,
        sample_interval_seconds=args.interval,
        max_reasonable_count=args.max_count,
    )
    if result is None:
        print("No crabs were detected in the sampled video frames.")
        return 2

    args.output_dir.mkdir(parents=True, exist_ok=True)
    detection_result = result["detection_result"]
    annotated = draw_crab_detections(result["frame"], detection_result)
    frame_path = args.output_dir / "best_frame.jpg"
    annotated_path = args.output_dir / "best_annotated.jpg"
    mask_path = args.output_dir / "best_mask.png"
    summary_path = args.output_dir / "summary.csv"

    cv2.imwrite(str(frame_path), result["frame"])
    cv2.imwrite(str(annotated_path), annotated)
    cv2.imwrite(str(mask_path), detection_result["unwrapped_mask"])

    with summary_path.open("w", newline="", encoding="utf-8") as summary_file:
        writer = csv.DictWriter(
            summary_file,
            fieldnames=[
                "video",
                "time_seconds",
                "frame_index",
                "total",
                "european_green",
                "jonah",
                "rock",
                "detector",
                "temporal_support",
                "confidence",
                "sharpness",
            ],
        )
        writer.writeheader()
        species_counts = detection_result.get("species_counts", {})
        temporal_vote = result.get("temporal_vote") or {}
        quality = result.get("quality") or {}
        writer.writerow(
            {
                "video": result["video_path"],
                "time_seconds": f"{result['time_seconds']:.3f}",
                "frame_index": result["frame_index"],
                "total": detection_result["count"],
                "european_green": species_counts.get("european_green", 0),
                "jonah": species_counts.get("jonah", 0),
                "rock": species_counts.get("native_rock", 0),
                "detector": detection_result.get("detector", ""),
                "temporal_support": (
                    f"{temporal_vote.get('support_count', '')}/"
                    f"{temporal_vote.get('eligible_count', '')}"
                    if temporal_vote
                    else ""
                ),
                "confidence": f"{quality.get('confidence', 0.0):.3f}",
                "sharpness": f"{quality.get('sharpness', 0.0):.3f}",
            }
        )

    temporal_text = ""
    if result.get("temporal_vote"):
        vote = result["temporal_vote"]
        temporal_text = (
            f" Temporal vote supported this count in "
            f"{vote['support_count']}/{vote['eligible_count']} plausible samples."
        )
    print(
        f"{detection_summary_text(detection_result)} "
        f"Best frame: {result['time_seconds']:.2f}s "
        f"(index {result['frame_index']})."
        f"{temporal_text}"
    )
    print(f"Saved annotated output to {annotated_path}")
    print(f"Saved summary to {summary_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
