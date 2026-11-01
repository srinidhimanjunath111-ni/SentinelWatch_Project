"""Optional Copernicus Data Space Ecosystem adapter, third in the failover chain.

The STAC endpoint is public, while the Process API needs OAuth client credentials. The
chain only includes this provider when those credentials are present.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import httpx
import numpy as np

from app.config import settings
from app.satellite.grid import AOIGrid
from app.satellite.provider import (
    AcquisitionCandidate,
    ProviderAuthRequired,
    ProviderError,
    ProviderResponseError,
    deduplicate_scenes,
    with_retries,
)
from app.satellite.stac import parse_stac_datetime

_BANDS_SCRIPT = """//VERSION=3
function setup() {
  return {
    input: [{ bands: ["B02", "B03", "B04", "B08", "B11", "B12"] }],
    output: { bands: 6, sampleType: "FLOAT32" }
  };
}
function evaluatePixel(s) { return [s.B02, s.B03, s.B04, s.B08, s.B11, s.B12]; }"""

_SCL_SCRIPT = """//VERSION=3
function setup() {
  return { input: [{ bands: ["SCL"] }], output: { bands: 1, sampleType: "UINT8" } };
}
function evaluatePixel(s) { return [s.SCL]; }"""


def _iso_z(value: datetime) -> str:
    return value.replace(microsecond=0).isoformat() + "Z"


class CopernicusDataSpaceProvider:
    """Sentinel-2 L2A provider backed by current official CDSE endpoints."""

    name = "copernicus-data-space"
    collection = "sentinel-2-l2a"
    requires_credentials = True

    def __init__(
        self,
        stac_url: str | None = None,
        process_url: str | None = None,
        token_url: str | None = None,
        client_id: str | None = None,
        client_secret: str | None = None,
        timeout_seconds: float | None = None,
    ):
        self.stac_url = (stac_url or settings.copernicus_stac_url).rstrip("/")
        self.process_url = process_url or settings.copernicus_process_url
        self.token_url = token_url or settings.copernicus_token_url
        self.client_id = settings.copernicus_client_id if client_id is None else client_id
        self.client_secret = (
            settings.copernicus_client_secret if client_secret is None else client_secret
        )
        self.timeout_seconds = timeout_seconds or settings.copernicus_request_timeout_seconds

    @property
    def has_credentials(self) -> bool:
        return bool(self.client_id and self.client_secret)

    def search_scenes(
        self, geometry: dict[str, Any], start: datetime, end: datetime, limit: int = 300
    ) -> list[AcquisitionCandidate]:
        payload = {
            "collections": [self.collection],
            "intersects": geometry,
            "datetime": f"{_iso_z(start)}/{_iso_z(end)}",
            "limit": max(1, min(int(limit), 100)),
            "sortby": [{"field": "properties.datetime", "direction": "desc"}],
        }

        def attempt() -> dict[str, Any]:
            response = httpx.post(
                f"{self.stac_url}/search", json=payload, timeout=self.timeout_seconds
            )
            response.raise_for_status()
            try:
                return response.json()
            except ValueError as error:
                raise ProviderResponseError("CDSE STAC returned non-JSON catalogue data.") from error

        catalog = with_retries(attempt, description="CDSE search", retryable=(httpx.HTTPError,))
        features = catalog.get("features")
        if not isinstance(features, list):
            raise ProviderResponseError("CDSE STAC response does not contain a feature list.")
        return deduplicate_scenes([self._parse_feature(feature) for feature in features])

    def _parse_feature(self, feature: dict[str, Any]) -> AcquisitionCandidate:
        if not isinstance(feature, dict) or not isinstance(feature.get("id"), str):
            raise ProviderResponseError("CDSE STAC returned an item without a product id.")
        properties = feature.get("properties") or {}
        cloud_value = properties.get("eo:cloud_cover")
        try:
            cloud_cover = float(cloud_value) if cloud_value is not None else None
        except (TypeError, ValueError):
            cloud_cover = None
        source_url = next(
            (
                link.get("href")
                for link in feature.get("links") or []
                if isinstance(link, dict) and link.get("rel") in {"self", "canonical"}
            ),
            None,
        )
        grid_code = str(properties.get("grid:code") or "")
        return AcquisitionCandidate(
            product_id=feature["id"],
            collection=str(feature.get("collection") or self.collection),
            acquisition_datetime=parse_stac_datetime(properties.get("datetime")),
            cloud_cover=cloud_cover,
            geometry=feature.get("geometry") if isinstance(feature.get("geometry"), dict) else None,
            bbox=feature.get("bbox") if isinstance(feature.get("bbox"), list) else None,
            satellite=str(properties.get("platform") or "") or None,
            sensor="msi",
            processing_level="L2A",
            source_url=source_url,
            provider=self.name,
            tile=grid_code.removeprefix("MGRS-") or None,
            processing_baseline=properties.get("processing:version"),
        )

    def _access_token(self) -> str:
        if not self.has_credentials:
            raise ProviderAuthRequired(
                "CDSE imagery retrieval requires COPERNICUS_CLIENT_ID and "
                "COPERNICUS_CLIENT_SECRET in the local .env file."
            )
        try:
            response = httpx.post(
                self.token_url,
                data={
                    "grant_type": "client_credentials",
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                },
                headers={"content-type": "application/x-www-form-urlencoded"},
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
        except httpx.HTTPError as error:
            raise ProviderAuthRequired(f"CDSE OAuth token request failed: {error}") from error
        token = response.json().get("access_token")
        if not isinstance(token, str) or not token:
            raise ProviderAuthRequired("CDSE OAuth response did not include an access token.")
        return token

    def _process(self, scene: AcquisitionCandidate, grid: AOIGrid, script: str) -> np.ndarray:
        """Request one AOI raster on our grid. CDSE harmonises baselines by default."""

        from rasterio.io import MemoryFile

        token = self._access_token()
        west, south, east, north = grid.bounds
        start = scene.acquisition_datetime - timedelta(minutes=2)
        end = scene.acquisition_datetime + timedelta(minutes=2)
        payload = {
            "input": {
                "bounds": {
                    "bbox": [west, south, east, north],
                    "properties": {"crs": f"http://www.opengis.net/def/crs/EPSG/0/{grid.epsg}"},
                },
                "data": [
                    {
                        "type": self.collection,
                        "dataFilter": {"timeRange": {"from": _iso_z(start), "to": _iso_z(end)}},
                    }
                ],
            },
            "output": {
                "width": grid.width,
                "height": grid.height,
                "responses": [{"identifier": "default", "format": {"type": "image/tiff"}}],
            },
            "evalscript": script,
        }

        def attempt() -> bytes:
            response = httpx.post(
                self.process_url,
                json=payload,
                headers={"Authorization": f"Bearer {token}"},
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
            return response.content

        content = with_retries(attempt, description="CDSE Process API", retryable=(httpx.HTTPError,))
        if not content:
            raise ProviderResponseError("CDSE Process API returned an empty imagery response.")
        try:
            with MemoryFile(content) as memory, memory.open() as dataset:
                return dataset.read()
        except Exception as error:  # Rasterio exposes several driver-specific errors.
            raise ProviderError(f"CDSE returned an unreadable raster: {error}") from error

    def read_scl(self, scene: AcquisitionCandidate, grid: AOIGrid) -> np.ndarray:
        return self._process(scene, grid, _SCL_SCRIPT)[0].astype(np.uint8)

    def read_bands(self, scene: AcquisitionCandidate, grid: AOIGrid) -> np.ndarray:
        data = self._process(scene, grid, _BANDS_SCRIPT).astype(np.float32)
        return np.clip(np.nan_to_num(data), 0.0, 1.0)
