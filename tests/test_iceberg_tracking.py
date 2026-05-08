import pytest

from analysis.iceberg_tracking import (
    DEFAULT_PLATFORMS,
    Platform,
    assess_platform,
    decimal_degrees_from_dms,
    decimal_degrees_to_dms,
    evaluate_survey_numbers,
    format_dms_coordinate,
    heading_unit_vector,
    local_point_to_lat_lon,
    local_offset_nm,
    subsea_threat_level,
    surface_threat_level,
)


def test_local_offset_uses_nautical_miles_and_heading_is_true_bearing():
    east_nm, north_nm = local_offset_nm(0.0, 0.0, 0.0, 0.1)

    assert east_nm == pytest.approx(6.0)
    assert north_nm == pytest.approx(0.0)
    assert heading_unit_vector(90.0) == pytest.approx((1.0, 0.0))
    assert heading_unit_vector(0.0) == pytest.approx((0.0, 1.0))


def test_local_offset_round_trips_back_to_lat_lon():
    east_nm, north_nm = local_offset_nm(46.5, -48.45, 47.0, -48.0)
    latitude, longitude = local_point_to_lat_lon(46.5, -48.45, east_nm, north_nm)

    assert latitude == pytest.approx(47.0)
    assert longitude == pytest.approx(-48.0)


def test_dms_coordinate_helpers_format_competition_notation():
    value = decimal_degrees_from_dms(47, 39, 0, "N")

    assert value == pytest.approx(47.65)
    assert decimal_degrees_from_dms(48, 30, 0, "W") == pytest.approx(-48.5)
    assert decimal_degrees_to_dms(-48.5, seconds_decimals=0) == (48, 30, 0.0)
    assert format_dms_coordinate(value, "lat") == "47o39'00\"N"
    assert format_dms_coordinate(-48.5, "lon") == "48o30'00\"W"


def test_default_platforms_match_updated_mate_table():
    platforms = {platform.name: platform for platform in DEFAULT_PLATFORMS}

    assert platforms["Hibernia"].latitude_deg == pytest.approx(46.7504)
    assert platforms["Hibernia"].longitude_deg == pytest.approx(-48.7819)
    assert platforms["Sea Rose"].longitude_deg == pytest.approx(-48.146)
    assert platforms["Hebron"].longitude_deg == pytest.approx(-48.518)


def test_surface_threat_thresholds_and_grounding_override():
    assert surface_threat_level(closest_approach_nm=10.01, grounds_before_platform=False)[0] == "green"
    assert surface_threat_level(closest_approach_nm=10.0, grounds_before_platform=False)[0] == "yellow"
    assert surface_threat_level(closest_approach_nm=5.0, grounds_before_platform=False)[0] == "yellow"
    assert surface_threat_level(closest_approach_nm=4.99, grounds_before_platform=False)[0] == "red"
    assert surface_threat_level(closest_approach_nm=0.0, grounds_before_platform=True)[0] == "green"


def test_subsea_threat_thresholds():
    assert subsea_threat_level(closest_approach_nm=25.01, keel_to_depth_ratio=1.0)[0] == "green"
    assert subsea_threat_level(closest_approach_nm=25.0, keel_to_depth_ratio=1.10)[0] == "green"
    assert subsea_threat_level(closest_approach_nm=25.0, keel_to_depth_ratio=0.90)[0] == "red"
    assert subsea_threat_level(closest_approach_nm=25.0, keel_to_depth_ratio=0.70)[0] == "yellow"
    assert subsea_threat_level(closest_approach_nm=25.0, keel_to_depth_ratio=0.69)[0] == "green"


def test_assessment_uses_forward_track_ray_by_default():
    platform = Platform("Behind", 0.0, -0.1, -100.0)

    forward_result = assess_platform(
        iceberg_latitude_deg=0.0,
        iceberg_longitude_deg=0.0,
        heading_deg=90.0,
        keel_depth_m=50.0,
        platform=platform,
    )
    full_line_result = assess_platform(
        iceberg_latitude_deg=0.0,
        iceberg_longitude_deg=0.0,
        heading_deg=90.0,
        keel_depth_m=50.0,
        platform=platform,
        future_track_only=False,
    )

    assert forward_result.geometry.platform_ahead is False
    assert forward_result.geometry.closest_approach_nm == pytest.approx(6.0)
    assert full_line_result.geometry.closest_approach_nm == pytest.approx(0.0)


def test_assessment_grounding_sets_surface_and_subsea_green():
    platform = Platform("Grounding", 0.0, 0.1, -100.0)

    result = assess_platform(
        iceberg_latitude_deg=0.0,
        iceberg_longitude_deg=0.0,
        heading_deg=90.0,
        keel_depth_m=110.0,
        platform=platform,
    )

    assert result.keel_to_depth_ratio == pytest.approx(1.10)
    assert result.grounds_before_platform is True
    assert result.surface_level == "green"
    assert result.subsea_level == "green"


def test_survey_number_validation_accepts_only_complete_sequences():
    assert evaluate_survey_numbers([0, 1, 2, 3, 4]).complete is True
    assert evaluate_survey_numbers([5, 6, 7, 8, 9]).sequence_label == "5-9"
    assert evaluate_survey_numbers([0, 1, 2, 3, None]).complete is False
    assert evaluate_survey_numbers([0, 1, 1, 3, 4]).complete is False
    assert evaluate_survey_numbers([0, 1, 2, 3, 5]).complete is False
