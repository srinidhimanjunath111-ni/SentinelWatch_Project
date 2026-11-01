"""Catalogue parsing, radiometry, failover and the on-disk window cache."""

from __future__ import annotations

from datetime import datetime

import httpx
import numpy as np
import pytest

from app.config import settings
from app.satellite.chain import FAILURES_BEFORE_COOLDOWN, ProviderChain
from app.satellite.geometry import bbox_polygon
from app.satellite.grid import AOITooLargeError, build_grid, utm_epsg_for
from app.satellite.provider import (
    AcquisitionCandidate,
    AmbiguousRadiometry,
    ProviderError,
    deduplicate_scenes,
)
from app.satellite.stac import EarthSearchProvider, PlanetaryComputerProvider
from tests.fakes import BARE, BBOX, FOREST, FakeProvider, Scene, days_ago


def _feature(product_id="S2B_43RGM_20261025_0_L2A", *, flag=True, baseline="05.13", hrefs="https"):
    prefix = "https://example.invalid" if hrefs == "https" else "s3://requester-pays"
    names = ("blue", "green", "red", "nir", "swir16", "swir22", "scl")
    return {
        "id": product_id,
        "collection": "sentinel-2-l2a",
        "properties": {
            "datetime": "2026-10-25T05:41:03.773000Z",
            "eo:cloud_cover": 2.5,
            "platform": "sentinel-2b",
            "grid:code": "MGRS-43RGM",
            "s2:processing_baseline": baseline,
            "earthsearch:boa_offset_applied": flag,
        },
        "assets": {
            name: {"href": f"{prefix}/{name}.tif", "raster:bands": [{"scale": 0.0001, "offset": -0.1}]}
            for name in names
        },
        "links": [{"rel": "self", "href": "https://example.invalid/item"}],
    }


def _candidate(day="2026-10-25", tile="43RGM", sequence=0, baseline="05.13", provider="x") -> AcquisitionCandidate:
    return AcquisitionCandidate(
        product_id=f"p-{day}-{tile}-{sequence}-{baseline}",
        collection="sentinel-2-l2a",
        acquisition_datetime=datetime.fromisoformat(day + "T05:41:00"),
        cloud_cover=1.0,
        geometry=None,
        bbox=None,
        satellite="sentinel-2b",
        sensor="msi",
        processing_level="L2A",
        source_url=None,
        provider=provider,
        tile=tile,
        processing_baseline=baseline,
        sequence=sequence,
    )


# Catalogue ----------------------------------------------------------------------------


def test_parses_earth_search_item_to_a_provider_independent_key():
    scene = EarthSearchProvider().parse_feature(_feature())
    assert scene.tile == "43RGM" and scene.sequence == 0
    assert scene.scene_key == "S2B_43RGM_20261025"
    assert scene.acquisition_datetime == datetime(2026, 10, 25, 5, 41, 3, 773000)
    assert set(scene.assets) == {"B02", "B03", "B04", "B08", "B11", "B12", "SCL"}


def test_planetary_computer_item_shares_the_scene_key():
    feature = {
        "id": "S2B_MSIL2A_20261025T052941_R105_T43RGM_20261025T083603",
        "properties": {
            "datetime": "2026-10-25T05:29:41Z",
            "platform": "Sentinel-2B",
            "s2:mgrs_tile": "43RGM",
            "s2:processing_baseline": "05.13",
        },
        "assets": {name: {"href": f"https://blob.invalid/{name}.tif"} for name in
                   ("B02", "B03", "B04", "B08", "B11", "B12", "SCL")},
    }
    assert PlanetaryComputerProvider().parse_feature(feature).scene_key == "S2B_43RGM_20261025"


def test_records_with_unreadable_requester_pays_assets_are_dropped():
    assert EarthSearchProvider().parse_feature(_feature(hrefs="s3")) is None


def test_reprocessings_collapse_to_the_latest_per_date_and_tile():
    kept = deduplicate_scenes(
        [
            _candidate(sequence=0),
            _candidate(sequence=1),
            _candidate(tile="43RFM"),
            _candidate(day="2026-10-20"),
            _candidate(baseline="05.09", sequence=0, day="2026-10-20"),
        ]
    )
    assert [(s.date_key, s.tile, s.sequence) for s in kept] == [
        ("2026-10-25", "43RGM", 1),
        ("2026-10-25", "43RFM", 0),
        ("2026-10-20", "43RGM", 0),
    ]
    assert next(s for s in kept if s.date_key == "2026-10-20").processing_baseline == "05.13"


def test_search_retries_then_raises_a_provider_error(monkeypatch):
    calls = []

    def down(*_args, **_kwargs):
        calls.append(1)
        raise httpx.ConnectTimeout("timed out")

    monkeypatch.setattr("app.satellite.stac.httpx.post", down)
    with pytest.raises(ProviderError):
        EarthSearchProvider().search_scenes(bbox_polygon(BBOX), datetime(2026, 1, 1), datetime(2026, 2, 1))
    assert len(calls) == settings.provider_retries


# Radiometry (review finding 4) -----------------------------------------------------------


def _with_flag(flag, baseline):
    return EarthSearchProvider().parse_feature(_feature(flag=flag, baseline=baseline))


def test_true_flag_is_honoured_and_the_listed_offset_is_not_applied_twice():
    assert EarthSearchProvider().dn_offset(_with_flag(True, "05.13")) == 0.0


def test_before_baseline_four_there_is_no_offset_whatever_the_flag():
    assert EarthSearchProvider().dn_offset(_with_flag(False, "03.01")) == 0.0
    assert EarthSearchProvider().dn_offset(_with_flag(None, "02.14")) == 0.0


@pytest.mark.parametrize("flag", [False, None])
def test_false_or_absent_flag_is_ambiguous_for_dark_and_bright_scenes_alike(flag):
    # S2C_43QBA_20250220 (false, dark, offset already removed) and S2A_43QBA_20220226_1 (false,
    # offset still present) carry the same metadata, so pixels are not asked to decide.
    scene = _with_flag(flag, "05.11")
    with pytest.raises(AmbiguousRadiometry):
        EarthSearchProvider().dn_offset(scene)


def test_ambiguous_scene_makes_no_pixel_request(monkeypatch):
    monkeypatch.setattr("app.satellite.stac.read_warped", lambda *a, **k: pytest.fail("no read expected"))
    with pytest.raises(AmbiguousRadiometry):
        EarthSearchProvider().read_bands(_with_flag(False, "05.11"), build_grid(bbox_polygon(BBOX)))


def test_true_flag_scene_scales_without_subtracting(monkeypatch):
    monkeypatch.setattr("app.satellite.stac.read_warped", lambda *a, **k: np.full((4, 4), 500, dtype=np.uint16))
    bands = EarthSearchProvider().read_bands(_with_flag(True, "05.13"), build_grid(bbox_polygon(BBOX)))
    assert bands.shape[0] == 6
    assert np.allclose(bands, 0.05)


def test_planetary_computer_removes_the_offset_only_from_baseline_four(monkeypatch):
    provider = PlanetaryComputerProvider()
    monkeypatch.setattr("app.satellite.stac.read_warped", lambda *a, **k: np.full((4, 4), 1500, dtype=np.uint16))
    monkeypatch.setattr(provider, "sign", lambda href: href)
    modern = _candidate(baseline="05.11")
    older = _candidate(baseline="03.00")
    assets = {b: {"href": "https://x.invalid/a.tif"} for b in ("B02", "B03", "B04", "B08", "B11", "B12", "SCL")}
    grid = build_grid(bbox_polygon(BBOX))
    from dataclasses import replace

    assert np.allclose(provider.read_bands(replace(modern, assets=assets), grid), 0.05)
    assert np.allclose(provider.read_bands(replace(older, assets=assets), grid), 0.15)


def test_digital_number_zero_stays_no_data_after_the_offset(monkeypatch):
    provider = PlanetaryComputerProvider()
    dn = np.full((4, 4), 1500, dtype=np.uint16)
    dn[0, 0] = 0
    monkeypatch.setattr("app.satellite.stac.read_warped", lambda *a, **k: dn)
    monkeypatch.setattr(provider, "sign", lambda href: href)
    assets = {b: {"href": "https://x.invalid/a.tif"} for b in ("B02", "B03", "B04", "B08", "B11", "B12", "SCL")}
    from dataclasses import replace

    bands = provider.read_bands(replace(_candidate(), assets=assets), build_grid(bbox_polygon(BBOX)))
    assert bands[0, 0, 0] == 0.0


class _AmbiguousFirst(FakeProvider):
    def read_bands(self, scene, grid):
        raise AmbiguousRadiometry("cannot tell")


def test_ambiguous_scene_routes_to_the_next_provider_without_a_health_penalty():
    day = days_ago(10)
    first = _AmbiguousFirst({day: Scene()}, name="first")
    second = FakeProvider({day: Scene(BARE)}, name="second")
    chain = ProviderChain([first, second])
    grid = build_grid(bbox_polygon(BBOX))
    scene = first._candidate(day)
    assert np.allclose(chain.read_bands(scene, grid)[0], BARE[0])
    assert second.band_reads == 1
    assert chain.status()[0]["consecutive_failures"] == 0


# Failover and cache --------------------------------------------------------------------


def test_catalogue_failure_fails_over_and_is_reflected_in_health():
    day = days_ago(10)
    first, second = FakeProvider({day: Scene()}, "first"), FakeProvider({day: Scene()}, "second")
    first.fail_search = True
    chain = ProviderChain([first, second])
    found = chain.search_scenes(bbox_polygon(BBOX), datetime(2000, 1, 1), datetime(2100, 1, 1))
    assert found[0].provider == "second"
    status = {item["name"]: item for item in chain.status()}
    assert status["first"]["consecutive_failures"] == 1 and status["first"]["last_error"]
    assert status["second"]["last_success_at"].endswith("Z")


def test_read_failure_finds_the_same_scene_on_the_next_provider():
    day = days_ago(10)
    first, second = FakeProvider({day: Scene()}, "first"), FakeProvider({day: Scene(BARE)}, "second")
    first.fail_reads = True
    chain = ProviderChain([first, second])
    grid = build_grid(bbox_polygon(BBOX))
    assert np.allclose(chain.read_bands(first._candidate(day), grid)[3], BARE[3])


def test_every_provider_failing_raises_with_each_reason():
    first, second = FakeProvider({}, "first"), FakeProvider({}, "second")
    first.fail_search = second.fail_search = True
    chain = ProviderChain([first, second])
    with pytest.raises(ProviderError, match="first.*second"):
        chain.search_scenes(bbox_polygon(BBOX), datetime(2000, 1, 1), datetime(2100, 1, 1))


def test_repeated_failures_mark_unhealthy_and_success_recovers():
    day = days_ago(10)
    provider = FakeProvider({day: Scene()}, "only")
    provider.fail_search = True
    chain = ProviderChain([provider])
    for _ in range(FAILURES_BEFORE_COOLDOWN):
        with pytest.raises(ProviderError):
            chain.search_scenes(bbox_polygon(BBOX), datetime(2000, 1, 1), datetime(2100, 1, 1))
    assert chain.summary()["healthy"] is False and chain.all_unhealthy
    provider.fail_search = False
    chain.search_scenes(bbox_polygon(BBOX), datetime(2000, 1, 1), datetime(2100, 1, 1))
    assert chain.summary()["healthy"] is True


def test_windows_are_cached_compressed_and_need_no_second_read():
    day = days_ago(10)
    provider = FakeProvider({day: Scene(FOREST, BARE)}, "only")
    chain = ProviderChain([provider])
    grid = build_grid(bbox_polygon(BBOX))
    scene = provider._candidate(day)
    first_scl, first = chain.read_scl(scene, grid), chain.read_bands(scene, grid)
    second_scl, second = chain.read_scl(scene, grid), chain.read_bands(scene, grid)
    assert provider.scl_reads == 1 and provider.band_reads == 1
    assert np.array_equal(first_scl, second_scl)
    assert np.allclose(first, second, atol=1e-4)
    files = list(settings.satellite_cache_dir.rglob("*.tif"))
    assert len(files) == 2
    import rasterio

    with rasterio.open(next(path for path in files if path.name.endswith(".bands.tif"))) as dataset:
        assert dataset.compression.value == "DEFLATE"
        assert dataset.crs.to_epsg() == grid.epsg


def test_corrupt_cache_file_is_discarded_and_read_again():
    day = days_ago(10)
    provider = FakeProvider({day: Scene()}, "only")
    chain = ProviderChain([provider])
    grid = build_grid(bbox_polygon(BBOX))
    scene = provider._candidate(day)
    chain.read_scl(scene, grid)
    next(settings.satellite_cache_dir.rglob("*.scl.tif")).write_bytes(b"not a tiff")
    chain.read_scl(scene, grid)
    assert provider.scl_reads == 2


# Grid ---------------------------------------------------------------------------------


def test_utm_zone_comes_from_the_aoi_centroid():
    assert utm_epsg_for(28.17, 77.61) == 32643
    assert utm_epsg_for(22.8, 82.8) == 32644
    assert utm_epsg_for(23.75, 86.42) == 32645


def test_grid_is_stable_snapped_and_north_up():
    grid = build_grid(bbox_polygon(BBOX))
    assert grid == build_grid(bbox_polygon(BBOX))
    assert grid.west % 10 == 0 and grid.north % 10 == 0 and grid.pixel_area_m2 == 100
    assert grid.transform.e < 0


def test_oversized_aoi_is_refused_with_a_clear_error_before_any_request():
    with pytest.raises(AOITooLargeError, match="maximum"):
        build_grid(bbox_polygon([77.0, 28.0, 77.5, 28.5]))
