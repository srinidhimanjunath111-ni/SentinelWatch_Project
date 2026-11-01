"""A scripted imagery provider: synthetic arrays standing in for the network in tests only."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta

import numpy as np

from app.database import SessionLocal
from app.models import MonitoringArea
from app.satellite.chain import ProviderChain, set_chain
from app.satellite.geometry import bbox_polygon, geometry_center, validate_geometry
from app.satellite.provider import AcquisitionCandidate, ProviderError, deduplicate_scenes
from app.services.monitoring import queue_monitoring_run
from app.timeutil import utcnow

# Reflectance spectra, band order B02 B03 B04 B08 B11 B12.
FOREST = (0.03, 0.05, 0.03, 0.40, 0.13, 0.06)
LEAFLESS = (0.05, 0.07, 0.08, 0.28, 0.16, 0.10)
BARE = (0.18, 0.22, 0.26, 0.30, 0.38, 0.34)
WATER = (0.05, 0.07, 0.04, 0.02, 0.01, 0.01)
BUILT = (0.15, 0.16, 0.17, 0.22, 0.23, 0.22)
CROP = (0.04, 0.07, 0.05, 0.38, 0.20, 0.10)
GRASS = (0.06, 0.09, 0.10, 0.25, 0.24, 0.15)
BURNED = (0.04, 0.05, 0.06, 0.12, 0.20, 0.22)
FOG = (0.24, 0.25, 0.25, 0.30, 0.26, 0.22)

BBOX = [77.60, 28.16, 77.61, 28.17]
SHAPE_PATCH = (slice(30, 70), slice(30, 70))


def uniform(spectrum, shape) -> np.ndarray:
    return np.broadcast_to(np.array(spectrum, dtype=np.float32)[:, None, None], (6, *shape)).copy()


def days_ago(count: int) -> date:
    return utcnow().date() - timedelta(days=count)


class Scene:
    """Paint a background spectrum, then optional patches, onto whatever grid is requested."""

    def __init__(self, background=FOREST, patch=None, scl: int = 4, patch_slices=SHAPE_PATCH):
        self.background = background
        self.patch = patch
        self.scl = scl
        self.patch_slices = patch_slices

    def bands(self, shape) -> np.ndarray:
        array = uniform(self.background, shape)
        if self.patch is not None:
            array[(slice(None), *self.patch_slices)] = np.array(self.patch, dtype=np.float32)[:, None, None]
        return array


class FakeProvider:
    requires_credentials = False

    def __init__(self, scenes: dict[date, Scene], name: str = "fake"):
        self.name = name
        self.scenes = scenes
        self.fail_search = False
        self.fail_reads = False
        self.scl_reads = 0
        self.band_reads = 0

    def _candidate(self, day: date) -> AcquisitionCandidate:
        return AcquisitionCandidate(
            product_id=f"S2A_43RGM_{day:%Y%m%d}_{self.name}",
            collection="sentinel-2-l2a",
            acquisition_datetime=datetime(day.year, day.month, day.day, 5, 41),
            cloud_cover=1.0,
            geometry=None,
            bbox=None,
            satellite="sentinel-2a",
            sensor="msi",
            processing_level="L2A",
            source_url=None,
            provider=self.name,
            tile="43RGM",
            processing_baseline="05.13",
        )

    def search_scenes(self, geometry, start, end, limit=300):
        if self.fail_search:
            raise ProviderError(f"{self.name} catalogue is down")
        found = [
            self._candidate(day)
            for day in self.scenes
            if start <= datetime(day.year, day.month, day.day, 5, 41) <= end
        ]
        return deduplicate_scenes(found)

    def _scene(self, candidate) -> Scene:
        return self.scenes[candidate.acquisition_datetime.date()]

    def read_scl(self, scene, grid):
        if self.fail_reads:
            raise ProviderError(f"{self.name} reads are down")
        self.scl_reads += 1
        return np.full(grid.shape, self._scene(scene).scl, dtype=np.uint8)

    def read_bands(self, scene, grid):
        if self.fail_reads:
            raise ProviderError(f"{self.name} reads are down")
        self.band_reads += 1
        return self._scene(scene).bands(grid.shape)


def install(scenes: dict[date, Scene]) -> FakeProvider:
    provider = FakeProvider(scenes)
    set_chain(ProviderChain([provider]))
    return provider


def make_area(**overrides) -> str:
    bbox = overrides.pop("bbox", BBOX)
    geometry = validate_geometry(bbox_polygon(bbox))
    latitude, longitude = geometry_center(geometry)
    values = {
        "name": "Test AOI",
        "geometry_json": json.dumps(geometry),
        "bbox_json": json.dumps(bbox),
        "center_lat": latitude,
        "center_lon": longitude,
        "categories_json": "[]",
        "min_change_area_m2": 1000.0,
        "state": "Kerala",
        "status": "ACTIVE",
    }
    values.update(overrides)
    db = SessionLocal()
    try:
        area = MonitoringArea(**values)
        db.add(area)
        db.commit()
        return area.id
    finally:
        db.close()


def queue(area_id: str, **kwargs) -> str:
    db = SessionLocal()
    try:
        return queue_monitoring_run(db, db.get(MonitoringArea, area_id), **kwargs).id
    finally:
        db.close()


def quiet_context(**overrides) -> dict:
    context = {
        "rainfall": {"available": True, "total_mm": 5.0, "max_daily_mm": 2.0},
        "disasters": [],
        "protected_areas": [],
        "osm_features": [],
        "unavailable": [],
    }
    context.update(overrides)
    return context


class FixedLandCover:
    """A classifier stand-in, for rules that the spectral fallback cannot exercise."""

    available = True
    device = "cpu"

    def __init__(self, classes_for_call: list[int]):
        self.classes = list(classes_for_call)

    def predict(self, scene, valid):
        value = self.classes.pop(0)
        return np.full(valid.shape, value, dtype=np.uint8), np.zeros((8, *valid.shape), dtype=np.float32)

