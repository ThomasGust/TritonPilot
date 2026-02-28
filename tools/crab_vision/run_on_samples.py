"""
Run the crab detector on the 4 sample images provided in TritonPilot/data/img/crab/sample.

Usage (from the TritonPilot/TritonPilot directory):
    python -m tools.crab_vision.run_on_samples

Or with custom output dir:
    python -m tools.crab_vision.run_on_samples --out data/img/crab/output
"""

from __future__ import annotations

import argparse
from pathlib import Path

import cv2

from .crab_detector import CrabDetector, default_template_paths


def main() -> int:
    here = Path(__file__).resolve()
    repo_root = here.parents[2]  # .../TritonPilot/TritonPilot

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--samples",
        type=Path,
        default=repo_root / "data" / "img" / "crab" / "sample",
        help="Directory with sample images (Crab Sample 1..4.jpg)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=repo_root / "data" / "img" / "crab" / "output",
        help="Directory to write annotated outputs",
    )
    args = parser.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)

    detector = CrabDetector(default_template_paths(repo_root))

    sample_paths = sorted(args.samples.glob("Crab Sample *.jpg"))
    if not sample_paths:
        raise FileNotFoundError(f"No sample images found in {args.samples}")

    print("Running crab detector on:")
    for p in sample_paths:
        print("  -", p.name)

    print("")
    for p in sample_paths:
        img = cv2.imread(str(p), cv2.IMREAD_COLOR)
        if img is None:
            print(f"SKIP (couldn't read): {p}")
            continue

        green = detector.detect_green(img)
        annotated = CrabDetector.draw_count_and_boxes(img, green, count_label="GREEN CRABS")

        out_path = args.out / f"{p.stem}_annotated.png"
        cv2.imwrite(str(out_path), annotated)

        # Be robust when --out is provided as a relative path.
        try:
            rel = out_path.resolve().relative_to(repo_root)
            out_txt = str(rel)
        except Exception:
            out_txt = str(out_path)
        print(f"{p.name}: green={len(green)}  ->  {out_txt}")

    print("\nDone.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
