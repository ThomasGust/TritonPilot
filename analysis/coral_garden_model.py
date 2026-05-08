from __future__ import annotations

from dataclasses import dataclass
from math import isfinite


DEFAULT_CORAL_GARDEN_WIDTH_CM = 36.0
_SEGMENT_RATIOS = (0.36, 0.22, 0.42)
_SEGMENT_HEIGHT_FACTORS = (0.56, 1.0, 0.36)


@dataclass(frozen=True)
class RectangularPrism:
    name: str
    x_min: float
    x_max: float
    y_min: float
    y_max: float
    z_min: float
    z_max: float

    @property
    def length_cm(self) -> float:
        return self.x_max - self.x_min

    @property
    def width_cm(self) -> float:
        return self.y_max - self.y_min

    @property
    def height_cm(self) -> float:
        return self.z_max - self.z_min

    def vertices(self) -> tuple[tuple[float, float, float], ...]:
        return (
            (self.x_min, self.y_min, self.z_min),
            (self.x_max, self.y_min, self.z_min),
            (self.x_max, self.y_max, self.z_min),
            (self.x_min, self.y_max, self.z_min),
            (self.x_min, self.y_min, self.z_max),
            (self.x_max, self.y_min, self.z_max),
            (self.x_max, self.y_max, self.z_max),
            (self.x_min, self.y_max, self.z_max),
        )

    def faces(self) -> tuple[tuple[int, int, int, int], ...]:
        return (
            (0, 1, 2, 3),
            (4, 7, 6, 5),
            (0, 4, 5, 1),
            (1, 5, 6, 2),
            (2, 6, 7, 3),
            (3, 7, 4, 0),
        )


def _positive_finite(value: float, label: str) -> float:
    value = float(value)
    if not isfinite(value) or value <= 0.0:
        raise ValueError(f"{label} must be a positive finite number")
    return value


def build_coral_garden_prisms(
    length_cm: float,
    height_cm: float,
    width_cm: float = DEFAULT_CORAL_GARDEN_WIDTH_CM,
) -> tuple[RectangularPrism, RectangularPrism, RectangularPrism]:
    length_cm = _positive_finite(length_cm, "length_cm")
    height_cm = _positive_finite(height_cm, "height_cm")
    width_cm = _positive_finite(width_cm, "width_cm")

    left_length = length_cm * _SEGMENT_RATIOS[0]
    center_length = length_cm * _SEGMENT_RATIOS[1]
    left_end = left_length
    center_end = left_length + center_length

    return (
        RectangularPrism(
            "left",
            0.0,
            left_end,
            0.0,
            width_cm,
            0.0,
            height_cm * _SEGMENT_HEIGHT_FACTORS[0],
        ),
        RectangularPrism(
            "center",
            left_end,
            center_end,
            0.0,
            width_cm,
            0.0,
            height_cm * _SEGMENT_HEIGHT_FACTORS[1],
        ),
        RectangularPrism(
            "right",
            center_end,
            length_cm,
            0.0,
            width_cm,
            0.0,
            height_cm * _SEGMENT_HEIGHT_FACTORS[2],
        ),
    )


def model_bounds(
    prisms: tuple[RectangularPrism, ...],
) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    if not prisms:
        raise ValueError("at least one prism is required")

    vertices = [vertex for prism in prisms for vertex in prism.vertices()]
    return (
        (
            min(vertex[0] for vertex in vertices),
            min(vertex[1] for vertex in vertices),
            min(vertex[2] for vertex in vertices),
        ),
        (
            max(vertex[0] for vertex in vertices),
            max(vertex[1] for vertex in vertices),
            max(vertex[2] for vertex in vertices),
        ),
    )


def format_cm(value: float) -> str:
    return f"{float(value):.1f} cm"


def export_obj(
    prisms: tuple[RectangularPrism, ...],
    *,
    length_cm: float,
    height_cm: float,
    width_cm: float,
) -> str:
    lines = [
        "# Coral garden manual CAD model",
        f"# Length: {format_cm(length_cm)}",
        f"# Height: {format_cm(height_cm)}",
        f"# Width: {format_cm(width_cm)}",
        "",
    ]
    vertex_offset = 1
    for prism in prisms:
        lines.append(f"o coral_garden_{prism.name}")
        for x, y, z in prism.vertices():
            lines.append(f"v {x:.6f} {y:.6f} {z:.6f}")
        for face in prism.faces():
            face_indices = " ".join(str(vertex_offset + index) for index in face)
            lines.append(f"f {face_indices}")
        vertex_offset += len(prism.vertices())
        lines.append("")

    offset = max(width_cm * 0.35, 12.0)
    dimension_vertices = [
        (0.0, -offset, 0.0),
        (length_cm, -offset, 0.0),
        (length_cm + offset, width_cm, 0.0),
        (length_cm + offset, width_cm, height_cm),
    ]
    lines.append("o coral_garden_dimension_guides")
    for x, y, z in dimension_vertices:
        lines.append(f"v {x:.6f} {y:.6f} {z:.6f}")
    lines.append(f"l {vertex_offset} {vertex_offset + 1}")
    lines.append(f"l {vertex_offset + 2} {vertex_offset + 3}")
    lines.append("")
    return "\n".join(lines)
