"""The physical rules, seasonal test and masks, on arrays with known answers."""

from __future__ import annotations

import numpy as np

from app.services.analysis import (
    BARE as BARE_CLASS,
)
from app.services.analysis import (
    BUILT as BUILT_CLASS,
)
from app.services.analysis import (
    CROP as CROP_CLASS,
)
from app.services.analysis import (
    MINE,
    TREE,
    AssessmentContext,
    Rainfall,
    assess_region,
    build_change_map,
    clear_fraction,
    compute_indices,
    haze_mask,
    seasonal_test,
    spectral_score,
    usable_fraction,
)
from app.services.analysis import (
    WATER as WATER_CLASS,
)
from tests.fakes import BARE, BURNED, FOG, FOREST, LEAFLESS, WATER, uniform

SHAPE = (100, 100)
VALID = np.ones(SHAPE, dtype=bool)
PATCH = np.zeros(SHAPE, dtype=bool)
PATCH[30:60, 30:60] = True


def _scene(background, patch=None) -> np.ndarray:
    array = uniform(background, SHAPE)
    if patch is not None:
        array[:, PATCH] = np.array(patch, dtype=np.float32)[:, None]
    return array


def _assess(before, after, *, from_class, to_class, reference=None, context=None, region=PATCH):
    before_indices, after_indices = compute_indices(before), compute_indices(after)
    change_map = build_change_map(before, after, before_indices, after_indices, VALID, min_pixels=4)
    return assess_region(
        region,
        change_map=change_map,
        before_indices=before_indices,
        after_indices=after_indices,
        after=after,
        before_classes=np.full(SHAPE, from_class, dtype=np.uint8),
        after_classes=np.full(SHAPE, to_class, dtype=np.uint8),
        reference_indices=compute_indices(reference) if reference is not None else None,
        reference_valid=VALID if reference is not None else None,
        context=context or AssessmentContext(reference_date="2025-11-01"),
        pixel_area_m2=100.0,
    )


# Review finding 1 ---------------------------------------------------------------------


def test_change_filling_the_whole_window_is_still_detected():
    before, after = _scene(FOREST), _scene(BARE)
    before_indices, after_indices = compute_indices(before), compute_indices(after)
    assert spectral_score(before_indices, after_indices, VALID).max() > 0.5
    for probability in (None, np.ones(SHAPE, dtype=np.float32)):
        change_map = build_change_map(
            before, after, before_indices, after_indices, VALID, min_pixels=4, probability=probability
        )
        assert change_map.component_ids


def test_a_small_haze_shift_across_the_window_is_not_change():
    before = _scene(FOREST)
    after = before.copy()
    after[3] -= 0.04  # A uniform shift in NIR, as illumination or thin haze would cause.
    score = spectral_score(compute_indices(before), compute_indices(after), VALID)
    assert score.max() < 0.5


def test_a_local_change_stands_out_against_a_common_shift():
    before = _scene(FOREST)
    after = _scene(FOREST, BARE)
    after[3][~PATCH] -= 0.04
    change_map = build_change_map(before, after, compute_indices(before), compute_indices(after), VALID, min_pixels=4)
    assert change_map.mask[PATCH].mean() > 0.9
    assert change_map.mask[~PATCH].mean() < 0.05


# Review finding 2 and the leaf-fall requirement ----------------------------------------


def _seasonal(after_patch, reference_patch, before_patch=FOREST):
    before, after = _scene(FOREST, before_patch), _scene(FOREST, after_patch)
    reference = _scene(FOREST, reference_patch)
    return seasonal_test(
        PATCH, compute_indices(before), compute_indices(after), compute_indices(reference), VALID, VALID, PATCH
    )


def test_leaf_fall_matching_last_year_is_seasonal():
    result = _seasonal(LEAFLESS, LEAFLESS)
    assert result.persists is False and result.status == "seasonal"


def test_clearing_absent_last_year_persists():
    result = _seasonal(BARE, FOREST)
    assert result.persists is True and result.status == "persists"


def test_a_reference_unlike_both_dates_is_inconclusive_rather_than_seasonal():
    # NDVI fell, but last year's value is lower still: opposite side of the after value.
    opposite = (0.18, 0.22, 0.30, 0.18, 0.38, 0.36)
    result = _seasonal(BARE, opposite)
    assert result.persists is None and result.status == "inconclusive"


def test_opposite_reference_keeps_the_physical_category_and_verdict():
    opposite = (0.18, 0.22, 0.30, 0.18, 0.38, 0.36)
    assessment = _assess(
        _scene(FOREST),
        _scene(FOREST, BARE),
        from_class=TREE,
        to_class=BARE_CLASS,
        reference=_scene(FOREST, opposite),
    )
    assert assessment.seasonal_persists is None
    assert assessment.category == "DEFORESTATION"
    assert assessment.verdict == "SUSPECTED_UNAUTHORISED"
    assert any("undecided" in reason for reason in assessment.reasons)


def test_false_deforestation_from_leaf_fall_is_benign_with_reason():
    assessment = _assess(
        _scene(FOREST),
        _scene(FOREST, LEAFLESS),
        from_class=TREE,
        to_class=TREE,
        reference=_scene(FOREST, LEAFLESS),
    )
    assert assessment.verdict == "SEASONAL_BENIGN"
    assert assessment.category == "SEASONAL_VEGETATION"
    assert assessment.seasonal_persists is False
    assert "same season last year" in assessment.reasons[0]


def test_missing_reference_is_null_and_says_so():
    assessment = _assess(_scene(FOREST), _scene(FOREST, BARE), from_class=TREE, to_class=BARE_CLASS)
    assert assessment.seasonal_persists is None
    assert any("No clear same-season scene" in reason for reason in assessment.reasons)


def test_year_on_year_offset_comes_from_stable_pixels_outside_the_change():
    # A wetter year lifts every stable pixel's NDVI, so last year's patch matches once removed.
    before, after = _scene(FOREST), _scene(FOREST, LEAFLESS)
    reference = _scene(FOREST, LEAFLESS)
    reference[3] += 0.015
    result = seasonal_test(
        PATCH, compute_indices(before), compute_indices(after), compute_indices(reference), VALID, VALID, PATCH
    )
    assert result.persists is False


# Cause rules --------------------------------------------------------------------------


def test_flood_needs_rain_or_a_disaster_report():
    rainy = AssessmentContext(rainfall=Rainfall(available=True, total_mm=180.0))
    assessment = _assess(_scene(BARE), _scene(BARE, WATER), from_class=BARE_CLASS, to_class=WATER_CLASS, context=rainy)
    assert (assessment.category, assessment.verdict) == ("FLOOD", "NATURAL_EVENT")
    assert any("180 mm" in reason for reason in assessment.reasons)

    reported = AssessmentContext(flood_events=["Yamuna flood"])
    assessment = _assess(_scene(BARE), _scene(BARE, WATER), from_class=BARE_CLASS, to_class=WATER_CLASS, context=reported)
    assert assessment.category == "FLOOD"

    dry = AssessmentContext(rainfall=Rainfall(available=True, total_mm=3.0))
    assessment = _assess(_scene(BARE), _scene(BARE, WATER), from_class=BARE_CLASS, to_class=WATER_CLASS, context=dry)
    assert (assessment.category, assessment.verdict) == ("WATER_CHANGE", "NEEDS_REVIEW")


def test_storm_weeks_before_the_scene_counts_through_the_gap_peak():
    rain = Rainfall(available=True, total_mm=4.0, peak_14d_mm=320.0)
    assessment = _assess(
        _scene(BARE), _scene(BARE, WATER), from_class=BARE_CLASS, to_class=WATER_CLASS,
        context=AssessmentContext(rainfall=rain),
    )
    assert assessment.category == "FLOOD"


def test_burn_scar_is_dark_with_high_dnbr():
    assessment = _assess(_scene(FOREST), _scene(FOREST, BURNED), from_class=TREE, to_class=BARE_CLASS)
    assert (assessment.category, assessment.verdict) == ("BURN_SCAR", "NATURAL_EVENT")


def test_dark_ground_on_a_mine_is_not_called_a_burn_without_a_fire_report():
    context = AssessmentContext(site_type="mining")
    assessment = _assess(_scene(FOREST), _scene(FOREST, BURNED), from_class=TREE, to_class=BARE_CLASS, context=context)
    assert assessment.category != "BURN_SCAR"


def test_dark_new_surface_on_infrastructure_site_is_construction():
    context = AssessmentContext(site_type="infrastructure")
    assessment = _assess(_scene(FOREST), _scene(FOREST, BURNED), from_class=TREE, to_class=BARE_CLASS, context=context)
    assert assessment.category == "CONSTRUCTION"


def test_tree_to_mine_is_mining_and_never_legal_language():
    assessment = _assess(_scene(FOREST), _scene(FOREST, BARE), from_class=TREE, to_class=MINE)
    assert (assessment.category, assessment.verdict) == ("MINING", "SUSPECTED_UNAUTHORISED")
    assert "illegal activity confirmed" not in " ".join(assessment.reasons).lower()
    assert any("not proof of illegality" in reason for reason in assessment.reasons)


def test_quarry_overlap_with_bsi_rise_is_mining_and_osm_is_not_authorisation():
    context = AssessmentContext(quarry_mask=PATCH, quarry_names=["Rawa quarry"])
    assessment = _assess(_scene(FOREST), _scene(FOREST, BARE), from_class=1, to_class=BARE_CLASS, context=context)
    assert assessment.category == "MINING"
    assert assessment.verdict == "SUSPECTED_UNAUTHORISED"


GRASS_LIKE = (0.06, 0.09, 0.10, 0.25, 0.24, 0.15)


def test_harvested_field_beside_a_mine_is_not_mining():
    context = AssessmentContext(site_type="mining")
    assessment = _assess(_scene(FOREST), _scene(FOREST, GRASS_LIKE), from_class=CROP_CLASS, to_class=1, context=context)
    assert assessment.category != "MINING"


def test_landslide_needs_steep_narrow_scar_after_extreme_rain():
    scar = np.zeros(SHAPE, dtype=bool)
    # A long diagonal scar whose bounding box is mostly empty.
    for offset in range(70):
        scar[10 + offset, 10 + offset : 15 + offset] = True
    rain = Rainfall(available=True, total_mm=20.0, peak_14d_mm=500.0)
    steep = AssessmentContext(rainfall=rain, slope_degrees=lambda: np.full(SHAPE, 30.0))
    after = _scene(FOREST)
    after[:, scar] = np.array(BARE, dtype=np.float32)[:, None]
    assessment = _assess(_scene(FOREST), after, from_class=TREE, to_class=BARE_CLASS, context=steep, region=scar)
    assert (assessment.category, assessment.verdict) == ("LANDSLIDE", "NATURAL_EVENT")

    flat = AssessmentContext(rainfall=rain, slope_degrees=lambda: np.full(SHAPE, 2.0))
    assessment = _assess(_scene(FOREST), after, from_class=TREE, to_class=BARE_CLASS, context=flat, region=scar)
    assert assessment.category == "DEFORESTATION"


def test_dark_debris_scar_after_extreme_rain_is_a_landslide_not_a_burn():
    scar = np.zeros(SHAPE, dtype=bool)
    for offset in range(70):
        scar[10 + offset, 10 + offset : 15 + offset] = True
    context = AssessmentContext(
        rainfall=Rainfall(available=True, peak_14d_mm=500.0, total_mm=10.0),
        slope_degrees=lambda: np.full(SHAPE, 30.0),
    )
    after = _scene(FOREST)
    after[:, scar] = np.array(BURNED, dtype=np.float32)[:, None]
    assessment = _assess(_scene(FOREST), after, from_class=TREE, to_class=BARE_CLASS, context=context, region=scar)
    assert assessment.category == "LANDSLIDE"


def test_construction_and_urbanisation_split_on_neighbouring_built_land():
    alone = _assess(_scene(GRASS_LIKE), _scene(GRASS_LIKE, BARE), from_class=1, to_class=BUILT_CLASS)
    assert alone.category == "CONSTRUCTION"
    before = _scene(GRASS_LIKE)
    after = _scene(GRASS_LIKE, BARE)
    before_indices, after_indices = compute_indices(before), compute_indices(after)
    change_map = build_change_map(before, after, before_indices, after_indices, VALID, min_pixels=4)
    before_classes = np.full(SHAPE, BUILT_CLASS, dtype=np.uint8)
    before_classes[PATCH] = 1
    assessment = assess_region(
        PATCH,
        change_map=change_map,
        before_indices=before_indices,
        after_indices=after_indices,
        after=after,
        before_classes=before_classes,
        after_classes=np.full(SHAPE, BUILT_CLASS, dtype=np.uint8),
        reference_indices=None,
        reference_valid=None,
        context=AssessmentContext(),
        pixel_area_m2=100.0,
    )
    assert assessment.category == "URBANISATION"
    assert any("already built" in reason for reason in assessment.reasons)


def test_crop_to_bare_is_a_benign_crop_cycle():
    assessment = _assess(_scene(FOREST), _scene(FOREST, BARE), from_class=CROP_CLASS, to_class=BARE_CLASS)
    assert (assessment.category, assessment.verdict) == ("CROP_CYCLE", "SEASONAL_BENIGN")


def test_regrowth_stays_below_the_alert_line():
    assessment = _assess(_scene(BARE), _scene(BARE, FOREST), from_class=BARE_CLASS, to_class=TREE)
    assert assessment.verdict == "NEEDS_REVIEW"
    assert assessment.confidence <= 0.55


# Authorisation ------------------------------------------------------------------------


def _zone(zone_type="MINING", **extra):
    return {"zone": {"name": "Lease 7", "zone_type": zone_type, "reference": "ML-7", **extra}, "mask": PATCH}


def test_matching_zone_gives_authorised_activity_with_registry_wording():
    context = AssessmentContext(zones=[_zone(authority="Dept", valid_to=None, is_illustrative=True)])
    assessment = _assess(_scene(FOREST), _scene(FOREST, BARE), from_class=TREE, to_class=MINE, context=context)
    assert assessment.verdict == "AUTHORISED_ACTIVITY"
    assert assessment.severity == "low"
    text = " ".join(assessment.reasons)
    assert "Lease 7" in text and "ML-7" in text
    assert "registry match, not a legal conclusion" in text
    assert "not the same as permanent validity" in text
    assert "illustrative" in text


def test_recorded_expiry_omits_the_no_expiry_sentence():
    context = AssessmentContext(zones=[_zone(valid_to="2030-01-01")])
    assessment = _assess(_scene(FOREST), _scene(FOREST, BARE), from_class=TREE, to_class=MINE, context=context)
    assert "not the same as permanent validity" not in " ".join(assessment.reasons)


def test_zone_of_another_type_does_not_authorise():
    context = AssessmentContext(zones=[_zone("CONSTRUCTION")])
    assessment = _assess(_scene(FOREST), _scene(FOREST, BARE), from_class=TREE, to_class=MINE, context=context)
    assert assessment.verdict == "SUSPECTED_UNAUTHORISED"


def test_region_mostly_outside_the_zone_is_not_authorised():
    corner = np.zeros(SHAPE, dtype=bool)
    corner[30:33, 30:33] = True
    context = AssessmentContext(zones=[{"zone": _zone()["zone"], "mask": corner}])
    assessment = _assess(_scene(FOREST), _scene(FOREST, BARE), from_class=TREE, to_class=MINE, context=context)
    assert assessment.verdict == "SUSPECTED_UNAUTHORISED"


# Masks --------------------------------------------------------------------------------


def test_clear_fraction_counts_only_inside_the_aoi():
    scl = np.full(SHAPE, 9, dtype=np.uint8)
    scl[:, :50] = 4
    aoi = np.zeros(SHAPE, dtype=bool)
    aoi[:, :50] = True
    assert clear_fraction(scl, aoi) == 1.0
    assert clear_fraction(scl, np.ones(SHAPE, dtype=bool)) == 0.5


def test_haze_mask_finds_fog_but_not_soil_forest_or_silted_water():
    assert haze_mask(_scene(FOG)).mean() > 0.95
    assert haze_mask(_scene(FOREST)).mean() == 0
    assert haze_mask(_scene(BARE)).mean() == 0
    silted = (0.13, 0.15, 0.14, 0.08, 0.04, 0.03)
    assert haze_mask(_scene(silted)).mean() == 0


def test_scene_is_only_rejected_when_a_veil_covers_most_of_it():
    scl = np.full(SHAPE, 4, dtype=np.uint8)
    aoi = np.ones(SHAPE, dtype=bool)
    assert usable_fraction(scl, np.ones(SHAPE, dtype=bool), aoi) == 0.0
    half = np.zeros(SHAPE, dtype=bool)
    half[:, :50] = True
    assert usable_fraction(scl, half, aoi) == 1.0


def test_water_seasonal_label_stays_water():
    assessment = _assess(_scene(BARE), _scene(BARE, WATER), from_class=BARE_CLASS, to_class=WATER_CLASS, reference=_scene(BARE, WATER))
    assert assessment.verdict == "SEASONAL_BENIGN"
    assert assessment.category == "WATER_CHANGE"


def test_crop_cleared_for_an_airport_is_construction_but_a_harvest_elsewhere_is_not():
    site = AssessmentContext(site_type="infrastructure")
    assessment = _assess(_scene(FOREST), _scene(FOREST, BARE), from_class=CROP_CLASS, to_class=BARE_CLASS, context=site)
    assert assessment.category == "CONSTRUCTION"
    other = AssessmentContext(site_type="forest")
    assessment = _assess(_scene(FOREST), _scene(FOREST, BARE), from_class=CROP_CLASS, to_class=BARE_CLASS, context=other)
    assert assessment.category == "CROP_CYCLE"


def test_mine_class_needs_support_from_the_site_or_a_mapped_quarry():
    site = AssessmentContext(site_type="infrastructure")
    assessment = _assess(_scene(FOREST), _scene(FOREST, BARE), from_class=TREE, to_class=MINE, context=site)
    assert assessment.category == "DEFORESTATION"
    assert assessment.transition_to == "bare"
    mine = AssessmentContext(site_type="mining")
    assert _assess(_scene(FOREST), _scene(FOREST, BARE), from_class=TREE, to_class=MINE, context=mine).category == "MINING"


def test_wide_debris_flow_on_a_disaster_site_is_a_landslide():
    context = AssessmentContext(
        site_type="disaster",
        rainfall=Rainfall(available=True, total_mm=20.0, peak_14d_mm=500.0, gap_days=300),
        slope_degrees=lambda: np.full(SHAPE, 30.0),
    )
    assessment = _assess(_scene(FOREST), _scene(FOREST, BARE), from_class=TREE, to_class=MINE, context=context)
    assert (assessment.category, assessment.verdict) == ("LANDSLIDE", "NATURAL_EVENT")


def test_old_storm_cannot_explain_water_seen_long_afterwards():
    rain = Rainfall(available=True, total_mm=3.0, peak_14d_mm=600.0, gap_days=900)
    assessment = _assess(_scene(BARE), _scene(BARE, WATER), from_class=BARE_CLASS, to_class=WATER_CLASS, context=AssessmentContext(rainfall=rain))
    assert assessment.category == "WATER_CHANGE"
    rain = Rainfall(available=True, total_mm=3.0, peak_14d_mm=600.0, gap_days=35)
    assessment = _assess(_scene(BARE), _scene(BARE, WATER), from_class=BARE_CLASS, to_class=WATER_CLASS, context=AssessmentContext(rainfall=rain))
    assert assessment.category == "FLOOD"


def test_browning_forest_is_not_a_burn_scar():
    browning = (0.05, 0.07, 0.08, 0.24, 0.20, 0.14)  # Loses NBR but keeps a clearly positive value.
    assessment = _assess(_scene(FOREST), _scene(FOREST, browning), from_class=TREE, to_class=TREE)
    assert assessment.category != "BURN_SCAR"


def test_contiguous_change_is_split_by_land_cover_story():
    from app.services.analysis import split_by_transition

    mask = np.zeros(SHAPE, dtype=bool)
    mask[10:50, 10:90] = True
    before = np.full(SHAPE, CROP_CLASS, dtype=np.uint8)
    after = np.full(SHAPE, CROP_CLASS, dtype=np.uint8)
    after[10:50, 50:90] = BUILT_CLASS
    regions = split_by_transition(mask, before, after, min_pixels=20, max_regions=10)
    assert len(regions) == 2
    assert all(np.mean(after[region] == np.median(after[region])) > 0.95 for region in regions)
    assert sum(int(region.sum()) for region in regions) == int(mask.sum())
