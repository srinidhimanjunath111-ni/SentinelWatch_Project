"""Provider contracts and shared catalogue data structures."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol


class ProviderError(RuntimeError):
    """A remote provider could not complete a genuine requested operation."""


class ProviderAuthRequired(ProviderError):
    """Raised when metadata is available but protected imagery cannot be retrieved."""


class ProviderResponseError(ProviderError):
    """Raised when a provider returns unusable or malformed data."""


@dataclass(frozen=True)
class AcquisitionCandidate:
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
    assets: dict[str, Any] = field(default_factory=dict)
    raw_properties: dict[str, Any] = field(default_factory=dict)

    @property
    def quality_score(self) -> float:
        """A transparent quality score used only for pair selection."""

        cloud_penalty = (self.cloud_cover if self.cloud_cover is not None else 100.0) / 100.0
        return max(0.0, 1.0 - cloud_penalty)


@dataclass(frozen=True)
class AcquisitionPair:
    before: AcquisitionCandidate
    after: AcquisitionCandidate
    rationale: str


@dataclass(frozen=True)
class DownloadResult:
    path: Path
    width: int
    height: int
    bands: tuple[str, ...]
    request_metadata: dict[str, Any]


class SatelliteDataProvider(Protocol):
    """Minimal provider interface, making another authorised source replaceable."""

    name: str

    def search_acquisitions(
        self,
        geometry: dict[str, Any],
        datetime_interval: str,
        max_cloud_cover: float,
        limit: int = 100,
    ) -> list[AcquisitionCandidate]: ...

    def select_pair(
        self, candidates: list[AcquisitionCandidate], min_gap_days: int
    ) -> AcquisitionPair | None: ...

    def download_multiband(
        self,
        candidate: AcquisitionCandidate,
        geometry: dict[str, Any],
        max_cloud_cover: float,
        destination: Path,
    ) -> DownloadResult: ...
