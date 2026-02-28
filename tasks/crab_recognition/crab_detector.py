"""Crab detection + classification for the MATE ROV Ranger 'Invasive Species' task.

The competition manual requires that each **European Green crab** is surrounded by a
bounding box and that the total number of green crabs is displayed on screen.

This module is intentionally "classic CV" (no ML training).

Why two pipelines?
------------------
The first iteration of this project used a board-dependent segmentation pipeline
(detect the white corrugated sheet, then segment darker blobs on it). That works
well on the provided sample images, but it is brittle when:
  - the corrugated sheet isn't visible,
  - there is strong color/white-balance shift,
  - the image is blurry,
  - the camera sees the scene at an angle.

To improve robustness, the default mode now uses **feature-based object
localization** (SIFT + RANSAC homography) to find *instances of the known crab
images anywhere in the frame*, without relying on the corrugated background.

Pipelines
---------
  A) Feature localization (default):
     - SIFT features on the full frame
     - per-template matching + iterative RANSAC homographies
     - bounding boxes from projected template corners

  B) Board segmentation (fallback / legacy):
     - Find the white board ROI (largest bright region)
     - Segment candidate crab regions via black-hat morphology
     - Classify each candidate by ORB feature matching to reference images
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Tuple

import cv2
import numpy as np


BBox = Tuple[int, int, int, int]  # (x, y, w, h)


@dataclass(frozen=True)
class Detection:
    label: str              # "green" | "rock" | "jonah" | "unknown"
    bbox: BBox              # axis-aligned bbox in original image coordinates
    score: float            # match score (inliers for SIFT, good matches for ORB)


class CrabDetector:
    """Detect and classify crabs in a single BGR image."""

    def __init__(
        self,
        template_paths: Dict[str, Path],
        *,
        # Overall mode.
        mode: str = "auto",  # "auto" | "sift" | "board"

        # Feature-localization (SIFT) settings.
        sift_features: int = 2000,
        sift_max_dim: int = 1600,
        sift_match_ratio: float = 0.75,
        # Lower thresholds = higher recall under blur/angle, at some risk of false positives.
        sift_min_good_matches: int = 16,
        sift_min_inliers: int = 12,
        sift_ransac_reproj_thresh: float = 6.0,
        nms_iou_thresh: float = 0.30,

        # Board-segmentation (legacy) settings.
        orb_features: int = 1200,
        orb_match_ratio: float = 0.75,
        min_candidate_area: int = 7000,
        max_candidate_area: int = 140000,
        edge_margin_px: int = 8,
    ) -> None:
        self.mode = str(mode or "auto").strip().lower()
        if self.mode not in {"auto", "sift", "board"}:
            raise ValueError("mode must be one of: 'auto', 'sift', 'board'")

        # SIFT settings
        self.sift_match_ratio = float(sift_match_ratio)
        self.sift_max_dim = int(sift_max_dim)
        self.sift_min_good_matches = int(sift_min_good_matches)
        self.sift_min_inliers = int(sift_min_inliers)
        self.sift_ransac_reproj_thresh = float(sift_ransac_reproj_thresh)
        self.nms_iou_thresh = float(nms_iou_thresh)

        # ORB (legacy) settings
        self.orb_match_ratio = float(orb_match_ratio)
        self.min_candidate_area = int(min_candidate_area)
        self.max_candidate_area = int(max_candidate_area)
        self.edge_margin_px = int(edge_margin_px)

        # ORB (legacy)
        self.orb = cv2.ORB_create(nfeatures=int(orb_features))
        self.bf_orb = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=False)

        # SIFT (robust)
        self.sift = cv2.SIFT_create(nfeatures=int(sift_features))
        self.bf_sift = cv2.BFMatcher(cv2.NORM_L2, crossCheck=False)

        # Template storage
        #   - ORB templates for board/segmentation fallback
        self.templates_orb: Dict[str, tuple[np.ndarray, list[cv2.KeyPoint], np.ndarray]] = {}
        #   - SIFT templates for robust matching
        self.templates_sift: Dict[str, tuple[np.ndarray, list[cv2.KeyPoint], np.ndarray, np.ndarray]] = {}

        for label, p in template_paths.items():
            img = cv2.imread(str(p), cv2.IMREAD_COLOR)
            if img is None:
                raise FileNotFoundError(f"Could not read template image: {p}")

            # ORB template
            gray_o = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            kp_o, des_o = self.orb.detectAndCompute(gray_o, None)
            if des_o is None or len(kp_o) < 20:
                raise RuntimeError(f"Template '{label}' has too few ORB features: {p}")
            self.templates_orb[label] = (img, kp_o, des_o)

            # SIFT template (preprocessed)
            gray_s = self._prep_gray_for_features(img)
            kp_s, des_s = self.sift.detectAndCompute(gray_s, None)
            if des_s is None or len(kp_s) < 20:
                raise RuntimeError(f"Template '{label}' has too few SIFT features: {p}")
            h, w = gray_s.shape[:2]
            corners = np.float32([[0, 0], [w, 0], [w, h], [0, h]]).reshape(-1, 1, 2)
            self.templates_sift[label] = (gray_s, kp_s, des_s.astype(np.float32), corners)

    # -------------------------
    # Public API
    # -------------------------
    def detect(self, img_bgr: np.ndarray) -> List[Detection]:
        """Return all detections (green + non-green)."""
        if img_bgr is None or getattr(img_bgr, "size", 0) == 0:
            return []

        out: List[Detection] = []

        # 1) Robust mode: SIFT matching (background independent)
        if self.mode in {"auto", "sift"}:
            try:
                out.extend(self._detect_by_sift(img_bgr))
            except Exception:
                # Keep going; fallback may still work.
                pass

        # 2) Fallback: board/segmentation + ORB classification
        # In "auto", we only run this if we actually detect a large white board region;
        # that improves recall on the official samples while avoiding garbage when the
        # corrugated board isn't present (e.g., testing on a phone screen).
        try:
            board_ok = self._board_present(img_bgr)
        except Exception:
            board_ok = False

        if self.mode == "board" or (self.mode == "auto" and board_ok):
            try:
                out.extend(self._detect_by_board_segmentation(img_bgr))
            except Exception:
                pass

        # Merge duplicates
        out = self._nms(out, iou_thresh=self.nms_iou_thresh)
        out.sort(key=lambda d: (d.bbox[1], d.bbox[0]))
        return out

    def detect_green(self, img_bgr: np.ndarray) -> List[Detection]:
        return [d for d in self.detect(img_bgr) if d.label == "green"]

    @staticmethod
    def draw_count_and_boxes(
        img_bgr: np.ndarray,
        detections: Iterable[Detection],
        *,
        count_label: str = "GREEN CRABS",
        box_color_bgr: tuple[int, int, int] = (0, 255, 0),
        thickness: int = 3,
        show_scores: bool = False,
    ) -> np.ndarray:
        """Draw bounding boxes + count on a copy of the image."""
        dets = list(detections)
        out = img_bgr.copy()

        for d in dets:
            x, y, w, h = d.bbox
            cv2.rectangle(out, (x, y), (x + w, y + h), box_color_bgr, thickness)
            if show_scores:
                txt = f"{d.label} {int(d.score)}"
                ty = y - 10 if y > 30 else y + 25
                cv2.putText(out, txt, (x, ty), cv2.FONT_HERSHEY_SIMPLEX, 0.6, box_color_bgr, 2, cv2.LINE_AA)

        cv2.putText(
            out,
            f"{count_label}: {len(dets)}",
            (20, 45),
            cv2.FONT_HERSHEY_SIMPLEX,
            1.2,
            box_color_bgr,
            3,
            cv2.LINE_AA,
        )
        return out

    # -------------------------
    # Robust detection: SIFT + homographies
    # -------------------------
    def _detect_by_sift(self, img_bgr: np.ndarray) -> List[Detection]:
        # Downscale very large frames for speed.
        H0, W0 = img_bgr.shape[:2]
        scale = 1.0
        if self.sift_max_dim > 0 and max(H0, W0) > self.sift_max_dim:
            scale = float(self.sift_max_dim) / float(max(H0, W0))
            img_bgr = cv2.resize(img_bgr, (int(round(W0 * scale)), int(round(H0 * scale))), interpolation=cv2.INTER_AREA)

        gray = self._prep_gray_for_features(img_bgr)
        kp_i, des_i = self.sift.detectAndCompute(gray, None)
        if des_i is None or len(kp_i) < 30:
            return []

        des_i = des_i.astype(np.float32)
        dets: List[Detection] = []
        for label in self.templates_sift.keys():
            dets.extend(self._sift_find_instances(label, kp_i, des_i, img_shape=gray.shape))

        # Map bboxes back to original coordinate space.
        if scale != 1.0 and dets:
            inv = 1.0 / float(scale)
            dets = [
                Detection(
                    label=d.label,
                    score=float(d.score),
                    bbox=(
                        int(round(d.bbox[0] * inv)),
                        int(round(d.bbox[1] * inv)),
                        int(round(d.bbox[2] * inv)),
                        int(round(d.bbox[3] * inv)),
                    ),
                )
                for d in dets
            ]
        return dets

    def _sift_find_instances(
        self,
        label: str,
        kp_img: list[cv2.KeyPoint],
        des_img: np.ndarray,
        *,
        img_shape: tuple[int, int],
    ) -> List[Detection]:
        """Iteratively find multiple instances of one template in the image."""
        _tpl_gray, kp_t, des_t, corners_t = self.templates_sift[label]

        matches = self.bf_sift.knnMatch(des_t, des_img, k=2)
        good: List[cv2.DMatch] = []
        for m_n in matches:
            if len(m_n) != 2:
                continue
            m, n = m_n
            if m.distance < self.sift_match_ratio * n.distance:
                good.append(m)

        if len(good) < self.sift_min_good_matches:
            return []

        H_img, W_img = img_shape[:2]
        dets: List[Detection] = []

        remaining = good
        max_instances = 10
        for _ in range(max_instances):
            if len(remaining) < self.sift_min_good_matches:
                break

            src_pts = np.float32([kp_t[m.queryIdx].pt for m in remaining]).reshape(-1, 1, 2)
            dst_pts = np.float32([kp_img[m.trainIdx].pt for m in remaining]).reshape(-1, 1, 2)
            H, mask = cv2.findHomography(src_pts, dst_pts, cv2.RANSAC, self.sift_ransac_reproj_thresh)
            if H is None or mask is None:
                break

            inliers = mask.ravel().astype(bool)
            inlier_count = int(inliers.sum())
            if inlier_count < self.sift_min_inliers:
                break

            proj = cv2.perspectiveTransform(corners_t, H)  # (4,1,2)
            xs = proj[:, 0, 0]
            ys = proj[:, 0, 1]
            x1 = int(np.floor(xs.min()))
            y1 = int(np.floor(ys.min()))
            x2 = int(np.ceil(xs.max()))
            y2 = int(np.ceil(ys.max()))

            # Clamp + sanity check
            x1c = max(0, min(W_img - 1, x1))
            y1c = max(0, min(H_img - 1, y1))
            x2c = max(0, min(W_img - 1, x2))
            y2c = max(0, min(H_img - 1, y2))
            w = max(0, x2c - x1c)
            h = max(0, y2c - y1c)

            if w == 0 or h == 0:
                remaining = [m for i, m in enumerate(remaining) if not inliers[i]]
                continue

            area = w * h
            if area < 2500 or area > (W_img * H_img * 0.50):
                remaining = [m for i, m in enumerate(remaining) if not inliers[i]]
                continue

            dets.append(Detection(label=label, bbox=(x1c, y1c, w, h), score=float(inlier_count)))

            # Remove inliers that supported this instance.
            remaining = [m for i, m in enumerate(remaining) if not inliers[i]]

        return dets

    @staticmethod
    def _prep_gray_for_features(img_bgr: np.ndarray) -> np.ndarray:
        """Preprocess for feature detection: grayscale + CLAHE + mild sharpening if blurry."""
        gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)

        # Contrast normalization improves resilience to color/lighting changes.
        try:
            clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
            gray = clahe.apply(gray)
        except Exception:
            pass

        # If very blurry, a mild unsharp mask can help keypoints.
        try:
            lap_var = float(cv2.Laplacian(gray, cv2.CV_64F).var())
            if lap_var < 40.0:
                blur = cv2.GaussianBlur(gray, (0, 0), 1.2)
                gray = cv2.addWeighted(gray, 1.6, blur, -0.6, 0)
        except Exception:
            pass

        return gray

    # -------------------------
    # Legacy detection: board segmentation + ORB classification
    # -------------------------
    def _detect_by_board_segmentation(self, img_bgr: np.ndarray) -> List[Detection]:
        board_roi, (offx, offy) = self._find_board_roi(img_bgr)
        candidate_boxes = self._segment_candidates(board_roi)

        out: List[Detection] = []
        for (x, y, w, h) in candidate_boxes:
            patch = board_roi[y : y + h, x : x + w]
            label, score = self._classify_patch_orb(patch)
            out.append(Detection(label=label, score=float(score), bbox=(x + offx, y + offy, w, h)))
        return out

    def _find_board_roi(self, img_bgr: np.ndarray) -> tuple[np.ndarray, tuple[int, int]]:
        """Find/crop the white corrugated board ROI."""
        gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
        _, bw = cv2.threshold(gray, 200, 255, cv2.THRESH_BINARY)
        bw = cv2.morphologyEx(
            bw,
            cv2.MORPH_CLOSE,
            cv2.getStructuringElement(cv2.MORPH_RECT, (15, 15)),
            iterations=2,
        )
        contours, _ = cv2.findContours(bw, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if not contours:
            return img_bgr, (0, 0)

        areas = [cv2.contourArea(c) for c in contours]
        c = contours[int(np.argmax(areas))]
        x, y, w, h = cv2.boundingRect(c)

        x2 = max(0, x)
        y2 = max(0, y)
        w2 = min(img_bgr.shape[1] - x2, w)
        h2 = min(img_bgr.shape[0] - y2, h)
        roi = img_bgr[y2 : y2 + h2, x2 : x2 + w2]
        return roi, (x2, y2)

    def _board_present(self, img_bgr: np.ndarray) -> bool:
        """Heuristic: is there a large bright 'board-like' region in frame?"""
        gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
        _, bw = cv2.threshold(gray, 200, 255, cv2.THRESH_BINARY)
        bw = cv2.morphologyEx(
            bw,
            cv2.MORPH_CLOSE,
            cv2.getStructuringElement(cv2.MORPH_RECT, (15, 15)),
            iterations=2,
        )
        contours, _ = cv2.findContours(bw, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if not contours:
            return False
        areas = [cv2.contourArea(c) for c in contours]
        max_area = float(max(areas))
        frame_area = float(img_bgr.shape[0] * img_bgr.shape[1])
        # If the board occupies a meaningful fraction of the frame, assume it's present.
        return (max_area / max(1.0, frame_area)) > 0.12

    def _segment_candidates(self, roi_bgr: np.ndarray) -> List[BBox]:
        gray = cv2.cvtColor(roi_bgr, cv2.COLOR_BGR2GRAY)

        bh_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (41, 41))
        blackhat = cv2.morphologyEx(gray, cv2.MORPH_BLACKHAT, bh_kernel)
        blackhat = cv2.GaussianBlur(blackhat, (5, 5), 0)

        _, th = cv2.threshold(blackhat, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        th = cv2.morphologyEx(
            th, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 7)), iterations=1
        )
        th = cv2.morphologyEx(
            th, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (19, 19)), iterations=2
        )

        contours, _ = cv2.findContours(th, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        H, W = gray.shape[:2]
        boxes: List[BBox] = []
        for c in contours:
            area = cv2.contourArea(c)
            if area < self.min_candidate_area or area > self.max_candidate_area:
                continue
            x, y, w, h = cv2.boundingRect(c)
            m = self.edge_margin_px
            if x < m or y < m or (x + w) > (W - m) or (y + h) > (H - m):
                continue
            boxes.append((x, y, w, h))

        boxes.sort(key=lambda b: (b[1], b[0]))
        return boxes

    def _classify_patch_orb(self, patch_bgr: np.ndarray) -> tuple[str, float]:
        gray = cv2.cvtColor(patch_bgr, cv2.COLOR_BGR2GRAY)
        kp, des = self.orb.detectAndCompute(gray, None)
        if des is None or len(kp) < 20:
            return "unknown", 0.0

        best_label = "unknown"
        best_score = 0
        for label, (_img_t, _kp_t, des_t) in self.templates_orb.items():
            matches = self.bf_orb.knnMatch(des_t, des, k=2)
            good = 0
            for m, n in matches:
                if m.distance < self.orb_match_ratio * n.distance:
                    good += 1
            if good > best_score:
                best_score = good
                best_label = label

        return best_label, float(best_score)

    # -------------------------
    # Helpers
    # -------------------------
    @staticmethod
    def _iou(a: BBox, b: BBox) -> float:
        ax, ay, aw, ah = a
        bx, by, bw, bh = b
        ax2, ay2 = ax + aw, ay + ah
        bx2, by2 = bx + bw, by + bh

        ix1, iy1 = max(ax, bx), max(ay, by)
        ix2, iy2 = min(ax2, bx2), min(ay2, by2)
        iw, ih = max(0, ix2 - ix1), max(0, iy2 - iy1)
        inter = iw * ih
        if inter <= 0:
            return 0.0
        union = (aw * ah) + (bw * bh) - inter
        return float(inter) / float(max(1, union))

    def _nms(self, dets: List[Detection], *, iou_thresh: float) -> List[Detection]:
        """Simple NMS over axis-aligned bboxes, keeping higher score."""
        if not dets:
            return []
        dets = sorted(dets, key=lambda d: float(d.score), reverse=True)
        kept: List[Detection] = []
        for d in dets:
            drop = False
            for k in kept:
                if self._iou(d.bbox, k.bbox) >= iou_thresh:
                    drop = True
                    break
            if not drop:
                kept.append(d)
        return kept


def _repo_root_from_here() -> Path:
    # tasks/crab_recognition/crab_detector.py -> .../TritonPilot/TritonPilot
    return Path(__file__).resolve().parents[2]


def default_template_paths(repo_root: Path | None = None) -> Dict[str, Path]:
    """Paths to the reference images shipped in data/img/crab/reference."""
    if repo_root is None:
        repo_root = _repo_root_from_here()
    ref_dir = repo_root / "data" / "img" / "crab" / "reference"
    return {
        "green": ref_dir / "European Green Crab Image.jpg",
        "rock": ref_dir / "Native Rock Crab.jpg",
        "jonah": ref_dir / "Jonah crab 2.png",
    }


def create_default_detector() -> CrabDetector:
    """Convenience factory for the Pilot app."""
    return CrabDetector(default_template_paths(), mode="auto")
