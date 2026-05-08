import pytest

from analysis.coral_garden_model import (
    DEFAULT_CORAL_GARDEN_WIDTH_CM,
    build_coral_garden_prisms,
    export_obj,
    model_bounds,
)


def test_three_prism_model_preserves_total_length_width_and_height():
    prisms = build_coral_garden_prisms(180.0, 64.0)

    bounds_min, bounds_max = model_bounds(prisms)

    assert len(prisms) == 3
    assert bounds_min == pytest.approx((0.0, 0.0, 0.0))
    assert bounds_max == pytest.approx((180.0, DEFAULT_CORAL_GARDEN_WIDTH_CM, 64.0))
    assert sum(prism.length_cm for prism in prisms) == pytest.approx(180.0)
    assert max(prism.height_cm for prism in prisms) == pytest.approx(64.0)


def test_prism_segments_are_adjacent_without_gaps():
    left, center, right = build_coral_garden_prisms(200.0, 50.0, 40.0)

    assert left.x_min == pytest.approx(0.0)
    assert left.x_max == pytest.approx(center.x_min)
    assert center.x_max == pytest.approx(right.x_min)
    assert right.x_max == pytest.approx(200.0)
    assert {prism.width_cm for prism in (left, center, right)} == {40.0}


def test_export_obj_contains_prism_meshes_and_dimension_guides():
    prisms = build_coral_garden_prisms(150.0, 45.0, 36.0)

    obj_text = export_obj(prisms, length_cm=150.0, height_cm=45.0, width_cm=36.0)

    assert "# Length: 150.0 cm" in obj_text
    assert "# Height: 45.0 cm" in obj_text
    assert obj_text.count("\nv ") == 28
    assert obj_text.count("\nf ") == 18
    assert obj_text.count("\nl ") == 2


@pytest.mark.parametrize(
    ("length_cm", "height_cm", "width_cm"),
    [
        (0.0, 45.0, 36.0),
        (150.0, -1.0, 36.0),
        (150.0, 45.0, 0.0),
    ],
)
def test_model_rejects_non_positive_dimensions(length_cm, height_cm, width_cm):
    with pytest.raises(ValueError):
        build_coral_garden_prisms(length_cm, height_cm, width_cm)
