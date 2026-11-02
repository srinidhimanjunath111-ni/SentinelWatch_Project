"""Provider contracts and the shared data structures for imagery access."""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, TypeVar

import numpy as np

from app.config import settings
from app.satellite.grid import AOIGrid

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Contract band order, keyed by the ESA band name.
BAND_NAMES = ("B02", "B03", "B04", "B08", "B11", "B12")
SCL_INVALID = (0, 1, 3, 8, 9, 10)


class ProviderError(RuntimeError):
    """A remote provider could not complete a genuine requested operation."""


class ProviderAuthRequired(ProviderError):
    """Raised when metadata is available but protected imagery cannot be retrieved."""


class ProviderResponseError(ProviderError):
    """Raised when a provider returns unusable or malformed data."""


class AmbiguousRadiometry(ProviderError):
    """This provider's record cannot say whether its pixels still carry the offset.

    Not a service failure: the chain moves on to a provider whose radiometry is known,
    without counting it against this provider's health.
    """


@dataclass(frozen=True)
class AcquisitionCandidate:
    """One catalogue record of one Sentinel-2 tile on one date."""

    product_id: str
    collection: str
    acquisition_datetime: datetime
    cloud_cover: float | None
    geometry: dict[str, Any] | None
    bbox: list[float] | None
    satellite: str | None
    sensor: str | None
    processing_level: str | None
    source_url: str | None
    provider: str = ""
    tile: str | None = None
    processing_baseline: str | None = None
    sequence: int = 0
    # {"B02": {"href", "scale"}, ..., "SCL": {"href"}}; provider-specific extras allowed.
    assets: dict[str, Any] = field(default_factory=dict)
    raw_properties: dict[str, Any] = field(default_factory=dict)

    @property
    def scene_key(self) -> str:
        """Identity independent of provider and reprocessing: satellite, tile and date."""

        platform = (self.satellite or "S2").upper().replace("SENTINEL-", "S")
        return f"{platform}_{self.tile or 'XXXXX'}_{self.acquisition_datetime:%Y%m%d}"

    @property
    def date_key(self) -> str:
        return f"{self.acquisition_datetime:%Y-%m-%d}"


def parse_baseline(value: str | None) -> float | None:
    try:
        return float(value) if value else None
    except (TypeError, ValueError):
        return None


def deduplicate_scenes(scenes: list[AcquisitionCandidate]) -> list[AcquisitionCandidate]:
    """Keep one record per date and tile, newest first.

    The catalogue can list one overpass as _0_ and _1_ reprocessings. The higher sequence
    wins, then the higher processing baseline.
    """

    best: dict[tuple[str, str | None], AcquisitionCandidate] = {}
    for scene in scenes:
        key = (scene.date_key, scene.tile)
        current = best.get(key)
        rank = (scene.sequence, parse_baseline(scene.processing_baseline) or 0.0)
        if current is None or rank > (
            current.sequence,
            parse_baseline(current.processing_baseline) or 0.0,
        ):
            best[key] = scene
    return sorted(best.values(), key=lambda item: item.acquisition_datetime, reverse=True)


def safe_scene_fragment(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-")[:180] or "scene"


def with_retries(
    operation: Callable[[], T],
    *,
    description: str,
    retryable: tuple[type[BaseException], ...],
    attempts: int | None = None,
    backoff_seconds: float | None = None,
) -> T:
    """Bounded exponential retry. ``ProviderAuthRequired`` is never worth retrying."""

    total = max(1, attempts if attempts is not None else settings.provider_retries)
    delay = settings.provider_backoff_seconds if backoff_seconds is None else backoff_seconds
    for attempt in range(1, total + 1):
        try:
            return operation()
        except ProviderAuthRequired:
            raise
        except retryable as error:
            if attempt == total:
                raise ProviderError(f"{description} failed after {total} attempt(s): {error}") from error
            logger.info("%s failed (attempt %d/%d): %s", description, attempt, total, error)
            time.sleep(delay * 2 ** (attempt - 1))
    raise ProviderError(f"{description} did not run.")  # Unreachable, keeps type checkers honest.


class ImageryProvider(Protocol):
    """What the pipeline needs from an imagery source, regardless of who hosts it."""

    name: str
    requires_credentials: bool

    def search_scenes(
        self, geometry: dict[str, Any], start: datetime, end: datetime, limit: int = 300
    ) -> list[AcquisitionCandidate]:
        """Real catalogue records intersecting the geometry, de-duplicated, newest first."""
        ...

    def read_scl(self, scene: AcquisitionCandidate, grid: AOIGrid) -> np.ndarray:
        """uint8 H x W Scene Classification Layer on the AOI grid (0 is no data)."""
        ...

    def read_bands(self, scene: AcquisitionCandidate, grid: AOIGrid) -> np.ndarray:
        """float32 [6,H,W] surface reflectance in [0,1], contract band order."""
        ...
