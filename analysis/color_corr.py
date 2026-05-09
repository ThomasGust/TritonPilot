"""
Underwater Structure Color Corrector - PyQt6 GUI

Goal:
    Load an underwater robot video and export a corrected version where:
      - white PVC-like pipes are more visible/prominent,
      - red targets are boosted,
      - non-target background is optionally suppressed,
      - processing is consistent frame-to-frame for photogrammetry preprocessing.

Install:
    pip install PyQt6 opencv-python numpy

    Run:
    python -m analysis.color_corr

Notes:
    This first version intentionally avoids heavy AI enhancement, deblurring, or
    stabilization. The goal is repeatable feature-preserving enhancement, not
    making the video look maximally pretty.
"""

from __future__ import annotations

import os
import sys
import time
import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

import cv2
import numpy as np
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QTimer
from PyQt6.QtGui import QAction, QImage, QPixmap
from PyQt6.QtWidgets import (
    QApplication,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QCheckBox,
    QDoubleSpinBox,
    QProgressBar,
    QSlider,
    QSpinBox,
    QComboBox,
    QVBoxLayout,
    QWidget,
)

from gui.responsive import horizontal_scroll_area, resize_to_available_screen, vertical_scroll_area


@dataclass
class ProcessingSettings:
    white_balance_strength: float = 0.35
    red_restore_strength: float = 0.20
    clahe_strength: float = 0.25
    haze_reduction: float = 0.10
    denoise_strength: int = 0
    sharpen_strength: float = 0.10
    pvc_boost: float = 0.0
    red_target_boost: float = 0.0
    background_suppression: float = 0.0
    mask_blur: int = 21
    show_mode: str = "Corrected"
    draw_masks: bool = False


@dataclass
class FrameSelectionSettings:
    window_seconds: float = 0.25
    min_motion_px: float = 8.0
    force_every_seconds: float = 2.0
    output_format: str = "jpg"
    jpeg_quality: int = 95


@dataclass
class FrameCandidate:
    frame_index: int
    time_s: float
    frame: np.ndarray
    gray_small: np.ndarray
    score: float
    sharpness: float
    contrast: float
    feature_count: int
    clipped_fraction: float


class VideoProcessor:
    """Contains image-processing code independent of the GUI."""

    @staticmethod
    def gray_world_white_balance(bgr: np.ndarray, strength: float) -> np.ndarray:
        if strength <= 0:
            return bgr

        means = np.array(cv2.mean(bgr)[:3], dtype=np.float32)
        gray = float(means.mean())
        gains = gray / (means + 1e-6)
        gains = 1.0 + strength * (gains - 1.0)

        channels = cv2.split(bgr)
        balanced = [
            cv2.convertScaleAbs(channel, alpha=float(gain), beta=0.0)
            for channel, gain in zip(channels, gains)
        ]
        return cv2.merge(balanced)

    @staticmethod
    def restore_red_channel(bgr: np.ndarray, strength: float) -> np.ndarray:
        """
        Underwater footage often loses red/orange wavelengths.
        This gently restores red using green as a reference, while avoiding
        extreme color shifts.
        """
        if strength <= 0:
            return bgr

        b, g, r = cv2.split(bgr)
        missing_red = cv2.subtract(g, r)
        restored_r = cv2.addWeighted(r, 1.0, missing_red, 0.45 * strength, 0.0)
        return cv2.merge([b, g, restored_r])

    @staticmethod
    def clahe_luminance(bgr: np.ndarray, strength: float) -> np.ndarray:
        if strength <= 0:
            return bgr

        lab = cv2.cvtColor(bgr, cv2.COLOR_BGR2LAB)
        l, a, bb = cv2.split(lab)
        clip_limit = 1.0 + 3.0 * strength
        tile_size = 8
        clahe = cv2.createCLAHE(clipLimit=clip_limit, tileGridSize=(tile_size, tile_size))
        l2 = clahe.apply(l)
        l = cv2.addWeighted(l, 1.0 - strength, l2, strength, 0)
        lab2 = cv2.merge([l, a, bb])
        return cv2.cvtColor(lab2, cv2.COLOR_LAB2BGR)

    @staticmethod
    def reduce_haze_unsharp_luma(bgr: np.ndarray, strength: float) -> np.ndarray:
        """
        Mild local-contrast dehazing approximation.
        This is deliberately conservative to avoid hallucinated edges/halos.
        """
        if strength <= 0:
            return bgr

        lab = cv2.cvtColor(bgr, cv2.COLOR_BGR2LAB)
        l, a, bb = cv2.split(lab)
        sigma = 18
        blurred = cv2.GaussianBlur(l, (0, 0), sigmaX=sigma, sigmaY=sigma)
        amount = 0.55 * strength
        l2 = cv2.addWeighted(l, 1.0 + amount, blurred, -amount, 0)
        lab2 = cv2.merge([l2, a, bb])
        return cv2.cvtColor(lab2, cv2.COLOR_LAB2BGR)

    @staticmethod
    def denoise(bgr: np.ndarray, strength: int) -> np.ndarray:
        if strength <= 0:
            return bgr
        # h values above ~7 can smear useful photogrammetry texture.
        h = max(1, min(10, int(strength)))
        return cv2.fastNlMeansDenoisingColored(bgr, None, h, h, 7, 21)

    @staticmethod
    def sharpen(bgr: np.ndarray, strength: float) -> np.ndarray:
        if strength <= 0:
            return bgr
        blurred = cv2.GaussianBlur(bgr, (0, 0), sigmaX=1.2, sigmaY=1.2)
        return cv2.addWeighted(bgr, 1.0 + strength, blurred, -strength, 0)

    @staticmethod
    def metric_gray(bgr: np.ndarray, max_width: int = 640) -> np.ndarray:
        height, width = bgr.shape[:2]
        if width > max_width:
            scale = max_width / float(width)
            bgr = cv2.resize(
                bgr,
                (max_width, max(1, int(round(height * scale)))),
                interpolation=cv2.INTER_AREA,
            )
        return cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)

    @staticmethod
    def frame_quality(gray: np.ndarray) -> Tuple[float, float, float, int, float]:
        laplacian = cv2.Laplacian(gray, cv2.CV_32F)
        _, lap_stddev = cv2.meanStdDev(laplacian)
        sharpness = float(lap_stddev[0, 0] ** 2)

        _, gray_stddev = cv2.meanStdDev(gray)
        contrast = float(gray_stddev[0, 0])

        corners = cv2.goodFeaturesToTrack(
            gray,
            maxCorners=500,
            qualityLevel=0.01,
            minDistance=6,
            blockSize=5,
        )
        feature_count = 0 if corners is None else int(len(corners))

        dark = cv2.countNonZero(cv2.inRange(gray, 0, 4))
        bright = cv2.countNonZero(cv2.inRange(gray, 251, 255))
        clipped_fraction = float(dark + bright) / float(gray.size)

        score = (
            2.0 * np.log1p(sharpness)
            + 0.75 * np.log1p(feature_count)
            + contrast / 24.0
            - 16.0 * clipped_fraction
        )
        return float(score), sharpness, contrast, feature_count, clipped_fraction

    @staticmethod
    def estimate_motion_px(previous_gray: np.ndarray, current_gray: np.ndarray) -> Optional[float]:
        points = cv2.goodFeaturesToTrack(
            previous_gray,
            maxCorners=300,
            qualityLevel=0.01,
            minDistance=8,
            blockSize=5,
        )
        if points is None or len(points) < 12:
            return None

        next_points, status, _err = cv2.calcOpticalFlowPyrLK(
            previous_gray,
            current_gray,
            points,
            None,
            winSize=(21, 21),
            maxLevel=3,
            criteria=(cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 20, 0.03),
        )
        if next_points is None or status is None:
            return None

        valid = status.reshape(-1) == 1
        if int(np.count_nonzero(valid)) < 12:
            return None

        previous = points.reshape(-1, 2)[valid]
        current = next_points.reshape(-1, 2)[valid]
        displacement = np.linalg.norm(current - previous, axis=1)
        return float(np.median(displacement))

    @staticmethod
    def detection_source(bgr: np.ndarray) -> np.ndarray:
        """
        Create a stable, gentle image for diagnostic masks.

        Masks should not be driven by the aggressive review correction, because
        that can turn sand/water into white PVC or orange targets.
        """
        detection = VideoProcessor.gray_world_white_balance(bgr, 0.25)
        detection = VideoProcessor.restore_red_channel(detection, 0.35)
        return detection

    @staticmethod
    def create_pvc_mask(bgr: np.ndarray) -> np.ndarray:
        """
        Detect likely white/PVC regions for preview diagnostics.

        White PVC is not just "bright and desaturated" here; the pool floor and
        water haze often match that. We therefore require neutral color plus
        nearby image edges, which tracks pipe/tube structures much better than a
        broad HSV white threshold.
        """
        hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
        _, s, v = cv2.split(hsv)
        lab = cv2.cvtColor(bgr, cv2.COLOR_BGR2LAB)
        _, a, bb = cv2.split(lab)
        gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)

        median = float(np.median(gray))
        lower = int(max(12, min(55, 0.55 * median)))
        upper = int(max(lower + 25, min(145, 1.55 * median)))
        edges = cv2.Canny(gray, lower, upper)

        height, width = gray.shape[:2]
        edge_kernel_size = max(7, (min(height, width) // 90) | 1)
        edge_kernel = cv2.getStructuringElement(
            cv2.MORPH_ELLIPSE,
            (edge_kernel_size, edge_kernel_size),
        )
        edge_support = cv2.dilate(edges, edge_kernel, iterations=1)

        low_saturation = s <= 65
        neutral_lab = (
            np.abs(a.astype(np.int16) - 128) <= 20
        ) & (
            np.abs(bb.astype(np.int16) - 128) <= 24
        )
        bright_enough = v >= 85

        mask = np.where(
            low_saturation & neutral_lab & bright_enough & (edge_support > 0),
            255,
            0,
        ).astype(np.uint8)

        close_kernel_size = max(5, (min(height, width) // 120) | 1)
        close_kernel = cv2.getStructuringElement(
            cv2.MORPH_ELLIPSE,
            (close_kernel_size, close_kernel_size),
        )
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, close_kernel, iterations=1)
        mask = cv2.dilate(mask, close_kernel, iterations=1)
        mask = VideoProcessor.clean_mask(mask, min_area=80, max_area_fraction=0.18)
        return mask

    @staticmethod
    def create_red_mask(bgr: np.ndarray) -> np.ndarray:
        """
        Detect likely red/magenta target regions for preview diagnostics.

        Underwater red targets often become dull magenta/brown, while sand can
        become orange after correction. Combining LAB red-green chroma with RGB
        opponent checks is more stable than a broad HSV orange range.
        """
        hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
        h, s, v = cv2.split(hsv)
        lab = cv2.cvtColor(bgr, cv2.COLOR_BGR2LAB)
        a = lab[:, :, 1]
        b_chan, g_chan, r_chan = cv2.split(bgr.astype(np.float32))

        chroma_sum = r_chan + g_chan + b_chan + 1.0
        red_chroma = ((r_chan - g_chan) + 0.5 * (r_chan - b_chan)) / chroma_sum
        warm_hue = ((h <= 18) | (h >= 150)) & (s >= 28) & (v >= 35)
        lab_red = (a >= 136) & (red_chroma >= 0.012) & (s >= 18) & (v >= 35)
        opponent_red = (r_chan >= g_chan + 7) & (r_chan >= 0.84 * b_chan + 4) & (s >= 18)

        mask = np.where(lab_red | (warm_hue & opponent_red), 255, 0).astype(np.uint8)
        mask = VideoProcessor.clean_mask(
            mask,
            min_area=40,
            max_area_fraction=0.025,
            reject_border=True,
        )
        return mask

    @staticmethod
    def clean_mask(
        mask: np.ndarray,
        min_area: int,
        max_area_fraction: Optional[float] = None,
        reject_border: bool = False,
    ) -> np.ndarray:
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel, iterations=1)
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel, iterations=2)

        num_labels, labels, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
        cleaned = np.zeros_like(mask)
        height, width = mask.shape[:2]
        max_area = None
        if max_area_fraction is not None:
            max_area = int(height * width * max_area_fraction)
        for label in range(1, num_labels):
            x = stats[label, cv2.CC_STAT_LEFT]
            y = stats[label, cv2.CC_STAT_TOP]
            w = stats[label, cv2.CC_STAT_WIDTH]
            h = stats[label, cv2.CC_STAT_HEIGHT]
            area = stats[label, cv2.CC_STAT_AREA]
            touches_border = x <= 0 or y <= 0 or x + w >= width or y + h >= height
            if area < min_area:
                continue
            if max_area is not None and area > max_area:
                continue
            if reject_border and touches_border:
                continue
            cleaned[labels == label] = 255
        return cleaned

    @staticmethod
    def boost_targets(
        bgr: np.ndarray,
        pvc_mask: np.ndarray,
        red_mask: np.ndarray,
        pvc_boost: float,
        red_boost: float,
        bg_suppression: float,
        mask_blur: int,
    ) -> np.ndarray:
        img = bgr.astype(np.float32)

        # Boost PVC regions: brighter, slightly less saturated, more contrast.
        if pvc_boost > 0:
            pvc = VideoProcessor.mask_to_float(pvc_mask, mask_blur)
            hsv = cv2.cvtColor(np.clip(img, 0, 255).astype(np.uint8), cv2.COLOR_BGR2HSV).astype(np.float32)
            hsv[:, :, 1] *= 1.0 - 0.25 * pvc_boost * pvc
            hsv[:, :, 2] *= 1.0 + 0.45 * pvc_boost * pvc
            pvc_boosted = cv2.cvtColor(np.clip(hsv, 0, 255).astype(np.uint8), cv2.COLOR_HSV2BGR).astype(np.float32)
            img = img * (1.0 - pvc[..., None]) + pvc_boosted * pvc[..., None]

        # Boost red/orange target regions: raise red channel and saturation/value.
        if red_boost > 0:
            red = VideoProcessor.mask_to_float(red_mask, mask_blur)
            hsv = cv2.cvtColor(np.clip(img, 0, 255).astype(np.uint8), cv2.COLOR_BGR2HSV).astype(np.float32)
            hsv[:, :, 1] *= 1.0 + 0.65 * red_boost * red
            hsv[:, :, 2] *= 1.0 + 0.35 * red_boost * red
            red_boosted = cv2.cvtColor(np.clip(hsv, 0, 255).astype(np.uint8), cv2.COLOR_HSV2BGR).astype(np.float32)
            red_boosted[:, :, 2] *= 1.0 + 0.35 * red_boost * red
            img = img * (1.0 - red[..., None]) + red_boosted * red[..., None]

        # Suppress background outside PVC/red target masks.
        if bg_suppression > 0:
            target = cv2.bitwise_or(pvc_mask, red_mask)
            target_float = VideoProcessor.mask_to_float(target, max(mask_blur, 11))
            bg = 1.0 - target_float

            gray = cv2.cvtColor(np.clip(img, 0, 255).astype(np.uint8), cv2.COLOR_BGR2GRAY).astype(np.float32)
            gray_bgr = np.repeat(gray[:, :, None], 3, axis=2)

            # Dim and desaturate non-target areas, but keep enough context for review.
            subdued = img * (1.0 - bg_suppression * 0.35)
            subdued = subdued * (1.0 - bg_suppression * 0.45) + gray_bgr * (bg_suppression * 0.45)
            img = img * (1.0 - bg[..., None] * bg_suppression) + subdued * (bg[..., None] * bg_suppression)

        return np.clip(img, 0, 255).astype(np.uint8)

    @staticmethod
    def mask_to_float(mask: np.ndarray, blur: int) -> np.ndarray:
        blur = max(1, int(blur))
        if blur % 2 == 0:
            blur += 1
        m = mask.astype(np.float32) / 255.0
        if blur > 1:
            m = cv2.GaussianBlur(m, (blur, blur), 0)
        return np.clip(m, 0.0, 1.0)

    @staticmethod
    def draw_mask_overlay(bgr: np.ndarray, pvc_mask: np.ndarray, red_mask: np.ndarray) -> np.ndarray:
        overlay = bgr.copy()
        # OpenCV BGR: PVC cyan-ish, targets red-ish.
        overlay[pvc_mask > 0] = (255, 255, 0)
        overlay[red_mask > 0] = (0, 0, 255)
        return cv2.addWeighted(bgr, 0.65, overlay, 0.35, 0)

    @staticmethod
    def needs_masks(settings: ProcessingSettings) -> bool:
        mask_preview_modes = {"PVC mask", "Red mask", "Combined mask"}
        return (
            settings.draw_masks
            or settings.show_mode in mask_preview_modes
            or settings.pvc_boost > 0.0
            or settings.red_target_boost > 0.0
            or settings.background_suppression > 0.0
        )

    @staticmethod
    def process_frame(bgr: np.ndarray, settings: ProcessingSettings) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
        pvc_mask = np.zeros(bgr.shape[:2], dtype=np.uint8)
        red_mask = np.zeros_like(pvc_mask)
        if VideoProcessor.needs_masks(settings):
            mask_source = VideoProcessor.detection_source(bgr)
            pvc_mask = VideoProcessor.create_pvc_mask(mask_source)
            red_mask = VideoProcessor.create_red_mask(mask_source)

        corrected = bgr.copy()
        corrected = VideoProcessor.gray_world_white_balance(corrected, settings.white_balance_strength)
        corrected = VideoProcessor.restore_red_channel(corrected, settings.red_restore_strength)
        corrected = VideoProcessor.clahe_luminance(corrected, settings.clahe_strength)
        corrected = VideoProcessor.reduce_haze_unsharp_luma(corrected, settings.haze_reduction)
        corrected = VideoProcessor.denoise(corrected, settings.denoise_strength)
        corrected = VideoProcessor.sharpen(corrected, settings.sharpen_strength)

        if settings.pvc_boost > 0.0 or settings.red_target_boost > 0.0 or settings.background_suppression > 0.0:
            corrected = VideoProcessor.boost_targets(
                corrected,
                pvc_mask,
                red_mask,
                settings.pvc_boost,
                settings.red_target_boost,
                settings.background_suppression,
                settings.mask_blur,
            )

        if settings.draw_masks:
            corrected = VideoProcessor.draw_mask_overlay(corrected, pvc_mask, red_mask)

        return corrected, pvc_mask, red_mask


class ExportWorker(QThread):
    progress = pyqtSignal(int)
    status = pyqtSignal(str)
    finished_ok = pyqtSignal(str)
    failed = pyqtSignal(str)

    def __init__(self, input_path: str, output_path: str, settings: ProcessingSettings):
        super().__init__()
        self.input_path = input_path
        self.output_path = output_path
        self.settings = settings
        self._cancel = False

    def cancel(self):
        self._cancel = True

    @staticmethod
    def format_duration(seconds: float) -> str:
        seconds = max(0, int(round(seconds)))
        minutes, secs = divmod(seconds, 60)
        if minutes:
            return f"{minutes}m {secs:02d}s"
        return f"{secs}s"

    def run(self):
        cap = cv2.VideoCapture(self.input_path)
        if not cap.isOpened():
            self.failed.emit("Could not open input video.")
            return

        fps = cap.get(cv2.CAP_PROP_FPS)
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))

        if fps <= 0 or width <= 0 or height <= 0:
            self.failed.emit("Input video metadata is invalid.")
            cap.release()
            return

        # mp4v is broadly available. For archival preprocessing, consider image sequences later.
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        writer = cv2.VideoWriter(self.output_path, fourcc, fps, (width, height), True)
        if not writer.isOpened():
            self.failed.emit("Could not create output video. Try a different path or filename ending in .mp4.")
            cap.release()
            return

        idx = 0
        start_time = time.perf_counter()
        try:
            while True:
                if self._cancel:
                    self.status.emit("Export canceled.")
                    break

                ok, frame = cap.read()
                if not ok:
                    break

                corrected, _, _ = VideoProcessor.process_frame(frame, self.settings)
                writer.write(corrected)

                idx += 1
                if idx % 10 == 0 or idx == total:
                    pct = int(100 * idx / max(1, total))
                    elapsed = max(1e-6, time.perf_counter() - start_time)
                    rate = idx / elapsed
                    remaining = max(0, total - idx)
                    eta = self.format_duration(remaining / rate) if rate > 0 and total > 0 else "--"
                    self.progress.emit(pct)
                    self.status.emit(f"Exporting frame {idx}/{total} | {rate:.1f} fps | ETA {eta}")

            writer.release()
            cap.release()

            if self._cancel:
                try:
                    os.remove(self.output_path)
                except OSError:
                    pass
                self.failed.emit("Export canceled.")
            else:
                self.progress.emit(100)
                self.finished_ok.emit(self.output_path)
        except Exception as exc:  # noqa: BLE001 - GUI should report unexpected failures.
            writer.release()
            cap.release()
            self.failed.emit(str(exc))


class FixedIntervalFrameExportWorker(QThread):
    progress = pyqtSignal(int)
    status = pyqtSignal(str)
    finished_ok = pyqtSignal(str)
    failed = pyqtSignal(str)

    def __init__(
        self,
        input_path: str,
        output_dir: str,
        settings: ProcessingSettings,
        interval_seconds: float = 0.1,
        output_format: str = "jpg",
        jpeg_quality: int = 95,
    ):
        super().__init__()
        self.input_path = input_path
        self.output_dir = Path(output_dir)
        self.settings = settings
        self.interval_seconds = interval_seconds
        self.output_format = output_format
        self.jpeg_quality = jpeg_quality
        self._cancel = False

    def cancel(self):
        self._cancel = True

    @staticmethod
    def target_frame_indices(fps: float, total_frames: int, interval_seconds: float) -> list[int]:
        if fps <= 0 or total_frames <= 0:
            return []

        interval_seconds = max(1e-6, float(interval_seconds))
        indices: list[int] = []
        target_number = 0
        last_index = -1
        while True:
            frame_index = int(target_number * interval_seconds * fps + 0.5)
            if frame_index >= total_frames:
                break
            if frame_index > last_index:
                indices.append(frame_index)
                last_index = frame_index
            target_number += 1
        return indices

    def _write_frame(self, frame: np.ndarray, written_index: int, source_frame_index: int, fps: float) -> None:
        corrected, _, _ = VideoProcessor.process_frame(frame, self.settings)

        ext = self.output_format.lower()
        if ext == "jpeg":
            ext = "jpg"
        filename = f"frame_{written_index:05d}_src{source_frame_index:06d}_t{source_frame_index / fps:08.3f}.{ext}"
        out_path = self.output_dir / filename

        if ext == "png":
            params = [int(cv2.IMWRITE_PNG_COMPRESSION), 1]
        else:
            params = [int(cv2.IMWRITE_JPEG_QUALITY), int(self.jpeg_quality)]

        if not cv2.imwrite(str(out_path), corrected, params):
            raise RuntimeError(f"Could not write frame: {out_path}")

    def run(self):
        cap = cv2.VideoCapture(self.input_path)
        if not cap.isOpened():
            self.failed.emit("Could not open input video.")
            return

        fps = cap.get(cv2.CAP_PROP_FPS)
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        if fps <= 0:
            self.failed.emit("Input video FPS metadata is invalid.")
            cap.release()
            return

        target_indices = self.target_frame_indices(fps, total, self.interval_seconds)
        if total > 0 and not target_indices:
            self.failed.emit("Input video contains no frames to export.")
            cap.release()
            return

        self.output_dir.mkdir(parents=True, exist_ok=True)

        idx = 0
        written = 0
        target_pos = 0
        start_time = time.perf_counter()

        try:
            while True:
                if self._cancel:
                    self.status.emit("Frame export canceled.")
                    break

                ok, frame = cap.read()
                if not ok:
                    break

                should_write = False
                if target_indices:
                    should_write = target_pos < len(target_indices) and idx == target_indices[target_pos]
                else:
                    target_index = int(target_pos * self.interval_seconds * fps + 0.5)
                    should_write = idx >= target_index

                if should_write:
                    written += 1
                    self._write_frame(frame, written, idx, fps)
                    target_pos += 1

                    if target_indices and target_pos >= len(target_indices):
                        idx += 1
                        break

                idx += 1
                if idx % 30 == 0 or (target_indices and target_pos >= len(target_indices)):
                    pct = int(100 * idx / max(1, total)) if total > 0 else 0
                    elapsed = max(1e-6, time.perf_counter() - start_time)
                    rate = idx / elapsed
                    eta = ExportWorker.format_duration((max(0, total - idx) / rate)) if rate > 0 and total > 0 else "--"
                    self.progress.emit(pct)
                    self.status.emit(
                        f"Exporting 0.1s frames: {written} saved | source frame {idx}/{total} | ETA {eta}"
                    )

            cap.release()

            if self._cancel:
                self.failed.emit(f"Frame export canceled. Partial frames kept in:\n{self.output_dir}")
            else:
                self.progress.emit(100)
                self.finished_ok.emit(f"{self.output_dir} ({written} frames)")
        except Exception as exc:  # noqa: BLE001 - GUI should report unexpected failures.
            cap.release()
            self.failed.emit(str(exc))


class FrameExportWorker(QThread):
    progress = pyqtSignal(int)
    status = pyqtSignal(str)
    finished_ok = pyqtSignal(str)
    failed = pyqtSignal(str)

    def __init__(
        self,
        input_path: str,
        output_dir: str,
        processing_settings: ProcessingSettings,
        selection_settings: FrameSelectionSettings,
    ):
        super().__init__()
        self.input_path = input_path
        self.output_dir = Path(output_dir)
        self.processing_settings = processing_settings
        self.selection_settings = selection_settings
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def _candidate_from_frame(self, frame: np.ndarray, frame_index: int, fps: float) -> FrameCandidate:
        gray = VideoProcessor.metric_gray(frame)
        score, sharpness, contrast, feature_count, clipped_fraction = VideoProcessor.frame_quality(gray)
        return FrameCandidate(
            frame_index=frame_index,
            time_s=frame_index / max(fps, 1e-6),
            frame=frame.copy(),
            gray_small=gray,
            score=score,
            sharpness=sharpness,
            contrast=contrast,
            feature_count=feature_count,
            clipped_fraction=clipped_fraction,
        )

    def _write_candidate(
        self,
        candidate: FrameCandidate,
        selected_index: int,
        motion_px: Optional[float],
        manifest_writer: csv.DictWriter,
    ) -> None:
        corrected, _, _ = VideoProcessor.process_frame(candidate.frame, self.processing_settings)

        ext = self.selection_settings.output_format.lower()
        if ext == "jpeg":
            ext = "jpg"
        filename = f"selected_{selected_index:05d}_src{candidate.frame_index:06d}_t{candidate.time_s:08.3f}.{ext}"
        out_path = self.output_dir / filename

        if ext == "png":
            params = [int(cv2.IMWRITE_PNG_COMPRESSION), 1]
        else:
            params = [int(cv2.IMWRITE_JPEG_QUALITY), int(self.selection_settings.jpeg_quality)]

        if not cv2.imwrite(str(out_path), corrected, params):
            raise RuntimeError(f"Could not write frame: {out_path}")

        manifest_writer.writerow(
            {
                "file": filename,
                "source_frame": candidate.frame_index,
                "time_s": f"{candidate.time_s:.3f}",
                "score": f"{candidate.score:.4f}",
                "sharpness": f"{candidate.sharpness:.4f}",
                "contrast": f"{candidate.contrast:.4f}",
                "feature_count": candidate.feature_count,
                "clipped_fraction": f"{candidate.clipped_fraction:.6f}",
                "motion_px": "" if motion_px is None else f"{motion_px:.3f}",
            }
        )

    def run(self):
        cap = cv2.VideoCapture(self.input_path)
        if not cap.isOpened():
            self.failed.emit("Could not open input video.")
            return

        fps = cap.get(cv2.CAP_PROP_FPS)
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        if fps <= 0:
            self.failed.emit("Input video FPS metadata is invalid.")
            cap.release()
            return

        self.output_dir.mkdir(parents=True, exist_ok=True)
        manifest_path = self.output_dir / "selection_manifest.csv"

        interval_s = max(1.0 / fps, float(self.selection_settings.window_seconds))
        min_motion_px = max(0.0, float(self.selection_settings.min_motion_px))
        force_every_s = max(0.0, float(self.selection_settings.force_every_seconds))

        idx = 0
        selected = 0
        best_candidate: Optional[FrameCandidate] = None
        current_window: Optional[int] = None
        last_selected_gray: Optional[np.ndarray] = None
        last_selected_time: Optional[float] = None
        start_time = time.perf_counter()

        def maybe_write(candidate: FrameCandidate, manifest_writer: csv.DictWriter) -> None:
            nonlocal selected, last_selected_gray, last_selected_time

            motion_px: Optional[float] = None
            if last_selected_gray is not None and min_motion_px > 0:
                motion_px = VideoProcessor.estimate_motion_px(last_selected_gray, candidate.gray_small)
                force_due = (
                    force_every_s > 0
                    and last_selected_time is not None
                    and candidate.time_s - last_selected_time >= force_every_s
                )
                if motion_px is not None and motion_px < min_motion_px and not force_due:
                    return

            selected += 1
            self._write_candidate(candidate, selected, motion_px, manifest_writer)
            last_selected_gray = candidate.gray_small
            last_selected_time = candidate.time_s

        try:
            with manifest_path.open("w", newline="", encoding="utf-8") as manifest_file:
                manifest_writer = csv.DictWriter(
                    manifest_file,
                    fieldnames=[
                        "file",
                        "source_frame",
                        "time_s",
                        "score",
                        "sharpness",
                        "contrast",
                        "feature_count",
                        "clipped_fraction",
                        "motion_px",
                    ],
                )
                manifest_writer.writeheader()

                while True:
                    if self._cancel:
                        self.status.emit("Frame export canceled.")
                        break

                    ok, frame = cap.read()
                    if not ok:
                        break

                    window = int((idx / fps) / interval_s)
                    if current_window is None:
                        current_window = window
                    elif window != current_window:
                        if best_candidate is not None:
                            maybe_write(best_candidate, manifest_writer)
                        best_candidate = None
                        current_window = window

                    candidate = self._candidate_from_frame(frame, idx, fps)
                    if best_candidate is None or candidate.score > best_candidate.score:
                        best_candidate = candidate

                    idx += 1
                    if idx % 30 == 0 or idx == total:
                        pct = int(100 * idx / max(1, total))
                        elapsed = max(1e-6, time.perf_counter() - start_time)
                        rate = idx / elapsed
                        eta = ExportWorker.format_duration((max(0, total - idx) / rate)) if total > 0 else "--"
                        self.progress.emit(pct)
                        self.status.emit(
                            f"Scanning frame {idx}/{total} | selected {selected} | {rate:.1f} fps | ETA {eta}"
                        )

                if not self._cancel and best_candidate is not None:
                    maybe_write(best_candidate, manifest_writer)

            cap.release()

            if self._cancel:
                self.failed.emit(f"Frame export canceled. Partial frames kept in:\n{self.output_dir}")
            else:
                self.progress.emit(100)
                self.finished_ok.emit(f"{self.output_dir} ({selected} frames)")
        except Exception as exc:  # noqa: BLE001 - GUI should report unexpected failures.
            cap.release()
            self.failed.emit(str(exc))


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Underwater PVC / Red Target Color Corrector")
        resize_to_available_screen(self, 1280, 820, min_width=900, min_height=620)

        self.video_path: Optional[str] = None
        self.cap: Optional[cv2.VideoCapture] = None
        self.current_frame_index = 0
        self.total_frames = 0
        self.fps = 30.0
        self.current_raw_frame: Optional[np.ndarray] = None
        self.current_corrected_frame: Optional[np.ndarray] = None
        self.current_pvc_mask: Optional[np.ndarray] = None
        self.current_red_mask: Optional[np.ndarray] = None
        self.export_worker: Optional[ExportWorker] = None
        self.fixed_frame_export_worker: Optional[FixedIntervalFrameExportWorker] = None
        self.frame_export_worker: Optional[FrameExportWorker] = None

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.next_frame)

        self._build_ui()
        self._connect_actions()
        resize_to_available_screen(self, 1280, 820, min_width=900, min_height=620)
        self.update_status("Open a video to begin.")

    def _build_ui(self):
        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)

        top_bar = QHBoxLayout()
        self.open_button = QPushButton("Open Video")
        self.export_button = QPushButton("Export Corrected Video")
        self.export_button.setEnabled(False)
        self.export_fixed_frames_button = QPushButton("Export 0.1s Frames")
        self.export_fixed_frames_button.setEnabled(False)
        self.export_frames_button = QPushButton("Export Selected Frames")
        self.export_frames_button.setEnabled(False)
        self.cancel_button = QPushButton("Cancel Export")
        self.cancel_button.setEnabled(False)
        top_bar.addWidget(self.open_button)
        top_bar.addWidget(self.export_button)
        top_bar.addWidget(self.export_fixed_frames_button)
        top_bar.addWidget(self.export_frames_button)
        top_bar.addWidget(self.cancel_button)
        top_bar.addStretch(1)
        layout.addWidget(horizontal_scroll_area(top_bar))

        main = QHBoxLayout()
        layout.addLayout(main, stretch=1)

        preview_col = QVBoxLayout()
        main.addLayout(preview_col, stretch=4)

        self.preview_label = QLabel("No video loaded")
        self.preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_label.setMinimumSize(520, 320)
        self.preview_label.setStyleSheet("QLabel { background: #111; color: #ddd; border: 1px solid #333; }")
        preview_col.addWidget(self.preview_label, stretch=1)

        timeline = QHBoxLayout()
        self.play_button = QPushButton("Play")
        self.play_button.setEnabled(False)
        self.frame_slider = QSlider(Qt.Orientation.Horizontal)
        self.frame_slider.setEnabled(False)
        self.frame_label = QLabel("Frame: -")
        timeline.addWidget(self.play_button)
        timeline.addWidget(self.frame_slider, stretch=1)
        timeline.addWidget(self.frame_label)
        preview_col.addWidget(horizontal_scroll_area(timeline))

        side_panel = QWidget()
        side = QVBoxLayout(side_panel)
        main.addWidget(vertical_scroll_area(side_panel), stretch=1)

        side.addWidget(self._make_correction_group())
        side.addWidget(self._make_target_group())
        side.addWidget(self._make_frame_selection_group())
        side.addWidget(self._make_preview_group())
        side.addStretch(1)

        bottom = QHBoxLayout()
        self.progress_bar = QProgressBar()
        self.progress_bar.setValue(0)
        self.status_label = QLabel("Ready")
        bottom.addWidget(self.progress_bar, stretch=1)
        bottom.addWidget(self.status_label, stretch=2)
        layout.addLayout(bottom)

        file_menu = self.menuBar().addMenu("File")
        open_action = QAction("Open Video", self)
        export_action = QAction("Export Corrected Video", self)
        export_fixed_frames_action = QAction("Export 0.1s Frames", self)
        export_frames_action = QAction("Export Selected Frames", self)
        file_menu.addAction(open_action)
        file_menu.addAction(export_action)
        file_menu.addAction(export_fixed_frames_action)
        file_menu.addAction(export_frames_action)
        open_action.triggered.connect(self.open_video)
        export_action.triggered.connect(self.export_video)
        export_fixed_frames_action.triggered.connect(self.export_fixed_interval_frames)
        export_frames_action.triggered.connect(self.export_selected_frames)

    def _make_correction_group(self) -> QGroupBox:
        group = QGroupBox("Underwater Color Correction")
        grid = QGridLayout(group)

        defaults = ProcessingSettings()
        self.wb_slider = self.add_slider(grid, 0, "White balance", 0, 100, int(defaults.white_balance_strength * 100))
        self.red_restore_slider = self.add_slider(
            grid,
            1,
            "Restore red channel",
            0,
            100,
            int(defaults.red_restore_strength * 100),
        )
        self.clahe_slider = self.add_slider(grid, 2, "Local contrast / CLAHE", 0, 100, int(defaults.clahe_strength * 100))
        self.haze_slider = self.add_slider(grid, 3, "Mild haze reduction", 0, 100, int(defaults.haze_reduction * 100))
        self.denoise_slider = self.add_slider(grid, 4, "Denoise", 0, 10, defaults.denoise_strength)
        self.sharpen_slider = self.add_slider(grid, 5, "Sharpen", 0, 100, int(defaults.sharpen_strength * 100))
        return group

    def _make_target_group(self) -> QGroupBox:
        group = QGroupBox("Diagnostic PVC / Red Target Emphasis")
        grid = QGridLayout(group)
        defaults = ProcessingSettings()

        self.pvc_slider = self.add_slider(grid, 0, "White PVC boost", 0, 100, int(defaults.pvc_boost * 100))
        self.red_target_slider = self.add_slider(grid, 1, "Red target boost", 0, 100, int(defaults.red_target_boost * 100))
        self.bg_slider = self.add_slider(grid, 2, "Background suppression", 0, 100, int(defaults.background_suppression * 100))

        grid.addWidget(QLabel("Mask blur"), 3, 0)
        self.mask_blur_spin = QSpinBox()
        self.mask_blur_spin.setRange(1, 51)
        self.mask_blur_spin.setSingleStep(2)
        self.mask_blur_spin.setValue(defaults.mask_blur)
        grid.addWidget(self.mask_blur_spin, 3, 1)
        return group

    def _make_frame_selection_group(self) -> QGroupBox:
        group = QGroupBox("Selected Frame Export")
        grid = QGridLayout(group)
        defaults = FrameSelectionSettings()

        grid.addWidget(QLabel("Best-frame window (s)"), 0, 0)
        self.frame_window_spin = QDoubleSpinBox()
        self.frame_window_spin.setRange(0.05, 5.0)
        self.frame_window_spin.setDecimals(2)
        self.frame_window_spin.setSingleStep(0.05)
        self.frame_window_spin.setValue(defaults.window_seconds)
        grid.addWidget(self.frame_window_spin, 0, 1)

        grid.addWidget(QLabel("Min viewpoint motion"), 1, 0)
        self.min_motion_spin = QDoubleSpinBox()
        self.min_motion_spin.setRange(0.0, 80.0)
        self.min_motion_spin.setDecimals(1)
        self.min_motion_spin.setSingleStep(1.0)
        self.min_motion_spin.setValue(defaults.min_motion_px)
        grid.addWidget(self.min_motion_spin, 1, 1)

        grid.addWidget(QLabel("Force at least every (s)"), 2, 0)
        self.force_frame_spin = QDoubleSpinBox()
        self.force_frame_spin.setRange(0.0, 20.0)
        self.force_frame_spin.setDecimals(1)
        self.force_frame_spin.setSingleStep(0.5)
        self.force_frame_spin.setValue(defaults.force_every_seconds)
        grid.addWidget(self.force_frame_spin, 2, 1)

        grid.addWidget(QLabel("Format"), 3, 0)
        self.frame_format_combo = QComboBox()
        self.frame_format_combo.addItems(["jpg", "png"])
        self.frame_format_combo.setCurrentText(defaults.output_format)
        grid.addWidget(self.frame_format_combo, 3, 1)

        grid.addWidget(QLabel("JPEG quality"), 4, 0)
        self.jpeg_quality_spin = QSpinBox()
        self.jpeg_quality_spin.setRange(80, 100)
        self.jpeg_quality_spin.setValue(defaults.jpeg_quality)
        grid.addWidget(self.jpeg_quality_spin, 4, 1)
        return group

    def _make_preview_group(self) -> QGroupBox:
        group = QGroupBox("Preview")
        layout = QVBoxLayout(group)

        self.mode_combo = QComboBox()
        self.mode_combo.addItems(["Corrected", "Original", "Side-by-side", "PVC mask", "Red mask", "Combined mask"])
        layout.addWidget(self.mode_combo)

        self.draw_masks_checkbox = QCheckBox("Overlay masks on corrected preview only")
        self.draw_masks_checkbox.setChecked(False)
        layout.addWidget(self.draw_masks_checkbox)

        hint = QLabel(
            "Tip: For photogrammetry, leave target boosts and background suppression at 0. Use masks as diagnostics."
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)
        return group

    @staticmethod
    def add_slider(grid: QGridLayout, row: int, name: str, lo: int, hi: int, value: int) -> QSlider:
        label = QLabel(name)
        slider = QSlider(Qt.Orientation.Horizontal)
        slider.setRange(lo, hi)
        slider.setValue(value)
        value_label = QLabel(str(value))
        slider.valueChanged.connect(lambda v, lab=value_label: lab.setText(str(v)))
        grid.addWidget(label, row, 0)
        grid.addWidget(slider, row, 1)
        grid.addWidget(value_label, row, 2)
        return slider

    def _connect_actions(self):
        self.open_button.clicked.connect(self.open_video)
        self.export_button.clicked.connect(self.export_video)
        self.export_fixed_frames_button.clicked.connect(self.export_fixed_interval_frames)
        self.export_frames_button.clicked.connect(self.export_selected_frames)
        self.cancel_button.clicked.connect(self.cancel_export)
        self.play_button.clicked.connect(self.toggle_playback)
        self.frame_slider.sliderReleased.connect(self.seek_from_slider)

        controls = [
            self.wb_slider,
            self.red_restore_slider,
            self.clahe_slider,
            self.haze_slider,
            self.denoise_slider,
            self.sharpen_slider,
            self.pvc_slider,
            self.red_target_slider,
            self.bg_slider,
        ]
        for slider in controls:
            slider.valueChanged.connect(self.refresh_current_frame)

        self.mask_blur_spin.valueChanged.connect(self.refresh_current_frame)
        self.mode_combo.currentTextChanged.connect(self.refresh_current_frame)
        self.draw_masks_checkbox.stateChanged.connect(self.refresh_current_frame)

    def settings(self, include_preview_overlay: bool = True) -> ProcessingSettings:
        return ProcessingSettings(
            white_balance_strength=self.wb_slider.value() / 100.0,
            red_restore_strength=self.red_restore_slider.value() / 100.0,
            clahe_strength=self.clahe_slider.value() / 100.0,
            haze_reduction=self.haze_slider.value() / 100.0,
            denoise_strength=self.denoise_slider.value(),
            sharpen_strength=self.sharpen_slider.value() / 100.0,
            pvc_boost=self.pvc_slider.value() / 100.0,
            red_target_boost=self.red_target_slider.value() / 100.0,
            background_suppression=self.bg_slider.value() / 100.0,
            mask_blur=self.mask_blur_spin.value(),
            show_mode=self.mode_combo.currentText() if include_preview_overlay else "Corrected",
            draw_masks=include_preview_overlay and self.draw_masks_checkbox.isChecked(),
        )

    def frame_selection_settings(self) -> FrameSelectionSettings:
        return FrameSelectionSettings(
            window_seconds=self.frame_window_spin.value(),
            min_motion_px=self.min_motion_spin.value(),
            force_every_seconds=self.force_frame_spin.value(),
            output_format=self.frame_format_combo.currentText(),
            jpeg_quality=self.jpeg_quality_spin.value(),
        )

    def open_video(self):
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Open underwater video",
            "",
            "Video Files (*.mp4 *.mov *.avi *.mkv *.m4v);;All Files (*)",
        )
        if not path:
            return

        cap = cv2.VideoCapture(path)
        if not cap.isOpened():
            QMessageBox.critical(self, "Open failed", "Could not open this video.")
            return

        if self.cap is not None:
            self.cap.release()

        self.video_path = path
        self.cap = cap
        self.fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        self.total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        self.current_frame_index = 0
        self.frame_slider.setRange(0, max(0, self.total_frames - 1))
        self.frame_slider.setValue(0)
        self.frame_slider.setEnabled(True)
        self.play_button.setEnabled(True)
        self.export_button.setEnabled(True)
        self.export_fixed_frames_button.setEnabled(True)
        self.export_frames_button.setEnabled(True)

        self.read_frame(0)
        self.update_status(f"Loaded: {os.path.basename(path)} | {self.total_frames} frames @ {self.fps:.2f} fps")

    def read_frame(self, frame_index: int):
        if self.cap is None:
            return
        frame_index = max(0, min(frame_index, max(0, self.total_frames - 1)))
        self.cap.set(cv2.CAP_PROP_POS_FRAMES, frame_index)
        ok, frame = self.cap.read()
        if not ok:
            return
        self.current_frame_index = frame_index
        self.current_raw_frame = frame
        self.refresh_current_frame()

    def refresh_current_frame(self):
        if self.current_raw_frame is None:
            return

        s = self.settings()
        corrected, pvc_mask, red_mask = VideoProcessor.process_frame(self.current_raw_frame, s)
        self.current_corrected_frame = corrected
        self.current_pvc_mask = pvc_mask
        self.current_red_mask = red_mask

        display = self.make_display_frame(s.show_mode)
        self.show_bgr(display)
        self.frame_label.setText(f"Frame: {self.current_frame_index + 1}/{max(1, self.total_frames)}")

    def make_display_frame(self, mode: str) -> np.ndarray:
        raw = self.current_raw_frame
        corrected = self.current_corrected_frame
        pvc_mask = self.current_pvc_mask
        red_mask = self.current_red_mask
        assert raw is not None and corrected is not None and pvc_mask is not None and red_mask is not None

        if mode == "Original":
            return raw
        if mode == "Corrected":
            return corrected
        if mode == "Side-by-side":
            h = min(raw.shape[0], corrected.shape[0])
            left = cv2.resize(raw, (raw.shape[1], h))
            right = cv2.resize(corrected, (corrected.shape[1], h))
            return np.hstack([left, right])
        if mode == "PVC mask":
            return cv2.cvtColor(pvc_mask, cv2.COLOR_GRAY2BGR)
        if mode == "Red mask":
            return cv2.cvtColor(red_mask, cv2.COLOR_GRAY2BGR)
        if mode == "Combined mask":
            combined = np.zeros_like(raw)
            combined[pvc_mask > 0] = (255, 255, 0)
            combined[red_mask > 0] = (0, 0, 255)
            return combined
        return corrected

    def show_bgr(self, bgr: np.ndarray):
        rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        h, w, ch = rgb.shape
        bytes_per_line = ch * w
        qimg = QImage(rgb.data, w, h, bytes_per_line, QImage.Format.Format_RGB888).copy()
        pix = QPixmap.fromImage(qimg)
        scaled = pix.scaled(
            self.preview_label.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self.preview_label.setPixmap(scaled)

    def resizeEvent(self, event):  # noqa: N802 - Qt naming convention.
        super().resizeEvent(event)
        if self.current_raw_frame is not None:
            self.refresh_current_frame()

    def seek_from_slider(self):
        self.read_frame(self.frame_slider.value())

    def toggle_playback(self):
        if self.cap is None:
            return
        if self.timer.isActive():
            self.timer.stop()
            self.play_button.setText("Play")
        else:
            interval_ms = int(1000 / max(1.0, self.fps))
            self.timer.start(interval_ms)
            self.play_button.setText("Pause")

    def next_frame(self):
        if self.cap is None:
            return
        next_idx = self.current_frame_index + 1
        if next_idx >= self.total_frames:
            self.timer.stop()
            self.play_button.setText("Play")
            return
        self.read_frame(next_idx)
        self.frame_slider.blockSignals(True)
        self.frame_slider.setValue(next_idx)
        self.frame_slider.blockSignals(False)

    def export_video(self):
        if not self.video_path:
            return

        base, _ = os.path.splitext(self.video_path)
        suggested = base + "_corrected_pvc_red.mp4"
        output_path, _ = QFileDialog.getSaveFileName(
            self,
            "Export corrected video",
            suggested,
            "MP4 Video (*.mp4);;All Files (*)",
        )
        if not output_path:
            return
        if not output_path.lower().endswith(".mp4"):
            output_path += ".mp4"

        self.export_button.setEnabled(False)
        self.export_fixed_frames_button.setEnabled(False)
        self.export_frames_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.progress_bar.setValue(0)
        self.update_status("Starting export...")

        self.export_worker = ExportWorker(self.video_path, output_path, self.settings(include_preview_overlay=False))
        self.export_worker.progress.connect(self.progress_bar.setValue)
        self.export_worker.status.connect(self.update_status)
        self.export_worker.finished_ok.connect(self.export_finished)
        self.export_worker.failed.connect(self.export_failed)
        self.export_worker.start()

    def export_fixed_interval_frames(self):
        if not self.video_path:
            return

        parent = QFileDialog.getExistingDirectory(
            self,
            "Choose parent folder for 0.1s frames",
            os.path.dirname(self.video_path),
        )
        if not parent:
            return

        video_base = Path(self.video_path).stem
        output_dir = self.unique_output_dir(Path(parent) / f"{video_base}_frames_0p1s")
        frame_settings = self.frame_selection_settings()

        self.export_button.setEnabled(False)
        self.export_fixed_frames_button.setEnabled(False)
        self.export_frames_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.progress_bar.setValue(0)
        self.update_status("Starting 0.1s frame export...")

        self.fixed_frame_export_worker = FixedIntervalFrameExportWorker(
            self.video_path,
            str(output_dir),
            self.settings(include_preview_overlay=False),
            interval_seconds=0.1,
            output_format=frame_settings.output_format,
            jpeg_quality=frame_settings.jpeg_quality,
        )
        self.fixed_frame_export_worker.progress.connect(self.progress_bar.setValue)
        self.fixed_frame_export_worker.status.connect(self.update_status)
        self.fixed_frame_export_worker.finished_ok.connect(self.export_finished)
        self.fixed_frame_export_worker.failed.connect(self.export_failed)
        self.fixed_frame_export_worker.start()

    @staticmethod
    def unique_output_dir(path: Path) -> Path:
        if not path.exists():
            return path
        for suffix in range(2, 1000):
            candidate = path.with_name(f"{path.name}_{suffix:02d}")
            if not candidate.exists():
                return candidate
        raise RuntimeError(f"Could not find an unused output folder near {path}")

    def export_selected_frames(self):
        if not self.video_path:
            return

        parent = QFileDialog.getExistingDirectory(
            self,
            "Choose parent folder for selected frames",
            os.path.dirname(self.video_path),
        )
        if not parent:
            return

        video_base = Path(self.video_path).stem
        output_dir = self.unique_output_dir(Path(parent) / f"{video_base}_selected_frames")

        self.export_button.setEnabled(False)
        self.export_fixed_frames_button.setEnabled(False)
        self.export_frames_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.progress_bar.setValue(0)
        self.update_status("Starting selected-frame export...")

        self.frame_export_worker = FrameExportWorker(
            self.video_path,
            str(output_dir),
            self.settings(include_preview_overlay=False),
            self.frame_selection_settings(),
        )
        self.frame_export_worker.progress.connect(self.progress_bar.setValue)
        self.frame_export_worker.status.connect(self.update_status)
        self.frame_export_worker.finished_ok.connect(self.export_finished)
        self.frame_export_worker.failed.connect(self.export_failed)
        self.frame_export_worker.start()

    def cancel_export(self):
        if self.export_worker is not None:
            self.export_worker.cancel()
        if self.fixed_frame_export_worker is not None:
            self.fixed_frame_export_worker.cancel()
        if self.frame_export_worker is not None:
            self.frame_export_worker.cancel()
        if (
            self.export_worker is not None
            or self.fixed_frame_export_worker is not None
            or self.frame_export_worker is not None
        ):
            self.cancel_button.setEnabled(False)
            self.update_status("Canceling export...")

    def export_finished(self, path: str):
        self.export_button.setEnabled(True)
        self.export_fixed_frames_button.setEnabled(True)
        self.export_frames_button.setEnabled(True)
        self.cancel_button.setEnabled(False)
        self.update_status(f"Export complete: {path}")
        self.export_worker = None
        self.fixed_frame_export_worker = None
        self.frame_export_worker = None
        QMessageBox.information(self, "Export complete", f"Saved:\n{path}")

    def export_failed(self, message: str):
        self.export_button.setEnabled(True)
        self.export_fixed_frames_button.setEnabled(True)
        self.export_frames_button.setEnabled(True)
        self.cancel_button.setEnabled(False)
        self.update_status(message)
        self.export_worker = None
        self.fixed_frame_export_worker = None
        self.frame_export_worker = None
        QMessageBox.warning(self, "Export stopped", message)

    def update_status(self, text: str):
        self.status_label.setText(text)

    def closeEvent(self, event):  # noqa: N802 - Qt naming convention.
        if self.cap is not None:
            self.cap.release()
        if self.export_worker is not None and self.export_worker.isRunning():
            self.export_worker.cancel()
            self.export_worker.wait(2000)
        if self.fixed_frame_export_worker is not None and self.fixed_frame_export_worker.isRunning():
            self.fixed_frame_export_worker.cancel()
            self.fixed_frame_export_worker.wait(2000)
        if self.frame_export_worker is not None and self.frame_export_worker.isRunning():
            self.frame_export_worker.cancel()
            self.frame_export_worker.wait(2000)
        super().closeEvent(event)


def main():
    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
