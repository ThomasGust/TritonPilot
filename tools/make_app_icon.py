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
    line([(128, 70), (128, 190)], 20, GOLD_DARK)
    line([(86, 92), (86, 128), (112, 142), (128, 142), (144, 142), (170, 128), (170, 92)], 20, GOLD_DARK)
    line([(78, 150), (178, 150)], 18, GOLD_DARK)
    polygon([(128, 46), (111, 76), (145, 76)], GOLD_DARK)
    polygon([(86, 58), (71, 91), (101, 91)], GOLD_DARK)
    polygon([(170, 58), (155, 91), (185, 91)], GOLD_DARK)
    polygon([(128, 198), (104, 222), (152, 222)], GOLD_DARK)

    # Gold pass.
    line([(128, 70), (128, 190)], 12, GOLD)
    line([(86, 92), (86, 125), (112, 134), (128, 134), (144, 134), (170, 125), (170, 92)], 12, GOLD)
    line([(78, 148), (178, 148)], 10, GOLD)
    polygon([(128, 45), (116, 73), (140, 73)], GOLD_LIGHT)
    polygon([(86, 60), (76, 88), (96, 88)], GOLD_LIGHT)
    polygon([(170, 60), (160, 88), (180, 88)], GOLD_LIGHT)
    polygon([(128, 190), (110, 216), (146, 216)], GOLD)

    # A small highlight keeps the icon legible at taskbar size.
    line([(123, 77), (123, 184)], 3, GOLD_LIGHT)


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
