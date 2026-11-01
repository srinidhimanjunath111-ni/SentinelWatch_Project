"""Change masking and per-region cause classification on real multispectral arrays.

Everything here is pure computation: arrays in, decisions out, no database or network, so
each rule can be exercised directly. The thresholds were checked against real Sentinel-2
windows over the seeded sites. They rank evidence for a person to review; none of them
establishes legality or cause beyond doubt, and the reasons they produce say so.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
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
# A pixel moving less than this share of an index's threshold counts as stable, and a common
# shift is only trusted when at least this share of the pixels are stable.
STABLE_TOLERANCE = 0.5
MIN_STABLE_SHARE = 0.25
# Model and spectral evidence share one 0-1 scale on which 0.5 means "changed".
MODEL_WEIGHT = 0.6
MIN_SPECTRAL_SUPPORT = 0.30
# Spectral evidence this strong stands on its own, so a model trained elsewhere cannot veto it.
STRONG_SPECTRAL = 0.75
MASK_THRESHOLD = 0.5
CLOUD_BUFFER_PIXELS = 3
# Veil test, set against real scenes. Fog that SCL passed as clear (S2B_43RGM_20230114) lifts
# even the darkest nearby pixels in blue and NIR. Clear land keeps some dark pixel, silted flood
# water stays dark in NIR (S2B_43RGM_20230713), and bare soil fails because it is redder.
HAZE_BLUR_PIXELS = 4.0
HAZE_HOT_THRESHOLD = -0.03
HAZE_DARK_FLOOR = 0.11
HAZE_WINDOW_PIXELS = 21
# Above this share of otherwise clear pixels, the scene as a whole is too veiled to compare.
HAZE_REJECT_SHARE = 0.75

BURN_DNBR = 0.27
BURN_MAX_VISIBLE = 0.13
BURN_MAX_NIR = 0.26
BURN_MAX_NBR_AFTER = 0.1
MINING_BSI_RISE = 0.15
QUARRY_OVERLAP = 0.3
CONSTRUCTION_NDBI_RISE = 0.15
WATER_GAIN_NDWI_RISE = 0.25
WATER_CLASS_NDWI_RISE = 0.1
# Flood water drains within weeks, so a storm only corroborates water seen soon afterwards.
# A landslide scar stays visible far longer.
FLOOD_RAIN_MAX_GAP_DAYS = 60
LANDSLIDE_RAIN_MAX_GAP_DAYS = 550
LANDSLIDE_MIN_SLOPE_DEGREES = 18.0
DEBRIS_RUNOUT_MIN_SLOPE_DEGREES = 5.0
LANDSLIDE_MAX_FILL = 0.55
PERSISTS_FRACTION = 0.5
# Seasonal requires positive similarity: most of the region this close to last year's value of
# each driving index, in either direction. The change persists when most of it remains.
SEASONAL_SIMILARITY = 0.6
PERSIST_CHANGE_SHARE = 0.75
SEASONAL_MATCH_FRACTION = 0.6
MIN_REFERENCE_COVERAGE = 0.5
AUTHORISED_OVERLAP = 0.5
# A region with no harmful cause pattern is kept as evidence but stays below the alert line.
WEAK_CAUSE_STRENGTH = 0.15
WEAK_CAUSE_CONFIDENCE_CAP = 0.55
VEGETATION_GAIN_NDVI = 0.1
# Within two months, drying vegetation can cross several index thresholds together.
# Reuse the land-cover fallback's vegetation boundary; a model label alone cannot
# establish conversion to a hard surface while the spectrum still shows vegetation.
SHORT_PAIR_DAYS = 60
VEGETATED_NDVI = 0.25
_DARK_GROUND_SITES = ("mining", "infrastructure")


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
    peak_14d_mm: float | None = None
    gap_days: int | None = None  # Days between the two scenes the peak was measured over.


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
    # What the operator registered this AOI as, which is context for dark or bare change.
    site_type: str | None = None
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


def haze_mask(scene: np.ndarray) -> np.ndarray:
    """Pixels under fog, smoke or thin cloud that the SCL layer often misses.

    A veil adds light everywhere, so the darkest pixel in a neighbourhood stays bright in both
    blue and NIR. Bright soil, bright roofs and turbid water each fail one of the tests.
    """

    hot = scene[B02] - 0.5 * scene[B04] - 0.08
    veiled = ndimage.gaussian_filter(hot, sigma=HAZE_BLUR_PIXELS) > HAZE_HOT_THRESHOLD
    neutral = ndimage.gaussian_filter(scene[B02] - 0.9 * scene[B04], sigma=HAZE_BLUR_PIXELS) > 0
    darkest_blue = ndimage.minimum_filter(scene[B02], size=HAZE_WINDOW_PIXELS)
    darkest_nir = ndimage.minimum_filter(scene[B08], size=HAZE_WINDOW_PIXELS)
    return veiled & neutral & (darkest_blue > HAZE_DARK_FLOOR) & (darkest_nir > HAZE_DARK_FLOOR)


def usable_fraction(scl: np.ndarray, haze: np.ndarray, aoi_mask: np.ndarray) -> float:
    """The AOI's clear fraction from SCL, unless a veil covers most of what SCL called clear."""

    clear = scl_valid(scl) & aoi_mask
    total = int(aoi_mask.sum())
    if total == 0 or not clear.any():
        return 0.0
    if float((haze & clear).sum()) / int(clear.sum()) >= HAZE_REJECT_SHARE:
        return float((clear & ~haze).sum()) / total
    return float(clear.sum()) / total


def analysis_valid(
    scl: np.ndarray, aoi_mask: np.ndarray, haze: np.ndarray | None = None
) -> np.ndarray:
    """Validity for comparison. Cloud edges are hazy, so invalid pixels get a small buffer."""

    bad = ~scl_valid(scl)
    if haze is not None:
        bad = bad | haze
    if bad.any():
        bad = ndimage.binary_dilation(bad, iterations=CLOUD_BUFFER_PIXELS)
    return ~bad & aoi_mask


def stable_shift(delta: np.ndarray, pool: np.ndarray, tolerance: float) -> float:
    """Common illumination or haze offset of an index difference, from stable pixels only.

    Only pixels that barely moved may vote, so real change cannot define its own baseline.
    When too few are left, for example because the whole window really did change, nothing
    is subtracted and the absolute difference stands as the evidence.
    """

    if not pool.any():
        return 0.0
    values = delta[pool]
    stable = values[np.abs(values) <= tolerance]
    if stable.size < MIN_STABLE_SHARE * values.size:
        return 0.0
    return float(np.median(stable))


def spectral_score(
    before_indices: dict[str, np.ndarray],
    after_indices: dict[str, np.ndarray],
    valid: np.ndarray,
    sensitivity: str = "MEDIUM",
) -> np.ndarray:
    """Per-pixel change evidence in [0,1], where 0.5 means an index crossed its threshold.

    A small common shift, measured on stable pixels, is removed first. That cancels haze and
    illumination differences, which move a whole window together, without erasing a real
    change however much of the window it covers.
    """

    scale = SENSITIVITY_SCALE.get(sensitivity.upper(), 1.0)
    score = np.zeros(valid.shape, dtype=np.float32)
    if not valid.any():
        return score
    for name, threshold in INDEX_THRESHOLDS.items():
        delta = after_indices[name] - before_indices[name]
        delta = delta - stable_shift(delta, valid, STABLE_TOLERANCE * threshold)
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
        raw_mask = (
            ((fused >= MASK_THRESHOLD) & (spectral >= MIN_SPECTRAL_SUPPORT))
            | (spectral >= STRONG_SPECTRAL)
        ) & valid

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


def split_by_transition(
    mask: np.ndarray,
    before_classes: np.ndarray,
    after_classes: np.ndarray,
    *,
    min_pixels: int,
    max_regions: int,
) -> list[np.ndarray]:
    """Cut the change mask into regions that each have one land-cover story.

    Change is often contiguous across very different ground. At an airport seen across a
    wheat season, the runway and the fields around it would otherwise form one region, whose
    majority class is crop, and the runway would be lost inside a crop cycle.
    """

    structure = np.ones((3, 3), dtype=bool)
    before = ndimage.median_filter(before_classes, size=3)
    after = ndimage.median_filter(after_classes, size=3)
    codes = before.astype(np.int32) * 256 + after.astype(np.int32)
    regions: list[np.ndarray] = []
    used = np.zeros(mask.shape, dtype=bool)

    def collect(part: np.ndarray) -> None:
        labels, count = ndimage.label(part, structure=structure)
        if not count:
            return
        sizes = ndimage.sum_labels(np.ones_like(labels), labels, index=np.arange(1, count + 1))
        for identifier, size in enumerate(sizes, start=1):
            if size >= min_pixels:
                region = labels == identifier
                regions.append(region)
                used[region] = True

    for code in np.unique(codes[mask]):
        collect(mask & (codes == code))
    # Fragments too small alone are still real change, so they stay together as mixed regions.
    collect(mask & ~used)
    regions.sort(key=lambda region: int(region.sum()), reverse=True)
    return regions[:max_regions]


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


@dataclass
class SeasonalResult:
    """Outcome of comparing a region with the same season last year.

    ``persists`` is True when the change is still there, False only when the region positively
    matches last year, and None when no reference exists or the comparison is inconclusive.
    """

    persists: bool | None
    persist_fraction: float | None = None
    similar_fraction: float | None = None
    coverage: float = 0.0
    status: str = "no_reference"


def seasonal_test(
    region: np.ndarray,
    before_indices: dict[str, np.ndarray],
    after_indices: dict[str, np.ndarray],
    reference_indices: dict[str, np.ndarray] | None,
    reference_valid: np.ndarray | None,
    shared_valid: np.ndarray,
    changed: np.ndarray | None = None,
) -> SeasonalResult:
    """Does this region still differ from the same season last year, the same way?

    Take the indices that drove the before-to-after change. The change persists if most
    pixels sit on the changed side of last year's value. It is seasonal only if most pixels
    are also close to last year's value in both directions. A forest that lost its leaves
    looks like last winter's forest, a cleared forest does not, and a region unlike last year
    in some other way is left undecided rather than excused as seasonal.

    A year-on-year offset is removed using stable pixels outside the detected change, so a
    generally wetter or drier year does not count as change.
    """

    if reference_indices is None or reference_valid is None:
        return SeasonalResult(None)
    usable = region & reference_valid
    coverage = float(usable.sum()) / max(1, int(region.sum()))
    if coverage < MIN_REFERENCE_COVERAGE:
        return SeasonalResult(None, coverage=coverage, status="low_coverage")
    pool = shared_valid & reference_valid & ~region
    if changed is not None:
        pool &= ~changed
    persist_fraction = 0.0
    close = np.ones(int(usable.sum()), dtype=bool)
    drivers = 0
    for name, threshold in INDEX_THRESHOLDS.items():
        change = float(np.mean(after_indices[name][region] - before_indices[name][region]))
        if abs(change) < 0.6 * threshold:
            continue
        drivers += 1
        versus_reference = after_indices[name] - reference_indices[name]
        versus_reference = versus_reference - stable_shift(
            versus_reference, pool, STABLE_TOLERANCE * threshold
        )
        values = versus_reference[usable]
        direction = 1.0 if change > 0 else -1.0
        required = max(PERSIST_CHANGE_SHARE * abs(change), 0.5 * threshold)
        persist_fraction = max(persist_fraction, float(np.mean(direction * values >= required)))
        # Last year's value explains the change if it sits much nearer than the earlier scene.
        # The allowance stays capped at one threshold, so a value far away never counts as near.
        allowance = max(SEASONAL_SIMILARITY * threshold, min(SEASONAL_SIMILARITY * abs(change), threshold))
        close &= np.abs(values) <= allowance
    if drivers == 0:
        # A model-only region with no strong index shift: compare every index with last year.
        distance = np.max(
            [
                np.abs(after_indices[name][usable] - reference_indices[name][usable]) / threshold
                for name, threshold in INDEX_THRESHOLDS.items()
            ],
            axis=0,
        )
        persist_fraction = float(np.mean(distance >= 1.0))
        close = distance <= SEASONAL_SIMILARITY
    similar_fraction = float(np.mean(close))
    if persist_fraction >= PERSISTS_FRACTION:
        return SeasonalResult(True, persist_fraction, similar_fraction, coverage, "persists")
    if similar_fraction >= SEASONAL_MATCH_FRACTION:
        return SeasonalResult(False, persist_fraction, similar_fraction, coverage, "seasonal")
    return SeasonalResult(None, persist_fraction, similar_fraction, coverage, "inconclusive")


def _names(values: list[str], limit: int = 3) -> str:
    unique = list(dict.fromkeys(value for value in values if value))
    return ", ".join(unique[:limit])


def _rain_sentence(rain: Rainfall) -> str:
    if not rain.available or rain.total_mm is None:
        return "Rainfall data was unavailable for this scene."
    text = f"Rainfall totalled {rain.total_mm:.0f} mm over the 14 days before the scene"
    if rain.anomaly_percent is not None:
        text += f", {abs(rain.anomaly_percent):.0f}% {'above' if rain.anomaly_percent >= 0 else 'below'} the same window in prior years"
    if rain.peak_14d_mm is not None:
        text += f"; the wettest 14 days since the baseline scene brought {rain.peak_14d_mm:.0f} mm"
    return text + "."


def _rain_at_least(rain: Rainfall, millimetres: float, max_gap_days: int) -> bool:
    """Was it this wet just before the scene, or in a gap short enough to still be relevant?"""

    if not rain.available:
        return False
    if rain.total_mm is not None and rain.total_mm >= millimetres:
        return True
    if rain.peak_14d_mm is None or rain.peak_14d_mm < millimetres:
        return False
    return rain.gap_days is None or rain.gap_days <= max_gap_days


def _flood_rain(rain: Rainfall, heavy_mm: float) -> bool:
    return _rain_at_least(rain, heavy_mm, FLOOD_RAIN_MAX_GAP_DAYS)


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

    in_quarry = 0.0
    if context.quarry_mask is not None:
        in_quarry = float(np.mean(context.quarry_mask[region]))
    mining_site = context.site_type == "mining"

    # Water gain comes first: flood water sits on top of whatever the land cover was.
    becomes_water = to_class in (WATER, WETLAND) and from_class not in (WATER, WETLAND)
    if (becomes_water and deltas["ndwi"] >= WATER_CLASS_NDWI_RISE) or (
        deltas["ndwi"] >= WATER_GAIN_NDWI_RISE and means["ndwi_after"] > 0.0
    ):
        reasons.append(
            f"Water appeared where the earlier scene showed {from_name}: NDWI rose by {deltas['ndwi']:.2f}."
        )
        if not mining_site and (_flood_rain(rain, heavy_rain_mm) or context.flood_events):
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

    # A landslide scar is narrow, on a steep slope, and follows extreme rain.
    disaster_site = context.site_type == "disaster"
    surface_stripped = to_class in (BARE, SHRUB_GRASS, MINE) and from_class in (
        (TREE, SHRUB_GRASS, CROP, BUILT) if disaster_site else (TREE, SHRUB_GRASS, CROP)
    )
    rain_extreme = _rain_at_least(rain, extreme_rain_mm, LANDSLIDE_RAIN_MAX_GAP_DAYS)
    if (
        not mining_site
        and surface_stripped
        and (rain_extreme or context.landslide_events)
        and context.slope_degrees is not None
    ):
        slope = context.slope_degrees()
        if slope is not None:
            mean_slope = float(np.mean(slope[region]))
            ys, xs = np.where(region)
            fill = float(region.sum()) / max(1, (int(np.ptp(ys)) + 1) * (int(np.ptp(xs)) + 1))
            # A debris flow runs out along gentler valley floors and fills its box where it
            # widens, so on a site registered as a disaster area rain and terrain decide.
            steep_enough = mean_slope >= (
                DEBRIS_RUNOUT_MIN_SLOPE_DEGREES if disaster_site else LANDSLIDE_MIN_SLOPE_DEGREES
            )
            if steep_enough and (fill <= LANDSLIDE_MAX_FILL or disaster_site):
                reasons.append(
                    f"A scar stripped {from_name} on a {mean_slope:.0f} degree slope "
                    "(Copernicus 30 m terrain)."
                )
                reasons.append(_rain_sentence(rain))
                return "LANDSLIDE", reasons, 0.85

    if to_class == MINE and from_class != MINE:
        reasons.append(f"Land cover moved from {from_name} to mine or quarry surface.")
        return "MINING", reasons, 0.9
    # Being on a mining site only counts for ground that ends up bare. Fields around a mine
    # still get harvested, which raises BSI too but leaves them vegetated or crop.
    site_bare = mining_site and to_class == BARE
    if deltas["bsi"] >= MINING_BSI_RISE and (
        from_class == MINE or to_class == MINE or in_quarry >= QUARRY_OVERLAP or site_bare
    ):
        if in_quarry >= QUARRY_OVERLAP:
            where = f"inside a mapped quarry ({_names(context.quarry_names)})"
        elif from_class == MINE or to_class == MINE:
            where = "on a surface classed as mine"
        else:
            where = "on a site registered as a mining area"
        reasons.append(f"Exposed soil rose sharply (BSI +{deltas['bsi']:.2f}) {where}.")
        return "MINING", reasons, 0.8

    # A burn is dark as well as low in NBR, which separates it from a bright fresh clearing.
    # Harvested fields also lose NBR but are handled by the crop rule below. On a mine or
    # construction site dark fresh ground is usually coal or paving, so a burn needs a fire report.
    dark_after = means["visible_after"] <= BURN_MAX_VISIBLE and means["nir_after"] <= BURN_MAX_NIR
    # Forest browning in the dry season also loses NBR, but burned ground ends with almost none.
    dark_after = dark_after and means["nbr_after"] <= BURN_MAX_NBR_AFTER
    burn_drop = -deltas["nbr"] >= BURN_DNBR and from_class != CROP
    if burn_drop and dark_after and context.site_type == "infrastructure" and not context.fire_events:
        reasons.append(
            f"The surface turned dark (dNBR {-deltas['nbr']:.2f}) on a site registered as "
            "infrastructure, consistent with new paving."
        )
        return "CONSTRUCTION", reasons, 0.7
    if burn_drop and ((dark_after and context.site_type not in _DARK_GROUND_SITES) or context.fire_events):
        reasons.append(
            f"Burn severity index dNBR reached {-deltas['nbr']:.2f} and the surface darkened, "
            "the spectral pattern of fire."
        )
        if context.fire_events:
            reasons.append(f"Disaster feeds list a nearby fire: {_names(context.fire_events)}.")
        return "BURN_SCAR", reasons, min(1.0, 0.6 + (-deltas["nbr"] - BURN_DNBR))

    if from_class == TREE and to_class in (BARE, SHRUB_GRASS):
        reasons.append(f"Tree cover became {to_name}: NDVI fell by {abs(deltas['ndvi']):.2f}.")
        return "DEFORESTATION", reasons, min(1.0, 0.5 + abs(deltas["ndvi"]))

    # Crop-to-bare is normally harvest or fallow, but crop-to-built is real conversion.
    becomes_built = to_class == BUILT and from_class in (CROP, SHRUB_GRASS, BARE, TREE)
    ndbi_rise = deltas["ndbi"] >= CONSTRUCTION_NDBI_RISE and deltas["ndvi"] < 0
    # Harvests raise NDBI too, so crop only counts on a site registered as infrastructure,
    # and a demolished or buried building is not new construction.
    ndbi_source_ok = from_class != BUILT and (from_class != CROP or context.site_type == "infrastructure")
    if becomes_built or (ndbi_rise and ndbi_source_ok and to_class in (BUILT, BARE)):
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

    # Fields turning bare inside a registered construction footprint are earthworks, not a harvest.
    if from_class == CROP and to_class == BARE:
        for item in context.zones:
            if item["zone"].get("zone_type") == "CONSTRUCTION" and float(np.mean(item["mask"][region])) >= AUTHORISED_OVERLAP:
                reasons.append(
                    f"Cropland became bare ground inside a registered construction footprint, "
                    f"consistent with earthworks (NDBI {deltas['ndbi']:+.2f})."
                )
                return "CONSTRUCTION", reasons, 0.6
    if (from_class == CROP and to_class in (BARE, SHRUB_GRASS, CROP)) or (from_class == BARE and to_class == CROP):
        reasons.append(f"Cropland moved from {from_name} to {to_name}, the pattern of a harvest or sowing cycle.")
        return "CROP_CYCLE", reasons, 0.6

    if deltas["bsi"] >= MINING_BSI_RISE:
        reasons.append(f"Exposed soil rose (BSI +{deltas['bsi']:.2f}) but no specific cause pattern matched.")
        return "OTHER_CHANGE", reasons, 0.5
    if deltas["ndvi"] >= VEGETATION_GAIN_NDVI and deltas["bsi"] < 0:
        reasons.append(
            f"Vegetation increased (NDVI +{deltas['ndvi']:.2f}), which is regrowth or green-up, "
            "not a pattern of damage."
        )
        return "OTHER_CHANGE", reasons, WEAK_CAUSE_STRENGTH
    if from_class is not None and from_class == to_class:
        reasons.append(
            f"Land cover read as {from_name} both before and after, so this looks like a change "
            "within that surface, such as moisture, turbidity or shadow, not a change of cover."
        )
        return "OTHER_CHANGE", reasons, WEAK_CAUSE_STRENGTH
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
        "nbr_after": float(np.mean(after_indices["nbr"][region])),
    }
    from_class, from_share = _majority(before_classes, region)
    to_class, to_share = _majority(after_classes, region)
    # The land-cover model calls much fresh bare ground "mine". Without a mining site or a mapped
    # quarry to support that, the honest reading is bare ground.
    quarry_share = float(np.mean(context.quarry_mask[region])) if context.quarry_mask is not None else 0.0
    if context.site_type not in (None, "mining") and quarry_share < QUARRY_OVERLAP:
        from_class = BARE if from_class == MINE else from_class
        to_class = BARE if to_class == MINE else to_class
    from_name = LANDCOVER_CLASSES[from_class] if from_class is not None else None
    to_name = LANDCOVER_CLASSES[to_class] if to_class is not None else None

    ring = ndimage.binary_dilation(region, iterations=5) & ~region
    built_nearby = float(np.mean(before_classes[ring] == BUILT)) if ring.any() else 0.0

    category, reasons, rule_strength = _classify_cause(
        from_class, to_class, deltas, means, region, built_nearby, context, heavy_rain_mm, extreme_rain_mm
    )

    short_pair_browning = (
        context.rainfall.gap_days is not None
        and 0 < context.rainfall.gap_days <= SHORT_PAIR_DAYS
        and category in HUMAN_CAUSED
        and deltas["ndvi"] < 0
        and float(np.mean(after_indices["ndvi"][region])) >= VEGETATED_NDVI
    )
    if short_pair_browning:
        category, rule_strength = "OTHER_CHANGE", WEAK_CAUSE_STRENGTH
        reasons = [
            f"Vegetation declined over {context.rainfall.gap_days} days, but the after-scene "
            "spectrum still shows vegetation. Drying or senescence can also raise BSI and "
            "NDBI; the land-cover labels do not independently establish clearing, mining or construction."
        ]

    seasonal = seasonal_test(
        region,
        before_indices,
        after_indices,
        reference_indices,
        reference_valid,
        change_map.valid,
        change_map.mask,
    )
    persists, fraction, coverage = seasonal.persists, seasonal.persist_fraction, seasonal.coverage
    # No season puts up a building. Bare ground can resemble a harvested field in every index
    # used here, so a match with last year cannot excuse land that is now classed as built.
    newly_built = to_class == BUILT and from_class != BUILT and not short_pair_browning
    if persists is False and newly_built:
        persists = None
        seasonal = replace(seasonal, persists=None, status="inconclusive")
    if seasonal.status == "no_reference":
        reasons.append(
            "No clear same-season scene from last year was available, so seasonality could not be tested."
        )
    elif seasonal.status == "low_coverage":
        reasons.append(
            f"Last year's same-season scene covered only {coverage:.0%} of this region, so seasonality could not be tested."
        )
    elif seasonal.status == "inconclusive":
        reasons.append(
            f"Compared with {context.reference_date} this region neither keeps the change nor "
            f"matches last year ({seasonal.similar_fraction:.0%} similar), so seasonality is undecided."
        )
    elif persists and category == "CROP_CYCLE":
        reasons.append(
            f"This field differs from {context.reference_date} across {fraction:.0%} of the region, "
            "which is usual when sowing dates shift between years."
        )
    elif persists:
        reasons.append(
            f"Same-season comparison with {context.reference_date} still shows this change "
            f"across {fraction:.0%} of the region. A single prior-year scene cannot rule out "
            "a shift in seasonal timing."
        )

    verdict: str
    if persists is False:
        reasons.insert(
            0,
            f"Compared with the same season last year ({context.reference_date}) this region "
            f"looks the same: {seasonal.similar_fraction:.0%} of it matches last year's values, "
            "so the change is seasonal.",
        )
        verdict = SEASONAL_BENIGN
        water_classes = (WATER, WETLAND)
        if category in ("WATER_CHANGE", "FLOOD") or from_class in water_classes or to_class in water_classes:
            # A lake's annual level is not vegetation, so the label stays honest about that.
            category = "WATER_CHANGE"
        else:
            category = "CROP_CYCLE" if CROP in (from_class, to_class) else "SEASONAL_VEGETATION"
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
            reasons.append(sentence + ". This is a registry match, not a legal conclusion.")
            if not detail.get("valid_to"):
                reasons.append(
                    "No expiry is recorded for this zone, which is not the same as permanent validity."
                )
            if detail.get("is_illustrative"):
                reasons.append(
                    "This zone's footprint is marked illustrative: an approximate outline, not an official boundary."
                )
        else:
            nearby = _illustrative_zone_nearby(region, category, context)
            if nearby is not None:
                verdict = NEEDS_REVIEW
                reasons.append(
                    f'This change is next to or partly inside the registered zone "{nearby["zone"].get("name")}". '
                    "Its footprint is illustrative, not an official boundary. Our review buffer "
                    "flags boundary uncertainty; it does not extend the authorisation. Verify "
                    "the actual boundary before assessing whether this activity is covered."
                )
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
    if rule_strength <= WEAK_CAUSE_STRENGTH:
        confidence = min(confidence, WEAK_CAUSE_CONFIDENCE_CAP)
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
            "seasonal_status": seasonal.status,
            "seasonal_similar_fraction": (
                round(seasonal.similar_fraction, 3) if seasonal.similar_fraction is not None else None
            ),
            "mean_spectral_score": round(float(np.mean(change_map.spectral[region])), 4),
            "mean_model_probability": (
                round(float(np.mean(change_map.probability[region])), 4)
                if change_map.probability is not None
                else None
            ),
            "requires_human_verification": True,
            "short_pair_browning": short_pair_browning,
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


def _illustrative_zone_nearby(
    region: np.ndarray, category: str, context: AssessmentContext
) -> dict[str, Any] | None:
    """Approximate matching footprints warrant review, never presumed authorisation."""

    for item in context.zones:
        detail = item["zone"]
        if not detail.get("is_illustrative"):
            continue
        if category not in ZONE_AUTHORISES.get(detail.get("zone_type"), frozenset()):
            continue
        nearby = item.get("review_mask")
        if np.any(item["mask"][region]) or (nearby is not None and np.any(nearby[region])):
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
