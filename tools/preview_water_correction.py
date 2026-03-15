from __future__ import annotations

import argparse
from pathlib import Path

import cv2
import numpy as np

from config import (
    WATER_CORRECTION_AIR_HFOV_DEG,
    WATER_CORRECTION_K1,
    WATER_CORRECTION_K2,
    WATER_CORRECTION_K3,
    WATER_CORRECTION_TARGET_HFOV_DEG,
    WATER_CORRECTION_ZOOM,
)
from video.frame_correction import WaterCorrection


def _parse_zoom_values(value: str) -> list[float]:
    vals = []
    for item in str(value).split(","):
        item = item.strip()
        if not item:
            continue
        vals.append(float(item))
    return vals or [WATER_CORRECTION_ZOOM]


def _label(frame: np.ndarray, text: str) -> np.ndarray:
    out = frame.copy()
    cv2.rectangle(out, (12, 12), (250, 52), (0, 0, 0), -1)
    cv2.putText(out, text, (20, 40), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 2, cv2.LINE_AA)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Preview DWE bench-to-water correction on a still image.")
    parser.add_argument("image", help="Path to the input image.")
    parser.add_argument(
        "--zooms",
        default=f"{max(0.5, WATER_CORRECTION_ZOOM - 0.1):.2f},{WATER_CORRECTION_ZOOM:.2f},{WATER_CORRECTION_ZOOM + 0.1:.2f}",
        help="Comma-separated zoom trim values to compare.",
    )
    parser.add_argument("--out", default="", help="Output image path. Defaults next to the input image.")
    args = parser.parse_args()

    image_path = Path(args.image)
    frame = cv2.imread(str(image_path), cv2.IMREAD_COLOR)
    if frame is None:
        raise SystemExit(f"Could not read image: {image_path}")

    tiles = [_label(frame, "original")]
    for zoom in _parse_zoom_values(args.zooms):
        corr = WaterCorrection(
            zoom=zoom,
            k1=WATER_CORRECTION_K1,
            k2=WATER_CORRECTION_K2,
            k3=WATER_CORRECTION_K3,
            air_hfov_deg=WATER_CORRECTION_AIR_HFOV_DEG,
            target_hfov_deg=WATER_CORRECTION_TARGET_HFOV_DEG,
        )
        tiles.append(_label(corr.apply(frame), f"zoom={zoom:.2f}"))

    grid = np.concatenate(tiles, axis=1)
    out_path = Path(args.out) if args.out else image_path.with_name(f"{image_path.stem}_water_preview.png")
    cv2.imwrite(str(out_path), grid)
    print(out_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
