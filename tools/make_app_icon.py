"""Generate TritonPilot app icon assets."""

from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageDraw


GOLD = (242, 184, 58, 255)
GOLD_DARK = (132, 94, 18, 255)
GOLD_LIGHT = (255, 224, 116, 255)
NAVY = (20, 28, 46, 255)
NAVY_2 = (31, 42, 68, 255)


def _scale_points(points: list[tuple[int, int]], scale: int) -> list[tuple[int, int]]:
    return [(x * scale, y * scale) for x, y in points]


def _draw_trident(draw: ImageDraw.ImageDraw, scale: int) -> None:
    def line(points, width, fill):
        draw.line(_scale_points(points, scale), width=width * scale, fill=fill, joint="curve")

    def polygon(points, fill):
        draw.polygon(_scale_points(points, scale), fill=fill)

    # Shadow/outline pass.
    line([(128, 62), (128, 212)], 22, GOLD_DARK)
    line([(80, 88), (80, 132), (108, 146), (128, 146), (148, 146), (176, 132), (176, 88)], 20, GOLD_DARK)
    line([(70, 158), (186, 158)], 18, GOLD_DARK)
    polygon([(128, 38), (109, 73), (147, 73)], GOLD_DARK)
    polygon([(82, 52), (67, 89), (100, 89)], GOLD_DARK)
    polygon([(174, 52), (156, 89), (189, 89)], GOLD_DARK)

    # Gold pass.
    line([(128, 62), (128, 212)], 13, GOLD)
    line([(80, 88), (80, 128), (108, 138), (128, 138), (148, 138), (176, 128), (176, 88)], 12, GOLD)
    line([(70, 156), (186, 156)], 10, GOLD)
    polygon([(128, 37), (115, 70), (141, 70)], GOLD_LIGHT)
    polygon([(82, 54), (72, 86), (97, 86)], GOLD_LIGHT)
    polygon([(174, 54), (159, 86), (184, 86)], GOLD_LIGHT)

    # A small highlight keeps the icon legible at taskbar size.
    line([(123, 70), (123, 205)], 3, GOLD_LIGHT)


def render_icon(size: int = 256) -> Image.Image:
    scale = 4
    canvas = Image.new("RGBA", (size * scale, size * scale), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    radius = 44 * scale
    rect = (12 * scale, 12 * scale, (size - 12) * scale, (size - 12) * scale)
    draw.rounded_rectangle(rect, radius=radius, fill=NAVY)
    draw.rounded_rectangle(rect, radius=radius, outline=NAVY_2, width=5 * scale)
    _draw_trident(draw, scale)
    return canvas.resize((size, size), Image.Resampling.LANCZOS)


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate TritonPilot icon assets.")
    parser.add_argument("--out-dir", default="assets", help="Directory for icon outputs.")
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    png_path = out_dir / "tritonpilot_icon.png"
    ico_path = out_dir / "tritonpilot_icon.ico"

    icon = render_icon(256)
    icon.save(png_path)
    icon.save(
        ico_path,
        sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
    )
    print(f"Wrote {png_path}")
    print(f"Wrote {ico_path}")


if __name__ == "__main__":
    main()
