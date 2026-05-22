"""Prepare underwater video frames and RealityScan command files.

This utility is an offline media-prep tool for recorded footage. It samples
video frames, scores image quality, writes preprocessing variants, and prepares
RealityScan/RealityCapture command files so model-building experiments are
repeatable.
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable

import cv2
import numpy as np


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


DEFAULT_REALITYSCAN_GLOBS = (
    r"C:\Program Files\Epic Games\RealityScan*\RealityScan.exe",
    r"C:\Program Files\Capturing Reality\RealityScan*\RealityScan.exe",
    r"C:\Program Files\Capturing Reality\RealityCapture*\RealityCapture.exe",
)


@dataclass(frozen=True)
class VideoInfo:
    """Basic metadata read from an input video file."""

    fps: float
    frame_count: int
    width: int
    height: int
    duration_s: float


@dataclass
class FrameMetric:
    """Quality and bookkeeping fields for one candidate video frame."""

    frame_index: int
    timestamp_s: float
    sharpness: float
    contrast: float
    brightness: float
    feature_count: int
    exposure_score: float
    quality: float = 0.0
    motion_delta: float = 0.0
    fingerprint: np.ndarray | None = None


@dataclass(frozen=True)
class VariantSpec:
    """One preprocessing/alignment variant to write and evaluate."""

    name: str
    geometry_mode: str
    rectify_water: bool = False
    cv_mask: bool = False
    ai_mask: bool = False
    distortion_model: str = ""
    detector_sensitivity: str = ""
    images_overlap: str = ""


@dataclass(frozen=True)
class VariantPaths:
    """Filesystem layout for one RealityScan alignment variant."""

    name: str
    frames: Path
    rscmd: Path
    project: Path
    progress: Path
    stdout: Path
    report: Path
    crash_reports: Path


@dataclass(frozen=True)
class AlignmentResult:
    """Parsed quality summary from one RealityScan alignment run."""

    name: str
    score: float
    component_count: int
    largest_component_images: int
    total_registered_images: int
    selected_image_count: int
    largest_component_ratio: float
    total_registered_ratio: float
    report: Path
    project: Path


@dataclass(frozen=True)
class OutputPaths:
    """All output directories and files created for one pipeline run."""

    root: Path
    frames: Path
    variants: Path
    alignments: Path
    reports: Path
    logs: Path
    model: Path
    project: Path
    rscmd: Path
    progress: Path
    crash_reports: Path
    metrics_csv: Path
    manifest_json: Path
    contact_sheet: Path


def _positive_float(value: str) -> float:
    """Parse a strictly positive CLI float value."""
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be greater than zero")
    return parsed


def _nonnegative_float(value: str) -> float:
    """Parse a CLI float value that may be zero but not negative."""
    parsed = float(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("value must be zero or greater")
    return parsed


def _path_for_cli(path: Path) -> str:
    """Return a quoted absolute path for RealityScan command files."""
    return f'"{path.resolve()}"'


def _safe_slug(text: str) -> str:
    """Return a conservative filesystem slug for an output workspace."""
    allowed = []
    for char in text:
        if char.isalnum():
            allowed.append(char)
        elif char in ("-", "_"):
            allowed.append(char)
        else:
            allowed.append("_")
    slug = "".join(allowed).strip("_")
    return slug or "scan"


def discover_realityscan() -> Path | None:
    """Locate a RealityScan/RealityCapture executable on this workstation."""
    env_path = os.environ.get("REALITYSCAN_EXE")
    if env_path:
        path = Path(env_path)
        if path.exists():
            return path

    from glob import glob

    candidates: list[Path] = []
    for pattern in DEFAULT_REALITYSCAN_GLOBS:
        for item in glob(pattern):
            path = Path(item)
            if path.exists():
                candidates.append(path)
    if not candidates:
        found = shutil.which("RealityScan.exe") or shutil.which("RealityCapture.exe")
        return Path(found) if found else None

    def version_key(path: Path) -> tuple[int, ...]:
        parts: list[int] = []
        for token in path.parent.name.replace("-", "_").split("_"):
            try:
                parts.append(int(token))
            except ValueError:
                continue
        return tuple(parts)

    return sorted(candidates, key=version_key, reverse=True)[0]


def open_video(video_path: Path) -> tuple[cv2.VideoCapture, VideoInfo]:
    """Open a video and return its capture handle with normalized metadata."""
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise RuntimeError(f"Could not open video: {video_path}")

    fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
    frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
    if fps <= 0:
        fps = 30.0
    duration_s = frame_count / fps if frame_count > 0 else 0.0
    return cap, VideoInfo(fps=fps, frame_count=frame_count, width=width, height=height, duration_s=duration_s)


def score_frame(frame: np.ndarray, frame_index: int, fps: float) -> FrameMetric:
    """Compute quality metrics used to choose useful photogrammetry frames."""
    h, w = frame.shape[:2]
    target_w = 480
    scale = target_w / max(w, 1)
    small = cv2.resize(frame, (target_w, max(1, int(h * scale))), interpolation=cv2.INTER_AREA)
    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    sharpness = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    contrast = float(gray.std())
    brightness = float(gray.mean())
    feature_points = cv2.goodFeaturesToTrack(
        gray,
        maxCorners=800,
        qualityLevel=0.01,
        minDistance=5,
        blockSize=5,
        useHarrisDetector=False,
    )
    feature_count = 0 if feature_points is None else int(len(feature_points))
    exposure_score = math.exp(-((brightness - 128.0) / 88.0) ** 2)
    fingerprint = cv2.resize(gray, (96, 54), interpolation=cv2.INTER_AREA)
    return FrameMetric(
        frame_index=frame_index,
        timestamp_s=frame_index / fps,
        sharpness=sharpness,
        contrast=contrast,
        brightness=brightness,
        feature_count=feature_count,
        exposure_score=exposure_score,
        fingerprint=fingerprint,
    )


def _normalize(values: list[float]) -> list[float]:
    """Normalize metric values with percentile clipping for robust scoring."""
    if not values:
        return []
    lo = float(np.percentile(values, 10))
    hi = float(np.percentile(values, 90))
    if hi <= lo:
        return [0.5 for _ in values]
    return [float(np.clip((value - lo) / (hi - lo), 0.0, 1.0)) for value in values]


def assign_quality(metrics: list[FrameMetric]) -> None:
    """Populate each frame metric with a blended quality score."""
    sharp = _normalize([m.sharpness for m in metrics])
    contrast = _normalize([m.contrast for m in metrics])
    features = _normalize([math.log1p(m.feature_count) for m in metrics])
    exposure = [m.exposure_score for m in metrics]
    for metric, sharp_n, contrast_n, feature_n, exposure_n in zip(metrics, sharp, contrast, features, exposure):
        metric.quality = (
            0.42 * sharp_n
            + 0.22 * contrast_n
            + 0.26 * feature_n
            + 0.10 * exposure_n
        )


def read_candidate_metrics(video_path: Path, candidate_fps: float) -> tuple[VideoInfo, list[FrameMetric]]:
    """Decode candidate frames from a video and score each candidate."""
    cap, info = open_video(video_path)
    stride = max(1, int(round(info.fps / candidate_fps)))
    metrics: list[FrameMetric] = []
    try:
        frame_index = 0
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            if frame_index % stride == 0:
                metrics.append(score_frame(frame, frame_index, info.fps))
            frame_index += 1
    finally:
        cap.release()

    if not metrics:
        raise RuntimeError("No frames could be decoded from the video.")
    assign_quality(metrics)
    return info, metrics


def _mean_abs_delta(a: np.ndarray | None, b: np.ndarray | None) -> float:
    """Return a small grayscale fingerprint distance between two frames."""
    if a is None or b is None:
        return 999.0
    return float(np.mean(cv2.absdiff(a, b)))


def estimate_dark_border_crop(video_path: Path, max_crop: float, sample_count: int = 18) -> float:
    """Estimate how much fixed dark lens/housing border to crop away."""
    if max_crop <= 0:
        return 0.0
    cap, info = open_video(video_path)
    samples: list[np.ndarray] = []
    try:
        if info.frame_count > 0:
            frame_indices = [int(round(i)) for i in np.linspace(0, info.frame_count - 1, sample_count)]
        else:
            frame_indices = []
        for frame_index in frame_indices:
            cap.set(cv2.CAP_PROP_POS_FRAMES, frame_index)
            ok, frame = cap.read()
            if not ok:
                continue
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            samples.append(cv2.resize(gray, (320, 180), interpolation=cv2.INTER_AREA))
    finally:
        cap.release()

    if len(samples) < 3:
        return 0.0
    median = np.median(np.stack(samples, axis=0), axis=0).astype(np.uint8)
    h, w = median.shape[:2]
    center = median[h // 4 : (3 * h) // 4, w // 4 : (3 * w) // 4]
    center_level = float(np.median(center))
    threshold = max(12.0, center_level * 0.46)
    valid = median > threshold
    col_valid = valid.mean(axis=0)
    row_valid = valid.mean(axis=1)

    def first_valid(values: np.ndarray, required: float) -> int:
        run = 0
        for idx, value in enumerate(values):
            run = run + 1 if value >= required else 0
            if run >= 5:
                return max(0, idx - 4)
        return 0

    def last_valid(values: np.ndarray, required: float) -> int:
        run = 0
        for idx in range(len(values) - 1, -1, -1):
            run = run + 1 if values[idx] >= required else 0
            if run >= 5:
                return min(len(values) - 1, idx + 4)
        return len(values) - 1

    left = first_valid(col_valid, 0.58)
    right = last_valid(col_valid, 0.58)
    top = first_valid(row_valid, 0.58)
    bottom = last_valid(row_valid, 0.58)
    crop = max(left / w, (w - 1 - right) / w, top / h, (h - 1 - bottom) / h)
    if crop < 0.015:
        return 0.0
    return min(float(crop), max_crop)


def select_frames(
    metrics: list[FrameMetric],
    *,
    target_fps: float,
    max_frames: int,
    min_frames: int,
    quality_quantile: float,
    min_motion: float,
    max_still_gap_s: float,
) -> list[FrameMetric]:
    """Choose high-quality, temporally distributed frames for reconstruction."""
    bucket_s = 1.0 / target_fps
    by_bucket: dict[int, FrameMetric] = {}
    for metric in metrics:
        bucket = int(metric.timestamp_s / bucket_s)
        current = by_bucket.get(bucket)
        if current is None or metric.quality > current.quality:
            by_bucket[bucket] = metric

    best_per_bucket = sorted(by_bucket.values(), key=lambda m: m.timestamp_s)
    threshold = float(np.quantile([m.quality for m in metrics], np.clip(quality_quantile, 0.0, 0.9)))
    selected = [m for m in best_per_bucket if m.quality >= threshold]
    if len(selected) < min_frames:
        selected_ids = {m.frame_index for m in selected}
        backfill = sorted(best_per_bucket, key=lambda m: m.quality, reverse=True)
        for metric in backfill:
            if metric.frame_index in selected_ids:
                continue
            selected.append(metric)
            selected_ids.add(metric.frame_index)
            if len(selected) >= min_frames:
                break
        selected.sort(key=lambda m: m.timestamp_s)

    motion_filtered: list[FrameMetric] = []
    last_kept: FrameMetric | None = None
    for metric in selected:
        if last_kept is None:
            motion_filtered.append(metric)
            last_kept = metric
            continue
        delta = _mean_abs_delta(last_kept.fingerprint, metric.fingerprint)
        metric.motion_delta = delta
        gap = metric.timestamp_s - last_kept.timestamp_s
        if delta >= min_motion or gap >= max_still_gap_s or len(selected) <= min_frames:
            motion_filtered.append(metric)
            last_kept = metric

    if len(motion_filtered) < min_frames:
        motion_filtered = selected

    if len(motion_filtered) <= max_frames:
        return motion_filtered

    window_count = max_frames
    duration = max(motion_filtered[-1].timestamp_s - motion_filtered[0].timestamp_s, 1e-6)
    window_s = duration / window_count
    capped: list[FrameMetric] = []
    for window in range(window_count):
        start = motion_filtered[0].timestamp_s + window * window_s
        end = start + window_s
        candidates = [m for m in motion_filtered if start <= m.timestamp_s < end]
        if candidates:
            capped.append(max(candidates, key=lambda m: m.quality))
    return sorted(capped[:max_frames], key=lambda m: m.timestamp_s)


def crop_frame(frame: np.ndarray, fraction: float) -> np.ndarray:
    """Crop an equal fraction from all image edges."""
    if fraction <= 0:
        return frame
    h, w = frame.shape[:2]
    x = int(round(w * fraction))
    y = int(round(h * fraction))
    if x * 2 >= w or y * 2 >= h:
        raise ValueError("crop fraction is too large for the frame size")
    return frame[y : h - y, x : w - x]


def gray_world_white_balance(frame: np.ndarray, max_gain: float) -> np.ndarray:
    """Apply bounded gray-world channel balancing to an underwater frame."""
    arr = frame.astype(np.float32)
    means = arr.reshape(-1, 3).mean(axis=0)
    target = float(means.mean())
    gains = np.clip(target / np.maximum(means, 1.0), 1.0 / max_gain, max_gain)
    return np.clip(arr * gains, 0, 255).astype(np.uint8)


def enhance_underwater(frame: np.ndarray, *, wb_gain: float, clahe_clip: float, sharpen: float) -> np.ndarray:
    """Create a color-enhanced frame for texture or standard geometry export."""
    balanced = gray_world_white_balance(frame, wb_gain)
    lab = cv2.cvtColor(balanced, cv2.COLOR_BGR2LAB)
    l_channel, a_channel, b_channel = cv2.split(lab)
    clahe = cv2.createCLAHE(clipLimit=clahe_clip, tileGridSize=(8, 8))
    l_channel = clahe.apply(l_channel)
    enhanced = cv2.cvtColor(cv2.merge((l_channel, a_channel, b_channel)), cv2.COLOR_LAB2BGR)
    if sharpen > 0:
        blurred = cv2.GaussianBlur(enhanced, (0, 0), sigmaX=1.2)
        amount = float(sharpen)
        enhanced = cv2.addWeighted(enhanced, 1.0 + amount, blurred, -amount, 0)
    return enhanced


def make_luma_geometry(
    frame: np.ndarray,
    *,
    wb_gain: float,
    clahe_clip: float,
    sharpen: float,
    flatten_turbidity: bool,
) -> np.ndarray:
    """Build a contrast-focused luminance frame for RealityScan alignment."""
    balanced = gray_world_white_balance(frame, wb_gain)
    lab = cv2.cvtColor(balanced, cv2.COLOR_BGR2LAB)
    l_channel = lab[:, :, 0]
    if flatten_turbidity:
        h, w = l_channel.shape[:2]
        sigma = max(12.0, min(h, w) / 28.0)
        background = cv2.GaussianBlur(l_channel, (0, 0), sigmaX=sigma, sigmaY=sigma)
        l_float = l_channel.astype(np.float32) - background.astype(np.float32)
        l_channel = cv2.normalize(l_float, None, 0, 255, cv2.NORM_MINMAX).astype(np.uint8)
    clahe = cv2.createCLAHE(clipLimit=clahe_clip, tileGridSize=(8, 8))
    l_channel = clahe.apply(l_channel)
    if sharpen > 0:
        blurred = cv2.GaussianBlur(l_channel, (0, 0), sigmaX=1.0)
        amount = min(float(sharpen) * 1.6, 0.8)
        l_channel = cv2.addWeighted(l_channel, 1.0 + amount, blurred, -amount, 0)
    return cv2.cvtColor(l_channel, cv2.COLOR_GRAY2BGR)


def make_geometry_frame(
    frame: np.ndarray,
    geometry_mode: str,
    *,
    wb_gain: float,
    clahe_clip: float,
    sharpen: float,
) -> np.ndarray:
    """Create the frame image used for geometry under the selected mode."""
    if geometry_mode == "enhanced":
        return enhance_underwater(frame, wb_gain=wb_gain, clahe_clip=clahe_clip, sharpen=sharpen)
    if geometry_mode == "luma":
        return make_luma_geometry(
            frame,
            wb_gain=wb_gain,
            clahe_clip=clahe_clip,
            sharpen=sharpen,
            flatten_turbidity=False,
        )
    if geometry_mode == "flat_luma":
        return make_luma_geometry(
            frame,
            wb_gain=wb_gain,
            clahe_clip=clahe_clip,
            sharpen=sharpen,
            flatten_turbidity=True,
        )
    raise ValueError(f"Unknown geometry mode: {geometry_mode}")


def make_cv_foreground_mask(color_frame: np.ndarray, geometry_frame: np.ndarray) -> np.ndarray:
    """Create a conservative foreground mask for texture/feature variants."""
    gray = cv2.cvtColor(geometry_frame, cv2.COLOR_BGR2GRAY)
    gray = cv2.GaussianBlur(gray, (3, 3), 0)
    median = float(np.median(gray))
    lower = int(max(12, 0.66 * median))
    upper = int(min(220, max(lower + 30, 1.33 * median)))
    edges = cv2.Canny(gray, lower, upper)

    lap = cv2.Laplacian(gray, cv2.CV_32F)
    lap_abs = cv2.convertScaleAbs(lap)
    contrast_threshold = float(np.percentile(lap_abs, 88))
    contrast_mask = (lap_abs >= max(contrast_threshold, 8.0)).astype(np.uint8) * 255

    hsv = cv2.cvtColor(color_frame, cv2.COLOR_BGR2HSV)
    h, s, v = cv2.split(hsv)
    red_hue = ((h <= 12) | (h >= 168)) & (s >= 35) & (v >= 28)
    lab = cv2.cvtColor(color_frame, cv2.COLOR_BGR2LAB)
    a_channel = lab[:, :, 1]
    red_lab = (a_channel.astype(np.float32) >= float(np.mean(a_channel) + 0.75 * np.std(a_channel))) & (v >= 25)
    red_mask = (red_hue | red_lab).astype(np.uint8) * 255

    mask = cv2.bitwise_or(edges, contrast_mask)
    mask = cv2.bitwise_or(mask, red_mask)
    h_img, w_img = mask.shape[:2]
    close_size = max(9, int(round(min(h_img, w_img) * 0.018)) | 1)
    dilate_size = max(13, int(round(min(h_img, w_img) * 0.028)) | 1)
    close_kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (close_size, close_size))
    dilate_kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (dilate_size, dilate_size))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, close_kernel)
    mask = cv2.dilate(mask, dilate_kernel, iterations=1)

    num_labels, labels, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    cleaned = np.zeros_like(mask)
    min_area = max(80, int(mask.size * 0.00045))
    for label in range(1, num_labels):
        if int(stats[label, cv2.CC_STAT_AREA]) >= min_area:
            cleaned[labels == label] = 255
    coverage = float(np.count_nonzero(cleaned)) / float(cleaned.size)
    if coverage < 0.025 or coverage > 0.88:
        return np.full_like(mask, 255)
    return cleaned


def maybe_rectify(frame: np.ndarray, enabled: bool, corrector: object | None) -> np.ndarray:
    """Apply optional water-refraction rectification to a frame."""
    if not enabled:
        return frame
    if corrector is None:
        raise RuntimeError("WaterCorrection could not be loaded.")
    return corrector.apply(frame)


def make_water_corrector(enabled: bool) -> object | None:
    """Construct the TritonPilot water-correction helper when requested."""
    if not enabled:
        return None
    from config import (
        WATER_CORRECTION_AIR_HFOV_DEG,
        WATER_CORRECTION_K1,
        WATER_CORRECTION_K2,
        WATER_CORRECTION_K3,
        WATER_CORRECTION_TARGET_HFOV_DEG,
        WATER_CORRECTION_ZOOM,
    )
    from video.frame_correction import WaterCorrection

    return WaterCorrection(
        zoom=WATER_CORRECTION_ZOOM,
        k1=WATER_CORRECTION_K1,
        k2=WATER_CORRECTION_K2,
        k3=WATER_CORRECTION_K3,
        air_hfov_deg=WATER_CORRECTION_AIR_HFOV_DEG,
        target_hfov_deg=WATER_CORRECTION_TARGET_HFOV_DEG,
    )


def prepare_output_paths(video_path: Path, output_root: Path | None, overwrite: bool) -> OutputPaths:
    """Create the output workspace layout for a pipeline run."""
    if output_root is None:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_root = REPO_ROOT / "results" / "realityscan" / f"{_safe_slug(video_path.stem)}_{stamp}"
    output_root = output_root.resolve()
    if output_root.exists() and overwrite:
        shutil.rmtree(output_root)
    output_root.mkdir(parents=True, exist_ok=True)

    variants = output_root / "variants"
    alignments = output_root / "alignments"
    reports = output_root / "reports"
    frames = output_root / "frames_enhanced"
    logs = output_root / "logs"
    frames.mkdir(parents=True, exist_ok=True)
    variants.mkdir(parents=True, exist_ok=True)
    alignments.mkdir(parents=True, exist_ok=True)
    reports.mkdir(parents=True, exist_ok=True)
    logs.mkdir(parents=True, exist_ok=True)
    return OutputPaths(
        root=output_root,
        frames=frames,
        variants=variants,
        alignments=alignments,
        reports=reports,
        logs=logs,
        model=output_root / "underwater_model.obj",
        project=output_root / "underwater_project.rsproj",
        rscmd=output_root / "reconstruct_underwater.rscmd",
        progress=logs / "realityscan_progress.txt",
        crash_reports=logs / "crash_reports",
        metrics_csv=output_root / "frame_metrics.csv",
        manifest_json=output_root / "manifest.json",
        contact_sheet=output_root / "selection_contact_sheet.jpg",
    )


def make_variant_paths(paths: OutputPaths, spec: VariantSpec) -> VariantPaths:
    """Create filesystem paths for one alignment/preprocessing variant."""
    frames = paths.frames if spec.name == "enhanced_brown4" else paths.variants / spec.name
    frames.mkdir(parents=True, exist_ok=True)
    paths.alignments.mkdir(parents=True, exist_ok=True)
    paths.reports.mkdir(parents=True, exist_ok=True)
    paths.logs.mkdir(parents=True, exist_ok=True)
    return VariantPaths(
        name=spec.name,
        frames=frames,
        rscmd=paths.alignments / f"{spec.name}.rscmd",
        project=paths.alignments / f"{spec.name}.rsproj",
        progress=paths.logs / f"align_{spec.name}_progress.txt",
        stdout=paths.logs / f"align_{spec.name}_stdout.log",
        report=paths.reports / f"{spec.name}_overview.html",
        crash_reports=paths.logs / f"crash_reports_{spec.name}",
    )


def write_variant_frames(
    video_path: Path,
    selected: list[FrameMetric],
    frames_dir: Path,
    spec: VariantSpec,
    *,
    crop_fraction: float,
    wb_gain: float,
    clahe_clip: float,
    sharpen: float,
    jpeg_quality: int,
    texture_layers: bool,
) -> list[Path]:
    """Write selected frames for one RealityScan preprocessing variant."""
    selected_by_index = {m.frame_index: m for m in selected}
    written: list[Path] = []
    cap, _ = open_video(video_path)
    corrector = make_water_corrector(spec.rectify_water)
    try:
        frame_index = 0
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            metric = selected_by_index.get(frame_index)
            if metric is None:
                frame_index += 1
                continue

            frame = crop_frame(frame, crop_fraction)
            frame = maybe_rectify(frame, spec.rectify_water, corrector)
            texture = enhance_underwater(frame, wb_gain=wb_gain, clahe_clip=clahe_clip, sharpen=sharpen)
            geometry = make_geometry_frame(
                frame,
                spec.geometry_mode,
                wb_gain=wb_gain,
                clahe_clip=clahe_clip,
                sharpen=sharpen,
            )
            name = f"frame_{len(written):04d}_src_{frame_index:06d}_t_{metric.timestamp_s:08.3f}.jpg"
            out_path = frames_dir / name
            ok = cv2.imwrite(str(out_path), geometry, [int(cv2.IMWRITE_JPEG_QUALITY), int(jpeg_quality)])
            if not ok:
                raise RuntimeError(f"Could not write frame: {out_path}")
            if texture_layers:
                texture_path = out_path.with_name(f"{out_path.name}.texture.jpg")
                ok = cv2.imwrite(str(texture_path), texture, [int(cv2.IMWRITE_JPEG_QUALITY), int(jpeg_quality)])
                if not ok:
                    raise RuntimeError(f"Could not write texture layer: {texture_path}")
            if spec.cv_mask:
                mask = make_cv_foreground_mask(texture, geometry)
                mask_path = out_path.with_name(f"{out_path.name}.mask.png")
                ok = cv2.imwrite(str(mask_path), mask)
                if not ok:
                    raise RuntimeError(f"Could not write mask layer: {mask_path}")
            written.append(out_path)
            frame_index += 1
    finally:
        cap.release()

    if len(written) != len(selected):
        raise RuntimeError(f"Expected to write {len(selected)} frames, wrote {len(written)}.")
    return written


def write_selected_frames(
    video_path: Path,
    selected: list[FrameMetric],
    paths: OutputPaths,
    *,
    crop_fraction: float,
    rectify_water: bool,
    wb_gain: float,
    clahe_clip: float,
    sharpen: float,
    jpeg_quality: int,
) -> list[Path]:
    """Write the default enhanced frame set without tournament variants."""
    spec = VariantSpec(
        name="enhanced_brown4",
        geometry_mode="enhanced",
        rectify_water=rectify_water,
        cv_mask=False,
        ai_mask=False,
    )
    return write_variant_frames(
        video_path,
        selected,
        paths.frames,
        spec,
        crop_fraction=crop_fraction,
        wb_gain=wb_gain,
        clahe_clip=clahe_clip,
        sharpen=sharpen,
        jpeg_quality=jpeg_quality,
        texture_layers=False,
    )


def write_metrics_csv(metrics: list[FrameMetric], selected: Iterable[FrameMetric], paths: OutputPaths) -> None:
    """Persist frame scores and selection flags for audit/debugging."""
    selected_ids = {m.frame_index for m in selected}
    with paths.metrics_csv.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            [
                "selected",
                "frame_index",
                "timestamp_s",
                "quality",
                "sharpness",
                "contrast",
                "brightness",
                "feature_count",
                "motion_delta",
            ]
        )
        for metric in metrics:
            writer.writerow(
                [
                    1 if metric.frame_index in selected_ids else 0,
                    metric.frame_index,
                    f"{metric.timestamp_s:.6f}",
                    f"{metric.quality:.6f}",
                    f"{metric.sharpness:.6f}",
                    f"{metric.contrast:.6f}",
                    f"{metric.brightness:.6f}",
                    metric.feature_count,
                    f"{metric.motion_delta:.6f}",
                ]
            )


def make_contact_sheet(frame_paths: list[Path], selected: list[FrameMetric], out_path: Path, max_tiles: int = 30) -> None:
    """Write a compact visual preview of selected reconstruction frames."""
    if not frame_paths:
        return
    if len(frame_paths) <= max_tiles:
        indices = list(range(len(frame_paths)))
    else:
        indices = sorted({int(round(i)) for i in np.linspace(0, len(frame_paths) - 1, max_tiles)})

    tiles: list[np.ndarray] = []
    for idx in indices:
        image = cv2.imread(str(frame_paths[idx]), cv2.IMREAD_COLOR)
        if image is None:
            continue
        tile_w = 320
        scale = tile_w / image.shape[1]
        tile_h = int(round(image.shape[0] * scale))
        image = cv2.resize(image, (tile_w, tile_h), interpolation=cv2.INTER_AREA)
        metric = selected[idx]
        label = f"{idx:03d}  t={metric.timestamp_s:05.1f}s  q={metric.quality:.2f}"
        cv2.rectangle(image, (0, 0), (tile_w, 26), (0, 0, 0), -1)
        cv2.putText(image, label, (8, 18), cv2.FONT_HERSHEY_SIMPLEX, 0.48, (255, 255, 255), 1, cv2.LINE_AA)
        tiles.append(image)

    if not tiles:
        return
    tile_h, tile_w = tiles[0].shape[:2]
    cols = min(5, len(tiles))
    rows = int(math.ceil(len(tiles) / cols))
    sheet = np.zeros((rows * tile_h, cols * tile_w, 3), dtype=np.uint8)
    for i, tile in enumerate(tiles):
        row, col = divmod(i, cols)
        sheet[row * tile_h : (row + 1) * tile_h, col * tile_w : (col + 1) * tile_w] = tile
    cv2.imwrite(str(out_path), sheet, [int(cv2.IMWRITE_JPEG_QUALITY), 92])


def write_connectivity_csv(frame_paths: list[Path], selected: list[FrameMetric], out_path: Path) -> None:
    """Write adjacent-frame feature matching diagnostics."""
    if len(frame_paths) < 2:
        return
    out_path.parent.mkdir(parents=True, exist_ok=True)
    orb = cv2.ORB_create(nfeatures=1600, fastThreshold=8)
    matcher = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True)

    def features(path: Path) -> tuple[list[cv2.KeyPoint], np.ndarray | None]:
        image = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
        if image is None:
            return [], None
        scale = min(1.0, 900.0 / max(image.shape[:2]))
        if scale < 1.0:
            image = cv2.resize(image, (int(image.shape[1] * scale), int(image.shape[0] * scale)), interpolation=cv2.INTER_AREA)
        keypoints, descriptors = orb.detectAndCompute(image, None)
        return keypoints, descriptors

    cached = [features(path) for path in frame_paths]
    with out_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            [
                "from_idx",
                "to_idx",
                "from_time_s",
                "to_time_s",
                "time_gap_s",
                "from_keypoints",
                "to_keypoints",
                "matches",
                "homography_inliers",
                "inlier_ratio",
            ]
        )
        for idx in range(len(frame_paths) - 1):
            kp_a, desc_a = cached[idx]
            kp_b, desc_b = cached[idx + 1]
            matches: list[cv2.DMatch] = []
            inliers = 0
            if desc_a is not None and desc_b is not None and len(kp_a) >= 8 and len(kp_b) >= 8:
                matches = sorted(matcher.match(desc_a, desc_b), key=lambda item: item.distance)[:240]
                if len(matches) >= 8:
                    pts_a = np.float32([kp_a[match.queryIdx].pt for match in matches])
                    pts_b = np.float32([kp_b[match.trainIdx].pt for match in matches])
                    _, mask = cv2.findHomography(pts_a, pts_b, cv2.RANSAC, 4.0)
                    if mask is not None:
                        inliers = int(mask.ravel().sum())
            inlier_ratio = inliers / max(len(matches), 1)
            writer.writerow(
                [
                    idx,
                    idx + 1,
                    f"{selected[idx].timestamp_s:.6f}",
                    f"{selected[idx + 1].timestamp_s:.6f}",
                    f"{selected[idx + 1].timestamp_s - selected[idx].timestamp_s:.6f}",
                    len(kp_a),
                    len(kp_b),
                    len(matches),
                    inliers,
                    f"{inlier_ratio:.6f}",
                ]
            )


def build_variant_specs(args: argparse.Namespace) -> list[VariantSpec]:
    """Build the preprocessing/alignment variants requested by CLI options."""
    base = VariantSpec(
        name="enhanced_brown4",
        geometry_mode="enhanced",
        rectify_water=args.rectify_water,
        cv_mask=False,
        ai_mask=False,
        distortion_model=args.distortion_model,
        detector_sensitivity=args.detector_sensitivity,
        images_overlap=args.images_overlap,
    )
    if args.alignment_tournament == "off":
        return [base]

    kplus = "KplusBrown4WithTangential2"
    variants = [
        VariantSpec(
            name="flat_luma_kplus",
            geometry_mode="flat_luma",
            rectify_water=args.rectify_water,
            distortion_model=kplus,
            detector_sensitivity="Ultra",
            images_overlap="Low",
        ),
        VariantSpec(
            name="flat_luma_mask_kplus",
            geometry_mode="flat_luma",
            rectify_water=args.rectify_water,
            cv_mask=args.cv_masks,
            distortion_model=kplus,
            detector_sensitivity="Ultra",
            images_overlap="Low",
        ),
        base,
    ]
    if args.alignment_tournament == "thorough":
        variants.extend(
            [
                VariantSpec(
                    name="luma_kplus_high_overlap",
                    geometry_mode="luma",
                    rectify_water=args.rectify_water,
                    distortion_model=kplus,
                    detector_sensitivity="Ultra",
                    images_overlap="High",
                ),
                VariantSpec(
                    name="flat_luma_division",
                    geometry_mode="flat_luma",
                    rectify_water=args.rectify_water,
                    distortion_model="Division",
                    detector_sensitivity="Ultra",
                    images_overlap="Low",
                ),
            ]
        )
        if args.include_ai_masks:
            variants.append(
                VariantSpec(
                    name="flat_luma_ai_mask_kplus",
                    geometry_mode="flat_luma",
                    rectify_water=args.rectify_water,
                    ai_mask=True,
                    distortion_model=kplus,
                    detector_sensitivity="Ultra",
                    images_overlap="Low",
                )
            )
        if args.rectify_tournament and not args.rectify_water:
            variants.append(
                VariantSpec(
                    name="rectified_flat_luma_kplus",
                    geometry_mode="flat_luma",
                    rectify_water=True,
                    distortion_model=kplus,
                    detector_sensitivity="Ultra",
                    images_overlap="Low",
                )
            )
    return variants


def overview_template_path(realityscan_exe: Path | None) -> Path | None:
    """Find RealityScan's bundled overview report template when available."""
    candidates: list[Path] = []
    if realityscan_exe is not None:
        candidates.append(realityscan_exe.parent / "Reports" / "Overview.html")
    candidates.append(Path(r"C:\Program Files\Epic Games\RealityScan_2.1\Reports\Overview.html"))
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def alignment_setting_commands(args: argparse.Namespace, spec: VariantSpec | None = None) -> list[str]:
    """Return common RealityScan alignment setting commands."""
    distortion_model = spec.distortion_model if spec and spec.distortion_model else args.distortion_model
    detector_sensitivity = spec.detector_sensitivity if spec and spec.detector_sensitivity else args.detector_sensitivity
    images_overlap = spec.images_overlap if spec and spec.images_overlap else args.images_overlap
    return [
        '-set "appIncSubdirs=false"',
        '-set "appQuitOnError=true"',
        '-set "suppressErrors=true"',
        '-set "appAutoSaveMode=true"',
        '-set "sfmFeatureDetectionQuality=High"',
        f'-set "sfmMaxFeaturesPerMpx={args.max_features_per_mpx}"',
        f'-set "sfmMaxFeaturesPerImage={args.max_features_per_image}"',
        f'-set "sfmPreselectorFeatures={args.preselector_features}"',
        f'-set "sfmDetectorSensitivity={detector_sensitivity}"',
        f'-set "sfmImagesOverlap={images_overlap}"',
        '-set "sfmImageDownscaleFactor=1"',
        '-set "sfmForceComponentRematch=true"',
        f'-set "sfmDistortionModel={distortion_model}"',
    ]


def write_alignment_command_file(
    variant_paths: VariantPaths,
    spec: VariantSpec,
    args: argparse.Namespace,
    overview_template: Path | None,
) -> None:
    """Write an RSCMD file that aligns one tournament variant."""
    commands = [
        "# Generated by tools/realityscan_underwater_pipeline.py",
        f"# Alignment tournament variant: {spec.name}",
        "-newScene",
        *alignment_setting_commands(args, spec),
        f"-addFolder {_path_for_cli(variant_paths.frames)}",
        "-selectAllImages",
    ]
    if spec.ai_mask:
        commands.append("-generateAIMasks")
    commands.extend(
        [
            "-setFeatureSource 2",
            "-setConstantCalibrationGroups",
            "-setPriorCalibrationGroup 1",
            "-setPriorLensGroup 1",
            "-align",
        ]
    )
    if args.try_merge_components:
        commands.append("-mergeComponents")
    commands.append("-selectMaximalComponent")
    if overview_template is not None:
        commands.append(f"-exportReport {_path_for_cli(variant_paths.report)} {_path_for_cli(overview_template)} true")
    commands.extend(
        [
            f"-save {_path_for_cli(variant_paths.project)}",
            "-quit",
        ]
    )
    variant_paths.rscmd.write_text("\n".join(commands) + "\n", encoding="utf-8")


def _strip_report_text(value: str) -> str:
    """Remove HTML tags/entities from a RealityScan report fragment."""
    text = re.sub(r"<[^>]+>", " ", value)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _parse_float(value: str) -> float:
    """Extract the first float-like number from a report value."""
    match = re.search(r"[-+]?\d+(?:\.\d+)?", value.replace(",", ""))
    return float(match.group(0)) if match else 0.0


def parse_alignment_report(
    report_path: Path,
    *,
    name: str,
    selected_image_count: int,
    project: Path,
) -> AlignmentResult:
    """Parse RealityScan's overview report into a sortable alignment result."""
    if not report_path.exists():
        return AlignmentResult(
            name=name,
            score=-1.0,
            component_count=0,
            largest_component_images=0,
            total_registered_images=0,
            selected_image_count=selected_image_count,
            largest_component_ratio=0.0,
            total_registered_ratio=0.0,
            report=report_path,
            project=project,
        )
    text = report_path.read_text(encoding="utf-8", errors="replace")
    sections = re.split(r'<p\s+class="itemTitle">\s*Component:', text, flags=re.IGNORECASE)
    registered_counts: list[int] = []
    reprojection_errors: list[float] = []
    point_counts: list[int] = []
    for section in sections[1:]:
        props: dict[str, str] = {}
        for key, value in re.findall(r"<th>\s*(.*?)\s*</th>\s*<td>\s*(.*?)\s*</td>", section, flags=re.DOTALL | re.IGNORECASE):
            props[_strip_report_text(key)] = _strip_report_text(value)
        registered = props.get("Count of registered images", "")
        registered_match = re.search(r"(\d+)\s*/\s*(\d+)", registered)
        if registered_match:
            registered_counts.append(int(registered_match.group(1)))
        points_value = props.get("Points' count")
        if points_value:
            point_counts.append(int(_parse_float(points_value)))
        reprojection_value = props.get("Mean reprojection error [pixels]")
        if reprojection_value:
            reprojection_errors.append(_parse_float(reprojection_value))

    component_count = len(registered_counts)
    largest_component_images = max(registered_counts, default=0)
    total_registered_images = sum(registered_counts)
    denom = max(selected_image_count, 1)
    largest_ratio = largest_component_images / denom
    total_ratio = min(total_registered_images / denom, 1.0)
    points_bonus = min(sum(point_counts) / max(denom * 800.0, 1.0), 1.0)
    reprojection_penalty = 0.0
    if reprojection_errors:
        reprojection_penalty = min(max(float(np.mean(reprojection_errors)) - 0.75, 0.0) / 2.0, 1.0)
    fragmentation_penalty = min(max(component_count - 1, 0) / 35.0, 1.0)
    score = (0.72 * largest_ratio) + (0.18 * total_ratio) + (0.06 * points_bonus) - (0.08 * fragmentation_penalty) - (0.04 * reprojection_penalty)
    return AlignmentResult(
        name=name,
        score=score,
        component_count=component_count,
        largest_component_images=largest_component_images,
        total_registered_images=total_registered_images,
        selected_image_count=selected_image_count,
        largest_component_ratio=largest_ratio,
        total_registered_ratio=total_ratio,
        report=report_path,
        project=project,
    )


def write_alignment_results(paths: OutputPaths, results: list[AlignmentResult]) -> None:
    """Write alignment tournament results as CSV and JSON summaries."""
    csv_path = paths.reports / "alignment_results.csv"
    json_path = paths.reports / "alignment_results.json"
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            [
                "name",
                "score",
                "largest_component_images",
                "selected_image_count",
                "largest_component_ratio",
                "total_registered_images",
                "total_registered_ratio",
                "component_count",
                "report",
                "project",
            ]
        )
        for result in results:
            writer.writerow(
                [
                    result.name,
                    f"{result.score:.6f}",
                    result.largest_component_images,
                    result.selected_image_count,
                    f"{result.largest_component_ratio:.6f}",
                    result.total_registered_images,
                    f"{result.total_registered_ratio:.6f}",
                    result.component_count,
                    str(result.report),
                    str(result.project),
                ]
            )
    json_path.write_text(
        json.dumps(
            [
                {
                    "name": result.name,
                    "score": result.score,
                    "largest_component_images": result.largest_component_images,
                    "selected_image_count": result.selected_image_count,
                    "largest_component_ratio": result.largest_component_ratio,
                    "total_registered_images": result.total_registered_images,
                    "total_registered_ratio": result.total_registered_ratio,
                    "component_count": result.component_count,
                    "report": str(result.report),
                    "project": str(result.project),
                }
                for result in results
            ],
            indent=2,
        ),
        encoding="utf-8",
    )


def model_command(quality: str) -> str:
    """Map a CLI model quality label to the RealityScan command name."""
    return {
        "preview": "-calculatePreviewModel",
        "normal": "-calculateNormalModel",
        "high": "-calculateHighModel",
    }[quality]


def write_realityscan_command_file(
    paths: OutputPaths,
    args: argparse.Namespace,
    *,
    frames_dir: Path | None = None,
    spec: VariantSpec | None = None,
    overview_template: Path | None = None,
) -> None:
    """Write the final RealityScan reconstruction command file."""
    frames_dir = frames_dir or paths.frames
    base_model_name = "Model 1"
    simplified_model_name = "Model 2" if args.simplify_triangles > 0 else base_model_name
    export_model_name = "Model 3" if args.simplify_triangles > 0 else "Model 2"
    commands = [
        "# Generated by tools/realityscan_underwater_pipeline.py",
        "-newScene",
        *alignment_setting_commands(args, spec),
        f'-set "mvsNormalDownscaleFactor={args.normal_downscale}"',
        '-set "MvsGeometryGpuAccel=true"',
        '-set "mvsLowTextureGroupingFactor=0.35"',
        '-set "mvsLowTextureNoiseFactor=1.75"',
        '-set "MvsDoCorrectColors=true"',
        '-set "unwrapStyle=MaxTexturesCount"',
        f'-set "unwrapMaximalTexCount={args.texture_count}"',
        f'-set "unwrapMaxTexResolution={args.texture_resolution}"',
        '-set "unwrapMinTexResolution=512"',
        '-set "txtImageDownscaleTexture=1"',
        '-set "txtStyle=VisibilityBased"',
        f"-addFolder {_path_for_cli(frames_dir)}",
        "-selectAllImages",
    ]
    if spec and spec.ai_mask:
        commands.append("-generateAIMasks")
    commands.extend(
        [
            "-setFeatureSource 2",
            "-setConstantCalibrationGroups",
            "-setPriorCalibrationGroup 1",
            "-setPriorLensGroup 1",
            "-align",
        ]
    )
    if args.try_merge_components:
        commands.append("-mergeComponents")
    commands.append("-selectMaximalComponent")
    if overview_template is not None:
        commands.append(f"-exportReport {_path_for_cli(paths.reports / 'final_overview.html')} {_path_for_cli(overview_template)} true")
    commands.extend(
        [
            "-setReconstructionRegionByDensity",
            f"-scaleReconstructionRegion {args.recon_region_scale_xy} {args.recon_region_scale_xy} {args.recon_region_scale_z} center factor",
            model_command(args.model_quality),
            f'-selectModel "{base_model_name}"',
        ]
    )
    if args.simplify_triangles > 0:
        commands.extend(
            [
                f"-simplify {args.simplify_triangles}",
                f'-selectModel "{simplified_model_name}"',
            ]
        )
    commands.extend(
        [
            "-cleanModel",
            f'-selectModel "{export_model_name}"',
            "-unwrap",
            "-correctColors",
            "-calculateTexture",
            f'-exportModel "{export_model_name}" {_path_for_cli(paths.model)}',
            f"-save {_path_for_cli(paths.project)}",
            "-quit",
        ]
    )
    paths.rscmd.write_text("\n".join(commands) + "\n", encoding="utf-8")


def write_manifest(
    paths: OutputPaths,
    video_path: Path,
    info: VideoInfo,
    selected: list[FrameMetric],
    frame_paths: list[Path],
    realityscan_exe: Path | None,
    args: argparse.Namespace,
    *,
    variant_specs: list[VariantSpec] | None = None,
    variant_paths: dict[str, VariantPaths] | None = None,
    variant_frame_paths: dict[str, list[Path]] | None = None,
    alignment_results: list[AlignmentResult] | None = None,
    best_alignment: AlignmentResult | None = None,
) -> None:
    """Write a machine-readable manifest for the generated workspace."""
    manifest = {
        "video": str(video_path.resolve()),
        "video_info": {
            "fps": info.fps,
            "frame_count": info.frame_count,
            "width": info.width,
            "height": info.height,
            "duration_s": info.duration_s,
        },
        "selected_frame_count": len(selected),
        "frames_dir": str(frame_paths[0].parent if frame_paths else paths.frames),
        "frame_files": [str(path) for path in frame_paths],
        "variants": [
            {
                "name": spec.name,
                "geometry_mode": spec.geometry_mode,
                "rectify_water": spec.rectify_water,
                "cv_mask": spec.cv_mask,
                "ai_mask": spec.ai_mask,
                "distortion_model": spec.distortion_model,
                "detector_sensitivity": spec.detector_sensitivity,
                "images_overlap": spec.images_overlap,
                "frames_dir": str(variant_paths[spec.name].frames) if variant_paths and spec.name in variant_paths else None,
                "frame_count": len(variant_frame_paths[spec.name]) if variant_frame_paths and spec.name in variant_frame_paths else None,
            }
            for spec in (variant_specs or [])
        ],
        "alignment_results": [
            {
                "name": result.name,
                "score": result.score,
                "largest_component_images": result.largest_component_images,
                "selected_image_count": result.selected_image_count,
                "largest_component_ratio": result.largest_component_ratio,
                "total_registered_images": result.total_registered_images,
                "total_registered_ratio": result.total_registered_ratio,
                "component_count": result.component_count,
                "report": str(result.report),
                "project": str(result.project),
            }
            for result in (alignment_results or [])
        ],
        "best_alignment": None
        if best_alignment is None
        else {
            "name": best_alignment.name,
            "score": best_alignment.score,
            "largest_component_images": best_alignment.largest_component_images,
            "selected_image_count": best_alignment.selected_image_count,
            "largest_component_ratio": best_alignment.largest_component_ratio,
            "component_count": best_alignment.component_count,
            "report": str(best_alignment.report),
            "project": str(best_alignment.project),
        },
        "realityscan_exe": str(realityscan_exe) if realityscan_exe else None,
        "rscmd": str(paths.rscmd),
        "project": str(paths.project),
        "model": str(paths.model),
        "settings": {
            key: value
            for key, value in vars(args).items()
            if key not in {"video", "output", "realityscan_exe"}
        },
    }
    paths.manifest_json.write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def wait_for_detached_realityscan(
    progress_path: Path,
    expected_paths: list[Path],
    timeout_hours: float,
    *,
    label: str,
) -> None:
    """Poll progress/output files until a detached RealityScan run settles."""
    deadline = time.monotonic() + timeout_hours * 3600.0
    idle_required_s = 45.0
    last_size = -1
    last_mtime = 0.0
    last_change = time.monotonic()
    last_reported_line = ""
    print(f"Monitoring RealityScan progress for {label}...")
    while time.monotonic() < deadline:
        changed = False
        if progress_path.exists():
            stat = progress_path.stat()
            changed = stat.st_size != last_size or stat.st_mtime != last_mtime
            if changed:
                last_size = stat.st_size
                last_mtime = stat.st_mtime
                last_change = time.monotonic()
                try:
                    lines = progress_path.read_text(encoding="utf-8", errors="replace").splitlines()
                except OSError:
                    lines = []
                if lines:
                    line = lines[-1]
                    if line != last_reported_line:
                        print(f"RealityScan progress: {line}")
                        last_reported_line = line
        elif any(path.exists() for path in expected_paths):
            last_change = time.monotonic()

        idle_s = time.monotonic() - last_change
        if any(path.exists() for path in expected_paths) and idle_s >= 10.0:
            return
        if expected_paths and expected_paths[-1].exists() and idle_s >= idle_required_s:
            return
        time.sleep(5.0)
    raise RuntimeError(f"RealityScan did not finish {label} within {timeout_hours} hours")


def run_realityscan_rscmd(
    *,
    rscmd: Path,
    progress: Path,
    crash_reports: Path,
    stdout_log: Path,
    expected_paths: list[Path],
    realityscan_exe: Path,
    timeout_hours: float,
    label: str,
) -> int:
    """Launch RealityScan with an RSCMD file and stream its console output."""
    crash_reports.mkdir(parents=True, exist_ok=True)
    progress.parent.mkdir(parents=True, exist_ok=True)
    stdout_log.parent.mkdir(parents=True, exist_ok=True)
    if progress.exists():
        progress.unlink()
    cmd = [
        str(realityscan_exe),
        "-headless",
        "-stdConsole",
        "-silent",
        str(crash_reports),
        "-writeProgress",
        str(progress),
        "-execRSCMD",
        str(rscmd),
    ]
    print(f"Launching RealityScan for {label}:")
    print(" ".join(f'"{part}"' if " " in part else part for part in cmd))
    with stdout_log.open("w", encoding="utf-8", errors="replace") as log:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                safe_line = line.encode(sys.stdout.encoding or "utf-8", errors="replace").decode(
                    sys.stdout.encoding or "utf-8",
                    errors="replace",
                )
                print(safe_line, end="")
                log.write(line)
            code = proc.wait(timeout=timeout_hours * 3600.0)
            if code != 0 and not any(path.exists() for path in expected_paths):
                return code
            wait_for_detached_realityscan(progress, expected_paths, timeout_hours, label=label)
            return code
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.wait(timeout=60)
            except subprocess.TimeoutExpired:
                proc.kill()
            raise RuntimeError(f"RealityScan timed out after {timeout_hours} hours")


def run_realityscan(paths: OutputPaths, realityscan_exe: Path, timeout_hours: float) -> int:
    """Run the final reconstruction command file."""
    return run_realityscan_rscmd(
        rscmd=paths.rscmd,
        progress=paths.progress,
        crash_reports=paths.crash_reports,
        stdout_log=paths.logs / "realityscan_stdout.log",
        expected_paths=[paths.model, paths.project],
        realityscan_exe=realityscan_exe,
        timeout_hours=timeout_hours,
        label="final reconstruction",
    )


def run_alignment_tournament(
    paths: OutputPaths,
    variant_specs: list[VariantSpec],
    variant_paths: dict[str, VariantPaths],
    selected_count: int,
    realityscan_exe: Path,
    args: argparse.Namespace,
) -> list[AlignmentResult]:
    """Execute and score all requested alignment tournament variants."""
    results: list[AlignmentResult] = []
    for spec in variant_specs:
        if args.alignment_tournament == "off":
            continue
        vp = variant_paths[spec.name]
        start = time.perf_counter()
        code = run_realityscan_rscmd(
            rscmd=vp.rscmd,
            progress=vp.progress,
            crash_reports=vp.crash_reports,
            stdout_log=vp.stdout,
            expected_paths=[vp.report, vp.project],
            realityscan_exe=realityscan_exe,
            timeout_hours=args.timeout_hours,
            label=f"alignment variant {spec.name}",
        )
        elapsed = time.perf_counter() - start
        result = parse_alignment_report(
            vp.report,
            name=spec.name,
            selected_image_count=selected_count,
            project=vp.project,
        )
        results.append(result)
        print(
            f"Alignment {spec.name}: exit={code}, {elapsed / 60.0:.1f}m, "
            f"largest={result.largest_component_images}/{selected_count} "
            f"({result.largest_component_ratio:.1%}), components={result.component_count}, score={result.score:.3f}"
        )
        write_alignment_results(paths, results)
        if code != 0 and args.stop_on_alignment_error:
            raise RuntimeError(f"RealityScan alignment variant failed: {spec.name} (exit code {code})")
    return results


def build_arg_parser() -> argparse.ArgumentParser:
    """Build the command-line parser for the video-to-RealityScan pipeline."""
    parser = argparse.ArgumentParser(
        description="Autonomous underwater video to RealityScan textured model pipeline.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("video", type=Path, help="Input video file.")
    parser.add_argument("--output", type=Path, default=None, help="Output workspace. Defaults under results/realityscan/.")
    parser.add_argument("--overwrite", action="store_true", help="Replace the output workspace if it already exists.")
    parser.add_argument("--prepare-only", action="store_true", help="Prepare frames and the .rscmd file without launching RealityScan.")
    parser.add_argument("--alignment-only", action="store_true", help="Run alignment tournament and stop before meshing/texturing.")
    parser.add_argument("--alignment-tournament", choices=("off", "standard", "thorough"), default="off", help="Opt in to multiple preprocessing/alignment variants and mesh the best one.")
    parser.add_argument("--realityscan-exe", type=Path, default=None, help="Path to RealityScan.exe. Uses REALITYSCAN_EXE or auto-discovery when omitted.")
    parser.add_argument("--candidate-fps", type=_positive_float, default=10.0, help="Decode this many candidate frames per second for scoring.")
    parser.add_argument("--target-fps", type=_positive_float, default=8.0, help="Keep about this many frames per second after quality filtering.")
    parser.add_argument("--max-frames", type=int, default=420, help="Maximum selected frames sent to RealityScan.")
    parser.add_argument("--min-frames", type=int, default=120, help="Minimum selected frames to keep when the clip is long enough.")
    parser.add_argument("--quality-quantile", type=_nonnegative_float, default=0.05, help="Drop candidate bucket winners below this quality quantile.")
    parser.add_argument("--min-motion", type=_nonnegative_float, default=0.8, help="Mean grayscale delta below which adjacent selected frames are treated as near duplicates.")
    parser.add_argument("--max-still-gap-s", type=_positive_float, default=0.9, help="Keep a frame after this gap even if motion is low.")
    parser.add_argument("--crop-fraction", type=_nonnegative_float, default=0.04, help="Crop this fraction from each image edge before export.")
    parser.add_argument("--auto-crop-border", action=argparse.BooleanOptionalAction, default=True, help="Automatically crop fixed dark lens/housing borders before export.")
    parser.add_argument("--max-auto-crop", type=_nonnegative_float, default=0.12, help="Maximum fraction auto-crop may remove from each image edge.")
    parser.add_argument("--rectify-water", action="store_true", help="Apply TritonPilot WaterCorrection remap. Off by default because it changes geometry.")
    parser.add_argument("--rectify-tournament", action="store_true", help="In thorough tournament mode, also try a rectified-water variant.")
    parser.add_argument("--wb-gain", type=_positive_float, default=2.4, help="Maximum channel gain for gray-world underwater white balance.")
    parser.add_argument("--clahe-clip", type=_positive_float, default=2.0, help="CLAHE clip limit for contrast enhancement.")
    parser.add_argument("--sharpen", type=_nonnegative_float, default=0.22, help="Unsharp-mask amount applied after contrast enhancement.")
    parser.add_argument("--jpeg-quality", type=int, default=96, help="JPEG quality for selected frames.")
    parser.add_argument("--texture-layers", action=argparse.BooleanOptionalAction, default=False, help="Write RealityScan texture layers so geometry can use luma/dehazed images while texture uses color-enhanced frames.")
    parser.add_argument("--cv-masks", action=argparse.BooleanOptionalAction, default=True, help="Allow CV-generated mask-layer variants in the alignment tournament.")
    parser.add_argument("--include-ai-masks", action="store_true", help="In thorough tournament mode, try RealityScan AI masks as one variant.")
    parser.add_argument("--connectivity-report", action=argparse.BooleanOptionalAction, default=False, help="Write an adjacent-frame feature connectivity report for diagnosing temporal gaps.")
    parser.add_argument("--model-quality", choices=("preview", "normal", "high"), default="normal", help="RealityScan reconstruction quality.")
    parser.add_argument("--simplify-triangles", type=int, default=1_500_000, help="Simplify before texturing/export. Use 0 to keep the computed mesh selected.")
    parser.add_argument("--texture-count", type=int, default=4, help="Maximum texture atlas count.")
    parser.add_argument("--texture-resolution", type=int, default=4096, help="Maximum texture atlas side in pixels.")
    parser.add_argument("--normal-downscale", type=int, default=2, help="RealityScan normal model depth-map downscale factor.")
    parser.add_argument("--detector-sensitivity", choices=("Low", "Medium", "High", "Ultra"), default="Ultra", help="RealityScan feature detector sensitivity.")
    parser.add_argument("--images-overlap", choices=("Low", "Medium", "High"), default="Low", help="RealityScan alignment overlap assumption.")
    parser.add_argument("--distortion-model", default="Brown4WithTangential2", help="RealityScan lens distortion model.")
    parser.add_argument("--try-merge-components", action="store_true", help="Ask RealityScan to merge components after alignment. Off by default because it can duplicate fragments in this data.")
    parser.add_argument("--min-good-component-ratio", type=_positive_float, default=0.45, help="Warn when the best alignment's largest component is below this fraction of selected frames.")
    parser.add_argument("--stop-on-alignment-error", action="store_true", help="Stop the tournament immediately if a RealityScan alignment variant exits with an error.")
    parser.add_argument("--fail-on-poor-alignment", action="store_true", help="Do not run the final mesh if the best alignment is below --min-good-component-ratio.")
    parser.add_argument("--max-features-per-mpx", type=int, default=20000, help="RealityScan alignment features per megapixel.")
    parser.add_argument("--max-features-per-image", type=int, default=80000, help="RealityScan alignment features per image.")
    parser.add_argument("--preselector-features", type=int, default=20000, help="RealityScan preselector features.")
    parser.add_argument("--recon-region-scale-xy", type=_positive_float, default=1.25, help="Scale reconstruction region in X/Y after density fit.")
    parser.add_argument("--recon-region-scale-z", type=_positive_float, default=1.35, help="Scale reconstruction region in Z after density fit.")
    parser.add_argument("--timeout-hours", type=_positive_float, default=8.0, help="RealityScan process timeout.")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the end-to-end frame preparation and optional reconstruction flow."""
    total_start = time.perf_counter()
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    video_path = args.video.expanduser().resolve()
    if not video_path.exists():
        parser.error(f"video does not exist: {video_path}")
    if args.min_frames > args.max_frames:
        parser.error("--min-frames cannot be greater than --max-frames")
    if not 0 <= args.quality_quantile <= 0.9:
        parser.error("--quality-quantile must be between 0 and 0.9")
    if args.min_good_component_ratio > 1.0:
        parser.error("--min-good-component-ratio must be no greater than 1.0")
    if args.alignment_only and args.alignment_tournament == "off":
        parser.error("--alignment-only requires --alignment-tournament standard or thorough")

    paths = prepare_output_paths(video_path, args.output, args.overwrite)
    realityscan_exe = args.realityscan_exe or discover_realityscan()
    if realityscan_exe is not None:
        realityscan_exe = realityscan_exe.resolve()
    overview_template = overview_template_path(realityscan_exe)

    print(f"Output workspace: {paths.root}")
    print("Scoring candidate frames...")
    info, metrics = read_candidate_metrics(video_path, args.candidate_fps)
    selected = select_frames(
        metrics,
        target_fps=args.target_fps,
        max_frames=args.max_frames,
        min_frames=min(args.min_frames, len(metrics)),
        quality_quantile=args.quality_quantile,
        min_motion=args.min_motion,
        max_still_gap_s=args.max_still_gap_s,
    )
    print(f"Video: {info.width}x{info.height}, {info.frame_count} frames, {info.duration_s:.2f}s at {info.fps:.2f} fps")
    print(f"Selected {len(selected)} of {len(metrics)} scored candidates")

    auto_crop = estimate_dark_border_crop(video_path, args.max_auto_crop) if args.auto_crop_border else 0.0
    effective_crop = max(args.crop_fraction, auto_crop)
    args.effective_crop_fraction = effective_crop
    if effective_crop > 0:
        print(f"Cropping {effective_crop:.3f} from each edge (auto={auto_crop:.3f}, requested={args.crop_fraction:.3f})")

    variant_specs = build_variant_specs(args)
    variant_paths = {spec.name: make_variant_paths(paths, spec) for spec in variant_specs}
    variant_frame_paths: dict[str, list[Path]] = {}
    if len(variant_specs) == 1:
        print("Writing enhanced selected frames...")
    else:
        print(f"Writing {len(variant_specs)} autonomous frame variant(s)...")
    for spec in variant_specs:
        vp = variant_paths[spec.name]
        if len(variant_specs) > 1:
            print(
                f"  {spec.name}: geometry={spec.geometry_mode}, cv_mask={spec.cv_mask}, "
                f"ai_mask={spec.ai_mask}, distortion={spec.distortion_model or args.distortion_model}"
            )
        variant_frame_paths[spec.name] = write_variant_frames(
            video_path,
            selected,
            vp.frames,
            spec,
            crop_fraction=effective_crop,
            wb_gain=args.wb_gain,
            clahe_clip=args.clahe_clip,
            sharpen=args.sharpen,
            jpeg_quality=args.jpeg_quality,
            texture_layers=args.texture_layers,
        )

    write_metrics_csv(metrics, selected, paths)
    contact_name = "enhanced_brown4" if "enhanced_brown4" in variant_frame_paths else variant_specs[0].name
    make_contact_sheet(variant_frame_paths[contact_name], selected, paths.contact_sheet)
    if args.connectivity_report:
        diagnostic_name = variant_specs[0].name
        connectivity_path = paths.reports / f"connectivity_{diagnostic_name}.csv"
        print(f"Writing connectivity diagnostics: {connectivity_path}")
        write_connectivity_csv(variant_frame_paths[diagnostic_name], selected, connectivity_path)

    for spec in variant_specs:
        if args.alignment_tournament != "off":
            write_alignment_command_file(variant_paths[spec.name], spec, args, overview_template)

    provisional_spec = variant_specs[0]
    write_realityscan_command_file(
        paths,
        args,
        frames_dir=variant_paths[provisional_spec.name].frames,
        spec=provisional_spec,
        overview_template=overview_template,
    )
    write_manifest(
        paths,
        video_path,
        info,
        selected,
        variant_frame_paths[provisional_spec.name],
        realityscan_exe,
        args,
        variant_specs=variant_specs,
        variant_paths=variant_paths,
        variant_frame_paths=variant_frame_paths,
    )

    print(f"Frames: {variant_paths[provisional_spec.name].frames}")
    print(f"Command file: {paths.rscmd}")
    print(f"Contact sheet: {paths.contact_sheet}")
    if args.prepare_only:
        if realityscan_exe:
            print(f"RealityScan executable: {realityscan_exe}")
        else:
            print("RealityScan executable was not found; pass --realityscan-exe or set REALITYSCAN_EXE.")
        if overview_template and args.alignment_tournament != "off":
            print(f"RealityScan report template: {overview_template}")
        elif args.alignment_tournament != "off":
            print("RealityScan report template was not found; alignment scoring requires Overview.html.")
        print("Prepared only; RealityScan was not launched.")
        return 0

    if realityscan_exe is None or not realityscan_exe.exists():
        raise RuntimeError("RealityScan executable was not found. Pass --realityscan-exe or set REALITYSCAN_EXE.")

    alignment_results: list[AlignmentResult] = []
    best_alignment: AlignmentResult | None = None
    best_spec = provisional_spec
    if args.alignment_tournament != "off":
        if overview_template is None:
            raise RuntimeError("RealityScan Overview.html report template was not found; cannot score alignment tournament.")
        alignment_results = run_alignment_tournament(
            paths,
            variant_specs,
            variant_paths,
            len(selected),
            realityscan_exe,
            args,
        )
        if not alignment_results:
            raise RuntimeError("Alignment tournament produced no results.")
        best_alignment = max(alignment_results, key=lambda result: result.score)
        best_spec = next(spec for spec in variant_specs if spec.name == best_alignment.name)
        print(
            f"Best alignment: {best_alignment.name} with "
            f"{best_alignment.largest_component_images}/{best_alignment.selected_image_count} "
            f"images in the largest component ({best_alignment.largest_component_ratio:.1%})."
        )
        if best_alignment.largest_component_ratio < args.min_good_component_ratio:
            message = (
                f"Warning: best component ratio {best_alignment.largest_component_ratio:.1%} is below "
                f"the {args.min_good_component_ratio:.0%} target, so the output may still be fragmented."
            )
            print(message)
            if args.fail_on_poor_alignment:
                write_manifest(
                    paths,
                    video_path,
                    info,
                    selected,
                    variant_frame_paths[best_spec.name],
                    realityscan_exe,
                    args,
                    variant_specs=variant_specs,
                    variant_paths=variant_paths,
                    variant_frame_paths=variant_frame_paths,
                    alignment_results=alignment_results,
                    best_alignment=best_alignment,
                )
                return 3
        make_contact_sheet(variant_frame_paths[best_spec.name], selected, paths.contact_sheet)
        write_realityscan_command_file(
            paths,
            args,
            frames_dir=variant_paths[best_spec.name].frames,
            spec=best_spec,
            overview_template=overview_template,
        )
        write_manifest(
            paths,
            video_path,
            info,
            selected,
            variant_frame_paths[best_spec.name],
            realityscan_exe,
            args,
            variant_specs=variant_specs,
            variant_paths=variant_paths,
            variant_frame_paths=variant_frame_paths,
            alignment_results=alignment_results,
            best_alignment=best_alignment,
        )
        if args.alignment_only:
            print(f"Alignment-only run complete. Reports: {paths.reports}")
            print(f"Elapsed: {(time.perf_counter() - total_start) / 60.0:.1f} minutes")
            return 0

    code = run_realityscan(paths, realityscan_exe, args.timeout_hours)
    print(f"RealityScan exit code: {code}")
    if code != 0:
        return code
    if paths.model.exists():
        print(f"Model exported: {paths.model}")
    else:
        print(f"RealityScan completed but the expected model is not present yet: {paths.model}")
    print(f"Elapsed: {(time.perf_counter() - total_start) / 60.0:.1f} minutes")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
