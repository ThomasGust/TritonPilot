import cv2
import numpy as np
from pathlib import Path
import csv
import re

DEFAULT_UNWRAP_SIZE = (700, 700)
SCRIPT_DIR = Path(__file__).resolve().parent
REFERENCE_IMAGE_PATHS = {
    "european_green": SCRIPT_DIR / "data" / "crab_reference" / "european_green.jpg",
    "native_rock": SCRIPT_DIR / "data" / "crab_reference" / "native_rock.jpg",
    "jonah": SCRIPT_DIR / "data" / "crab_reference" / "jonah.png",
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


def build_white_board_mask(image):
    height, width = image.shape[:2]
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    lab = cv2.cvtColor(image, cv2.COLOR_BGR2LAB)

    saturation = hsv[:, :, 1]
    value = hsv[:, :, 2]
    lightness = lab[:, :, 0]

    white_mask = np.where(
        (value > 150) & (saturation < 80) & (lightness > 160),
        255,
        0,
    ).astype(np.uint8)

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
    mask = build_board_mask(detection_image)

    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None

    board_contour = max(contours, key=cv2.contourArea)
    if cv2.contourArea(board_contour) < 0.05 * mask.shape[0] * mask.shape[1]:
        return None

    polygon = fit_board_quadrilateral(board_contour)
    ordered = order_corners(polygon.reshape(-1, 2)) / scale

    recovered_mask = recover_board_mask_from_core(mask)
    if recovered_mask is not None:
        recovered_contours, _ = cv2.findContours(recovered_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if recovered_contours:
            recovered_contour = max(recovered_contours, key=cv2.contourArea)
            if cv2.contourArea(recovered_contour) >= 0.04 * mask.shape[0] * mask.shape[1]:
                recovered_polygon = fit_board_quadrilateral(recovered_contour)
                ordered = order_corners(recovered_polygon.reshape(-1, 2)) / scale

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

        lab = cv2.cvtColor(image, cv2.COLOR_BGR2LAB)
        reference_models[label] = {
            "contour": contour,
            "mean_lab": cv2.mean(lab, mask=mask)[:3],
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
    mean_bgr = cv2.mean(crop_image, mask=mask)[:3]
    hsv = cv2.cvtColor(crop_image, cv2.COLOR_BGR2HSV)
    mean_hsv = cv2.mean(hsv, mask=mask)[:3]
    lab = cv2.cvtColor(crop_image, cv2.COLOR_BGR2LAB)
    mean_lab = cv2.mean(lab, mask=mask)[:3]
    shape_scores = {
        label: cv2.matchShapes(contour, reference["contour"], cv2.CONTOURS_MATCH_I1, 0.0)
        for label, reference in reference_models.items()
    }
    color_scores = {
        label: float(np.linalg.norm(np.asarray(mean_lab) - np.asarray(reference["mean_lab"])))
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
    is_european_green = mean_lab[1] < 132.5 and red_minus_blue < 18.0

    return {
        "label": "european_green" if is_european_green else non_green_label,
        "is_european_green": is_european_green,
        "shape_scores": shape_scores,
        "color_scores": color_scores,
        "combined_scores": combined_scores,
        "mean_bgr": mean_bgr,
        "mean_hsv": mean_hsv,
        "mean_lab": mean_lab,
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


def detect_crabs(image, force_square=True, unwrap_size=DEFAULT_UNWRAP_SIZE):
    board_polygon = detect_board_polygon(image)
    if board_polygon is None:
        return None

    unwrapped_image, transform, _ = unwrap_board(
        image,
        polygon=board_polygon,
        force_square=force_square,
        output_size=unwrap_size,
    )
    if unwrapped_image is None:
        return None

    detections, crab_mask = detect_crabs_in_unwrapped(unwrapped_image)
    inverse_transform = np.linalg.inv(transform)

    projected_detections = []
    for index, detection in enumerate(detections, start=1):
        x, y, box_width, box_height = detection["box"]
        crop_image = unwrapped_image[y : y + box_height, x : x + box_width]
        classification = classify_crab_crop(crop_image)
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

    return {
        "board_polygon": board_polygon,
        "unwrapped_image": unwrapped_image,
        "unwrapped_mask": crab_mask,
        "transform": transform,
        "detections": projected_detections,
        "count": len(projected_detections),
        "green_count": green_count,
        "other_count": len(projected_detections) - green_count,
    }


def draw_crab_detections(original_image, detection_result):
    annotated = original_image.copy()
    count = detection_result["count"]
    green_count = detection_result["green_count"]
    other_count = detection_result["other_count"]

    cv2.polylines(
        annotated,
        [detection_result["board_polygon"].reshape(-1, 1, 2)],
        True,
        (255, 255, 0),
        3,
    )
    cv2.putText(
        annotated,
        f"Total: {count}  Green: {green_count}  Other: {other_count}",
        (20, 40),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.9,
        (0, 255, 0),
        2,
    )

    for detection in detection_result["detections"]:
        quadrilateral = detection["original_quad"]
        is_green = detection["classification"]["is_european_green"]
        box_color = (0, 255, 0) if is_green else (0, 165, 255)
        label_prefix = "G" if is_green else "O"
        cv2.polylines(annotated, [quadrilateral.reshape(-1, 1, 2)], True, box_color, 3)

        label_anchor = quadrilateral[np.argmin(quadrilateral[:, 1] + quadrilateral[:, 0])]
        label_position = (int(label_anchor[0]), int(max(20, label_anchor[1] - 10)))
        cv2.putText(
            annotated,
            f"{label_prefix}{detection['index']}",
            label_position,
            cv2.FONT_HERSHEY_SIMPLEX,
            0.75,
            (255, 0, 0),
            2,
        )

    return annotated


def draw_unwrapped_crab_detections(detection_result):
    annotated = detection_result["unwrapped_image"].copy()
    cv2.putText(
        annotated,
        (
            f"Total: {detection_result['count']}  "
            f"Green: {detection_result['green_count']}  "
            f"Other: {detection_result['other_count']}"
        ),
        (20, 40),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.85,
        (0, 255, 0),
        2,
    )

    for detection in detection_result["detections"]:
        x, y, box_width, box_height = detection["unwrapped_box"]
        is_green = detection["classification"]["is_european_green"]
        box_color = (0, 255, 0) if is_green else (0, 165, 255)
        label_prefix = "G" if is_green else "O"
        cv2.rectangle(annotated, (x, y), (x + box_width, y + box_height), box_color, 3)
        cv2.putText(
            annotated,
            f"{label_prefix}{detection['index']}",
            (int(x), int(max(20, y - 8))),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.75,
            (255, 0, 0),
            2,
        )

    return annotated


def detection_summary_text(detection_result):
    return (
        f"Detected {detection_result['count']} crabs total: "
        f"{detection_result['green_count']} European green, "
        f"{detection_result['other_count']} other."
    )


def render_detection_views(image, force_square=True, unwrap_size=DEFAULT_UNWRAP_SIZE):
    detection_result = detect_crabs(image, force_square=force_square, unwrap_size=unwrap_size)
    if detection_result is None:
        return None, None, None
    annotated_original = draw_crab_detections(image, detection_result)
    annotated_unwrapped = draw_unwrapped_crab_detections(detection_result)
    return detection_result, annotated_original, annotated_unwrapped


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
    for folder_name in ("practice", "sample"):
        folder_path = SCRIPT_DIR / folder_name
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
    output_dir=SCRIPT_DIR / "results",
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
