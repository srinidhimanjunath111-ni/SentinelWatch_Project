"""Sentinel-2 access through public STAC catalogues and cloud-optimised GeoTIFFs.

Windowed COG reads fetch only the AOI, so a scene costs megabytes and seconds, and neither
provider here needs credentials. Reflectance handling needs care, see ``dn_offset``.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Any

import httpx
import numpy as np

from app.config import settings
from app.satellite.grid import AOIGrid
from app.satellite.provider import (
    BAND_NAMES,
    AcquisitionCandidate,
    AmbiguousRadiometry,
    ProviderError,
    ProviderResponseError,
    deduplicate_scenes,
    parse_baseline,
    with_retries,
)

logger = logging.getLogger(__name__)

# Bounded so a stalled connection fails the attempt instead of pinning a worker.
GDAL_ENV = {
    "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
    "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".tif,.TIF,.tiff",
    "GDAL_HTTP_TIMEOUT": "45",
    "GDAL_HTTP_CONNECTTIMEOUT": "15",
    "GDAL_HTTP_LOW_SPEED_TIME": "20",
    "GDAL_HTTP_LOW_SPEED_LIMIT": "1024",
    "GDAL_HTTP_MAX_RETRY": "2",
    "GDAL_HTTP_RETRY_DELAY": "1",
    "GDAL_HTTP_MERGE_CONSECUTIVE_RANGES": "YES",
    "GDAL_CACHEMAX": 64,
    "VSI_CACHE": "FALSE",
    "AWS_NO_SIGN_REQUEST": "YES",
}
USER_AGENT = "SentinelWatch/3.0"
_SEARCH_PAGE_SIZE = 100
_MAX_PAGES = 8
RAW_OFFSET_DN = 1000.0
DEFAULT_SCALE = 1e-4


def parse_stac_datetime(value: Any) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ProviderResponseError("Catalogue item has no acquisition datetime.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ProviderResponseError(f"Invalid catalogue datetime: {value!r}") from error
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(UTC).replace(tzinfo=None)
    return parsed


def _iso(value: datetime) -> str:
    return value.replace(microsecond=0).isoformat() + "Z"


def read_warped(href: str, grid: AOIGrid, *, nearest: bool) -> np.ndarray:
    """Read one band onto the AOI grid, reprojecting if the tile is in another UTM zone."""

    import rasterio
    from rasterio.enums import Resampling
    from rasterio.vrt import WarpedVRT

    with rasterio.Env(**GDAL_ENV), rasterio.open(href) as source:
        same_crs = source.crs is not None and source.crs.to_epsg() == grid.epsg
        native_10m = same_crs and abs(source.transform.a - grid.pixel_size) < 1e-6
        # Same-zone 10 m bands land exactly on the lattice; nearest keeps them unresampled.
        resampling = (
            Resampling.nearest if (nearest or native_10m) else Resampling.bilinear
        )
        with WarpedVRT(
            source,
            crs=grid.crs,
            transform=grid.transform,
            width=grid.width,
            height=grid.height,
            resampling=resampling,
            src_nodata=0,
            nodata=0,
        ) as warped:
            return warped.read(1)


class StacCogProvider:
    """Shared search and windowed-read logic for STAC catalogues serving COG assets."""

    name = "stac"
    requires_credentials = False
    collection = "sentinel-2-l2a"
    stac_url = ""
    sort_field = "properties.datetime"
    band_assets: dict[str, str] = {}
    scl_asset = "SCL"

    def __init__(self, stac_url: str | None = None, timeout_seconds: float | None = None):
        self.stac_url = (stac_url or self.stac_url).rstrip("/")
        self.timeout_seconds = timeout_seconds or settings.provider_timeout_seconds

    # Catalogue -------------------------------------------------------------------------

    def _post_search(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        def attempt() -> dict[str, Any]:
            response = httpx.post(
                url,
                json=payload,
                timeout=self.timeout_seconds,
                headers={"User-Agent": USER_AGENT},
            )
            if response.status_code >= 500 or response.status_code == 429:
                raise httpx.HTTPStatusError(
                    f"HTTP {response.status_code}", request=response.request, response=response
                )
            if response.status_code >= 400:
                raise ProviderResponseError(
                    f"{self.name} catalogue rejected the search: "
                    f"HTTP {response.status_code} {response.text[:200]}"
                )
            try:
                return response.json()
            except ValueError as error:
                raise ProviderResponseError(f"{self.name} returned non-JSON catalogue data.") from error

        return with_retries(attempt, description=f"{self.name} search", retryable=(httpx.HTTPError,))

    def search_scenes(
        self, geometry: dict[str, Any], start: datetime, end: datetime, limit: int = 300
    ) -> list[AcquisitionCandidate]:
        payload: dict[str, Any] = {
            "collections": [self.collection],
            "intersects": geometry,
            "datetime": f"{_iso(start)}/{_iso(end)}",
            "limit": min(_SEARCH_PAGE_SIZE, max(1, limit)),
            "sortby": [{"field": self.sort_field, "direction": "desc"}],
        }
        url = f"{self.stac_url}/search"
        scenes: list[AcquisitionCandidate] = []
        for _page in range(_MAX_PAGES):
            body = self._post_search(url, payload)
            features = body.get("features")
            if not isinstance(features, list):
                raise ProviderResponseError(f"{self.name} response has no feature list.")
            for feature in features:
                scene = self.parse_feature(feature)
                if scene is not None:
                    scenes.append(scene)
            next_link = next(
                (link for link in body.get("links", []) if link.get("rel") == "next"), None
            )
            if not next_link or not features or len(scenes) >= limit:
                break
            if isinstance(next_link.get("body"), dict):
                payload = next_link["body"]
                url = next_link.get("href") or url
            else:
                break
        return deduplicate_scenes(scenes)

    def parse_feature(self, feature: dict[str, Any]) -> AcquisitionCandidate | None:
        """Return a usable scene, or None for a record whose assets cannot be read."""

        if not isinstance(feature, dict) or not isinstance(feature.get("id"), str):
            raise ProviderResponseError(f"{self.name} returned an item without an id.")
        properties = feature.get("properties") or {}
        raw_assets = feature.get("assets") or {}
        assets: dict[str, Any] = {}
        for band, asset_name in {**self.band_assets, "SCL": self.scl_asset}.items():
            asset = raw_assets.get(asset_name) or {}
            href = asset.get("href")
            # Some catalogue records point only at requester-pays s3:// JP2s we cannot read.
            if not isinstance(href, str) or not href.startswith("http"):
                return None
            raster_bands = asset.get("raster:bands") or [{}]
            assets[band] = {"href": href, "scale": raster_bands[0].get("scale")}
        try:
            cloud = properties.get("eo:cloud_cover")
            cloud_cover = float(cloud) if cloud is not None else None
        except (TypeError, ValueError):
            cloud_cover = None
        platform = str(properties.get("platform") or "")
        source_url = next(
            (
                link.get("href")
                for link in feature.get("links", [])
                if isinstance(link, dict) and link.get("rel") in {"self", "canonical"}
            ),
            None,
        )
        return AcquisitionCandidate(
            product_id=feature["id"],
            collection=str(feature.get("collection") or self.collection),
            acquisition_datetime=parse_stac_datetime(properties.get("datetime")),
            cloud_cover=cloud_cover,
            geometry=feature.get("geometry") if isinstance(feature.get("geometry"), dict) else None,
            bbox=feature.get("bbox") if isinstance(feature.get("bbox"), list) else None,
            satellite=platform or None,
            sensor="msi",
            processing_level="L2A",
            source_url=source_url,
            provider=self.name,
            tile=self.tile_of(feature, properties),
            processing_baseline=properties.get("s2:processing_baseline"),
            sequence=self.sequence_of(feature, properties),
            assets=assets,
            raw_properties=self.keep_properties(properties),
        )

    def tile_of(self, feature: dict[str, Any], properties: dict[str, Any]) -> str | None:
        raise NotImplementedError

    def sequence_of(self, feature: dict[str, Any], properties: dict[str, Any]) -> int:
        return 0

    def keep_properties(self, properties: dict[str, Any]) -> dict[str, Any]:
        return {}

    # Imagery ---------------------------------------------------------------------------

    def sign(self, href: str) -> str:
        return href

    def dn_offset(self, scene: AcquisitionCandidate) -> float:
        """The digital-number offset present in this scene's pixels, from metadata alone."""

        raise NotImplementedError

    def _read_asset(self, scene: AcquisitionCandidate, band: str, grid: AOIGrid, *, nearest: bool):
        from rasterio.errors import RasterioError

        asset = scene.assets.get(band)
        if not asset:
            raise ProviderResponseError(f"{scene.product_id} has no {band} asset.")
        return with_retries(
            lambda: read_warped(self.sign(asset["href"]), grid, nearest=nearest),
            description=f"{self.name} read of {band} for {scene.product_id}",
            retryable=(RasterioError, OSError),
            attempts=max(1, settings.provider_retries - 1),
        )

    def read_scl(self, scene: AcquisitionCandidate, grid: AOIGrid) -> np.ndarray:
        return self._read_asset(scene, "SCL", grid, nearest=True).astype(np.uint8)

    def read_bands(self, scene: AcquisitionCandidate, grid: AOIGrid) -> np.ndarray:
        offset = self.dn_offset(scene)  # Before any download, so an ambiguous scene costs nothing.
        with ThreadPoolExecutor(max_workers=len(BAND_NAMES), thread_name_prefix="band") as pool:
            futures = [
                pool.submit(self._read_asset, scene, band, grid, nearest=False)
                for band in BAND_NAMES
            ]
            dn = np.stack([future.result() for future in futures]).astype(np.float32)
        scale = scene.assets[BAND_NAMES[0]].get("scale") or DEFAULT_SCALE
        reflectance = np.clip((dn - offset) * float(scale), 0.0, 1.0)
        reflectance[dn == 0] = 0.0  # Digital number 0 is no-data in every baseline.
        return reflectance.astype(np.float32)


class EarthSearchProvider(StacCogProvider):
    """Element84 Earth Search: public COGs on AWS, no credentials."""

    name = "earth-search"
    stac_url = settings.earth_search_url
    band_assets = {
        "B02": "blue",
        "B03": "green",
        "B04": "red",
        "B08": "nir",
        "B11": "swir16",
        "B12": "swir22",
    }
    scl_asset = "scl"

    def tile_of(self, feature: dict[str, Any], properties: dict[str, Any]) -> str | None:
        code = str(properties.get("grid:code") or "")
        if code.startswith("MGRS-"):
            return code.removeprefix("MGRS-")
        zone, band, square = (
            properties.get("mgrs:utm_zone"),
            properties.get("mgrs:latitude_band"),
            properties.get("mgrs:grid_square"),
        )
        if zone and band and square:
            return f"{int(zone):02d}{band}{square}"
        match = re.match(r"^S2[ABC]_(\d{2}[A-Z]{3})_", feature["id"])
        return match.group(1) if match else None

    def sequence_of(self, feature: dict[str, Any], properties: dict[str, Any]) -> int:
        match = re.search(r"_(\d+)_L2A$", feature["id"])
        return int(match.group(1)) if match else 0

    def keep_properties(self, properties: dict[str, Any]) -> dict[str, Any]:
        return {
            "earthsearch:boa_offset_applied": properties.get("earthsearch:boa_offset_applied"),
        }

    def dn_offset(self, scene: AcquisitionCandidate) -> float:
        """The digital-number offset still in this COG, or an error if metadata cannot say.

        ``raster:bands`` lists offset -0.1 on nearly every modern item, but pixel checks
        against the raw Planetary Computer copy of the same scenes show Earth Search has
        usually removed it already, so applying that metadata would subtract it twice:

        - baseline below 04.00: no offset exists.
        - flag true: pixels are offset-free (S2A_43QBA_20220427_0: 451 here, raw 1446).
        - flag false or absent: either state occurs. S2A_43QBA_20220226_1 still has the
          offset (1224), while S2C_43QBA_20250220_0 does not (187, raw 1187), despite the
          same metadata, and pixel brightness cannot settle it because a bright window looks
          like an offset one. These scenes are read from a provider serving raw values.
        """

        baseline = parse_baseline(scene.processing_baseline)
        if baseline is None or baseline < 4.0:
            return 0.0
        if scene.raw_properties.get("earthsearch:boa_offset_applied") is True:
            return 0.0
        raise AmbiguousRadiometry(
            f"{scene.product_id} has baseline {scene.processing_baseline} without a true "
            "offset flag, so its radiometry cannot be determined from Earth Search metadata."
        )


class PlanetaryComputerProvider(StacCogProvider):
    """Microsoft Planetary Computer: public catalogue, short-lived SAS tokens for reads."""

    name = "planetary-computer"
    stac_url = settings.planetary_computer_url
    sort_field = "datetime"
    band_assets = {band: band for band in BAND_NAMES}
    scl_asset = "SCL"
    _token_margin_seconds = 300

    def __init__(self, stac_url: str | None = None, timeout_seconds: float | None = None):
        super().__init__(stac_url, timeout_seconds)
        self._token: str | None = None
        self._token_expires = 0.0
        self._token_lock = threading.Lock()

    def _sas_token(self) -> str:
        with self._token_lock:
            if self._token and time.time() < self._token_expires - self._token_margin_seconds:
                return self._token

            def attempt() -> dict[str, Any]:
                response = httpx.get(
                    f"{settings.planetary_computer_sas_url}/token/{self.collection}",
                    timeout=self.timeout_seconds,
                    headers={"User-Agent": USER_AGENT},
                )
                response.raise_for_status()
                return response.json()

            body = with_retries(
                attempt, description="Planetary Computer signing", retryable=(httpx.HTTPError,)
            )
            token = body.get("token")
            if not isinstance(token, str) or not token:
                raise ProviderError("Planetary Computer signing returned no token.")
            expiry = str(body.get("msft:expiry") or "")
            try:
                self._token_expires = datetime.fromisoformat(expiry.replace("Z", "+00:00")).timestamp()
            except ValueError:
                self._token_expires = time.time() + 1800
            self._token = token
            return token

    def sign(self, href: str) -> str:
        return f"{href}?{self._sas_token()}"

    def tile_of(self, feature: dict[str, Any], properties: dict[str, Any]) -> str | None:
        tile = properties.get("s2:mgrs_tile")
        return str(tile) if tile else None

    def dn_offset(self, scene: AcquisitionCandidate) -> float:
        """Planetary Computer serves ESA's digital numbers unmodified."""

        baseline = parse_baseline(scene.processing_baseline)
        return RAW_OFFSET_DN if baseline is not None and baseline >= 4.0 else 0.0
