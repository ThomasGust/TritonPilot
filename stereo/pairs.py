"""Stereo pair configuration loaded from TritonPilot stream definitions."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class StereoPairConfig:
    """Configuration for one logical stereo rig."""

    name: str
    left: str
    right: str
    rig_id: str
    enabled: bool = True
    calibration_id: str | None = None
    max_pair_delta_ms: float = 50.0
    apply_stream_rotation: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def max_pair_delta_s(self) -> float:
        return max(0.0, float(self.max_pair_delta_ms) / 1000.0)


def _clean_name(value: object) -> str:
    return str(value or "").strip()


def _pair_from_dict(raw: dict[str, Any], stream_names: set[str]) -> StereoPairConfig:
    name = _clean_name(raw.get("name"))
    left = _clean_name(raw.get("left") or raw.get("left_stream"))
    right = _clean_name(raw.get("right") or raw.get("right_stream"))
    if not name:
        raise ValueError("Stereo pair is missing a name")
    if not left or not right:
        raise ValueError(f"Stereo pair '{name}' must define left and right streams")
    if left == right:
        raise ValueError(f"Stereo pair '{name}' cannot use the same stream for left and right")
    missing = [stream for stream in (left, right) if stream not in stream_names]
    if missing:
        raise ValueError(f"Stereo pair '{name}' references unknown stream(s): {', '.join(missing)}")

    rig_id = _clean_name(raw.get("rig_id")) or _clean_name(raw.get("id")) or name
    max_pair_delta_ms = float(raw.get("max_pair_delta_ms", 50.0))
    metadata = dict(raw.get("metadata", {}) or {})
    for key in ("baseline_mm", "mount_notes", "board_notes", "camera_model"):
        if key in raw and key not in metadata:
            metadata[key] = raw[key]

    return StereoPairConfig(
        name=name,
        left=left,
        right=right,
        rig_id=rig_id,
        enabled=bool(raw.get("enabled", True)),
        calibration_id=_clean_name(raw.get("calibration_id")) or None,
        max_pair_delta_ms=max_pair_delta_ms,
        apply_stream_rotation=bool(raw.get("apply_stream_rotation", True)),
        metadata=metadata,
    )


def load_stereo_pairs(config_path: str | Path, *, include_disabled: bool = False) -> list[StereoPairConfig]:
    """Load stereo pair definitions from a TritonPilot streams JSON file."""

    path = Path(config_path)
    with path.open("r", encoding="utf-8") as f:
        cfg = json.load(f)

    stream_names = {_clean_name(stream.get("name")) for stream in cfg.get("streams", [])}
    pairs = [_pair_from_dict(raw, stream_names) for raw in cfg.get("stereo_pairs", [])]
    if include_disabled:
        return pairs
    return [pair for pair in pairs if pair.enabled]
