from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Iterable, Sequence


THREAT_GREEN = "green"
THREAT_YELLOW = "yellow"
THREAT_RED = "red"

NAUTICAL_MILES_PER_DEGREE_LATITUDE = 60.0


@dataclass(frozen=True)
class Platform:
    name: str
    latitude_deg: float
    longitude_deg: float
    ocean_depth_m: float

    @property
    def water_depth_m(self) -> float:
        return abs(float(self.ocean_depth_m))


@dataclass(frozen=True)
class TrackGeometry:
    east_nm: float
    north_nm: float
    range_nm: float
    bearing_deg: float
    along_track_nm: float
    cross_track_nm: float
    closest_approach_nm: float
    closest_point_east_nm: float
    closest_point_north_nm: float
    platform_ahead: bool


@dataclass(frozen=True)
class ThreatAssessment:
    platform: Platform
    geometry: TrackGeometry
    keel_depth_m: float
    keel_to_depth_ratio: float
    grounds_before_platform: bool
    surface_level: str
    surface_reason: str
    subsea_level: str
    subsea_reason: str


@dataclass(frozen=True)
class SurveyStatus:
    numbers: tuple[int | None, ...]
    surveyed_count: int
    complete: bool
    sequence_label: str | None
    message: str


DEFAULT_PLATFORMS: tuple[Platform, ...] = (
    Platform("Hibernia", 46.7504, -48.7819, -78.0),
    Platform("Sea Rose", 46.7895, -48.146, -107.0),
    Platform("Terra Nova", 46.4, -48.4, -91.0),
    Platform("Hebron", 46.544, -48.518, -93.0),
)


def normalize_heading_deg(heading_deg: float) -> float:
    heading = float(heading_deg) % 360.0
    if heading < 0.0:
        heading += 360.0
    return heading


def heading_unit_vector(heading_deg: float) -> tuple[float, float]:
    """Return heading as an east/north unit vector.

    Competition headings are treated as true bearings: 0 is north, 90 is east.
    """

    radians = math.radians(normalize_heading_deg(heading_deg))
    return math.sin(radians), math.cos(radians)


def local_offset_nm(
    origin_latitude_deg: float,
    origin_longitude_deg: float,
    target_latitude_deg: float,
    target_longitude_deg: float,
) -> tuple[float, float]:
    """Approximate a local latitude/longitude offset in nautical miles."""

    origin_lat = float(origin_latitude_deg)
    target_lat = float(target_latitude_deg)
    origin_lon = float(origin_longitude_deg)
    target_lon = float(target_longitude_deg)
    mean_latitude = math.radians((origin_lat + target_lat) * 0.5)
    north_nm = (target_lat - origin_lat) * NAUTICAL_MILES_PER_DEGREE_LATITUDE
    east_nm = (
        (target_lon - origin_lon)
        * NAUTICAL_MILES_PER_DEGREE_LATITUDE
        * math.cos(mean_latitude)
    )
    return east_nm, north_nm


def local_point_to_lat_lon(
    origin_latitude_deg: float,
    origin_longitude_deg: float,
    east_nm: float,
    north_nm: float,
) -> tuple[float, float]:
    latitude = float(origin_latitude_deg) + float(north_nm) / NAUTICAL_MILES_PER_DEGREE_LATITUDE
    mean_latitude = math.radians((float(origin_latitude_deg) + latitude) * 0.5)
    cosine = max(abs(math.cos(mean_latitude)), 1.0e-9)
    longitude = float(origin_longitude_deg) + float(east_nm) / (
        NAUTICAL_MILES_PER_DEGREE_LATITUDE * cosine
    )
    return latitude, longitude


def decimal_degrees_from_dms(
    degrees: int | float,
    minutes: int | float,
    seconds: int | float,
    hemisphere: str,
) -> float:
    hemi = str(hemisphere).strip().upper()
    if hemi not in {"N", "S", "E", "W"}:
        raise ValueError("hemisphere must be N, S, E, or W")
    value = abs(float(degrees)) + abs(float(minutes)) / 60.0 + abs(float(seconds)) / 3600.0
    if hemi in {"S", "W"}:
        value *= -1.0
    return value


def decimal_degrees_to_dms(value: float, *, seconds_decimals: int = 2) -> tuple[int, int, float]:
    absolute = abs(float(value))
    degrees = int(math.floor(absolute))
    minutes_total = (absolute - degrees) * 60.0
    minutes = int(math.floor(minutes_total))
    seconds = round((minutes_total - minutes) * 60.0, int(seconds_decimals))

    second_limit = round(60.0, int(seconds_decimals))
    if seconds >= second_limit:
        seconds = 0.0
        minutes += 1
    if minutes >= 60:
        minutes = 0
        degrees += 1
    return degrees, minutes, seconds


def format_dms_coordinate(
    value: float,
    coordinate: str,
    *,
    seconds_decimals: int = 0,
) -> str:
    coordinate_type = str(coordinate).strip().lower()
    if coordinate_type not in {"lat", "latitude", "lon", "longitude"}:
        raise ValueError("coordinate must be latitude/lat or longitude/lon")
    degrees, minutes, seconds = decimal_degrees_to_dms(
        value,
        seconds_decimals=seconds_decimals,
    )
    if coordinate_type in {"lat", "latitude"}:
        hemisphere = "N" if float(value) >= 0.0 else "S"
    else:
        hemisphere = "E" if float(value) >= 0.0 else "W"

    if seconds_decimals <= 0:
        seconds_text = f"{int(round(seconds)):02d}"
    else:
        width = 3 + int(seconds_decimals)
        seconds_text = f"{seconds:0{width}.{int(seconds_decimals)}f}"
    return f"{degrees}o{minutes:02d}'{seconds_text}\"{hemisphere}"


def _bearing_from_offset_deg(east_nm: float, north_nm: float) -> float:
    if abs(east_nm) < 1.0e-12 and abs(north_nm) < 1.0e-12:
        return 0.0
    return normalize_heading_deg(math.degrees(math.atan2(east_nm, north_nm)))


def closest_approach_to_platform(
    *,
    iceberg_latitude_deg: float,
    iceberg_longitude_deg: float,
    heading_deg: float,
    platform: Platform,
    future_track_only: bool = True,
) -> TrackGeometry:
    east_nm, north_nm = local_offset_nm(
        iceberg_latitude_deg,
        iceberg_longitude_deg,
        platform.latitude_deg,
        platform.longitude_deg,
    )
    range_nm = math.hypot(east_nm, north_nm)
    heading_east, heading_north = heading_unit_vector(heading_deg)
    along_track_nm = east_nm * heading_east + north_nm * heading_north
    cross_track_nm = abs(heading_east * north_nm - heading_north * east_nm)

    platform_ahead = along_track_nm >= 0.0
    if future_track_only and not platform_ahead:
        closest_approach_nm = range_nm
        closest_east = 0.0
        closest_north = 0.0
    else:
        closest_approach_nm = cross_track_nm
        closest_east = heading_east * along_track_nm
        closest_north = heading_north * along_track_nm

    return TrackGeometry(
        east_nm=float(east_nm),
        north_nm=float(north_nm),
        range_nm=float(range_nm),
        bearing_deg=float(_bearing_from_offset_deg(east_nm, north_nm)),
        along_track_nm=float(along_track_nm),
        cross_track_nm=float(cross_track_nm),
        closest_approach_nm=float(closest_approach_nm),
        closest_point_east_nm=float(closest_east),
        closest_point_north_nm=float(closest_north),
        platform_ahead=bool(platform_ahead),
    )


def surface_threat_level(*, closest_approach_nm: float, grounds_before_platform: bool) -> tuple[str, str]:
    if grounds_before_platform:
        return (
            THREAT_GREEN,
            "Keel is at least 110% of local water depth; iceberg should ground before the platform.",
        )
    if closest_approach_nm > 10.0:
        return THREAT_GREEN, "Closest approach is more than 10 NM from the platform."
    if closest_approach_nm >= 5.0:
        return THREAT_YELLOW, "Closest approach is between 5 and 10 NM from the platform."
    return THREAT_RED, "Closest approach is less than 5 NM from the platform."


def subsea_threat_level(*, closest_approach_nm: float, keel_to_depth_ratio: float) -> tuple[str, str]:
    if closest_approach_nm > 25.0:
        return THREAT_GREEN, "Track stays more than 25 NM from this platform's subsea assets."
    if keel_to_depth_ratio >= 1.10:
        return (
            THREAT_GREEN,
            "Keel is at least 110% of local water depth; iceberg should ground before the assets.",
        )
    if keel_to_depth_ratio >= 0.90:
        return THREAT_RED, "Keel is 90% to 110% of local water depth."
    if keel_to_depth_ratio >= 0.70:
        return THREAT_YELLOW, "Keel is 70% to 90% of local water depth."
    return THREAT_GREEN, "Keel is less than 70% of local water depth."


def assess_platform(
    *,
    iceberg_latitude_deg: float,
    iceberg_longitude_deg: float,
    heading_deg: float,
    keel_depth_m: float,
    platform: Platform,
    future_track_only: bool = True,
) -> ThreatAssessment:
    keel_depth = float(keel_depth_m)
    if keel_depth < 0.0:
        raise ValueError("keel_depth_m must be non-negative")
    if platform.water_depth_m <= 0.0:
        raise ValueError("platform water depth must be positive")

    geometry = closest_approach_to_platform(
        iceberg_latitude_deg=iceberg_latitude_deg,
        iceberg_longitude_deg=iceberg_longitude_deg,
        heading_deg=heading_deg,
        platform=platform,
        future_track_only=future_track_only,
    )
    ratio = keel_depth / platform.water_depth_m
    grounds_before_platform = ratio >= 1.10
    surface_level, surface_reason = surface_threat_level(
        closest_approach_nm=geometry.closest_approach_nm,
        grounds_before_platform=grounds_before_platform,
    )
    subsea_level, subsea_reason = subsea_threat_level(
        closest_approach_nm=geometry.closest_approach_nm,
        keel_to_depth_ratio=ratio,
    )
    return ThreatAssessment(
        platform=platform,
        geometry=geometry,
        keel_depth_m=keel_depth,
        keel_to_depth_ratio=float(ratio),
        grounds_before_platform=bool(grounds_before_platform),
        surface_level=surface_level,
        surface_reason=surface_reason,
        subsea_level=subsea_level,
        subsea_reason=subsea_reason,
    )


def assess_all_platforms(
    *,
    iceberg_latitude_deg: float,
    iceberg_longitude_deg: float,
    heading_deg: float,
    keel_depth_m: float,
    platforms: Iterable[Platform] = DEFAULT_PLATFORMS,
    future_track_only: bool = True,
) -> list[ThreatAssessment]:
    return [
        assess_platform(
            iceberg_latitude_deg=iceberg_latitude_deg,
            iceberg_longitude_deg=iceberg_longitude_deg,
            heading_deg=heading_deg,
            keel_depth_m=keel_depth_m,
            platform=platform,
            future_track_only=future_track_only,
        )
        for platform in platforms
    ]


def evaluate_survey_numbers(numbers: Sequence[int | None]) -> SurveyStatus:
    normalized = tuple(None if number is None else int(number) for number in numbers)
    present = [number for number in normalized if number is not None]
    surveyed_count = len(present)
    if surveyed_count < 5:
        return SurveyStatus(
            numbers=normalized,
            surveyed_count=surveyed_count,
            complete=False,
            sequence_label=None,
            message=f"{surveyed_count}/5 survey numbers recorded.",
        )

    if len(set(present)) != len(present):
        return SurveyStatus(
            numbers=normalized,
            surveyed_count=surveyed_count,
            complete=False,
            sequence_label=None,
            message="Survey numbers include a duplicate.",
        )

    present_set = set(present)
    if present_set == set(range(0, 5)):
        return SurveyStatus(
            numbers=normalized,
            surveyed_count=surveyed_count,
            complete=True,
            sequence_label="0-4",
            message="All five sequential survey numbers recorded: 0-4.",
        )
    if present_set == set(range(5, 10)):
        return SurveyStatus(
            numbers=normalized,
            surveyed_count=surveyed_count,
            complete=True,
            sequence_label="5-9",
            message="All five sequential survey numbers recorded: 5-9.",
        )

    return SurveyStatus(
        numbers=normalized,
        surveyed_count=surveyed_count,
        complete=False,
        sequence_label=None,
        message="Five numbers are present, but they are not the expected 0-4 or 5-9 sequence.",
    )


def count_levels(assessments: Iterable[ThreatAssessment], attribute: str) -> dict[str, int]:
    counts = {THREAT_GREEN: 0, THREAT_YELLOW: 0, THREAT_RED: 0}
    for assessment in assessments:
        level = str(getattr(assessment, attribute))
        if level in counts:
            counts[level] += 1
    return counts


def format_level(level: str) -> str:
    normalized = str(level).strip().lower()
    if normalized == THREAT_RED:
        return "RED"
    if normalized == THREAT_YELLOW:
        return "YELLOW"
    return "GREEN"


def build_judge_report(
    *,
    iceberg_latitude_deg: float,
    iceberg_longitude_deg: float,
    heading_deg: float,
    keel_depth_m: float,
    survey_status: SurveyStatus,
    assessments: Sequence[ThreatAssessment],
    future_track_only: bool = True,
) -> str:
    track_mode = "forward heading ray" if future_track_only else "full heading line"
    lines = [
        "Iceberg Tracking Report",
        f"Survey: {survey_status.message}",
        (
            "Iceberg: "
            f"{format_dms_coordinate(iceberg_latitude_deg, 'lat')}, "
            f"{format_dms_coordinate(iceberg_longitude_deg, 'lon')} "
            f"({iceberg_latitude_deg:.5f}, {iceberg_longitude_deg:.5f}), "
            f"heading {normalize_heading_deg(heading_deg):.1f} deg true, "
            f"keel depth {keel_depth_m:.1f} m"
        ),
        f"Track model: {track_mode}",
        "",
        "Platform threat levels:",
    ]
    for assessment in assessments:
        geometry = assessment.geometry
        relative = "ahead" if geometry.platform_ahead else "behind current heading"
        lines.append(
            " - "
            f"{assessment.platform.name}: "
            f"surface {format_level(assessment.surface_level)}, "
            f"subsea {format_level(assessment.subsea_level)}, "
            f"CPA {geometry.closest_approach_nm:.2f} NM, "
            f"along-track {geometry.along_track_nm:.2f} NM ({relative}), "
            f"keel/depth {assessment.keel_to_depth_ratio * 100.0:.0f}%"
        )
    lines.extend(["", "Decision basis:"])
    for assessment in assessments:
        lines.append(
            f" - {assessment.platform.name} surface: {assessment.surface_reason} "
            f"Subsea: {assessment.subsea_reason}"
        )
    return "\n".join(lines)
