"""Change masking, baseline comparison, evidence extraction, and visual panels.

The spectral baseline is intentionally explicit.  It is a real calculation over the
downloaded pair, not a substitute model or a claimed accuracy result.  A validated
Siamese U-Net only replaces the mask-proposal stage when its checkpoint is present.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

from app.config import settings
from app.ml.inference import SiameseInference


@dataclass
class Detection:
    activity: str
    category: str
    confidence: float
    severity: str
    area_pixels: int
    area_m2: float
    centroid_x: float
    centroid_y: float
    bbox: tuple[int, int, int, int]
    ndvi_before: float | None
    ndvi_after: float | None
    ndvi_delta: float | None
    evidence: dict[str, float | str | bool]


@dataclass
class AnalysisResult:
    method: str
    mask: np.ndarray
    detections: list[Detection]
    component_masks: list[np.ndarray]
    changed_area_pixels: int
    changed_area_m2: float
    confidence: float


def _load_rgb(path: Path, shape: tuple[int, int] | None = None) -> np.ndarray:
    with Image.open(path) as image:
        image = image.convert("RGB")
        if shape and image.size != (shape[1], shape[0]):
            image = image.resize((shape[1], shape[0]), Image.Resampling.BILINEAR)
        return np.asarray(image, dtype=np.float32) / 255.0


def _illumination_normalise(before: np.ndarray, after: np.ndarray) -> np.ndarray:
    """Match post-image channel distribution to limit broad brightness false alerts."""

    before_mean, before_std = before.mean((0, 1)), before.std((0, 1)) + 1e-4
    after_mean, after_std = after.mean((0, 1)), after.std((0, 1)) + 1e-4
    corrected = (after - after_mean) * (before_std / after_std) + before_mean
    return np.clip(corrected, 0.0, 1.0)


def _box_blur(array: np.ndarray, radius: int = 1) -> np.ndarray:
    padded = np.pad(array, radius, mode="edge")
    total = np.zeros_like(array, dtype=np.float32)
    size = radius * 2 + 1
    for row in range(size):
        for col in range(size):
            total += padded[row : row + array.shape[0], col : col + array.shape[1]]
    return total / (size * size)


def _binary_dilate(mask: np.ndarray) -> np.ndarray:
    return _box_blur(mask.astype(np.float32), radius=1) > 0.0


def _binary_erode(mask: np.ndarray) -> np.ndarray:
    return _box_blur(mask.astype(np.float32), radius=1) >= 0.999


def _morphological_cleanup(mask: np.ndarray) -> np.ndarray:
    """Close small holes then open isolated speckles without optional CV packages."""

    closed = _binary_erode(_binary_dilate(mask))
    return _binary_dilate(_binary_erode(closed))


def _baseline_probability(
    before: np.ndarray, after: np.ndarray, ndvi_delta: np.ndarray | None = None
) -> np.ndarray:
    """Explainable RGB/NDVI change score used until a trained checkpoint is available."""

    difference = np.mean(np.abs(before - after), axis=2)
    green_a = before[:, :, 1] - (before[:, :, 0] + before[:, :, 2]) / 2
    green_b = after[:, :, 1] - (after[:, :, 0] + after[:, :, 2]) / 2
    vegetation_shift = np.abs(green_a - green_b)
    score = 0.72 * difference + 0.20 * vegetation_shift
    if ndvi_delta is not None:
        score += 0.18 * np.clip(np.abs(ndvi_delta), 0.0, 1.0)
    smooth = _box_blur(score, radius=1)
    low, high = np.quantile(smooth, [0.55, 0.985])
    return np.clip((smooth - low) / max(high - low, 1e-5), 0.0, 1.0).astype(np.float32)


def _connected_components(mask: np.ndarray, min_pixels: int) -> list[np.ndarray]:
    """Return 8-connected regions without requiring OpenCV/scikit-image."""

    height, width = mask.shape
    visited = np.zeros_like(mask, dtype=bool)
    components: list[np.ndarray] = []
    for start_y, start_x in np.argwhere(mask):
        if visited[start_y, start_x]:
            continue
        stack = [(int(start_y), int(start_x))]
        visited[start_y, start_x] = True
        pixels: list[tuple[int, int]] = []
        while stack:
            y, x = stack.pop()
            pixels.append((y, x))
            for ny in range(max(0, y - 1), min(height, y + 2)):
                for nx in range(max(0, x - 1), min(width, x + 2)):
                    if mask[ny, nx] and not visited[ny, nx]:
                        visited[ny, nx] = True
                        stack.append((ny, nx))
        if len(pixels) >= min_pixels:
            component = np.zeros_like(mask, dtype=bool)
            ys, xs = zip(*pixels, strict=True)
            component[np.asarray(ys), np.asarray(xs)] = True
            components.append(component)
    return components


def _severity(area_m2: float, confidence: float, persistence: float = 0.0) -> str:
    # A transparent prioritisation score, not a legal/activity certainty.
    risk = area_m2 * (0.45 + confidence) * (1.0 + 0.35 * persistence)
    if risk >= 20_000:
        return "critical"
    if risk >= 5_000:
        return "high"
    if risk >= 1_000:
        return "medium"
    return "low"


def _classify_component(
    component: np.ndarray,
    before: np.ndarray,
    after: np.ndarray,
    probability: np.ndarray,
    pixel_area_m2: float,
    before_ndvi: np.ndarray | None = None,
    after_ndvi: np.ndarray | None = None,
) -> Detection:
    ys, xs = np.where(component)
    area_pixels = len(xs)
    x1, x2, y1, y2 = int(xs.min()), int(xs.max()), int(ys.min()), int(ys.max())
    before_pixels, after_pixels = before[component], after[component]
    green_before = before_pixels[:, 1] - (before_pixels[:, 0] + before_pixels[:, 2]) / 2
    green_after = after_pixels[:, 1] - (after_pixels[:, 0] + after_pixels[:, 2]) / 2
    vegetation_loss_rgb = float(np.mean(green_before - green_after))
    post_green = float(np.mean(green_after))
    post_brown = float(np.mean(after_pixels[:, 0] + after_pixels[:, 1] - 2 * after_pixels[:, 2]))
    post_grayness = float(1.0 - np.mean(np.std(after_pixels, axis=1)) / 0.48)
    bbox_area = max(1, (x2 - x1 + 1) * (y2 - y1 + 1))
    rectangularity = float(area_pixels / bbox_area)
    base_confidence = float(np.mean(probability[component]))
    ndvi_before_mean = float(np.mean(before_ndvi[component])) if before_ndvi is not None else None
    ndvi_after_mean = float(np.mean(after_ndvi[component])) if after_ndvi is not None else None
    ndvi_delta = (
        ndvi_after_mean - ndvi_before_mean
        if ndvi_before_mean is not None and ndvi_after_mean is not None
        else None
    )
    vegetation_loss = -ndvi_delta if ndvi_delta is not None else vegetation_loss_rgb

    # These evidence rules rank a category after segmentation. They are deliberately
    # separate from the model's binary change probability and never prove illegality.
    if post_grayness > 0.90 and rectangularity > 0.42:
        activity, category = "construction", "CONSTRUCTION"
        signal, rationale = min(0.26, post_grayness * 0.22 + rectangularity * 0.15), "bright structured footprint"
    elif post_brown > 0.20 and post_green < 0.05 and vegetation_loss < 0.23:
        activity, category = "mining", "MINING_LAND_DISTURBANCE"
        signal, rationale = min(0.28, post_brown * 1.8), "bare-earth / soil signature"
    elif vegetation_loss > 0.075 and float(np.mean(green_before)) > 0.035:
        activity, category = "deforestation", "VEGETATION_LOSS"
        signal, rationale = min(0.33, vegetation_loss * 2.8), "vegetation-index decrease"
    else:
        activity, category = "land_disturbance", "OTHER_CHANGE"
        signal, rationale = 0.06, "non-specific spectral or structural change"

    confidence = float(np.clip(0.50 * base_confidence + 0.15 + signal, 0.0, 0.95))
    area_m2 = float(area_pixels * pixel_area_m2)
    return Detection(
        activity=activity,
        category=category,
        confidence=confidence,
        severity=_severity(area_m2, confidence),
        area_pixels=area_pixels,
        area_m2=area_m2,
        centroid_x=float(xs.mean()),
        centroid_y=float(ys.mean()),
        bbox=(x1, y1, x2, y2),
        ndvi_before=ndvi_before_mean,
        ndvi_after=ndvi_after_mean,
        ndvi_delta=ndvi_delta,
        evidence={
            "rationale": rationale,
            "mean_change_probability": round(base_confidence, 4),
            "vegetation_loss": round(vegetation_loss, 4),
            "post_bare_earth_index": round(post_brown, 4),
            "post_grayness": round(post_grayness, 4),
            "rectangularity": round(rectangularity, 4),
            "requires_human_verification": True,
        },
    )


def analyse_arrays(
    before: np.ndarray,
    after: np.ndarray,
    pixel_area_m2: float,
    *,
    before_ndvi: np.ndarray | None = None,
    after_ndvi: np.ndarray | None = None,
    valid_mask: np.ndarray | None = None,
    sensitivity: str = "MEDIUM",
) -> AnalysisResult:
    """Analyse already aligned RGB arrays, retaining physical area and quality masks."""

    if before.shape != after.shape or before.ndim != 3 or before.shape[2] != 3:
        raise ValueError("Before/after arrays must be aligned H×W×3 RGB arrays.")
    if pixel_area_m2 <= 0:
        raise ValueError("pixel_area_m2 must be positive.")
    if valid_mask is None:
        valid_mask = np.ones(before.shape[:2], dtype=bool)
    if valid_mask.shape != before.shape[:2]:
        raise ValueError("valid_mask must align with the RGB image dimensions.")
    ndvi_delta = after_ndvi - before_ndvi if before_ndvi is not None and after_ndvi is not None else None
    corrected_after = _illumination_normalise(before, after)
    predictor = SiameseInference(settings.model_path)
    sensitivity = sensitivity.upper()
    if predictor.available:
        probability = predictor.predict(before, corrected_after)
        method = "siamese-unet"
        thresholds = {"LOW": 0.60, "MEDIUM": 0.50, "HIGH": 0.43}
    else:
        probability = _baseline_probability(before, corrected_after, ndvi_delta)
        method = "spectral-baseline"
        thresholds = {"LOW": 0.74, "MEDIUM": 0.67, "HIGH": 0.58}
    threshold = thresholds.get(sensitivity, thresholds["MEDIUM"])
    raw_mask = (probability >= threshold) & valid_mask
    cleaned = _morphological_cleanup(raw_mask) & valid_mask
    min_pixels = max(settings.min_component_pixels, int(raw_mask.size * 0.00012))
    components = _connected_components(cleaned, min_pixels=min_pixels)
    cleaned_mask = np.zeros_like(raw_mask, dtype=bool)
    detections: list[Detection] = []
    retained_components: list[np.ndarray] = []
    for component in components:
        cleaned_mask |= component
        retained_components.append(component)
        detections.append(
            _classify_component(
                component,
                before,
                after,
                probability,
                pixel_area_m2,
                before_ndvi,
                after_ndvi,
            )
        )
    ordered = sorted(zip(detections, retained_components, strict=True), key=lambda item: item[0].confidence, reverse=True)
    detections = [item[0] for item in ordered]
    retained_components = [item[1] for item in ordered]
    changed_area_pixels = int(cleaned_mask.sum())
    changed_area_m2 = float(changed_area_pixels * pixel_area_m2)
    confidence = float(max((item.confidence for item in detections), default=0.0))
    return AnalysisResult(
        method=method,
        mask=cleaned_mask,
        detections=detections,
        component_masks=retained_components,
        changed_area_pixels=changed_area_pixels,
        changed_area_m2=changed_area_m2,
        confidence=confidence,
    )


def analyse_pair(before_path: Path, after_path: Path, pixel_resolution_m: float) -> AnalysisResult:
    """Developer/testing path for a manually supplied aligned image pair."""

    before = _load_rgb(before_path)
    after = _load_rgb(after_path, before.shape[:2])
    return analyse_arrays(before, after, pixel_resolution_m**2)


def _image_from_array(array: np.ndarray) -> Image.Image:
    return Image.fromarray(np.clip(array * 255, 0, 255).astype(np.uint8), mode="RGB")


def write_visual_evidence_arrays(
    before: np.ndarray, after: np.ndarray, result: AnalysisResult, destination: Path
) -> None:
    """Create before/after/change evidence panel from any aligned RGB array pair."""

    before_image, after_image = _image_from_array(before), _image_from_array(after)
    mask_pixels = np.asarray(result.mask, dtype=np.uint8) * 185
    red = Image.fromarray(mask_pixels, mode="L")
    change_colour = Image.new("RGBA", before_image.size, (255, 62, 76, 0))
    change_colour.putalpha(red)
    overlay = Image.alpha_composite(after_image.convert("RGBA"), change_colour).convert("RGB")
    panel = Image.new("RGB", (before_image.width * 3, before_image.height + 36), (12, 29, 26))
    panel.paste(before_image, (0, 36))
    panel.paste(after_image, (before_image.width, 36))
    panel.paste(overlay, (before_image.width * 2, 36))
    draw = ImageDraw.Draw(panel)
    for offset, label in zip(
        (10, before_image.width + 10, before_image.width * 2 + 10),
        ("BEFORE", "AFTER", "CHANGE EVIDENCE"),
        strict=True,
    ):
        draw.text((offset, 10), label, fill=(210, 239, 228))
    for detection in result.detections:
        x1, y1, x2, y2 = detection.bbox
        draw.rectangle(
            (x1 + before_image.width * 2, y1 + 36, x2 + before_image.width * 2, y2 + 36),
            outline=(255, 208, 76),
            width=2,
        )
        draw.text(
            (x1 + before_image.width * 2 + 3, y1 + 39),
            detection.category.replace("_", " "),
            fill=(255, 226, 135),
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    panel.save(destination, quality=94)


def write_visual_evidence(
    before_path: Path, after_path: Path, result: AnalysisResult, destination: Path
) -> None:
    before = _load_rgb(before_path)
    after = _load_rgb(after_path, before.shape[:2])
    write_visual_evidence_arrays(before, after, result, destination)


def detection_to_dict(detection: Detection) -> dict:
    data = asdict(detection)
    data["bbox"] = ",".join(map(str, detection.bbox))
    return data
