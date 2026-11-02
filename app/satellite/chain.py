"""Ordered failover across imagery providers, with health tracking and a window cache.

The pipeline only ever talks to this chain. It tries providers in order, skips one that is
cooling down after repeated failures, and caches every AOI window to disk, so re-analysis
of a known scene needs no network and survives every provider being down.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np

from app.config import settings
from app.satellite.geometry import bbox_polygon
from app.satellite.grid import AOIGrid
from app.satellite.provider import (
    AcquisitionCandidate,
    AmbiguousRadiometry,
    ImageryProvider,
    ProviderError,
    safe_scene_fragment,
)
from app.timeutil import iso_z, utcnow

logger = logging.getLogger(__name__)

FAILURES_BEFORE_COOLDOWN = 3
_REFLECTANCE_SCALE = 10_000.0


@dataclass
class ProviderHealth:
    name: str
    requires_credentials: bool = False
    consecutive_failures: int = 0
    last_error: str | None = None
    last_error_at: datetime | None = None
    last_success_at: datetime | None = None
    last_scene_at: datetime | None = None
    cooldown_until: float = 0.0

    @property
    def healthy(self) -> bool:
        return self.consecutive_failures < FAILURES_BEFORE_COOLDOWN

    @property
    def cooling_down(self) -> bool:
        return time.monotonic() < self.cooldown_until

    def payload(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "healthy": self.healthy,
            "requires_credentials": self.requires_credentials,
            "consecutive_failures": self.consecutive_failures,
            "last_error": self.last_error,
            "last_error_at": iso_z(self.last_error_at),
            "last_success_at": iso_z(self.last_success_at),
            "last_scene_at": iso_z(self.last_scene_at),
        }


class ProviderChain:
    def __init__(self, providers: list[ImageryProvider], *, isolate_fetches: bool = True):
        if not providers:
            raise ValueError("At least one imagery provider is required.")
        self.providers = providers
        self.isolate_fetches = isolate_fetches
        self._health = {
            provider.name: ProviderHealth(
                provider.name, getattr(provider, "requires_credentials", False)
            )
            for provider in providers
        }
        self._lock = threading.Lock()

    # Health ----------------------------------------------------------------------------

    def _ok(self, provider: ImageryProvider, newest: datetime | None = None) -> None:
        with self._lock:
            health = self._health[provider.name]
            health.consecutive_failures = 0
            health.cooldown_until = 0.0
            health.last_success_at = utcnow()
            health.last_error = None
            if newest is not None and (
                health.last_scene_at is None or newest > health.last_scene_at
            ):
                health.last_scene_at = newest

    def _failed(self, provider: ImageryProvider, error: Exception) -> None:
        with self._lock:
            health = self._health[provider.name]
            health.consecutive_failures += 1
            health.last_error = str(error)[:500]
            health.last_error_at = utcnow()
            if not health.healthy:
                health.cooldown_until = time.monotonic() + settings.provider_cooldown_seconds
        logger.warning("Provider %s failed: %s", provider.name, error)

    def _ordered(self) -> list[ImageryProvider]:
        """Providers worth trying now. If everything is cooling down, try everything."""

        with self._lock:
            ready = [p for p in self.providers if not self._health[p.name].cooling_down]
        return ready or list(self.providers)

    def status(self) -> list[dict[str, Any]]:
        with self._lock:
            return [self._health[p.name].payload() for p in self.providers]

    def summary(self) -> dict[str, Any]:
        """The provider block of /api/overview: who is serving, and whether anyone can."""

        with self._lock:
            healths = [self._health[p.name] for p in self.providers]
        active = next((health for health in healths if health.healthy), healths[0])
        scenes = [health.last_scene_at for health in healths if health.last_scene_at]
        return {
            "name": active.name,
            "healthy": any(health.healthy for health in healths),
            "last_scene_at": iso_z(max(scenes)) if scenes else None,
            "last_error": next((h.last_error for h in healths if h.last_error), None),
        }

    @property
    def all_unhealthy(self) -> bool:
        with self._lock:
            return not any(self._health[p.name].healthy for p in self.providers)

    def _can_isolate(self) -> bool:
        return self.isolate_fetches and all(_provider_spec(p) is not None for p in self.providers)

    def _remote(self, operation: str, args: tuple):
        from app.satellite.fetch import FetchDeadlineExceeded, bounded_call

        providers = self._ordered()
        specs = [_provider_spec(provider) for provider in providers]
        try:
            result, error, health = bounded_call(
                _remote_operation, (specs, operation, args, self._health), settings.imagery_fetch_budget_seconds
            )
        except FetchDeadlineExceeded as error:
            for provider in providers:
                self._failed(provider, error)
            raise
        with self._lock:
            for name, state in health.items():
                if state.last_success_at is not None or state.last_error_at is not None:
                    self._health[name] = state
        if error:
            raise ProviderError(error)
        return result

    # Catalogue -------------------------------------------------------------------------

    def search_scenes(
        self, geometry: dict[str, Any], start: datetime, end: datetime, limit: int = 300
    ) -> list[AcquisitionCandidate]:
        if self._can_isolate():
            return self._remote("search", (geometry, start, end, limit))
        errors: list[str] = []
        for provider in self._ordered():
            try:
                scenes = provider.search_scenes(geometry, start, end, limit=limit)
            except ProviderError as error:
                self._failed(provider, error)
                errors.append(f"{provider.name}: {error}")
                continue
            self._ok(provider, scenes[0].acquisition_datetime if scenes else None)
            return scenes
        raise ProviderError("All imagery providers failed. " + " | ".join(errors))

    def _equivalent(
        self, provider: ImageryProvider, scene: AcquisitionCandidate, grid: AOIGrid
    ) -> AcquisitionCandidate | None:
        """Find the same overpass in another provider's catalogue."""

        if scene.provider == provider.name:
            return scene
        west, south, east, north = grid.wgs84_bounds_inner()
        day = scene.acquisition_datetime.replace(hour=0, minute=0, second=0, microsecond=0)
        matches = provider.search_scenes(
            bbox_polygon([west, south, east, north]), day, day + timedelta(days=1), limit=20
        )
        return next(
            (
                item
                for item in matches
                if item.date_key == scene.date_key and (item.tile == scene.tile or not scene.tile)
            ),
            None,
        )

    def _read_with_failover(self, scene: AcquisitionCandidate, grid: AOIGrid, what: str):
        if self._can_isolate():
            return self._remote(what, (scene, grid))
        errors: list[str] = []
        # The scene's own provider goes first: its catalogue record is already in hand.
        ordered = sorted(self._ordered(), key=lambda p: p.name != scene.provider)
        for provider in ordered:
            try:
                local = self._equivalent(provider, scene, grid)
                if local is None:
                    errors.append(f"{provider.name}: scene not in catalogue")
                    continue
                result = getattr(provider, "read_scl" if what == "SCL" else "read_bands")(local, grid)
            except AmbiguousRadiometry as error:
                logger.info("Trying the next provider: %s", error)
                errors.append(f"{provider.name}: {error}")
                continue
            except ProviderError as error:
                self._failed(provider, error)
                errors.append(f"{provider.name}: {error}")
                continue
            self._ok(provider)
            return result
        raise ProviderError(f"Could not read {what} for {scene.scene_key}. " + " | ".join(errors))

    # Imagery with disk cache -----------------------------------------------------------

    def _cache_path(self, scene: AcquisitionCandidate, grid: AOIGrid, suffix: str) -> Path:
        folder = settings.satellite_cache_dir / grid.key
        return folder / f"{safe_scene_fragment(scene.scene_key)}{suffix}"

    def read_scl(self, scene: AcquisitionCandidate, grid: AOIGrid) -> np.ndarray:
        path = self._cache_path(scene, grid, ".scl.tif")
        cached = _read_cached(path, grid)
        if cached is not None:
            return cached[0].astype(np.uint8)
        scl = self._read_with_failover(scene, grid, "SCL")
        if scl.shape != grid.shape or not np.isfinite(scl).all() or not np.isin(scl, range(12)).all():
            raise ProviderError("Provider returned an invalid SCL window; nothing was cached.")
        _write_cached(path, scl[np.newaxis].astype(np.uint8), grid)
        return scl

    def read_bands(self, scene: AcquisitionCandidate, grid: AOIGrid) -> np.ndarray:
        path = self._cache_path(scene, grid, ".bands.tif")
        cached = _read_cached(path, grid)
        if cached is not None:
            return (cached.astype(np.float32) / _REFLECTANCE_SCALE).astype(np.float32)
        bands = self._read_with_failover(scene, grid, "bands")
        if (
            bands.shape != (6, *grid.shape) or not np.isfinite(bands).all()
            or (bands < 0).any() or (bands > 1).any()
        ):
            raise ProviderError("Provider returned an invalid reflectance window; nothing was cached.")
        encoded = np.clip(np.rint(bands * _REFLECTANCE_SCALE), 0, 65535).astype(np.uint16)
        _write_cached(path, encoded, grid)
        return bands


def _provider_spec(provider):
    from app.satellite.copernicus import CopernicusDataSpaceProvider
    from app.satellite.stac import EarthSearchProvider, PlanetaryComputerProvider

    if type(provider) in (EarthSearchProvider, PlanetaryComputerProvider):
        return type(provider).__name__, {
            "stac_url": provider.stac_url, "timeout_seconds": provider.timeout_seconds,
        }
    if type(provider) is CopernicusDataSpaceProvider:
        return type(provider).__name__, {
            key: getattr(provider, key) for key in (
                "stac_url", "process_url", "token_url", "client_id", "client_secret", "timeout_seconds"
            )
        }
    return None


def _remote_operation(specs, operation, args, health):
    from app.satellite.copernicus import CopernicusDataSpaceProvider
    from app.satellite.stac import EarthSearchProvider, PlanetaryComputerProvider

    classes = {cls.__name__: cls for cls in (
        EarthSearchProvider, PlanetaryComputerProvider, CopernicusDataSpaceProvider
    )}
    chain = ProviderChain([classes[name](**kwargs) for name, kwargs in specs], isolate_fetches=False)
    chain._health = {p.name: health[p.name] for p in chain.providers}
    try:
        if operation == "search":
            result = chain.search_scenes(*args)
        else:
            scene, grid = args
            result = chain._read_with_failover(scene, grid, operation)
        return result, None, chain._health
    except ProviderError as error:
        return None, str(error), chain._health


def _read_cached(path: Path, grid: AOIGrid) -> np.ndarray | None:
    if not path.is_file():
        return None
    import rasterio
    from rasterio.errors import RasterioError

    try:
        with rasterio.open(path) as dataset:
            if (dataset.height, dataset.width) != grid.shape:
                raise ValueError("cached window has a different shape")
            return dataset.read()
    except (RasterioError, OSError, ValueError) as error:
        logger.warning("Discarding unreadable cache file %s: %s", path, error)
        path.unlink(missing_ok=True)
        return None


def _write_cached(path: Path, array: np.ndarray, grid: AOIGrid) -> None:
    """Best effort, written atomically; a full disk must not cost a run its result."""

    import rasterio
    from rasterio.errors import RasterioError

    temporary = path.with_name(f"{path.stem}.{threading.get_ident()}.tmp.tif")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        predictor = 2 if array.dtype == np.uint16 else 1
        with rasterio.open(
            temporary,
            "w",
            driver="GTiff",
            height=array.shape[1],
            width=array.shape[2],
            count=array.shape[0],
            dtype=array.dtype.name,
            crs=grid.crs,
            transform=grid.transform,
            compress="deflate",
            predictor=predictor,
            tiled=True,
        ) as dataset:
            dataset.write(array)
        temporary.replace(path)
    except (RasterioError, OSError) as error:
        logger.warning("Could not cache %s: %s", path, error)
        temporary.unlink(missing_ok=True)


_chain: ProviderChain | None = None
_chain_lock = threading.Lock()


def default_providers() -> list[ImageryProvider]:
    from app.satellite.copernicus import CopernicusDataSpaceProvider
    from app.satellite.stac import EarthSearchProvider, PlanetaryComputerProvider

    providers: list[ImageryProvider] = [EarthSearchProvider(), PlanetaryComputerProvider()]
    if settings.has_copernicus_credentials:
        providers.append(CopernicusDataSpaceProvider())
    return providers


def get_chain() -> ProviderChain:
    global _chain
    with _chain_lock:
        if _chain is None:
            _chain = ProviderChain(default_providers())
        return _chain


def set_chain(chain: ProviderChain | None) -> None:
    """Replace the process-wide chain; used by tests to inject mocked providers."""

    global _chain
    with _chain_lock:
        _chain = chain
