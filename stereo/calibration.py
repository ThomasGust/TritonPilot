"""Stereo calibration loading and lookup helpers."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import numpy as np


@dataclass(frozen=True)
class StereoCalibration:
    """OpenCV stereo calibration values exported by TritonAnalysis."""

    path: Path
    image_size: tuple[int, int]
    rig_id: str
    baseline_mm: float
    left_camera_matrix: np.ndarray
    right_camera_matrix: np.ndarray
    left_dist_coeffs: np.ndarray
    right_dist_coeffs: np.ndarray
    rotation: np.ndarray
    translation_mm: np.ndarray


def _matrix_from_json(value: object, *, shape: tuple[int, int], label: str) -> np.ndarray:
    arr = np.asarray(value, dtype=np.float64)
    if arr.shape != shape:
        raise RuntimeError(f"Calibration field {label} has shape {arr.shape}, expected {shape}")
    return arr


def _dist_coeffs_from_json(value: object, *, label: str) -> np.ndarray:
    arr = np.asarray(value, dtype=np.float64).reshape(-1)
    if arr.size < 4:
        raise RuntimeError(f"Calibration field {label} has {arr.size} coefficients, expected at least 4")
    return arr


def _image_size_from_json(value: object) -> tuple[int, int]:
    if isinstance(value, dict):
        width = int(value.get("width") or value.get("w") or 0)
        height = int(value.get("height") or value.get("h") or 0)
    else:
        seq = list(value or [])
        width = int(seq[0]) if len(seq) > 0 else 0
        height = int(seq[1]) if len(seq) > 1 else 0
    if width <= 0 or height <= 0:
        raise RuntimeError("Calibration field image_size must contain positive width and height")
    return width, height


def load_stereo_calibration(path: str | Path) -> StereoCalibration:
    """Load a TritonAnalysis/OpenCV stereo calibration JSON file."""

    calibration_path = Path(path).expanduser().resolve()
    try:
        data = json.loads(calibration_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(f"Could not read stereo calibration: {exc}") from exc

    stereo = data.get("stereo") if isinstance(data.get("stereo"), dict) else {}
    left = data.get("left") if isinstance(data.get("left"), dict) else {}
    right = data.get("right") if isinstance(data.get("right"), dict) else {}
    translation = np.asarray(stereo.get("translation"), dtype=np.float64).reshape(-1)
    if translation.size != 3:
        raise RuntimeError("Calibration field stereo.translation must contain 3 values")

    baseline = stereo.get("baseline")
    baseline_mm = float(baseline) if baseline not in (None, "") else float(np.linalg.norm(translation))

    return StereoCalibration(
        path=calibration_path,
        image_size=_image_size_from_json(data.get("image_size")),
        rig_id=str(data.get("rig_id") or "stereo_rig"),
        baseline_mm=baseline_mm,
        left_camera_matrix=_matrix_from_json(left.get("camera_matrix"), shape=(3, 3), label="left.camera_matrix"),
        right_camera_matrix=_matrix_from_json(right.get("camera_matrix"), shape=(3, 3), label="right.camera_matrix"),
        left_dist_coeffs=_dist_coeffs_from_json(left.get("dist_coeffs"), label="left.dist_coeffs"),
        right_dist_coeffs=_dist_coeffs_from_json(right.get("dist_coeffs"), label="right.dist_coeffs"),
        rotation=_matrix_from_json(stereo.get("rotation"), shape=(3, 3), label="stereo.rotation"),
        translation_mm=translation.astype(np.float64),
    )


def _identifier_variants(identifier: str) -> list[Path]:
    raw = Path(identifier).expanduser()
    variants = [raw]
    if raw.suffix.lower() != ".json":
        variants.append(Path(str(raw) + ".json").expanduser())
    return variants


def _candidate_dirs(base_dir: Path | None, search_dirs: Iterable[str | Path] | None) -> list[Path | None]:
    dirs: list[Path | None] = [None]
    if base_dir is not None:
        base = Path(base_dir).expanduser()
        dirs.extend(
            [
                base,
                base / "calibration",
                base / "calibrations",
                base.parent / "calibration",
                base.parent / "calibrations",
                base.parent / "data" / "calibration",
                base.parent / "data" / "calibrations",
            ]
        )
    for directory in search_dirs or ():
        dirs.append(Path(directory).expanduser())

    seen: set[str] = set()
    unique: list[Path | None] = []
    for directory in dirs:
        key = "" if directory is None else str(directory)
        if key in seen:
            continue
        seen.add(key)
        unique.append(directory)
    return unique


def resolve_stereo_calibration_path(
    identifier: str | Path | None,
    *,
    base_dir: str | Path | None = None,
    search_dirs: Iterable[str | Path] | None = None,
) -> Path | None:
    """Resolve a calibration id or path to an existing JSON file.

    ``identifier`` may be an absolute path, a path relative to ``base_dir``, or
    a short id such as ``explorehd_forward_v1``. Short ids are searched as both
    ``<id>`` and ``<id>.json`` in common calibration folders.
    """

    text = str(identifier or "").strip()
    if not text:
        return None

    dirs = _candidate_dirs(Path(base_dir).expanduser() if base_dir is not None else None, search_dirs)
    for variant in _identifier_variants(text):
        if variant.is_absolute():
            if variant.is_file():
                return variant.resolve()
            continue
        for directory in dirs:
            candidate = variant if directory is None else directory / variant
            if candidate.is_file():
                return candidate.resolve()
    return None
