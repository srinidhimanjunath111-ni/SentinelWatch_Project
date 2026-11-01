"""Official Copernicus Data Space Ecosystem catalogue and Process API adapter.

The STAC endpoint is used for real product discovery.  Only the protected Process
API request needs OAuth client credentials; absence of credentials is a deliberate
blocked state, never a fabricated image fallback.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from app.config import settings
from app.satellite.geometry import geometry_bbox
from app.satellite.provider import (
    AcquisitionCandidate,
    AcquisitionPair,
    DownloadResult,
    ProviderAuthRequired,
    ProviderError,
    ProviderResponseError,
)


def _parse_datetime(value: Any) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ProviderResponseError("Catalogue item has no acquisition datetime.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ProviderResponseError(f"Invalid catalogue datetime: {value!r}") from error
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(UTC).replace(tzinfo=None)
    return parsed


def _iso_z(value: datetime) -> str:
    return value.replace(tzinfo=UTC).isoformat().replace("+00:00", "Z")


class CopernicusDataSpaceProvider:
    """Sentinel-2 L2A provider backed by current official CDSE endpoints."""

    name = "copernicus-data-space"
    collection = "sentinel-2-l2a"
    bands = ("B02", "B03", "B04", "B08", "SCL", "CLM")

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

    def search_acquisitions(
        self,
        geometry: dict[str, Any],
        datetime_interval: str,
        max_cloud_cover: float,
        limit: int = 100,
    ) -> list[AcquisitionCandidate]:
        """Search real Sentinel-2 L2A item metadata through CDSE STAC Item Search."""

        payload = {
            "collections": [self.collection],
            "intersects": geometry,
            "datetime": datetime_interval,
            "limit": max(1, min(int(limit), 100)),
            "query": {"eo:cloud_cover": {"lte": float(max_cloud_cover)}},
            "sortby": [{"field": "properties.datetime", "direction": "desc"}],
        }
        try:
            response = httpx.post(
                f"{self.stac_url}/search", json=payload, timeout=self.timeout_seconds
            )
            response.raise_for_status()
        except httpx.HTTPError as error:
            raise ProviderError(f"CDSE STAC search failed: {error}") from error
        try:
            catalog = response.json()
        except ValueError as error:
            raise ProviderResponseError("CDSE STAC returned non-JSON catalogue data.") from error
        features = catalog.get("features")
        if not isinstance(features, list):
            raise ProviderResponseError("CDSE STAC response does not contain a feature list.")
        candidates = [self._parse_feature(feature) for feature in features]
        # The API query is server-side filtering; this local guard protects against
        # malformed supplier records and makes the stored selection reproducible.
        candidates = [
            candidate
            for candidate in candidates
            if candidate.cloud_cover is None or candidate.cloud_cover <= max_cloud_cover
        ]
        return sorted(candidates, key=lambda candidate: candidate.acquisition_datetime, reverse=True)

    def _parse_feature(self, feature: dict[str, Any]) -> AcquisitionCandidate:
        if not isinstance(feature, dict) or not isinstance(feature.get("id"), str):
            raise ProviderResponseError("CDSE STAC returned an item without a product id.")
        properties = feature.get("properties") or {}
        if not isinstance(properties, dict):
            raise ProviderResponseError(f"CDSE item {feature['id']} has invalid properties.")
        cloud_value = properties.get("eo:cloud_cover")
        try:
            cloud_cover = float(cloud_value) if cloud_value is not None else None
        except (TypeError, ValueError):
            cloud_cover = None
        links = feature.get("links") or []
        source_url = next(
            (
                link.get("href")
                for link in links
                if isinstance(link, dict) and link.get("rel") in {"self", "canonical"}
            ),
            None,
        )
        assets = feature.get("assets") if isinstance(feature.get("assets"), dict) else {}
        satellite = properties.get("platform") or properties.get("sat:platform_international_designator")
        instrument = properties.get("instruments") or properties.get("instrument")
        sensor = ", ".join(instrument) if isinstance(instrument, list) else instrument
        return AcquisitionCandidate(
            product_id=feature["id"],
            collection=str(feature.get("collection") or self.collection),
            acquisition_datetime=_parse_datetime(properties.get("datetime") or feature.get("datetime")),
            cloud_cover=cloud_cover,
            geometry=feature.get("geometry") if isinstance(feature.get("geometry"), dict) else None,
            bbox=feature.get("bbox") if isinstance(feature.get("bbox"), list) else None,
            satellite=str(satellite) if satellite else None,
            sensor=str(sensor) if sensor else None,
            processing_level=str(
                properties.get("processing:level")
                or properties.get("product:processing_level")
                or "L2A"
            ),
            source_url=source_url,
            assets=assets,
            raw_properties=properties,
        )

    def select_pair(
        self, candidates: list[AcquisitionCandidate], min_gap_days: int
    ) -> AcquisitionPair | None:
        """Choose a comparable, separated pair rather than blindly taking two dates."""

        ordered = sorted(candidates, key=lambda candidate: candidate.acquisition_datetime, reverse=True)
        minimum_gap = timedelta(days=max(1, min_gap_days))
        best: tuple[float, AcquisitionCandidate, AcquisitionCandidate] | None = None
        for after_index, after in enumerate(ordered):
            for before in ordered[after_index + 1 :]:
                if after.acquisition_datetime - before.acquisition_datetime < minimum_gap:
                    continue
                if after.collection != before.collection:
                    continue
                if after.processing_level and before.processing_level:
                    if after.processing_level != before.processing_level:
                        continue
                if after.satellite and before.satellite and after.satellite != before.satellite:
                    # S2A/S2B can still be compatible, but an unknown platform pair
                    # should not be preferred over a same-platform pair.
                    platform_penalty = 0.08
                else:
                    platform_penalty = 0.0
                gap_days = (after.acquisition_datetime - before.acquisition_datetime).days
                recency = after.acquisition_datetime.timestamp() / 10_000_000_000
                score = after.quality_score + before.quality_score + recency - platform_penalty
                # A modest bonus encourages enough temporal separation without making
                # old scenes beat a much clearer current acquisition.
                score += min(gap_days, 120) / 2000
                if best is None or score > best[0]:
                    best = (score, before, after)
        if best is None:
            return None
        _, before, after = best
        rationale = (
            f"same collection/processing level; {before.cloud_cover or 0:.1f}% and "
            f"{after.cloud_cover or 0:.1f}% catalogue cloud cover; "
            f"{(after.acquisition_datetime - before.acquisition_datetime).days} day separation"
        )
        return AcquisitionPair(before=before, after=after, rationale=rationale)

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

    @staticmethod
    def _output_dimensions(geometry: dict[str, Any]) -> tuple[int, int]:
        west, south, east, north = geometry_bbox(geometry)
        latitude = (south + north) / 2
        width_m = abs(east - west) * 111_320 * max(0.05, math.cos(math.radians(latitude)))
        height_m = abs(north - south) * 110_540
        max_size = max(128, settings.max_aoi_request_pixels)
        width = max(32, min(max_size, int(math.ceil(width_m / settings.pixel_resolution_m))))
        height = max(32, min(max_size, int(math.ceil(height_m / settings.pixel_resolution_m))))
        return width, height

    def download_multiband(
        self,
        candidate: AcquisitionCandidate,
        geometry: dict[str, Any],
        max_cloud_cover: float,
        destination: Path,
    ) -> DownloadResult:
        """Retrieve a cropped, six-band GeoTIFF for one selected real acquisition."""

        token = self._access_token()
        width, height = self._output_dimensions(geometry)
        # A short interval focuses the Process API request on the STAC-selected
        # acquisition timestamp rather than creating an uncontrolled mosaic.
        start = candidate.acquisition_datetime - timedelta(minutes=2)
        end = candidate.acquisition_datetime + timedelta(minutes=2)
        evalscript = """//VERSION=3
function setup() {
  return {
    input: [{ bands: [\"B02\", \"B03\", \"B04\", \"B08\", \"SCL\", \"CLM\"] }],
    output: { bands: 6, sampleType: \"FLOAT32\" }
  };
}
function evaluatePixel(sample) {
  return [sample.B02, sample.B03, sample.B04, sample.B08, sample.SCL, sample.CLM];
}"""
        payload = {
            "input": {
                "bounds": {
                    "geometry": geometry,
                    "properties": {"crs": "http://www.opengis.net/def/crs/EPSG/0/4326"},
                },
                "data": [
                    {
                        "type": self.collection,
                        "dataFilter": {
                            "timeRange": {"from": _iso_z(start), "to": _iso_z(end)},
                            "mosaickingOrder": "leastCC",
                            "maxCloudCoverage": float(max_cloud_cover),
                        },
                    }
                ],
            },
            "output": {
                "width": width,
                "height": height,
                "responses": [{"identifier": "default", "format": {"type": "image/tiff"}}],
            },
            "evalscript": evalscript,
        }
        try:
            response = httpx.post(
                self.process_url,
                json=payload,
                headers={"Authorization": f"Bearer {token}"},
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as error:
            detail = error.response.text[:500]
            raise ProviderError(f"CDSE Process API rejected imagery request: {detail}") from error
        except httpx.HTTPError as error:
            raise ProviderError(f"CDSE imagery request failed: {error}") from error
        if not response.content:
            raise ProviderResponseError("CDSE Process API returned an empty imagery response.")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(response.content)
        return DownloadResult(
            path=destination,
            width=width,
            height=height,
            bands=self.bands,
            request_metadata={
                "provider": self.name,
                "collection": self.collection,
                "product_id": candidate.product_id,
                "time_range": {"from": _iso_z(start), "to": _iso_z(end)},
                "max_cloud_cover": max_cloud_cover,
                "width": width,
                "height": height,
            },
        )
