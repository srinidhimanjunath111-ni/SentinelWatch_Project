"""Change masking and per-region cause classification on real multispectral arrays.

Everything here is pure computation: arrays in, decisions out, no database or network, so
each rule can be exercised directly. The thresholds were checked against real Sentinel-2
windows over the seeded sites. They rank evidence for a person to review; none of them
establishes legality or cause beyond doubt, and the reasons they produce say so.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from scipy import ndimage

from app.ml.inference import LANDCOVER_CLASSES
from app.satellite.provider import SCL_INVALID
from app.vocabulary import (
    AUTHORISED_ACTIVITY,
    HUMAN_CAUSED,
    NATURAL_EVENT,
    NEEDS_REVIEW,
    SEASONAL_BENIGN,
    SUSPECTED_UNAUTHORISED,
    ZONE_AUTHORISES,
)

B02, B03, B04, B08, B11, B12 = range(6)
TREE, SHRUB_GRASS, CROP, BUILT, BARE, WATER, WETLAND, MINE = range(8)
NODATA_CLASS = 255

# Index change treated as meaningful on its own, per index at MEDIUM sensitivity.
INDEX_THRESHOLDS = {"ndvi": 0.22, "nbr": 0.25, "ndwi": 0.25, "bsi": 0.13, "ndbi": 0.18}
SENSITIVITY_SCALE = {"LOW": 1.3, "MEDIUM": 1.0, "HIGH": 0.8}
# Model and spectral evidence share one 0-1 scale on which 0.5 means "changed".
MODEL_WEIGHT = 0.6
MIN_SPECTRAL_SUPPORT = 0.30
MASK_THRESHOLD = 0.5
CLOUD_BUFFER_PIXELS = 3

BURN_DNBR = 0.27
BURN_MAX_VISIBLE = 0.13
BURN_MAX_NIR = 0.26
MINING_BSI_RISE = 0.15
QUARRY_OVERLAP = 0.3
CONSTRUCTION_NDBI_RISE = 0.15
WATER_GAIN_NDWI_RISE = 0.25
LANDSLIDE_MIN_SLOPE_DEGREES = 18.0
LANDSLIDE_MAX_FILL = 0.55
PERSISTS_FRACTION = 0.5
MIN_REFERENCE_COVERAGE = 0.5
AUTHORISED_OVERLAP = 0.5


@dataclass
class ChangeMap:
    method: str
    fused: np.ndarray  # H x W, 0-1
    spectral: np.ndarray
    probability: np.ndarray | None
    mask: np.ndarray
    labels: np.ndarray
    component_ids: list[int]
    valid: np.ndarray


@dataclass
class Rainfall:
    available: bool = False
    total_mm: float | None = None
    max_daily_mm: float | None = None
    normal_mm: float | None = None
    anomaly_percent: float | None = None


@dataclass
class AssessmentContext:
    """Everything beyond the pair itself that a verdict may lean on. All of it is optional."""

    after_date: str = ""
    reference_date: str | None = None
    rainfall: Rainfall = field(default_factory=Rainfall)
    flood_events: list[str] = field(default_factory=list)
    fire_events: list[str] = field(default_factory=list)
    landslide_events: list[str] = field(default_factory=list)
    quarry_mask: np.ndarray | None = None
    quarry_names: list[str] = field(default_factory=list)
    protected_mask: np.ndarray | None = None
    protected_names: list[str] = field(default_factory=list)
    # [{"zone": dict, "mask": bool H x W}, ...], already filtered to dates in force.
    zones: list[dict[str, Any]] = field(default_factory=list)
    slope_degrees: Callable[[], np.ndarray | None] | None = None
    context_unavailable: list[str] = field(default_factory=list)


@dataclass
class Assessment:
    category: str
    verdict: str
    reasons: list[str]
    transition_from: str | None
    transition_to: str | None
    indices: dict[str, float]
    change_probability: float
    seasonal_persists: bool | None
    seasonal_fraction: float | None
    confidence: float
    severity: str
    area_pixels: int
    area_m2: float
    centroid_x: float
    centroid_y: float
    bbox: tuple[int, int, int, int]
    ndvi_before: float
    ndvi_after: float
    evidence: dict[str, Any]


def _normalised_difference(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    total = a + b
    return np.divide(a - b, total, out=np.zeros_like(a, dtype=np.float32), where=np.abs(total) > 1e-5)


def compute_indices(scene: np.ndarray) -> dict[str, np.ndarray]:
    """NDVI, NBR, NDWI, BSI and NDBI from a [6,H,W] reflectance array."""

    swir_red = scene[B11] + scene[B04]
    nir_blue = scene[B08] + scene[B02]
    return {
        "ndvi": _normalised_difference(scene[B08], scene[B04]),
        "nbr": _normalised_difference(scene[B08], scene[B12]),
        "ndwi": _normalised_difference(scene[B03], scene[B08]),
        "bsi": _normalised_difference(swir_red, nir_blue),
        "ndbi": _normalised_difference(scene[B11], scene[B08]),
    }


def scl_valid(scl: np.ndarray) -> np.ndarray:
    """Contract validity: False for no-data, defective, shadow, cloud and cirrus."""

    return ~np.isin(scl, SCL_INVALID)


def clear_fraction(scl: np.ndarray, aoi_mask: np.ndarray) -> float:
    """Share of the AOI actually usable, measured from SCL rather than the tile's metadata."""

    total = int(aoi_mask.sum())
    if total == 0:
        return 0.0
    return float((scl_valid(scl) & aoi_mask).sum()) / total


def analysis_valid(scl: np.ndarray, aoi_mask: np.ndarray) -> np.ndarray:
    """Validity for comparison. Cloud edges are hazy, so invalid pixels get a small buffer."""

    bad = ~scl_valid(scl)
    if bad.any():
        bad = ndimage.binary_dilation(bad, iterations=CLOUD_BUFFER_PIXELS)
    return ~bad & aoi_mask


def spectral_score(
    before_indices: dict[str, np.ndarray],
    after_indices: dict[str, np.ndarray],
    valid: np.ndarray,
    sensitivity: str = "MEDIUM",
) -> np.ndarray:
    """Per-pixel change evidence in [0,1], where 0.5 means an index crossed its threshold.

    The AOI-wide median shift of each index is removed first. That cancels haze and
    illumination differences, which move a whole window together, while a real local
    change stands out against its surroundings.
    """

    scale = SENSITIVITY_SCALE.get(sensitivity.upper(), 1.0)
    score = np.zeros(valid.shape, dtype=np.float32)
    if not valid.any():
        return score
    for name, threshold in INDEX_THRESHOLDS.items():
        delta = after_indices[name] - before_indices[name]
        delta = delta - float(np.median(delta[valid]))
        score = np.maximum(score, np.clip(np.abs(delta) / (2.0 * threshold * scale), 0.0, 1.0))
    # Light smoothing suppresses residual misregistration along field edges and roads.
    score = ndimage.gaussian_filter(score, sigma=1.0)
    score[~valid] = 0.0
    return score


def build_change_map(
    before: np.ndarray,
    after: np.ndarray,
    before_indices: dict[str, np.ndarray],
    after_indices: dict[str, np.ndarray],
    valid: np.ndarray,
    *,
    sensitivity: str = "MEDIUM",
    min_pixels: int = 8,
    max_components: int = 25,
    probability: np.ndarray | None = None,
    model_threshold: float = 0.5,
    method: str = "spectral-baseline",
) -> ChangeMap:
    """Fuse model probability (when supplied) with spectral evidence into labelled regions."""

    spectral = spectral_score(before_indices, after_indices, valid, sensitivity)
    if probability is None:
        fused = spectral
        raw_mask = (fused >= MASK_THRESHOLD) & valid
    else:
        # Rescale so the model's validated threshold lands on 0.5, matching the spectral
        # scale, then blend. A region also needs some spectral support, since a physical
        # change that moves no index at all is more likely a model artefact.
        rescaled = np.where(
            probability < model_threshold,
            0.5 * probability / max(model_threshold, 1e-3),
            0.5 + 0.5 * (probability - model_threshold) / max(1.0 - model_threshold, 1e-3),
        ).astype(np.float32)
        fused = MODEL_WEIGHT * rescaled + (1.0 - MODEL_WEIGHT) * spectral
        fused[~valid] = 0.0
        raw_mask = (fused >= MASK_THRESHOLD) & (spectral >= MIN_SPECTRAL_SUPPORT) & valid

    cleaned = ndimage.binary_opening(raw_mask, structure=np.ones((2, 2), dtype=bool))
    cleaned = ndimage.binary_closing(cleaned, structure=np.ones((3, 3), dtype=bool)) & valid
    connectivity = np.ones((3, 3), dtype=bool)
    labels, count = ndimage.label(cleaned, structure=connectivity)
    component_ids: list[int] = []
    if count:
        sizes = ndimage.sum_labels(np.ones_like(labels), labels, index=np.arange(1, count + 1))
        kept = [
            (int(size), int(identifier))
            for identifier, size in zip(range(1, count + 1), sizes, strict=True)
            if size >= min_pixels
        ]
        kept.sort(reverse=True)
        component_ids = [identifier for _, identifier in kept[:max_components]]
    final_labels = np.zeros_like(labels, dtype=np.int32)
    for new_id, identifier in enumerate(component_ids, start=1):
        final_labels[labels == identifier] = new_id
    return ChangeMap(
        method=method,
        fused=fused,
        spectral=spectral,
        probability=probability,
        mask=final_labels > 0,
        labels=final_labels,
        component_ids=list(range(1, len(component_ids) + 1)),
        valid=valid,
    )


def spectral_landcover(scene: np.ndarray, indices: dict[str, np.ndarray], valid: np.ndarray) -> np.ndarray:
    """Coarse index-based land cover, used only while the trained classifier is unavailable.

    It cannot recognise a mine and separates crop from tree only by vigour, and the finding
    records that this fallback ran.
    """

    classes = np.full(valid.shape, BARE, dtype=np.uint8)
    ndvi, ndwi, bsi, ndbi = indices["ndvi"], indices["ndwi"], indices["bsi"], indices["ndbi"]
    classes[(bsi < 0.05) & (ndbi > -0.05) & (ndvi < 0.3)] = BUILT
    classes[(ndvi >= 0.25) & (ndvi < 0.5)] = SHRUB_GRASS
    classes[(ndvi >= 0.5) & (scene[B11] >= 0.17)] = CROP
    classes[(ndvi >= 0.5) & (scene[B11] < 0.17)] = TREE
    classes[(ndwi > 0.05) & (ndvi < 0.15)] = WATER
    classes[~valid] = NODATA_CLASS
    return classes


def _majority(classes: np.ndarray, region: np.ndarray) -> tuple[int | None, float]:
    values = classes[region]
    values = values[values != NODATA_CLASS]
    if values.size == 0:
        return None, 0.0
    counts = np.bincount(values, minlength=len(LANDCOVER_CLASSES))
    index = int(counts.argmax())
    return index, float(counts[index]) / values.size


def seasonal_test(
    region: np.ndarray,
    before_indices: dict[str, np.ndarray],
    after_indices: dict[str, np.ndarray],
    reference_indices: dict[str, np.ndarray] | None,
    reference_valid: np.ndarray | None,
    shared_valid: np.ndarray,
) -> tuple[bool | None, float | None, float]:
    """Does this region still differ from the same season last year, the same way?

    Take the indices that drove the before-to-after change. The change persists if most
    pixels also sit on the changed side of last year's value for that index. A forest that
    lost its leaves looks like last winter's forest, so it fails this. A forest that was
    cleared does not. The AOI-wide year-on-year median is removed first, so a generally
    wetter or drier year does not count as change.

    Returns (persists, fraction of region persisting, reference coverage of the region).
    """

    if reference_indices is None or reference_valid is None:
        return None, None, 0.0
    usable = region & reference_valid
    coverage = float(usable.sum()) / max(1, int(region.sum()))
    if coverage < MIN_REFERENCE_COVERAGE:
        return None, None, coverage
    aoi_common = shared_valid & reference_valid
    best_fraction = 0.0
    drivers = 0
    for name, threshold in INDEX_THRESHOLDS.items():
        change = float(np.mean(after_indices[name][region] - before_indices[name][region]))
        if abs(change) < 0.6 * threshold:
            continue
        drivers += 1
        versus_reference = after_indices[name] - reference_indices[name]
        if aoi_common.any():
            versus_reference = versus_reference - float(np.median(versus_reference[aoi_common]))
        direction = 1.0 if change > 0 else -1.0
        required = max(0.5 * abs(change), 0.5 * threshold)
        fraction = float(np.mean(direction * versus_reference[usable] >= required))
        best_fraction = max(best_fraction, fraction)
    if drivers == 0:
        # A model-only region with no strong index shift: compare overall index distance.
        distance = np.max(
            [
                np.abs(after_indices[name] - reference_indices[name]) / threshold
                for name, threshold in INDEX_THRESHOLDS.items()
            ],
            axis=0,
        )
        best_fraction = float(np.mean(distance[usable] >= 0.5))
    return best_fraction >= PERSISTS_FRACTION, best_fraction, coverage


def _names(values: list[str], limit: int = 3) -> str:
    unique = list(dict.fromkeys(value for value in values if value))
    return ", ".join(unique[:limit])


def _rain_sentence(rain: Rainfall) -> str:
    if not rain.available or rain.total_mm is None:
        return "Rainfall data was unavailable for this scene."
    text = f"Rainfall totalled {rain.total_mm:.0f} mm over the 14 days before the scene"
    if rain.anomaly_percent is not None:
        text += f", {abs(rain.anomaly_percent):.0f}% {'above' if rain.anomaly_percent >= 0 else 'below'} the same window in prior years"
    return text + "."


def _heavy_rain(rain: Rainfall, heavy_mm: float) -> bool:
    return bool(rain.available and rain.total_mm is not None and rain.total_mm >= heavy_mm)


def _classify_cause(
    from_class: int | None,
    to_class: int | None,
    deltas: dict[str, float],
    means: dict[str, float],
    region: np.ndarray,
    built_nearby: float,
    context: AssessmentContext,
    heavy_rain_mm: float,
    extreme_rain_mm: float,
) -> tuple[str, list[str], float]:
    """Apply the physical rules in priority order. Returns category, reasons, rule strength."""

    reasons: list[str] = []
    from_name = LANDCOVER_CLASSES[from_class] if from_class is not None else "unknown"
    to_name = LANDCOVER_CLASSES[to_class] if to_class is not None else "unknown"
    rain = context.rainfall

    # Water gain comes first: flood water sits on top of whatever the land cover was.
    water_gain = to_class in (WATER, WETLAND) and from_class not in (WATER, WETLAND)
    if water_gain or (deltas["ndwi"] >= WATER_GAIN_NDWI_RISE and means["ndwi_after"] > 0.0):
        reasons.append(
            f"Water appeared where the earlier scene showed {from_name}: NDWI rose by {deltas['ndwi']:.2f}."
        )
        if _heavy_rain(rain, heavy_rain_mm) or context.flood_events:
            reasons.append(_rain_sentence(rain))
            if context.flood_events:
                reasons.append(f"Disaster feeds list a nearby flood event: {_names(context.flood_events)}.")
            return "FLOOD", reasons, min(1.0, 0.6 + deltas["ndwi"])
        reasons.append(_rain_sentence(rain))
        reasons.append("No heavy rain or flood report corroborates a flood, so this is logged as a water change.")
        return "WATER_CHANGE", reasons, 0.5
    if from_class in (WATER, WETLAND) and to_class not in (WATER, WETLAND) and to_class is not None:
        reasons.append(f"Open water receded to {to_name}: NDWI changed by {deltas['ndwi']:.2f}.")
        return "WATER_CHANGE", reasons, 0.5

    in_quarry = 0.0
    if context.quarry_mask is not None:
        in_quarry = float(np.mean(context.quarry_mask[region]))

    if to_class == MINE and from_class != MINE:
        reasons.append(f"Land cover moved from {from_name} to mine or quarry surface.")
        return "MINING", reasons, 0.9
    if deltas["bsi"] >= MINING_BSI_RISE and (from_class == MINE or to_class == MINE or in_quarry >= QUARRY_OVERLAP):
        where = (
            f"inside a mapped quarry ({_names(context.quarry_names)})"
            if in_quarry >= QUARRY_OVERLAP
            else "on a surface already classed as mine"
        )
        reasons.append(f"Exposed soil rose sharply (BSI +{deltas['bsi']:.2f}) {where}.")
        return "MINING", reasons, 0.8

    # A burn is dark as well as low in NBR, which separates it from a bright fresh clearing.
    # Harvested fields also lose NBR but are handled by the crop rule below.
    dark_after = means["visible_after"] <= BURN_MAX_VISIBLE and means["nir_after"] <= BURN_MAX_NIR
    if -deltas["nbr"] >= BURN_DNBR and from_class != CROP and (dark_after or context.fire_events):
        reasons.append(
            f"Burn severity index dNBR reached {-deltas['nbr']:.2f} and the surface darkened, "
            "the spectral pattern of fire."
        )
        if context.fire_events:
            reasons.append(f"Disaster feeds list a nearby fire: {_names(context.fire_events)}.")
        return "BURN_SCAR", reasons, min(1.0, 0.6 + (-deltas["nbr"] - BURN_DNBR))

    # A landslide scar is narrow, on a steep slope, and follows extreme rain.
    vegetation_stripped = from_class in (TREE, SHRUB_GRASS, CROP) and to_class in (BARE, SHRUB_GRASS)
    rain_extreme = bool(rain.available and rain.total_mm is not None and rain.total_mm >= extreme_rain_mm)
    if vegetation_stripped and (rain_extreme or context.landslide_events) and context.slope_degrees is not None:
        slope = context.slope_degrees()
        if slope is not None:
            mean_slope = float(np.mean(slope[region]))
            ys, xs = np.where(region)
            fill = float(region.sum()) / max(1, (int(np.ptp(ys)) + 1) * (int(np.ptp(xs)) + 1))
            if mean_slope >= LANDSLIDE_MIN_SLOPE_DEGREES and fill <= LANDSLIDE_MAX_FILL:
                reasons.append(
                    f"A narrow scar stripped {from_name} on a {mean_slope:.0f} degree slope "
                    "(Copernicus 30 m terrain)."
                )
                reasons.append(_rain_sentence(rain))
                return "LANDSLIDE", reasons, 0.85

    if from_class == TREE and to_class in (BARE, SHRUB_GRASS):
        reasons.append(f"Tree cover became {to_name}: NDVI fell by {abs(deltas['ndvi']):.2f}.")
        return "DEFORESTATION", reasons, min(1.0, 0.5 + abs(deltas["ndvi"]))

    # Crop-to-bare is normally harvest or fallow, but crop-to-built is real conversion.
    becomes_built = to_class == BUILT and from_class in (CROP, SHRUB_GRASS, BARE, TREE)
    ndbi_rise = deltas["ndbi"] >= CONSTRUCTION_NDBI_RISE and deltas["ndvi"] < 0
    if becomes_built or (ndbi_rise and from_class != CROP and to_class in (BUILT, BARE)):
        if becomes_built:
            reasons.append(f"Land cover moved from {from_name} to built surface.")
        else:
            reasons.append(f"Built-up index NDBI rose by {deltas['ndbi']:.2f} as vegetation fell.")
        strength = min(1.0, 0.55 + deltas["ndbi"])
        if built_nearby >= 0.25:
            reasons.append(
                f"{built_nearby:.0%} of the surrounding ring was already built, so this extends an existing settlement."
            )
            return "URBANISATION", reasons, strength
        return "CONSTRUCTION", reasons, strength

    if (from_class == CROP and to_class in (BARE, SHRUB_GRASS, CROP)) or (from_class == BARE and to_class == CROP):
        reasons.append(f"Cropland moved from {from_name} to {to_name}, the pattern of a harvest or sowing cycle.")
        return "CROP_CYCLE", reasons, 0.6

    if deltas["bsi"] >= MINING_BSI_RISE:
        reasons.append(f"Exposed soil rose (BSI +{deltas['bsi']:.2f}) but no specific cause pattern matched.")
        return "OTHER_CHANGE", reasons, 0.5
    reasons.append(f"Land cover read as {from_name} before and {to_name} after, which matches no specific cause pattern.")
    return "OTHER_CHANGE", reasons, 0.4


def assess_region(
    region: np.ndarray,
    *,
    change_map: ChangeMap,
    before_indices: dict[str, np.ndarray],
    after_indices: dict[str, np.ndarray],
    after: np.ndarray,
    before_classes: np.ndarray,
    after_classes: np.ndarray,
    reference_indices: dict[str, np.ndarray] | None,
    reference_valid: np.ndarray | None,
    context: AssessmentContext,
    pixel_area_m2: float,
    heavy_rain_mm: float = 100.0,
    extreme_rain_mm: float = 200.0,
) -> Assessment:
    """Decide category and verdict for one connected change region."""

    deltas = {
        name: float(np.mean(after_indices[name][region] - before_indices[name][region]))
        for name in INDEX_THRESHOLDS
    }
    means = {
        "ndwi_after": float(np.mean(after_indices["ndwi"][region])),
        "visible_after": float(np.mean(after[[B02, B03, B04]][:, region])),
        "nir_after": float(np.mean(after[B08][region])),
    }
    from_class, from_share = _majority(before_classes, region)
    to_class, to_share = _majority(after_classes, region)
    from_name = LANDCOVER_CLASSES[from_class] if from_class is not None else None
    to_name = LANDCOVER_CLASSES[to_class] if to_class is not None else None

    ring = ndimage.binary_dilation(region, iterations=5) & ~region
    built_nearby = float(np.mean(before_classes[ring] == BUILT)) if ring.any() else 0.0

    category, reasons, rule_strength = _classify_cause(
        from_class, to_class, deltas, means, region, built_nearby, context, heavy_rain_mm, extreme_rain_mm
    )

    persists, fraction, coverage = seasonal_test(
        region,
        before_indices,
        after_indices,
        reference_indices,
        reference_valid,
        change_map.valid,
    )
    if persists is None:
        if reference_indices is None:
            reasons.append(
                "No clear same-season scene from last year was available, so seasonality could not be tested."
            )
        else:
            reasons.append(
                f"Last year's same-season scene covered only {coverage:.0%} of this region, so seasonality could not be tested."
            )
    elif persists:
        reasons.append(
            f"Same-season comparison with {context.reference_date} still shows this change "
            f"across {fraction:.0%} of the region, so it is not seasonal."
        )

    verdict: str
    if persists is False:
        reasons.insert(
            0,
            f"Compared with the same season last year ({context.reference_date}) this region "
            f"looks the same: only {fraction:.0%} of it still differs, so the change is seasonal.",
        )
        verdict = SEASONAL_BENIGN
        if category not in ("WATER_CHANGE", "FLOOD"):
            uses_crop = CROP in (from_class, to_class)
            category = "CROP_CYCLE" if uses_crop else "SEASONAL_VEGETATION"
        else:
            category = "WATER_CHANGE"
    elif category == "CROP_CYCLE":
        verdict = SEASONAL_BENIGN
    elif category in HUMAN_CAUSED:
        zone = _authorising_zone(region, category, context)
        if zone is not None:
            verdict = AUTHORISED_ACTIVITY
            detail = zone["zone"]
            sentence = f"The region lies inside the registered zone \"{detail.get('name')}\""
            if detail.get("reference"):
                sentence += f" (reference {detail['reference']})"
            if detail.get("authority"):
                sentence += f", issued by {detail['authority']}"
            reasons.append(sentence + ".")
            if detail.get("is_illustrative"):
                reasons.append("This zone is marked illustrative, not an official permit record.")
        else:
            verdict = SUSPECTED_UNAUTHORISED
            reasons.append(
                "No registered authorisation covers this region. That is an absence of record, "
                "not proof of illegality, and it needs field verification."
            )
    elif category in ("FLOOD", "LANDSLIDE", "BURN_SCAR"):
        verdict = NATURAL_EVENT
    else:
        verdict = NEEDS_REVIEW
        reasons.append("Material change with no determined cause; a person should review it.")

    if context.protected_mask is not None:
        protected_share = float(np.mean(context.protected_mask[region]))
        if protected_share >= 0.1:
            reasons.append(
                f"{protected_share:.0%} of the region overlaps a protected area in OpenStreetMap "
                f"({_names(context.protected_names) or 'unnamed'})."
            )
    if context.quarry_mask is not None and category != "MINING":
        quarry_share = float(np.mean(context.quarry_mask[region]))
        if quarry_share >= 0.1:
            reasons.append(
                f"{quarry_share:.0%} of the region overlaps a quarry mapped in OpenStreetMap "
                f"({_names(context.quarry_names) or 'unnamed'}). OpenStreetMap is context, not authorisation."
            )

    ys, xs = np.where(region)
    area_pixels = int(region.sum())
    area_m2 = float(area_pixels * pixel_area_m2)
    change_probability = float(np.mean(change_map.fused[region]))
    seasonal_support = 1.0 if persists else (0.5 if persists is None else 0.0)
    # A review-ranking score built from visible evidence, not a calibrated probability.
    confidence = float(
        np.clip(0.5 * change_probability + 0.3 * rule_strength + 0.2 * seasonal_support, 0.0, 0.99)
    )
    return Assessment(
        category=category,
        verdict=verdict,
        reasons=reasons,
        transition_from=from_name,
        transition_to=to_name,
        indices={
            "ndvi_delta": round(deltas["ndvi"], 4),
            "nbr_delta": round(deltas["nbr"], 4),
            "ndwi_delta": round(deltas["ndwi"], 4),
            "bsi_delta": round(deltas["bsi"], 4),
            "ndbi_delta": round(deltas["ndbi"], 4),
        },
        change_probability=round(change_probability, 4),
        seasonal_persists=persists,
        seasonal_fraction=round(fraction, 4) if fraction is not None else None,
        confidence=round(confidence, 4),
        severity=severity_for(verdict, area_m2),
        area_pixels=area_pixels,
        area_m2=area_m2,
        centroid_x=float(xs.mean()),
        centroid_y=float(ys.mean()),
        bbox=(int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())),
        ndvi_before=float(np.mean(before_indices["ndvi"][region])),
        ndvi_after=float(np.mean(after_indices["ndvi"][region])),
        evidence={
            "from_share": round(from_share, 3),
            "to_share": round(to_share, 3),
            "built_nearby": round(built_nearby, 3),
            "rule_strength": round(rule_strength, 3),
            "seasonal_reference_coverage": round(coverage, 3),
            "mean_spectral_score": round(float(np.mean(change_map.spectral[region])), 4),
            "mean_model_probability": (
                round(float(np.mean(change_map.probability[region])), 4)
                if change_map.probability is not None
                else None
            ),
            "requires_human_verification": True,
        },
    )


def _authorising_zone(
    region: np.ndarray, category: str, context: AssessmentContext
) -> dict[str, Any] | None:
    """The first in-force zone of a matching type covering most of the region."""

    for item in context.zones:
        zone_type = item["zone"].get("zone_type")
        if category not in ZONE_AUTHORISES.get(zone_type, frozenset()):
            continue
        if float(np.mean(item["mask"][region])) >= AUTHORISED_OVERLAP:
            return item
    return None


def severity_for(verdict: str, area_m2: float) -> str:
    if verdict == SUSPECTED_UNAUTHORISED:
        return "critical" if area_m2 >= 50_000 else "high"
    if verdict == NATURAL_EVENT:
        return "critical" if area_m2 >= 200_000 else "high"
    if verdict == NEEDS_REVIEW:
        return "high" if area_m2 >= 200_000 else "medium"
    return "low"
