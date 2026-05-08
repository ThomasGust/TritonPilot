import cv2
import numpy as np
from pathlib import Path
import csv
import re

DEFAULT_UNWRAP_SIZE = (700, 700)
ANALYSIS_DIR = Path(__file__).resolve().parent
REPO_ROOT = ANALYSIS_DIR.parent
DATA_DIR = ANALYSIS_DIR / "data"
SCRIPT_DIR = REPO_ROOT
REFERENCE_IMAGE_PATHS = {
    "european_green": DATA_DIR / "crab_reference" / "european_green.jpg",
    "native_rock": DATA_DIR / "crab_reference" / "native_rock.jpg",
    "jonah": DATA_DIR / "crab_reference" / "jonah.png",
}
SPECIES_DISPLAY_NAMES = {
    "european_green": "European green",
    "native_rock": "Rock",
    "jonah": "Jonah",
    "other": "Other",
}
SPECIES_DRAW_COLORS = {
    "european_green": (0, 255, 0),
    "native_rock": (0, 165, 255),
    "jonah": (0, 0, 255),
    "other": (255, 255, 0),
}


def resize_for_detection(image, max_side=900):
    height, width = image.shape[:2]
    scale = min(1.0, max_side / max(height, width))
    if scale == 1.0:
        return image.copy(), scale
    resized = cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    return resized, scale


def odd_kernel_size(value, minimum=7):
    size = max(minimum, int(value))
    return size if size % 2 == 1 else size + 1


def order_corners(points):
    points = np.asarray(points, dtype=np.float32)
    sums = points.sum(axis=1)
    diffs = points[:, 0] - points[:, 1]

    top_left = points[np.argmin(sums)]
    bottom_right = points[np.argmax(sums)]
    top_right = points[np.argmax(diffs)]
    bottom_left = points[np.argmin(diffs)]

    return np.array([top_left, top_right, bottom_right, bottom_left], dtype=np.float32)


def polygon_area(points):
    contour = np.asarray(points, dtype=np.float32).reshape(-1, 1, 2)
    return float(abs(cv2.contourArea(contour)))


def edge_length(point_a, point_b):
    return float(np.linalg.norm(np.asarray(point_a, dtype=np.float32) - np.asarray(point_b, dtype=np.float32)))


def contour_center(contour):
    moments = cv2.moments(contour)
    if moments["m00"] == 0:
        x, y, width, height = cv2.boundingRect(contour)
        return np.array([x + width / 2.0, y + height / 2.0], dtype=np.float32)
    return np.array(
        [moments["m10"] / moments["m00"], moments["m01"] / moments["m00"]],
        dtype=np.float32,
    )


def infer_unwrap_size(polygon, force_square=True):
    top_left, top_right, bottom_right, bottom_left = polygon

    top_width = edge_length(top_left, top_right)
    bottom_width = edge_length(bottom_left, bottom_right)
    left_height = edge_length(top_left, bottom_left)
    right_height = edge_length(top_right, bottom_right)

    average_width = max(1, int(round((top_width + bottom_width) / 2.0)))
    average_height = max(1, int(round((left_height + right_height) / 2.0)))

    if force_square:
        side = max(average_width, average_height)
        return side, side

    return average_width, average_height


def score_quadrilateral_fit(contour, quadrilateral):
    x, y, width, height = cv2.boundingRect(contour)
    padding = 8

    contour_mask = np.zeros((height + 2 * padding, width + 2 * padding), dtype=np.uint8)
    quad_mask = np.zeros_like(contour_mask)

    offset = np.array([x - padding, y - padding])
    shifted_contour = contour - offset
    shifted_quad = quadrilateral - offset

    cv2.drawContours(contour_mask, [shifted_contour], -1, 255, thickness=cv2.FILLED)
    cv2.fillConvexPoly(quad_mask, shifted_quad.astype(np.int32), 255)

    intersection = np.count_nonzero((contour_mask > 0) & (quad_mask > 0))
    union = np.count_nonzero((contour_mask > 0) | (quad_mask > 0))
    return intersection / union if union else 0.0


def fit_board_quadrilateral(contour):
    best_candidate = None
    best_score = -1.0

    candidate_contours = [contour, cv2.convexHull(contour)]
    for candidate_contour in candidate_contours:
        perimeter = cv2.arcLength(candidate_contour, True)
        for epsilon_scale in np.linspace(0.01, 0.08, 29):
            approximation = cv2.approxPolyDP(candidate_contour, epsilon_scale * perimeter, True)
            if len(approximation) != 4:
                continue
            if not cv2.isContourConvex(approximation):
                continue

            quadrilateral = approximation.reshape(-1, 2)
            score = score_quadrilateral_fit(contour, quadrilateral)
            if score > best_score:
                best_score = score
                best_candidate = quadrilateral

    if best_candidate is not None:
        return best_candidate

    return cv2.boxPoints(cv2.minAreaRect(contour))


def polygon_mask(shape, polygon):
    mask = np.zeros(shape[:2], dtype=np.uint8)
    cv2.fillConvexPoly(mask, np.round(polygon).astype(np.int32), 255)
    return mask


def board_edge_support(image, polygon):
    height, width = image.shape[:2]
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    equalized = clahe.apply(gray)
    blurred = cv2.GaussianBlur(equalized, (5, 5), 0)
    median = float(np.median(blurred))
    lower = int(max(0, 0.66 * median))
    upper = int(min(255, 1.33 * median + 20))
    edges = cv2.Canny(blurred, lower, upper)

    edge_kernel_size = odd_kernel_size(min(height, width) // 220, minimum=3)
    edge_kernel = cv2.getStructuringElement(
        cv2.MORPH_ELLIPSE,
        (edge_kernel_size, edge_kernel_size),
    )
    supported_edges = cv2.dilate(edges, edge_kernel)

    line_mask = np.zeros((height, width), dtype=np.uint8)
    line_thickness = max(2, min(height, width) // 180)
    cv2.polylines(
        line_mask,
        [np.round(polygon).astype(np.int32).reshape(-1, 1, 2)],
        True,
        255,
        line_thickness,
    )
    line_pixels = np.count_nonzero(line_mask)
    if line_pixels == 0:
        return 0.0
    supported_pixels = np.count_nonzero((supported_edges > 0) & (line_mask > 0))
    return float(supported_pixels) / float(line_pixels)


def score_board_candidate(image, contour, polygon):
    height, width = image.shape[:2]
    image_area = float(height * width)
    ordered_polygon = order_corners(polygon).astype(np.float32)
    rounded_polygon = np.round(ordered_polygon).astype(np.int32)

    if not cv2.isContourConvex(rounded_polygon.reshape(-1, 1, 2)):
        return -1.0

    polygon_area_value = polygon_area(ordered_polygon)
    if polygon_area_value <= 0:
        return -1.0

    area_ratio = polygon_area_value / image_area
    if area_ratio < 0.035 or area_ratio > 0.72:
        return -1.0

    top_left, top_right, bottom_right, bottom_left = ordered_polygon
    side_lengths = np.array(
        [
            edge_length(top_left, top_right),
            edge_length(top_right, bottom_right),
            edge_length(bottom_right, bottom_left),
            edge_length(bottom_left, top_left),
        ],
        dtype=np.float32,
    )
    if np.any(side_lengths < min(height, width) * 0.08):
        return -1.0

    width_estimate = max(edge_length(top_left, top_right), edge_length(bottom_left, bottom_right))
    height_estimate = max(edge_length(top_left, bottom_left), edge_length(top_right, bottom_right))
    aspect_ratio = width_estimate / max(1.0, height_estimate)
    if aspect_ratio < 0.45 or aspect_ratio > 2.25:
        return -1.0

    x, y, box_width, box_height = cv2.boundingRect(rounded_polygon.reshape(-1, 1, 2))
    border_margin = max(4, min(height, width) // 120)
    border_penalty = 0.0
    if (
        x <= border_margin
        or y <= border_margin
        or x + box_width >= width - border_margin
        or y + box_height >= height - border_margin
    ):
        border_penalty = 0.35

    mask = polygon_mask(image.shape, ordered_polygon)
    if np.count_nonzero(mask) < 1:
        return -1.0

    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    lab = cv2.cvtColor(image, cv2.COLOR_BGR2LAB)

    inside_value = cv2.mean(hsv[:, :, 2], mask=mask)[0]
    inside_saturation = cv2.mean(hsv[:, :, 1], mask=mask)[0]
    inside_lightness = cv2.mean(lab[:, :, 0], mask=mask)[0]

    outside_mask = cv2.bitwise_not(mask)
    outside_value = cv2.mean(hsv[:, :, 2], mask=outside_mask)[0]
    outside_saturation = cv2.mean(hsv[:, :, 1], mask=outside_mask)[0]
    outside_lightness = cv2.mean(lab[:, :, 0], mask=outside_mask)[0]

    fill_score = score_quadrilateral_fit(contour, ordered_polygon)
    contour_area = max(1.0, float(cv2.contourArea(contour)))
    rectangularity = min(1.0, contour_area / polygon_area_value)
    edge_score = board_edge_support(image, ordered_polygon)

    white_score = (
        0.45 * (inside_value / 255.0)
        + 0.40 * (inside_lightness / 255.0)
        + 0.35 * (1.0 - inside_saturation / 255.0)
    )
    contrast_score = np.clip(
        ((inside_value - outside_value) + (inside_lightness - outside_lightness)) / 180.0
        + (outside_saturation - inside_saturation) / 220.0,
        -0.75,
        1.25,
    )
    square_score = max(0.0, 1.0 - abs(np.log(max(0.01, aspect_ratio))) / np.log(2.4))
    area_score = max(0.0, 1.0 - abs(np.log(max(0.001, area_ratio / 0.16))) / np.log(7.0))

    return float(
        2.1 * fill_score
        + 1.1 * rectangularity
        + 1.0 * white_score
        + 0.75 * contrast_score
        + 0.65 * edge_score
        + 0.35 * square_score
        + 0.25 * area_score
        - border_penalty
    )


def build_white_board_mask(image):
    height, width = image.shape[:2]
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    lab = cv2.cvtColor(image, cv2.COLOR_BGR2LAB)

    saturation = hsv[:, :, 1]
    value = hsv[:, :, 2]
    lightness = lab[:, :, 0]
    channel_min = np.min(image, axis=2)
    channel_spread = np.max(image, axis=2) - channel_min

    fixed_white = (value > 150) & (saturation < 80) & (lightness > 160)

    white_score = (
        value.astype(np.float32) * 0.50
        + lightness.astype(np.float32) * 0.40
        + channel_min.astype(np.float32) * 0.20
        - saturation.astype(np.float32) * 0.55
        - channel_spread.astype(np.float32) * 0.10
    )
    score_floor = max(
        float(np.percentile(white_score, 76)),
        float(np.mean(white_score) + 0.20 * np.std(white_score)),
    )
    value_floor = max(110.0, float(np.percentile(value, 68)))
    lightness_floor = max(125.0, float(np.percentile(lightness, 68)))
    saturation_ceiling = min(140.0, max(85.0, float(np.percentile(saturation, 55) + 32.0)))

    adaptive_white = (
        (white_score >= score_floor)
        & ((value >= value_floor) | (lightness >= lightness_floor))
        & (saturation <= saturation_ceiling)
        & (value >= 95)
        & (lightness >= 105)
    )

    white_mask = np.where(fixed_white | adaptive_white, 255, 0).astype(np.uint8)

    open_kernel_size = odd_kernel_size(min(height, width) // 180, minimum=5)
    close_kernel_size = odd_kernel_size(min(height, width) // 28, minimum=21)
    open_kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (open_kernel_size, open_kernel_size))
    close_kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (close_kernel_size, close_kernel_size))

    white_mask = cv2.morphologyEx(white_mask, cv2.MORPH_OPEN, open_kernel)
    white_mask = cv2.morphologyEx(white_mask, cv2.MORPH_CLOSE, close_kernel)
    return white_mask


def build_grabcut_board_mask(image, white_mask):
    height, width = image.shape[:2]
    grabcut_mask = np.full((height, width), cv2.GC_PR_BGD, dtype=np.uint8)

    border = max(20, min(height, width) // 20)
    grabcut_mask[:border, :] = cv2.GC_BGD
    grabcut_mask[-border:, :] = cv2.GC_BGD
    grabcut_mask[:, :border] = cv2.GC_BGD
    grabcut_mask[:, -border:] = cv2.GC_BGD

    x1, x2 = int(width * 0.2), int(width * 0.8)
    y1, y2 = int(height * 0.15), int(height * 0.9)
    central_window = np.zeros((height, width), dtype=np.uint8)
    central_window[y1:y2, x1:x2] = 255

    probable_foreground = (white_mask > 0) & (central_window > 0)
    grabcut_mask[probable_foreground] = cv2.GC_PR_FGD

    background_model = np.zeros((1, 65), dtype=np.float64)
    foreground_model = np.zeros((1, 65), dtype=np.float64)
    cv2.setRNGSeed(0)
    cv2.grabCut(
        image,
        grabcut_mask,
        None,
        background_model,
        foreground_model,
        2,
        cv2.GC_INIT_WITH_MASK,
    )

    board_mask = np.where(
        (grabcut_mask == cv2.GC_FGD) | (grabcut_mask == cv2.GC_PR_FGD),
        255,
        0,
    ).astype(np.uint8)

    kernel = odd_kernel_size(min(height, width) // 40)
    open_kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (kernel, kernel))
    close_kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (kernel * 2 + 1, kernel * 2 + 1))

    board_mask = cv2.morphologyEx(board_mask, cv2.MORPH_OPEN, open_kernel)
    board_mask = cv2.morphologyEx(board_mask, cv2.MORPH_CLOSE, close_kernel)
    return board_mask


def is_high_confidence_white_board(contour, image):
    height, width = image.shape[:2]
    image_area = height * width
    contour_area = cv2.contourArea(contour)
    area_ratio = contour_area / image_area

    filled_mask = np.zeros((height, width), dtype=np.uint8)
    cv2.drawContours(filled_mask, [contour], -1, 255, thickness=cv2.FILLED)
    outside_mask = filled_mask == 0
    outside_ratio = float(np.count_nonzero(outside_mask)) / image_area

    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    outside_saturation = float(hsv[:, :, 1][outside_mask].mean()) if np.any(outside_mask) else 0.0

    return (
        area_ratio >= 0.85
        or (area_ratio >= 0.55 and outside_ratio >= 0.002 and outside_saturation >= 90.0)
    )


def build_board_mask(image):
    white_mask = build_white_board_mask(image)
    contours, _ = cv2.findContours(white_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return build_grabcut_board_mask(image, white_mask)

    white_contour = max(contours, key=cv2.contourArea)
    if is_high_confidence_white_board(white_contour, image):
        filled_mask = np.zeros_like(white_mask)
        cv2.drawContours(filled_mask, [white_contour], -1, 255, thickness=cv2.FILLED)
        return filled_mask

    return build_grabcut_board_mask(image, white_mask)


def build_board_candidate_masks(image):
    white_mask = build_white_board_mask(image)
    candidate_masks = [white_mask]

    close_kernel_size = odd_kernel_size(min(image.shape[:2]) // 20, minimum=35)
    close_kernel = cv2.getStructuringElement(
        cv2.MORPH_ELLIPSE,
        (close_kernel_size, close_kernel_size),
    )
    candidate_masks.append(cv2.morphologyEx(white_mask, cv2.MORPH_CLOSE, close_kernel))
    candidate_masks.append(build_board_mask(image))

    unique_masks = []
    seen_signatures = set()
    for mask in candidate_masks:
        signature = (mask.shape, int(np.count_nonzero(mask)))
        if signature in seen_signatures:
            continue
        seen_signatures.add(signature)
        unique_masks.append(mask)

    return unique_masks


def find_best_board_polygon_in_image(image):
    best_polygon = None
    best_score = -1.0
    image_area = image.shape[0] * image.shape[1]

    for mask in build_board_candidate_masks(image):
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        candidate_contours = list(contours)

        recovered_mask = recover_board_mask_from_core(mask)
        if recovered_mask is not None:
            recovered_contours, _ = cv2.findContours(
                recovered_mask,
                cv2.RETR_EXTERNAL,
                cv2.CHAIN_APPROX_SIMPLE,
            )
            candidate_contours.extend(recovered_contours)

        candidate_contours = sorted(candidate_contours, key=cv2.contourArea, reverse=True)[:8]

        for contour in candidate_contours:
            if cv2.contourArea(contour) < 0.03 * image_area:
                continue

            polygon = fit_board_quadrilateral(contour)
            ordered_polygon = order_corners(polygon.reshape(-1, 2)).astype(np.float32)
            score = score_board_candidate(image, contour, ordered_polygon)
            if score > best_score:
                best_score = score
                best_polygon = ordered_polygon

    return best_polygon


def recover_board_mask_from_core(board_mask):
    height, width = board_mask.shape[:2]
    image_area = height * width
    erode_kernel_size = odd_kernel_size(min(height, width) // 38, minimum=21)
    erode_kernel = cv2.getStructuringElement(
        cv2.MORPH_ELLIPSE,
        (erode_kernel_size, erode_kernel_size),
    )
    eroded_mask = cv2.erode(board_mask, erode_kernel)
    contours, _ = cv2.findContours(eroded_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    significant_contours = [
        contour
        for contour in contours
        if cv2.contourArea(contour) >= max(2500, image_area * 0.015)
    ]
    if len(significant_contours) < 2:
        return None

    image_center = np.array([width / 2.0, height / 2.0], dtype=np.float32)
    max_distance = float(np.linalg.norm(image_center)) or 1.0
    best_contour = max(
        significant_contours,
        key=lambda contour: (
            cv2.contourArea(contour) / image_area
            - 0.65 * np.linalg.norm(contour_center(contour) - image_center) / max_distance
        ),
    )

    recovered_mask = np.zeros_like(board_mask)
    cv2.drawContours(recovered_mask, [best_contour], -1, 255, thickness=cv2.FILLED)

    dilate_kernel = cv2.getStructuringElement(
        cv2.MORPH_ELLIPSE,
        (erode_kernel_size, erode_kernel_size),
    )
    close_kernel_size = odd_kernel_size(erode_kernel_size * 2 - 1, minimum=31)
    close_kernel = cv2.getStructuringElement(
        cv2.MORPH_ELLIPSE,
        (close_kernel_size, close_kernel_size),
    )

    recovered_mask = cv2.dilate(recovered_mask, dilate_kernel)
    recovered_mask = cv2.bitwise_and(recovered_mask, board_mask)
    recovered_mask = cv2.morphologyEx(recovered_mask, cv2.MORPH_CLOSE, close_kernel)
    return recovered_mask

def refine_board_polygon(image, polygon, iterations=1):
    current_polygon = order_corners(polygon).astype(np.float32)
    image_area = image.shape[0] * image.shape[1]

    for _ in range(iterations):
        unwrapped, transform, _ = unwrap_board(
            image,
            polygon=current_polygon,
            force_square=True,
            output_size=DEFAULT_UNWRAP_SIZE,
        )
        if unwrapped is None:
            break

        white_mask = build_white_board_mask(unwrapped)
        contours, _ = cv2.findContours(white_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if not contours:
            break

        board_contour = max(contours, key=cv2.contourArea)
        if cv2.contourArea(board_contour) < 0.15 * white_mask.shape[0] * white_mask.shape[1]:
            break

        refined_polygon = fit_board_quadrilateral(board_contour)
        refined_polygon = order_corners(refined_polygon.reshape(-1, 2)).astype(np.float32)
        inverse_transform = np.linalg.inv(transform)
        candidate_polygon = project_points(refined_polygon, inverse_transform)
        candidate_polygon = order_corners(candidate_polygon).astype(np.float32)

        if polygon_area(candidate_polygon) < 0.05 * image_area:
            break
        if not cv2.isContourConvex(np.round(candidate_polygon).astype(np.int32).reshape(-1, 1, 2)):
            break

        current_polygon = candidate_polygon

    return np.round(current_polygon).astype(np.int32)


def detect_board_polygon(image):
    detection_image, scale = resize_for_detection(image)
    polygon = find_best_board_polygon_in_image(detection_image)
    if polygon is None:
        return None

    ordered = order_corners(polygon.reshape(-1, 2)) / scale
    refined_polygon = refine_board_polygon(image, ordered, iterations=1)
    return refined_polygon


def unwrap_board(image, polygon=None, force_square=True, output_size=None):
    if polygon is None:
        polygon = detect_board_polygon(image)
    if polygon is None:
        return None, None, None

    source = order_corners(polygon).astype(np.float32)
    if output_size is None:
        output_width, output_height = infer_unwrap_size(source, force_square=force_square)
    else:
        output_width, output_height = output_size

    destination = np.array(
        [
            [0, 0],
            [output_width - 1, 0],
            [output_width - 1, output_height - 1],
            [0, output_height - 1],
        ],
        dtype=np.float32,
    )

    transform = cv2.getPerspectiveTransform(source, destination)
    unwrapped = cv2.warpPerspective(image, transform, (output_width, output_height))
    return unwrapped, transform, source


def build_crab_mask(unwrapped_image):
    height, width = unwrapped_image.shape[:2]
    hsv = cv2.cvtColor(unwrapped_image, cv2.COLOR_BGR2HSV)
    gray = cv2.cvtColor(unwrapped_image, cv2.COLOR_BGR2GRAY)

    _, saturation, value = cv2.split(hsv)
    blur_sigma = max(15.0, min(height, width) / 28.0)
    local_background = cv2.GaussianBlur(gray, (0, 0), blur_sigma)
    local_darkness = cv2.subtract(local_background, gray)

    crab_mask = np.where(
        (saturation > 40) | (local_darkness > 22) | (value < 120),
        255,
        0,
    ).astype(np.uint8)

    open_kernel_size = odd_kernel_size(min(height, width) // 220, minimum=3)
    close_kernel_size = odd_kernel_size(min(height, width) // 55, minimum=9)
    open_kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (open_kernel_size, open_kernel_size))
    close_kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (close_kernel_size, close_kernel_size))

    crab_mask = cv2.morphologyEx(crab_mask, cv2.MORPH_OPEN, open_kernel)
    crab_mask = cv2.morphologyEx(crab_mask, cv2.MORPH_CLOSE, close_kernel)
    return crab_mask


def project_points(points, transform):
    points = np.asarray(points, dtype=np.float32).reshape(-1, 1, 2)
    return cv2.perspectiveTransform(points, transform).reshape(-1, 2)


def extract_largest_contour(mask):
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None
    return max(contours, key=cv2.contourArea)


def build_crop_crab_mask(crop_image):
    hsv = cv2.cvtColor(crop_image, cv2.COLOR_BGR2HSV)
    gray = cv2.cvtColor(crop_image, cv2.COLOR_BGR2GRAY)

    _, saturation, value = cv2.split(hsv)
    blur_sigma = max(5.0, min(crop_image.shape[:2]) / 18.0)
    local_background = cv2.GaussianBlur(gray, (0, 0), blur_sigma)
    local_darkness = cv2.subtract(local_background, gray)

    crop_mask = np.where(
        (saturation > 35) | (local_darkness > 18) | (value < 150),
        255,
        0,
    ).astype(np.uint8)

    crop_mask = cv2.morphologyEx(
        crop_mask,
        cv2.MORPH_OPEN,
        cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3)),
    )
    crop_mask = cv2.morphologyEx(
        crop_mask,
        cv2.MORPH_CLOSE,
        cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 7)),
    )
    return crop_mask


def select_crab_color_mask(crop_image, crab_mask):
    hsv = cv2.cvtColor(crop_image, cv2.COLOR_BGR2HSV)
    saturation = hsv[:, :, 1]
    value = hsv[:, :, 2]

    crab_pixels = crab_mask > 0
    color_pixels = crab_pixels & ((saturation > 22) | (value < 210)) & (value < 248)
    if np.count_nonzero(color_pixels) < max(20, int(np.count_nonzero(crab_pixels) * 0.18)):
        color_pixels = crab_pixels

    return np.where(color_pixels, 255, 0).astype(np.uint8)


def crab_color_stats(crop_image, crab_mask):
    color_mask = select_crab_color_mask(crop_image, crab_mask)
    mean_bgr = cv2.mean(crop_image, mask=color_mask)[:3]
    hsv = cv2.cvtColor(crop_image, cv2.COLOR_BGR2HSV)
    mean_hsv = cv2.mean(hsv, mask=color_mask)[:3]
    lab = cv2.cvtColor(crop_image, cv2.COLOR_BGR2LAB)
    mean_lab = cv2.mean(lab, mask=color_mask)[:3]

    bgr = np.asarray(mean_bgr, dtype=np.float32)
    lab_values = np.asarray(mean_lab, dtype=np.float32)
    color_vector = np.array(
        [
            lab_values[1],
            lab_values[2],
            bgr[2] - bgr[0],
            bgr[2] - bgr[1],
        ],
        dtype=np.float32,
    )

    return {
        "mean_bgr": mean_bgr,
        "mean_hsv": mean_hsv,
        "mean_lab": mean_lab,
        "color_vector": color_vector,
        "color_mask": color_mask,
    }


def estimate_board_white_balance_gains(unwrapped_image, crab_mask=None):
    height, width = unwrapped_image.shape[:2]
    hsv = cv2.cvtColor(unwrapped_image, cv2.COLOR_BGR2HSV)
    lab = cv2.cvtColor(unwrapped_image, cv2.COLOR_BGR2LAB)

    saturation = hsv[:, :, 1]
    value = hsv[:, :, 2]
    lightness = lab[:, :, 0]
    background_mask = np.full((height, width), 255, dtype=np.uint8)
    if crab_mask is not None:
        avoid_kernel_size = odd_kernel_size(min(height, width) // 80, minimum=9)
        avoid_kernel = cv2.getStructuringElement(
            cv2.MORPH_ELLIPSE,
            (avoid_kernel_size, avoid_kernel_size),
        )
        avoid_mask = cv2.dilate(crab_mask, avoid_kernel)
        background_mask = cv2.bitwise_not(avoid_mask)

    background_pixels = background_mask > 0
    if np.count_nonzero(background_pixels) < max(100, int(height * width * 0.05)):
        background_pixels = np.ones((height, width), dtype=bool)

    background_value = value[background_pixels]
    background_lightness = lightness[background_pixels]
    value_floor = max(105.0, float(np.percentile(background_value, 60)))
    lightness_floor = max(115.0, float(np.percentile(background_lightness, 55)))
    saturation_ceiling = min(115.0, max(55.0, float(np.percentile(saturation[background_pixels], 45) + 35.0)))

    white_pixels = (
        background_pixels
        & (value >= value_floor)
        & (lightness >= lightness_floor)
        & (saturation <= saturation_ceiling)
    )
    if np.count_nonzero(white_pixels) < max(80, int(height * width * 0.015)):
        white_pixels = background_pixels & (value >= value_floor) & (saturation <= 140)
    if np.count_nonzero(white_pixels) < max(80, int(height * width * 0.01)):
        return np.ones(3, dtype=np.float32)

    white_samples = unwrapped_image[white_pixels].astype(np.float32)
    white_bgr = np.percentile(white_samples, 82, axis=0)
    target = float(np.mean(white_bgr))
    if target <= 1.0:
        return np.ones(3, dtype=np.float32)

    gains = target / np.maximum(white_bgr, 1.0)
    return np.clip(gains, 0.55, 2.65).astype(np.float32)


def apply_channel_gains(image, gains):
    corrected = image.astype(np.float32) * np.asarray(gains, dtype=np.float32).reshape(1, 1, 3)
    return np.clip(corrected, 0, 255).astype(np.uint8)


def load_reference_models():
    cached_models = getattr(load_reference_models, "_cache", None)
    if cached_models is not None:
        return cached_models

    reference_models = {}
    for label, image_path in REFERENCE_IMAGE_PATHS.items():
        image = cv2.imread(str(image_path))
        if image is None:
            raise FileNotFoundError(f"Could not read reference image at {image_path}")

        mask = build_crop_crab_mask(image)
        contour = extract_largest_contour(mask)
        if contour is None:
            raise RuntimeError(f"Could not extract a contour from reference image {image_path}")

        stats = crab_color_stats(image, mask)
        reference_models[label] = {
            "contour": contour,
            "mean_lab": stats["mean_lab"],
            "color_vector": stats["color_vector"],
            "path": str(image_path),
        }

    load_reference_models._cache = reference_models
    return reference_models


def classify_crab_crop(crop_image):
    mask = build_crop_crab_mask(crop_image)
    contour = extract_largest_contour(mask)
    if contour is None:
        return {
            "label": "other",
            "is_european_green": False,
            "shape_scores": {},
        }

    reference_models = load_reference_models()
    stats = crab_color_stats(crop_image, mask)
    mean_bgr = stats["mean_bgr"]
    mean_hsv = stats["mean_hsv"]
    mean_lab = stats["mean_lab"]
    shape_scores = {
        label: cv2.matchShapes(contour, reference["contour"], cv2.CONTOURS_MATCH_I1, 0.0)
        for label, reference in reference_models.items()
    }
    color_scores = {
        label: float(np.linalg.norm(stats["color_vector"] - np.asarray(reference["color_vector"])))
        for label, reference in reference_models.items()
    }

    combined_scores = {
        label: color_scores[label] + 8.0 * shape_scores[label]
        for label in reference_models
    }
    best_label = min(combined_scores, key=combined_scores.get)
    non_green_label = min(
        ("native_rock", "jonah"),
        key=lambda label: combined_scores[label],
    )
    red_minus_blue = mean_bgr[2] - mean_bgr[0]
    red_minus_green = mean_bgr[2] - mean_bgr[1]
    non_green_score = combined_scores[non_green_label]
    green_score = combined_scores["european_green"]
    warm_rock_signal = mean_lab[1] >= 136.0 or red_minus_blue >= 18.0 or red_minus_green >= 22.0
    green_color_signal = mean_lab[1] <= 131.5 and red_minus_blue <= 13.0 and red_minus_green <= 19.0
    best_other_shape_label = min(
        ("native_rock", "jonah"),
        key=lambda label: shape_scores[label],
    )
    other_shape_advantage = shape_scores["european_green"] - shape_scores[best_other_shape_label]
    other_shape_signal = other_shape_advantage >= 0.18 and mean_lab[1] >= 132.0
    is_european_green = (
        not warm_rock_signal
        and not (other_shape_signal and not green_color_signal)
        and (
            green_score <= non_green_score * 0.92
            or (green_color_signal and green_score <= non_green_score + 18.0)
        )
    )

    return {
        "label": "european_green" if is_european_green else non_green_label,
        "is_european_green": is_european_green,
        "shape_scores": shape_scores,
        "color_scores": color_scores,
        "combined_scores": combined_scores,
        "mean_bgr": mean_bgr,
        "mean_hsv": mean_hsv,
        "mean_lab": mean_lab,
        "red_minus_blue": red_minus_blue,
        "red_minus_green": red_minus_green,
        "other_shape_signal": other_shape_signal,
        "best_other_shape_label": best_other_shape_label,
    }


def detect_crabs_in_unwrapped(unwrapped_image):
    crab_mask = build_crab_mask(unwrapped_image)
    height, width = crab_mask.shape[:2]
    image_area = height * width
    border_margin = max(2, min(height, width) // 100)
    min_component_area = max(1200, int(image_area * 0.003))
    max_component_area = int(image_area * 0.09)

    component_count, _, stats, _ = cv2.connectedComponentsWithStats(crab_mask, 8)
    detections = []
    for component_index in range(1, component_count):
        x, y, box_width, box_height, area = stats[component_index]
        touches_border = (
            x <= border_margin
            or y <= border_margin
            or x + box_width >= width - border_margin
            or y + box_height >= height - border_margin
        )
        if touches_border:
            plausible_edge_crab = (
                area >= min_component_area * 1.35
                and box_width <= width * 0.46
                and box_height <= height * 0.46
                and box_width >= 45
                and box_height >= 45
            )
            if not plausible_edge_crab:
                continue
        if area < min_component_area or area > max_component_area:
            continue
        if box_width < 25 or box_height < 25:
            continue

        box = np.array([x, y, box_width, box_height], dtype=np.int32)
        quadrilateral = np.array(
            [
                [x, y],
                [x + box_width, y],
                [x + box_width, y + box_height],
                [x, y + box_height],
            ],
            dtype=np.float32,
        )
        detections.append(
            {
                "box": box,
                "quad": quadrilateral,
                "area": int(area),
            }
        )

    detections.sort(key=lambda detection: (int(detection["box"][1]), int(detection["box"][0])))
    return detections, crab_mask


def axis_aligned_quad_from_box(box):
    x, y, width, height = [int(value) for value in box]
    return np.array(
        [
            [x, y],
            [x + width, y],
            [x + width, y + height],
            [x, y + height],
        ],
        dtype=np.int32,
    )


def build_species_counts(detections):
    counts = {label: 0 for label in REFERENCE_IMAGE_PATHS}
    counts["other"] = 0
    for detection in detections:
        label = detection.get("classification", {}).get("label", "other")
        counts[label if label in counts else "other"] += 1
    return counts


def build_reference_copy_color_mask(image):
    height, width = image.shape[:2]
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    lab = cv2.cvtColor(image, cv2.COLOR_BGR2LAB)

    saturation = hsv[:, :, 1].astype(np.float32)
    lightness = lab[:, :, 0].astype(np.float32)
    channel_a = lab[:, :, 1].astype(np.float32)
    channel_b = lab[:, :, 2].astype(np.float32)

    blur_sigma = max(25.0, min(height, width) / 22.0)
    background_lightness = cv2.GaussianBlur(lightness, (0, 0), blur_sigma)
    background_a = cv2.GaussianBlur(channel_a, (0, 0), blur_sigma)
    background_b = cv2.GaussianBlur(channel_b, (0, 0), blur_sigma)
    background_saturation = cv2.GaussianBlur(saturation, (0, 0), blur_sigma)

    local_darkness = np.maximum(background_lightness - lightness, 0.0)
    chroma_delta = np.sqrt(
        (channel_a - background_a) ** 2
        + (channel_b - background_b) ** 2
    )
    saturation_delta = np.maximum(saturation - background_saturation, 0.0)
    foreground_score = np.maximum.reduce(
        [
            local_darkness * 5.0,
            chroma_delta * 7.0,
            saturation_delta * 4.0,
            saturation * 1.4,
        ]
    )
    foreground_score = cv2.GaussianBlur(foreground_score, (3, 3), 0)

    threshold = max(28.0, float(np.percentile(foreground_score, 86)))
    foreground_mask = np.where(foreground_score > threshold, 255, 0).astype(np.uint8)
    foreground_mask = cv2.morphologyEx(
        foreground_mask,
        cv2.MORPH_OPEN,
        cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3)),
    )
    foreground_mask = cv2.morphologyEx(
        foreground_mask,
        cv2.MORPH_CLOSE,
        cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (13, 13)),
    )
    return foreground_mask


def extract_reference_copy_candidate_boxes(mask):
    height, width = mask.shape[:2]
    image_area = height * width
    min_area = max(900, int(image_area * 0.00065))
    max_area = int(image_area * 0.04)
    min_width = max(22, int(max(height, width) * 0.020))
    min_height = max(18, int(max(height, width) * 0.018))

    component_count, _, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
    boxes = []
    for component_index in range(1, component_count):
        x, y, box_width, box_height, area = [
            int(value) for value in stats[component_index]
        ]
        if area < min_area or area > max_area:
            continue
        if box_width < min_width or box_height < min_height:
            continue
        if x <= 4 or x + box_width >= width - 4 or y + box_height >= height - 4:
            continue

        aspect_ratio = box_width / max(1, box_height)
        fill_ratio = area / float(max(1, box_width * box_height))
        if aspect_ratio > 3.2 or aspect_ratio < 0.22:
            continue
        if fill_ratio < 0.16:
            continue
        if (box_width > width * 0.28 or box_height > height * 0.42) and fill_ratio < 0.25:
            continue

        boxes.append(
            {
                "box": np.array([x, y, box_width, box_height], dtype=np.int32),
                "area": int(area),
            }
        )
    return boxes


def box_intersection_area(box_a, box_b):
    ax, ay, aw, ah = [int(value) for value in box_a]
    bx, by, bw, bh = [int(value) for value in box_b]
    x1 = max(ax, bx)
    y1 = max(ay, by)
    x2 = min(ax + aw, bx + bw)
    y2 = min(ay + ah, by + bh)
    return max(0, x2 - x1) * max(0, y2 - y1)


def merge_reference_copy_candidates(candidates):
    sorted_candidates = sorted(
        candidates,
        key=lambda candidate: int(candidate["area"]),
        reverse=True,
    )
    groups = []
    for candidate in sorted_candidates:
        candidate_box = candidate["box"]
        candidate_area = int(candidate_box[2] * candidate_box[3])
        matched_group = None
        for group in groups:
            for existing in group:
                existing_box = existing["box"]
                overlap = box_intersection_area(candidate_box, existing_box)
                existing_area = int(existing_box[2] * existing_box[3])
                if overlap > 0.35 * min(candidate_area, existing_area):
                    matched_group = group
                    break
            if matched_group is not None:
                break
        if matched_group is None:
            groups.append([candidate])
        else:
            matched_group.append(candidate)

    merged = []
    for group in groups:
        x1 = min(int(candidate["box"][0]) for candidate in group)
        y1 = min(int(candidate["box"][1]) for candidate in group)
        x2 = max(int(candidate["box"][0] + candidate["box"][2]) for candidate in group)
        y2 = max(int(candidate["box"][1] + candidate["box"][3]) for candidate in group)
        merged.append(
            {
                "box": np.array([x1, y1, x2 - x1, y2 - y1], dtype=np.int32),
                "area": int(sum(int(candidate["area"]) for candidate in group)),
            }
        )
    return merged


def select_main_candidate_cluster(candidates, image_shape):
    if len(candidates) <= 2:
        return candidates

    height, width = image_shape[:2]
    distance_threshold = max(160.0, 0.18 * max(height, width))
    centers = [
        np.array(
            [
                float(candidate["box"][0] + candidate["box"][2] / 2.0),
                float(candidate["box"][1] + candidate["box"][3] / 2.0),
            ],
            dtype=np.float32,
        )
        for candidate in candidates
    ]

    seen = [False] * len(candidates)
    components = []
    for start_index in range(len(candidates)):
        if seen[start_index]:
            continue
        stack = [start_index]
        seen[start_index] = True
        component = []
        while stack:
            index = stack.pop()
            component.append(index)
            for neighbor_index in range(len(candidates)):
                if seen[neighbor_index]:
                    continue
                if (
                    np.linalg.norm(centers[index] - centers[neighbor_index])
                    <= distance_threshold
                ):
                    seen[neighbor_index] = True
                    stack.append(neighbor_index)
        components.append(component)

    best_component = max(
        components,
        key=lambda component: (
            len(component),
            sum(int(candidates[index]["area"]) for index in component),
        ),
    )
    if len(best_component) < 3:
        return candidates
    return [candidates[index] for index in best_component]


def build_reference_copy_candidate_mask(image_shape, masks, candidates):
    candidate_mask = np.zeros(image_shape[:2], dtype=np.uint8)
    combined_mask = np.zeros_like(candidate_mask)
    for mask in masks:
        combined_mask = cv2.bitwise_or(combined_mask, mask)

    for candidate in candidates:
        x, y, box_width, box_height = [int(value) for value in candidate["box"]]
        crop_mask = combined_mask[y : y + box_height, x : x + box_width]
        if np.count_nonzero(crop_mask) == 0:
            candidate_mask[y : y + box_height, x : x + box_width] = 255
        else:
            candidate_mask[y : y + box_height, x : x + box_width] = crop_mask
    return candidate_mask


def load_reference_copy_feature_models():
    cached_models = getattr(load_reference_copy_feature_models, "_cache", None)
    if cached_models is not None:
        return cached_models

    if hasattr(cv2, "SIFT_create"):
        detector = cv2.SIFT_create(
            nfeatures=1800,
            contrastThreshold=0.006,
            edgeThreshold=12,
        )
        descriptor_kind = "sift"
    else:
        detector = cv2.ORB_create(nfeatures=1800)
        descriptor_kind = "orb"

    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    reference_models = {}
    for label, image_path in REFERENCE_IMAGE_PATHS.items():
        image = cv2.imread(str(image_path))
        if image is None:
            raise FileNotFoundError(f"Could not read reference image at {image_path}")
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        enhanced = clahe.apply(gray)
        keypoints, descriptors = detector.detectAndCompute(enhanced, None)
        reference_models[label] = {
            "image": image,
            "keypoints": keypoints,
            "descriptors": descriptors,
        }

    cached_models = {
        "detector": detector,
        "descriptor_kind": descriptor_kind,
        "clahe": clahe,
        "references": reference_models,
    }
    load_reference_copy_feature_models._cache = cached_models
    return cached_models


def match_reference_descriptors(reference_descriptors, crop_descriptors, descriptor_kind):
    if reference_descriptors is None or crop_descriptors is None:
        return []
    if len(reference_descriptors) < 2 or len(crop_descriptors) < 2:
        return []

    if descriptor_kind == "sift":
        matcher = cv2.FlannBasedMatcher(
            {"algorithm": 1, "trees": 5},
            {"checks": 80},
        )
    else:
        matcher = cv2.BFMatcher(cv2.NORM_HAMMING)

    try:
        return matcher.knnMatch(reference_descriptors, crop_descriptors, k=2)
    except cv2.error:
        return []


def score_candidate_reference_features(crop_image):
    feature_models = load_reference_copy_feature_models()
    gray = cv2.cvtColor(crop_image, cv2.COLOR_BGR2GRAY)
    enhanced = feature_models["clahe"].apply(gray)
    crop_keypoints, crop_descriptors = feature_models["detector"].detectAndCompute(
        enhanced,
        None,
    )
    if crop_descriptors is None or len(crop_keypoints) < 4:
        return {}

    descriptor_kind = feature_models["descriptor_kind"]
    scores = {}
    for label, reference in feature_models["references"].items():
        knn_matches = match_reference_descriptors(
            reference["descriptors"],
            crop_descriptors,
            descriptor_kind,
        )
        good_matches = []
        for pair in knn_matches:
            if len(pair) < 2:
                continue
            match, neighbor = pair
            if descriptor_kind == "sift":
                if match.distance < 0.80 * neighbor.distance and match.distance < 380:
                    good_matches.append(match)
            elif match.distance < 0.86 * neighbor.distance:
                good_matches.append(match)

        inlier_count = 0
        homography_area = 0.0
        homography_box = None
        is_valid = False
        if len(good_matches) >= 4:
            reference_points = np.float32(
                [reference["keypoints"][match.queryIdx].pt for match in good_matches]
            )
            crop_points = np.float32(
                [crop_keypoints[match.trainIdx].pt for match in good_matches]
            )
            homography, inlier_mask = cv2.findHomography(
                reference_points,
                crop_points,
                cv2.RANSAC,
                6.0,
            )
            if homography is not None and inlier_mask is not None:
                inlier_count = int(inlier_mask.sum())
                ref_height, ref_width = reference["image"].shape[:2]
                reference_corners = np.float32(
                    [
                        [0, 0],
                        [ref_width, 0],
                        [ref_width, ref_height],
                        [0, ref_height],
                    ]
                ).reshape(-1, 1, 2)
                projected_corners = cv2.perspectiveTransform(
                    reference_corners,
                    homography,
                ).reshape(-1, 2)
                homography_area = float(abs(cv2.contourArea(projected_corners)))
                homography_box = cv2.boundingRect(
                    projected_corners.astype(np.float32).reshape(-1, 1, 2)
                )
                box_x, box_y, box_width, box_height = homography_box
                crop_area = crop_image.shape[0] * crop_image.shape[1]
                is_valid = (
                    inlier_count >= 8
                    and homography_area >= 0.08 * crop_area
                    and homography_area <= 2.6 * crop_area
                    and box_width >= 0.25 * crop_image.shape[1]
                    and box_height >= 0.25 * crop_image.shape[0]
                    and box_x > -0.70 * crop_image.shape[1]
                    and box_y > -0.70 * crop_image.shape[0]
                    and box_x + box_width < 1.70 * crop_image.shape[1]
                    and box_y + box_height < 1.70 * crop_image.shape[0]
                )

        if is_valid:
            score = inlier_count * 2.0 + len(good_matches) * 0.15
        else:
            score = inlier_count * 0.25 + len(good_matches) * 0.03
        scores[label] = {
            "good_matches": len(good_matches),
            "inliers": inlier_count,
            "homography_area": homography_area,
            "homography_box": homography_box,
            "valid": is_valid,
            "score": float(score),
        }
    return scores


def classify_reference_copy_color(crop_image):
    color_mask = cv2.bitwise_or(
        build_reference_copy_color_mask(crop_image),
        build_crab_mask(crop_image),
    )
    color_pixels = color_mask > 0
    if np.count_nonzero(color_pixels) < max(20, int(crop_image.shape[0] * crop_image.shape[1] * 0.08)):
        color_pixels = np.ones(crop_image.shape[:2], dtype=bool)

    bgr_pixels = crop_image[color_pixels]
    hsv = cv2.cvtColor(crop_image, cv2.COLOR_BGR2HSV)
    lab = cv2.cvtColor(crop_image, cv2.COLOR_BGR2LAB)
    mean_bgr = bgr_pixels.mean(axis=0)
    mean_hsv = hsv[color_pixels].mean(axis=0)
    mean_lab = lab[color_pixels].mean(axis=0)
    red_minus_blue = float(mean_bgr[2] - mean_bgr[0])
    red_minus_green = float(mean_bgr[2] - mean_bgr[1])

    strong_jonah_signal = (
        (mean_lab[1] >= 133.8 and red_minus_green >= 4.0)
        or red_minus_blue >= -8.0
    )
    rock_signal = (
        mean_lab[2] >= 118.5
        or red_minus_blue >= -20.0
        or (red_minus_blue >= -28.0 and mean_lab[2] >= 117.2)
        or mean_lab[1] >= 131.5
    )

    if strong_jonah_signal:
        label = "jonah"
    elif rock_signal:
        label = "native_rock"
    else:
        label = "european_green"

    return label, {
        "mean_bgr": tuple(float(value) for value in mean_bgr),
        "mean_hsv": tuple(float(value) for value in mean_hsv),
        "mean_lab": tuple(float(value) for value in mean_lab),
        "red_minus_blue": red_minus_blue,
        "red_minus_green": red_minus_green,
        "strong_jonah_signal": bool(strong_jonah_signal),
        "rock_signal": bool(rock_signal),
    }


def expanded_box_crop(image, box, padding_fraction=0.35):
    x, y, box_width, box_height = [int(value) for value in box]
    padding = int(max(box_width, box_height) * padding_fraction)
    x1 = max(0, x - padding)
    y1 = max(0, y - padding)
    x2 = min(image.shape[1], x + box_width + padding)
    y2 = min(image.shape[0], y + box_height + padding)
    return image[y1:y2, x1:x2], (x1, y1)


def classify_reference_copy_candidate(image, box):
    crop_image, _ = expanded_box_crop(image, box)
    color_label, color_stats = classify_reference_copy_color(crop_image)
    feature_scores = score_candidate_reference_features(crop_image)

    label = color_label
    feature_label_is_decisive = False
    if feature_scores:
        ranked_scores = sorted(
            feature_scores.items(),
            key=lambda item: item[1]["score"],
            reverse=True,
        )
        best_label, best_score = ranked_scores[0]
        second_score = ranked_scores[1][1]["score"] if len(ranked_scores) > 1 else 0.0
        score_margin = best_score["score"] - second_score
        if best_score["valid"] and (best_score["inliers"] >= 14 or score_margin >= 8.0):
            label = best_label
            feature_label_is_decisive = (
                best_score["inliers"] >= 18
                or best_score["score"] >= 42.0
                or score_margin >= 18.0
            )

    if (
        color_label == "jonah"
        and color_stats["strong_jonah_signal"]
        and not feature_label_is_decisive
    ):
        label = "jonah"

    return {
        "label": label,
        "is_european_green": label == "european_green",
        "copy_feature_scores": feature_scores,
        "copy_color_label": color_label,
        "copy_color_stats": color_stats,
    }


def classify_board_crab_candidate(classification_image, box):
    x, y, box_width, box_height = [int(value) for value in box]
    crop_image = classification_image[y : y + box_height, x : x + box_width]
    classification = classify_crab_crop(crop_image)

    expanded_crop, _ = expanded_box_crop(classification_image, box, padding_fraction=0.35)
    color_label, color_stats = classify_reference_copy_color(expanded_crop)
    feature_scores = score_candidate_reference_features(expanded_crop)
    classification["board_color_label"] = color_label
    classification["board_color_stats"] = color_stats
    classification["board_feature_scores"] = feature_scores

    if not feature_scores:
        return classification

    ranked_scores = sorted(
        feature_scores.items(),
        key=lambda item: float(item[1].get("score", 0.0)),
        reverse=True,
    )
    if not ranked_scores:
        return classification

    best_label, best_score = ranked_scores[0]
    second_score_value = float(ranked_scores[1][1].get("score", 0.0)) if len(ranked_scores) > 1 else 0.0
    score_margin = float(best_score.get("score", 0.0)) - second_score_value
    best_inliers = int(best_score.get("inliers", 0))
    best_is_valid = bool(best_score.get("valid"))

    native_score = feature_scores.get("native_rock", {})
    native_is_valid = bool(native_score.get("valid"))
    native_score_value = float(native_score.get("score", 0.0))
    native_inliers = int(native_score.get("inliers", 0))

    label = classification.get("label", "other")
    if best_is_valid:
        if best_label == "european_green" and (
            best_inliers >= 20
            or float(best_score.get("score", 0.0)) >= 70.0
            or score_margin >= 25.0
        ):
            label = "european_green"
        elif best_label == "native_rock" and (
            best_inliers >= 16
            or float(best_score.get("score", 0.0)) >= 40.0
            or score_margin >= 18.0
        ):
            label = "native_rock"
        elif best_label == "jonah":
            strong_jonah = (
                best_inliers >= 28
                or float(best_score.get("score", 0.0)) >= 65.0
                or score_margin >= 35.0
                or (not native_is_valid and best_inliers >= 16)
            )
            ambiguous_with_rock = (
                color_label == "native_rock"
                and native_is_valid
                and float(best_score.get("score", 0.0)) < max(50.0, native_score_value * 1.75)
                and best_inliers < 24
            )
            if strong_jonah and not ambiguous_with_rock:
                label = "jonah"
            elif color_label == "native_rock" and (native_is_valid or label == "native_rock"):
                label = "native_rock"

    if label == "european_green" and color_label == "native_rock":
        if native_is_valid and (native_inliers >= 8 or native_score_value >= 18.0):
            label = "native_rock"
        elif best_is_valid and best_label in {"native_rock", "jonah"} and best_inliers >= 18:
            label = best_label

    classification["label"] = label
    classification["is_european_green"] = label == "european_green"
    return classification


def has_valid_reference_feature(classification, min_inliers=8):
    feature_scores = (
        classification.get("copy_feature_scores")
        or classification.get("board_feature_scores")
        or {}
    )
    return any(
        bool(score.get("valid")) and int(score.get("inliers", 0)) >= min_inliers
        for score in feature_scores.values()
    )


def detect_reference_copy_crabs(image):
    dark_mask = build_crab_mask(image)
    color_mask = build_reference_copy_color_mask(image)
    candidates = extract_reference_copy_candidate_boxes(dark_mask)
    candidates.extend(extract_reference_copy_candidate_boxes(color_mask))
    candidates = merge_reference_copy_candidates(candidates)
    candidates = select_main_candidate_cluster(candidates, image.shape)
    if not candidates:
        return None

    candidate_mask = build_reference_copy_candidate_mask(
        image.shape,
        [dark_mask, color_mask],
        candidates,
    )
    candidates = sorted(
        candidates,
        key=lambda candidate: (
            int(candidate["box"][1]),
            int(candidate["box"][0]),
        ),
    )

    detections = []
    for index, candidate in enumerate(candidates, start=1):
        box = candidate["box"].astype(np.int32)
        quadrilateral = axis_aligned_quad_from_box(box)
        classification = classify_reference_copy_candidate(image, box)
        detections.append(
            {
                "index": index,
                "unwrapped_box": box,
                "unwrapped_quad": quadrilateral.astype(np.float32),
                "original_quad": quadrilateral,
                "original_box": box.copy(),
                "area": int(candidate["area"]),
                "classification": classification,
            }
        )

    if len(detections) > 14:
        feature_backed_detections = [
            detection
            for detection in detections
            if has_valid_reference_feature(detection["classification"])
        ]
        if len(feature_backed_detections) < 3:
            return None

        detections = feature_backed_detections
        candidate_mask = np.zeros(image.shape[:2], dtype=np.uint8)
        for index, detection in enumerate(detections, start=1):
            detection["index"] = index
            x, y, box_width, box_height = [int(value) for value in detection["original_box"]]
            candidate_mask[y : y + box_height, x : x + box_width] = 255

    green_count = sum(
        1 for detection in detections if detection["classification"]["is_european_green"]
    )
    species_counts = build_species_counts(detections)
    return {
        "board_polygon": None,
        "board_polygon_source": "reference_copy",
        "detector": "reference_copy",
        "unwrapped_image": image.copy(),
        "classification_gains": np.ones(3, dtype=np.float32),
        "unwrapped_mask": candidate_mask,
        "transform": np.eye(3, dtype=np.float32),
        "detections": detections,
        "count": len(detections),
        "green_count": green_count,
        "other_count": len(detections) - green_count,
        "species_counts": species_counts,
    }


def detect_crabs(image, force_square=True, unwrap_size=DEFAULT_UNWRAP_SIZE, board_polygon=None):
    board_polygon_source = "manual" if board_polygon is not None else "auto"
    if board_polygon is None:
        board_polygon = detect_board_polygon(image)
    else:
        board_polygon = np.round(order_corners(board_polygon)).astype(np.int32)
    if board_polygon is None:
        return detect_reference_copy_crabs(image)

    unwrapped_image, transform, _ = unwrap_board(
        image,
        polygon=board_polygon,
        force_square=force_square,
        output_size=unwrap_size,
    )
    if unwrapped_image is None:
        return None

    detections, crab_mask = detect_crabs_in_unwrapped(unwrapped_image)
    classification_gains = estimate_board_white_balance_gains(unwrapped_image, crab_mask)
    classification_image = apply_channel_gains(unwrapped_image, classification_gains)
    inverse_transform = np.linalg.inv(transform)

    projected_detections = []
    for index, detection in enumerate(detections, start=1):
        x, y, box_width, box_height = detection["box"]
        classification = classify_board_crab_candidate(
            classification_image,
            detection["box"],
        )
        original_quad = project_points(detection["quad"], inverse_transform)
        rounded_quad = np.round(original_quad).astype(np.int32)
        original_box = np.array(cv2.boundingRect(rounded_quad.reshape(-1, 1, 2)), dtype=np.int32)

        projected_detections.append(
            {
                "index": index,
                "unwrapped_box": detection["box"],
                "unwrapped_quad": detection["quad"],
                "original_quad": rounded_quad,
                "original_box": original_box,
                "area": detection["area"],
                "classification": classification,
            }
        )

    green_count = sum(
        1 for detection in projected_detections if detection["classification"]["is_european_green"]
    )
    species_counts = build_species_counts(projected_detections)

    detection_result = {
        "board_polygon": board_polygon,
        "board_polygon_source": board_polygon_source,
        "detector": "board_unwrap",
        "unwrapped_image": unwrapped_image,
        "classification_gains": classification_gains,
        "unwrapped_mask": crab_mask,
        "transform": transform,
        "detections": projected_detections,
        "count": len(projected_detections),
        "green_count": green_count,
        "other_count": len(projected_detections) - green_count,
        "species_counts": species_counts,
    }
    board_area_ratio = polygon_area(board_polygon) / float(max(1, image.shape[0] * image.shape[1]))
    border_margin = max(4, min(image.shape[:2]) // 120)
    board_touches_image_border = bool(
        np.any(board_polygon[:, 0] <= border_margin)
        or np.any(board_polygon[:, 1] <= border_margin)
        or np.any(board_polygon[:, 0] >= image.shape[1] - border_margin)
        or np.any(board_polygon[:, 1] >= image.shape[0] - border_margin)
    )
    if (
        board_polygon_source == "auto"
        and board_area_ratio > 0.45
        and board_touches_image_border
    ):
        fallback_result = detect_reference_copy_crabs(image)
        if fallback_result is not None:
            return fallback_result
        return None

    if detection_result["count"] == 0 and board_polygon_source == "auto":
        fallback_result = detect_reference_copy_crabs(image)
        if fallback_result is not None:
            return fallback_result
    return detection_result


def draw_crab_detections(original_image, detection_result):
    annotated = original_image.copy()
    count = detection_result["count"]
    green_count = detection_result["green_count"]
    other_count = detection_result["other_count"]
    species_counts = detection_result.get("species_counts", {})

    board_polygon = detection_result.get("board_polygon")
    if board_polygon is not None:
        cv2.polylines(
            annotated,
            [board_polygon.reshape(-1, 1, 2)],
            True,
            (255, 255, 0),
            3,
        )
    put_readable_text(
        annotated,
        (
            f"Total: {count}  Green: {green_count}  "
            f"Jonah: {species_counts.get('jonah', 0)}  "
            f"Rock: {species_counts.get('native_rock', 0)}  "
            f"Non-green: {other_count}"
        ),
        (20, 40),
        0.9,
        2,
    )

    for detection in detection_result["detections"]:
        quadrilateral = detection["original_quad"]
        label = detection["classification"].get("label", "other")
        box_color = SPECIES_DRAW_COLORS.get(label, SPECIES_DRAW_COLORS["other"])
        label_text = f"{SPECIES_DISPLAY_NAMES.get(label, 'Other')} {detection['index']}"
        cv2.polylines(annotated, [quadrilateral.reshape(-1, 1, 2)], True, box_color, 3)

        label_anchor = quadrilateral[np.argmin(quadrilateral[:, 1] + quadrilateral[:, 0])]
        label_position = (int(label_anchor[0]), int(max(20, label_anchor[1] - 10)))
        cv2.putText(
            annotated,
            label_text,
            label_position,
            cv2.FONT_HERSHEY_SIMPLEX,
            0.65,
            (255, 0, 0),
            2,
        )

    return annotated


def put_readable_text(
    image,
    text,
    origin,
    font_scale,
    thickness=2,
    text_color=(0, 255, 0),
    background_color=(0, 0, 0),
):
    font = cv2.FONT_HERSHEY_SIMPLEX
    x, y = [int(value) for value in origin]
    (text_width, text_height), baseline = cv2.getTextSize(text, font, font_scale, thickness)
    padding = max(5, int(round(8 * font_scale)))
    top_left = (max(0, x - padding), max(0, y - text_height - padding))
    bottom_right = (
        min(image.shape[1] - 1, x + text_width + padding),
        min(image.shape[0] - 1, y + baseline + padding),
    )
    cv2.rectangle(image, top_left, bottom_right, background_color, cv2.FILLED)
    cv2.putText(image, text, (x, y), font, font_scale, text_color, thickness, cv2.LINE_AA)


def european_green_count(detection_result):
    species_counts = detection_result.get("species_counts", {})
    return int(species_counts.get("european_green", detection_result.get("green_count", 0)))


def draw_unwrapped_crab_detections(detection_result):
    annotated = detection_result["unwrapped_image"].copy()
    species_counts = detection_result.get("species_counts", {})
    put_readable_text(
        annotated,
        (
            f"Total: {detection_result['count']}  "
            f"Green: {detection_result['green_count']}  "
            f"Jonah: {species_counts.get('jonah', 0)}  "
            f"Rock: {species_counts.get('native_rock', 0)}"
        ),
        (20, 40),
        0.85,
        2,
    )

    for detection in detection_result["detections"]:
        x, y, box_width, box_height = detection["unwrapped_box"]
        label = detection["classification"].get("label", "other")
        box_color = SPECIES_DRAW_COLORS.get(label, SPECIES_DRAW_COLORS["other"])
        label_text = f"{SPECIES_DISPLAY_NAMES.get(label, 'Other')} {detection['index']}"
        cv2.rectangle(annotated, (x, y), (x + box_width, y + box_height), box_color, 3)
        cv2.putText(
            annotated,
            label_text,
            (int(x), int(max(20, y - 8))),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.65,
            (255, 0, 0),
            2,
        )

    return annotated


def draw_competition_green_crab_detections(detection_result):
    source_image = detection_result["unwrapped_image"]
    header_height = max(70, min(120, int(round(source_image.shape[0] * 0.12))))
    annotated = np.full(
        (source_image.shape[0] + header_height, source_image.shape[1], 3),
        (14, 18, 18),
        dtype=np.uint8,
    )
    annotated[header_height : header_height + source_image.shape[0], :] = source_image

    green_count = european_green_count(detection_result)
    count_text = f"European green crabs: {green_count}"
    font = cv2.FONT_HERSHEY_SIMPLEX
    font_scale = max(0.75, min(1.8, source_image.shape[1] / 520.0))
    thickness = max(2, int(round(font_scale * 2.4)))
    while font_scale > 0.55:
        (text_width, text_height), _ = cv2.getTextSize(count_text, font, font_scale, thickness)
        if text_width <= source_image.shape[1] - 40:
            break
        font_scale *= 0.9
        thickness = max(2, int(round(font_scale * 2.4)))

    (text_width, text_height), baseline = cv2.getTextSize(count_text, font, font_scale, thickness)
    text_x = max(20, (source_image.shape[1] - text_width) // 2)
    text_y = max(text_height + 10, (header_height + text_height) // 2)
    cv2.rectangle(
        annotated,
        (0, 0),
        (source_image.shape[1] - 1, header_height - 1),
        (7, 12, 11),
        cv2.FILLED,
    )
    cv2.putText(
        annotated,
        count_text,
        (text_x, text_y),
        font,
        font_scale,
        (70, 255, 120),
        thickness,
        cv2.LINE_AA,
    )
    cv2.line(
        annotated,
        (0, header_height - 1),
        (source_image.shape[1] - 1, header_height - 1),
        (70, 255, 120),
        2,
    )

    green_index = 1
    for detection in detection_result["detections"]:
        label = detection["classification"].get("label", "other")
        if label != "european_green":
            continue
        x, y, box_width, box_height = [int(value) for value in detection["unwrapped_box"]]
        y += header_height
        cv2.rectangle(
            annotated,
            (x, y),
            (x + box_width, y + box_height),
            SPECIES_DRAW_COLORS["european_green"],
            3,
        )
        put_readable_text(
            annotated,
            f"European green {green_index}",
            (int(x), int(max(header_height + 28, y - 8))),
            0.62,
            2,
            text_color=(70, 255, 120),
        )
        green_index += 1

    return annotated


def detection_summary_text(detection_result):
    species_counts = detection_result.get("species_counts", {})
    if species_counts:
        return (
            f"Detected {detection_result['count']} crabs total: "
            f"{species_counts.get('european_green', detection_result['green_count'])} European green, "
            f"{species_counts.get('jonah', 0)} Jonah, "
            f"{species_counts.get('native_rock', 0)} rock."
        )
    return (
        f"Detected {detection_result['count']} crabs total: "
        f"{detection_result['green_count']} European green, "
        f"{detection_result['other_count']} other."
    )


def competition_summary_text(detection_result):
    return f"European green crabs: {european_green_count(detection_result)}"


def render_detection_views(image, force_square=True, unwrap_size=DEFAULT_UNWRAP_SIZE, board_polygon=None):
    detection_result = detect_crabs(
        image,
        force_square=force_square,
        unwrap_size=unwrap_size,
        board_polygon=board_polygon,
    )
    if detection_result is None:
        return None, None, None
    annotated_original = draw_crab_detections(image, detection_result)
    annotated_unwrapped = draw_unwrapped_crab_detections(detection_result)
    return detection_result, annotated_original, annotated_unwrapped


def score_video_detection_result(detection_result):
    if detection_result is None:
        return (-1, -1, -1.0)
    species_counts = detection_result.get("species_counts", {})
    labeled_count = sum(
        species_counts.get(label, 0)
        for label in REFERENCE_IMAGE_PATHS
    )
    total_area = float(sum(int(detection.get("area", 0)) for detection in detection_result["detections"]))
    return (int(detection_result["count"]), int(labeled_count), total_area)


def detection_species_signature(detection_result):
    if detection_result is None:
        return (0, 0, 0, 0)
    species_counts = detection_result.get("species_counts", {})
    return (
        int(species_counts.get("european_green", detection_result.get("green_count", 0))),
        int(species_counts.get("jonah", 0)),
        int(species_counts.get("native_rock", 0)),
        int(species_counts.get("other", 0)),
    )


def detection_confidence_score(detection):
    classification = detection.get("classification", {})
    label = classification.get("label", "other")

    feature_scores = (
        classification.get("copy_feature_scores")
        or classification.get("board_feature_scores")
        or {}
    )
    if feature_scores:
        ranked_scores = sorted(
            feature_scores.items(),
            key=lambda item: float(item[1].get("score", 0.0)),
            reverse=True,
        )
        label_score = feature_scores.get(label) or (ranked_scores[0][1] if ranked_scores else {})
        best_score = float(label_score.get("score", 0.0))
        second_score = 0.0
        if ranked_scores:
            other_scores = [score for score_label, score in ranked_scores if score_label != label]
            if other_scores:
                second_score = float(other_scores[0].get("score", 0.0))

        inlier_score = min(1.0, float(label_score.get("inliers", 0)) / 35.0)
        match_score = min(1.0, float(label_score.get("good_matches", 0)) / 90.0)
        margin_score = min(1.0, max(0.0, best_score - second_score) / 45.0)
        validity_scale = 1.0 if label_score.get("valid") else 0.45
        valid_bonus = 0.20 if label_score.get("valid") else 0.0
        color_bonus = 0.12 if (
            classification.get("copy_color_label") == label
            or classification.get("board_color_label") == label
        ) else 0.0
        ranking_penalty = 0.0
        if ranked_scores:
            best_overall_label, best_overall_score = ranked_scores[0]
            if (
                best_overall_label != label
                and float(best_overall_score.get("score", 0.0)) > best_score + 4.0
            ):
                ranking_penalty = 0.12
        return float(
            np.clip(
                0.16
                + validity_scale * (0.30 * inlier_score + 0.18 * match_score)
                + 0.22 * margin_score
                + valid_bonus
                + color_bonus
                - ranking_penalty,
                0.0,
                1.0,
            )
        )

    combined_scores = classification.get("combined_scores") or {}
    if combined_scores:
        ranked_scores = sorted(combined_scores.items(), key=lambda item: float(item[1]))
        best_label, best_value = ranked_scores[0]
        if label != best_label and label in combined_scores:
            best_value = combined_scores[label]
        second_values = [float(value) for score_label, value in ranked_scores if score_label != label]
        second_value = second_values[0] if second_values else float(best_value)
        margin = max(0.0, second_value - float(best_value))
        normalized_margin = margin / max(1.0, second_value)
        return float(np.clip(0.35 + 0.65 * normalized_margin, 0.0, 1.0))

    return 0.35 if label != "other" else 0.15


def detection_result_confidence(detection_result):
    if detection_result is None or not detection_result.get("detections"):
        return 0.0
    scores = [
        detection_confidence_score(detection)
        for detection in detection_result["detections"]
    ]
    return float(np.mean(scores)) if scores else 0.0


def detection_visibility_score(frame, detection_result):
    if detection_result is None or not detection_result.get("detections"):
        return 0.0

    height, width = frame.shape[:2]
    image_area = max(1.0, float(height * width))
    margin = max(6, min(height, width) // 80)
    total_area = 0.0
    edge_penalty = 0.0
    for detection in detection_result["detections"]:
        x, y, box_width, box_height = [int(value) for value in detection["original_box"]]
        total_area += float(max(0, box_width) * max(0, box_height))
        if (
            x <= margin
            or y <= margin
            or x + box_width >= width - margin
            or y + box_height >= height - margin
        ):
            edge_penalty += 1.0

    area_ratio = total_area / image_area
    area_score = min(1.0, area_ratio / 0.11)
    edge_score = max(0.0, 1.0 - edge_penalty / max(1, len(detection_result["detections"])))
    return float(0.65 * area_score + 0.35 * edge_score)


def detection_sharpness_score(frame, detection_result):
    if detection_result is None or not detection_result.get("detections"):
        return 0.0

    height, width = frame.shape[:2]
    x1 = min(int(detection["original_box"][0]) for detection in detection_result["detections"])
    y1 = min(int(detection["original_box"][1]) for detection in detection_result["detections"])
    x2 = max(int(detection["original_box"][0] + detection["original_box"][2]) for detection in detection_result["detections"])
    y2 = max(int(detection["original_box"][1] + detection["original_box"][3]) for detection in detection_result["detections"])
    padding = max(12, int(max(x2 - x1, y2 - y1) * 0.12))
    x1 = max(0, x1 - padding)
    y1 = max(0, y1 - padding)
    x2 = min(width, x2 + padding)
    y2 = min(height, y2 + padding)
    if x2 <= x1 or y2 <= y1:
        return 0.0

    gray = cv2.cvtColor(frame[y1:y2, x1:x2], cv2.COLOR_BGR2GRAY)
    sharpness = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    return float(np.clip(sharpness / 140.0, 0.0, 1.0))


def build_video_sample_quality(frame, detection_result, max_reasonable_count=12):
    max_reasonable_count = max(1, int(max_reasonable_count))
    if detection_result is None:
        return {
            "count": 0,
            "signature": (0, 0, 0, 0),
            "confidence": 0.0,
            "sharpness": 0.0,
            "visibility": 0.0,
            "species_diversity": 0.0,
            "count_plausibility": 0.0,
            "over_count_penalty": 0.0,
            "quality": 0.0,
        }

    signature = detection_species_signature(detection_result)
    count = int(detection_result.get("count", 0))
    confidence = detection_result_confidence(detection_result)
    sharpness = detection_sharpness_score(frame, detection_result)
    visibility = detection_visibility_score(frame, detection_result)
    labeled_count = sum(signature[:3])
    present_species = sum(1 for species_count in signature[:3] if species_count > 0)
    species_diversity = present_species / 3.0 if labeled_count > 0 else 0.0

    if count <= max_reasonable_count:
        count_plausibility = 1.0
    else:
        excess = count - max_reasonable_count
        count_plausibility = max(0.0, 1.0 - excess / max(3.0, max_reasonable_count * 0.5))

    over_count_penalty = max(0, count - max_reasonable_count) * 12.0
    if detection_result.get("detector") == "reference_copy" and count > max_reasonable_count:
        over_count_penalty += 18.0

    quality = (
        4.0 * min(count, max_reasonable_count)
        + 2.0 * labeled_count
        + 4.0 * confidence
        + 1.0 * sharpness
        + 4.0 * visibility
        + 3.0 * species_diversity
        - over_count_penalty
    )
    return {
        "count": count,
        "signature": signature,
        "confidence": confidence,
        "sharpness": sharpness,
        "visibility": visibility,
        "species_diversity": float(species_diversity),
        "count_plausibility": float(count_plausibility),
        "over_count_penalty": float(over_count_penalty),
        "quality": float(quality),
    }


def choose_temporal_vote_sample(samples):
    valid_samples = [
        sample
        for sample in samples
        if sample.get("detection_result") is not None
        and sample.get("quality", {}).get("count", 0) > 0
    ]
    if not valid_samples:
        return None, None

    plausible_samples = [
        sample
        for sample in valid_samples
        if float(sample["quality"].get("count_plausibility", 1.0)) > 0.0
    ]
    voting_samples = plausible_samples or valid_samples

    peak_count = max(int(sample["quality"]["count"]) for sample in voting_samples)
    minimum_consensus_count = 1 if peak_count <= 3 else max(2, int(round(peak_count * 0.45)))
    eligible_samples = [
        sample
        for sample in voting_samples
        if int(sample["quality"]["count"]) >= minimum_consensus_count
    ]

    vote_rows = {}
    for sample in eligible_samples:
        signature = sample["quality"]["signature"]
        count = int(sample["quality"]["count"])
        green_count = int(signature[0])
        confidence = float(sample["quality"]["confidence"])
        quality = float(sample["quality"]["quality"])
        capped_count = min(count, peak_count)
        weight = (1.0 + confidence) * max(1, capped_count)
        weight += quality * 0.05
        weight += float(sample["quality"]["visibility"]) * 2.5
        weight += float(sample["quality"].get("species_diversity", 0.0)) * 5.0
        weight += green_count * 0.85
        if sample.get("detection_result", {}).get("detector") == "board_unwrap":
            weight += 3.0
        elif green_count == 0:
            weight -= 2.0
        row = vote_rows.setdefault(
            signature,
            {
                "signature": signature,
                "support_count": 0,
                "vote_weight": 0.0,
                "quality_sum": 0.0,
                "best_quality": 0.0,
                "sample_times": [],
            },
        )
        row["support_count"] += 1
        row["vote_weight"] += float(weight)
        row["quality_sum"] += quality
        row["best_quality"] = max(float(row["best_quality"]), quality)
        row["sample_times"].append(float(sample["time_seconds"]))

    winning_vote = max(
        vote_rows.values(),
        key=lambda row: (
            row["vote_weight"],
            row["support_count"],
            row["best_quality"],
            row["signature"][0],
            sum(row["signature"][:3]),
        ),
    )
    winning_samples = [
        sample
        for sample in eligible_samples
        if sample["quality"]["signature"] == winning_vote["signature"]
    ]
    best_sample = max(
        winning_samples,
        key=lambda sample: (
            sample["quality"]["quality"],
            sample["quality"]["confidence"],
            sample["quality"]["sharpness"],
        ),
    )
    times = winning_vote["sample_times"]
    temporal_vote = {
        "signature": winning_vote["signature"],
        "support_count": int(winning_vote["support_count"]),
        "eligible_count": len(eligible_samples),
        "sample_count": len(samples),
        "plausible_count": len(voting_samples),
        "vote_weight": float(winning_vote["vote_weight"]),
        "start_seconds": min(times) if times else None,
        "end_seconds": max(times) if times else None,
        "peak_count": int(peak_count),
        "selected_quality": best_sample["quality"],
    }
    return best_sample, temporal_vote


def detect_crabs_in_video(
    video_path,
    *,
    start_seconds=0.0,
    end_seconds=None,
    sample_interval_seconds=0.5,
    temporal_voting=True,
    max_reasonable_count=12,
    force_square=True,
    unwrap_size=DEFAULT_UNWRAP_SIZE,
):
    capture = cv2.VideoCapture(str(video_path))
    if not capture.isOpened():
        raise FileNotFoundError(f"Could not open video at {video_path}")

    fps = float(capture.get(cv2.CAP_PROP_FPS) or 0.0)
    frame_count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    duration_seconds = frame_count / fps if fps > 0 else None
    if end_seconds is None:
        end_seconds = duration_seconds
    if end_seconds is None:
        end_seconds = start_seconds

    start_seconds = max(0.0, float(start_seconds))
    end_seconds = max(start_seconds, float(end_seconds))
    sample_interval_seconds = max(0.05, float(sample_interval_seconds))

    best_sample = None
    samples = []
    current_time = start_seconds
    while current_time <= end_seconds + 1e-6:
        capture.set(cv2.CAP_PROP_POS_MSEC, current_time * 1000.0)
        ok, frame = capture.read()
        if not ok:
            break

        detection_result = detect_crabs(
            frame,
            force_square=force_square,
            unwrap_size=unwrap_size,
        )
        frame_index = int(capture.get(cv2.CAP_PROP_POS_FRAMES) - 1)
        sample = {
            "time_seconds": float(current_time),
            "frame_index": frame_index,
            "frame": frame,
            "detection_result": detection_result,
            "score": score_video_detection_result(detection_result),
            "quality": build_video_sample_quality(
                frame,
                detection_result,
                max_reasonable_count=max_reasonable_count,
            ),
        }
        samples.append(sample)
        if best_sample is None or sample["score"] > best_sample["score"]:
            best_sample = sample
        current_time += sample_interval_seconds

    capture.release()
    temporal_vote = None
    if temporal_voting:
        voted_sample, temporal_vote = choose_temporal_vote_sample(samples)
        if voted_sample is not None:
            best_sample = voted_sample

    if best_sample is None or best_sample["detection_result"] is None:
        return None

    return {
        "video_path": str(video_path),
        "fps": fps,
        "frame_count": frame_count,
        "duration_seconds": duration_seconds,
        "time_seconds": best_sample["time_seconds"],
        "frame_index": best_sample["frame_index"],
        "frame": best_sample["frame"],
        "detection_result": best_sample["detection_result"],
        "quality": best_sample.get("quality"),
        "temporal_vote": temporal_vote,
        "samples": samples,
    }


def draw_board_outline(image_path):
    image = cv2.imread(image_path)
    if image is None:
        print(f"Error: Could not read image at {image_path}")
        return

    polygon = detect_board_polygon(image)
    if polygon is None:
        print("Could not find a board-shaped white surface in the image.")
        return

    outlined = image.copy()
    cv2.polylines(outlined, [polygon.reshape(-1, 1, 2)], True, (0, 255, 0), 4)

    cv2.imshow("Detected Board", outlined)
    cv2.waitKey(0)
    cv2.destroyAllWindows()


def show_board_outline_and_unwrap(image_path, force_square=True):
    image = cv2.imread(image_path)
    if image is None:
        print(f"Error: Could not read image at {image_path}")
        return

    polygon = detect_board_polygon(image)
    if polygon is None:
        print("Could not find a board-shaped white surface in the image.")
        return

    outlined = image.copy()
    cv2.polylines(outlined, [polygon.reshape(-1, 1, 2)], True, (0, 255, 0), 4)

    unwrapped, _, _ = unwrap_board(image, polygon=polygon, force_square=force_square)
    if unwrapped is None:
        print("Could not unwrap the detected board.")
        return

    cv2.imshow("Detected Board", outlined)
    cv2.imshow("Unwrapped Board", unwrapped)
    cv2.waitKey(0)
    cv2.destroyAllWindows()


def show_crab_detections(image_path, force_square=True, unwrap_size=DEFAULT_UNWRAP_SIZE):
    image = cv2.imread(image_path)
    if image is None:
        print(f"Error: Could not read image at {image_path}")
        return

    detection_result = detect_crabs(image, force_square=force_square, unwrap_size=unwrap_size)
    if detection_result is None:
        print("Could not find the board or detect any crabs.")
        return

    annotated_original = draw_crab_detections(image, detection_result)
    annotated_unwrapped = draw_unwrapped_crab_detections(detection_result)

    print(
        "Detected "
        f"{detection_result['count']} crabs total: "
        f"{detection_result['green_count']} European green, "
        f"{detection_result['other_count']} other."
    )
    cv2.imshow("Original Image Crab Detections", annotated_original)
    cv2.imshow("Unwrapped Crab Detections", annotated_unwrapped)
    cv2.imshow("Crab Mask", detection_result["unwrapped_mask"])
    cv2.waitKey(0)
    cv2.destroyAllWindows()


def natural_case_sort_key(path):
    parts = re.split(r"(\d+)", path.stem.lower())
    parsed_parts = [int(part) if part.isdigit() else part for part in parts]
    return (path.parent.name.lower(), parsed_parts, path.suffix.lower())


def iter_case_paths():
    image_paths = []
    for folder_path in (
        ANALYSIS_DIR / "practice",
        ANALYSIS_DIR / "sample",
        REPO_ROOT / "practice",
        REPO_ROOT / "sample",
    ):
        if not folder_path.exists():
            continue
        for pattern in ("*.jpg", "*.jpeg", "*.png"):
            image_paths.extend(folder_path.glob(pattern))
    return sorted(image_paths, key=natural_case_sort_key)


def make_case_slug(image_path):
    raw_slug = f"{image_path.parent.name}_{image_path.stem}"
    return re.sub(r"[^A-Za-z0-9_-]+", "_", raw_slug).strip("_")


def save_case_outputs(image, image_path, detection_result, output_root):
    case_dir = output_root / make_case_slug(image_path)
    case_dir.mkdir(parents=True, exist_ok=True)

    original_overlay = draw_crab_detections(image, detection_result)
    unwrapped_overlay = draw_unwrapped_crab_detections(detection_result)

    cv2.imwrite(str(case_dir / "annotated_original.jpg"), original_overlay)
    cv2.imwrite(str(case_dir / "annotated_unwrapped.jpg"), unwrapped_overlay)
    cv2.imwrite(str(case_dir / "unwrapped_board.jpg"), detection_result["unwrapped_image"])
    cv2.imwrite(str(case_dir / "crab_mask.png"), detection_result["unwrapped_mask"])


def run_all_cases(
    output_dir=REPO_ROOT / "results",
    force_square=True,
    unwrap_size=DEFAULT_UNWRAP_SIZE,
):
    output_root = Path(output_dir)
    output_root.mkdir(parents=True, exist_ok=True)

    rows = []
    case_paths = iter_case_paths()
    if not case_paths:
        print("No practice or sample images were found.")
        return []

    for image_path in case_paths:
        image = cv2.imread(str(image_path))
        if image is None:
            rows.append(
                {
                    "case": image_path.name,
                    "group": image_path.parent.name,
                    "status": "read_failed",
                    "total": "",
                    "green": "",
                    "other": "",
                    "board_polygon": "",
                }
            )
            print(f"[FAIL] {image_path}: could not read image")
            continue

        detection_result = detect_crabs(image, force_square=force_square, unwrap_size=unwrap_size)
        if detection_result is None:
            rows.append(
                {
                    "case": image_path.name,
                    "group": image_path.parent.name,
                    "status": "detect_failed",
                    "total": "",
                    "green": "",
                    "other": "",
                    "board_polygon": "",
                }
            )
            print(f"[FAIL] {image_path}: could not find board/crabs")
            continue

        save_case_outputs(image, image_path, detection_result, output_root)
        rows.append(
            {
                "case": image_path.name,
                "group": image_path.parent.name,
                "status": "ok",
                "total": detection_result["count"],
                "green": detection_result["green_count"],
                "other": detection_result["other_count"],
                "board_polygon": detection_result["board_polygon"].reshape(-1, 2).tolist(),
            }
        )
        print(
            f"[OK] {image_path.parent.name}/{image_path.name}: "
            f"total={detection_result['count']} "
            f"green={detection_result['green_count']} "
            f"other={detection_result['other_count']}"
        )

    summary_path = output_root / "summary.csv"
    with summary_path.open("w", newline="", encoding="utf-8") as summary_file:
        writer = csv.DictWriter(
            summary_file,
            fieldnames=["group", "case", "status", "total", "green", "other", "board_polygon"],
        )
        writer.writeheader()
        writer.writerows(rows)

    print(f"Saved batch results to {output_root}")
    print(f"Saved summary to {summary_path}")
    return rows


if __name__ == "__main__":
    run_all_cases()
