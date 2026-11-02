"""A stable 10 m analysis grid for one AOI, independent of any particular scene.

Every scene of an AOI is read onto the same grid: same UTM zone, same origin, same shape.
That is what makes pixels from different dates, tiles and even UTM zones comparable.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from typing import Any

import numpy as np

from app.config import settings
from app.satellite.geometry import geometry_bbox, geometry_center

PIXEL_SIZE_M = 10.0


class AOITooLargeError(ValueError):
    """Raised before any network access when an AOI exceeds the configured window."""


def utm_epsg_for(latitude: float, longitude: float) -> int:
    zone = min(60, max(1, int(math.floor((longitude + 180.0) / 6.0)) + 1))
    return (32600 if latitude >= 0 else 32700) + zone


@dataclass(frozen=True)
class AOIGrid:
    epsg: int
    west: float  # UTM metres of the outer pixel edges, multiples of the pixel size.
    north: float
    width: int
    height: int
    pixel_size: float = PIXEL_SIZE_M

    @property
    def crs(self) -> str:
        return f"EPSG:{self.epsg}"

    @property
    def transform(self):
        from rasterio.transform import from_origin

        return from_origin(self.west, self.north, self.pixel_size, self.pixel_size)

    @property
    def shape(self) -> tuple[int, int]:
        return self.height, self.width

    @property
    def pixel_area_m2(self) -> float:
        return self.pixel_size**2

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        return (
            self.west,
            self.north - self.height * self.pixel_size,
            self.west + self.width * self.pixel_size,
            self.north,
        )

    @property
    def key(self) -> str:
        """Short cache key; any AOI with the same grid can share cached windows."""

        text = f"{self.epsg}:{self.west}:{self.north}:{self.width}:{self.height}:{self.pixel_size}"
        return hashlib.sha1(text.encode()).hexdigest()[:16]

    def to_wgs84(self, x: np.ndarray | float, y: np.ndarray | float):
        from pyproj import Transformer

        return Transformer.from_crs(self.epsg, 4326, always_xy=True).transform(x, y)

    def pixel_to_lonlat(self, column: float, row: float) -> tuple[float, float]:
        x = self.west + (column + 0.5) * self.pixel_size
        y = self.north - (row + 0.5) * self.pixel_size
        lon, lat = self.to_wgs84(x, y)
        return float(lon), float(lat)

    def wgs84_bounds_inner(self) -> tuple[float, float, float, float]:
        """West, south, east, north of the largest lat/lon box inside the UTM rectangle.

        A UTM grid is slightly rotated against lat/lon away from the central meridian, so
        evidence warped to this box has no no-data slivers and aligns exactly on a map.
        """

        left, bottom, right, top = self.bounds
        lons, lats = self.to_wgs84(
            np.array([left, right, right, left]), np.array([top, top, bottom, bottom])
        )
        return (
            float(max(lons[0], lons[3])),
            float(max(lats[2], lats[3])),
            float(min(lons[1], lons[2])),
            float(min(lats[0], lats[1])),
        )


def build_grid(geometry: dict[str, Any]) -> AOIGrid:
    """Build the AOI grid from its centroid's UTM zone, or refuse an oversized AOI."""

    from pyproj import Transformer

    west, south, east, north = geometry_bbox(geometry)
    latitude, longitude = geometry_center(geometry)
    epsg = utm_epsg_for(latitude, longitude)
    transformer = Transformer.from_crs(4326, epsg, always_xy=True)
    # A lat/lon box bulges in UTM, so transform points along each edge rather than corners.
    steps = np.linspace(0.0, 1.0, 11)
    lons = np.concatenate(
        [west + (east - west) * steps, np.full(11, east), west + (east - west) * steps, np.full(11, west)]
    )
    lats = np.concatenate(
        [np.full(11, south), south + (north - south) * steps, np.full(11, north), south + (north - south) * steps]
    )
    xs, ys = transformer.transform(lons, lats)
    # Snap to whole pixels of the 10 m Sentinel-2 lattice so reads need no resampling.
    x_min = math.floor(float(np.min(xs)) / PIXEL_SIZE_M) * PIXEL_SIZE_M
    x_max = math.ceil(float(np.max(xs)) / PIXEL_SIZE_M) * PIXEL_SIZE_M
    y_min = math.floor(float(np.min(ys)) / PIXEL_SIZE_M) * PIXEL_SIZE_M
    y_max = math.ceil(float(np.max(ys)) / PIXEL_SIZE_M) * PIXEL_SIZE_M
    width = int(round((x_max - x_min) / PIXEL_SIZE_M))
    height = int(round((y_max - y_min) / PIXEL_SIZE_M))
    limit = settings.max_aoi_request_pixels
    if width > limit or height > limit:
        side_km = limit * PIXEL_SIZE_M / 1000
        raise AOITooLargeError(
            f"AOI spans {width * PIXEL_SIZE_M / 1000:.1f} km x {height * PIXEL_SIZE_M / 1000:.1f} km; "
            f"the maximum is {side_km:.1f} km on either side. Draw a smaller area or split it."
        )
    if width < 8 or height < 8:
        raise AOITooLargeError(
            "AOI is smaller than 80 m across, which is too small to analyse at 10 m resolution."
        )
    return AOIGrid(epsg=epsg, west=x_min, north=y_max, width=width, height=height)


def rasterize_geometry(geometry: dict[str, Any], grid: AOIGrid) -> np.ndarray:
    """Burn a WGS84 GeoJSON geometry onto the grid as a bool mask."""

    from rasterio.features import rasterize
    from rasterio.warp import transform_geom

    projected = transform_geom("EPSG:4326", grid.crs, geometry)
    mask = rasterize(
        [(projected, 1)],
        out_shape=grid.shape,
        transform=grid.transform,
        fill=0,
        dtype="uint8",
        all_touched=False,
    )
    return mask.astype(bool)


def mask_to_geojson(mask: np.ndarray, grid: AOIGrid) -> dict[str, Any] | None:
    """Vectorise a bool mask to a WGS84 Polygon or MultiPolygon."""

    from rasterio.features import shapes
    from rasterio.warp import transform_geom

    if not mask.any():
        return None
    polygons = [
        geometry["coordinates"]
        for geometry, value in shapes(mask.astype(np.uint8), mask=mask, transform=grid.transform)
        if value
    ]
    merged: dict[str, Any] = (
        {"type": "Polygon", "coordinates": polygons[0]}
        if len(polygons) == 1
        else {"type": "MultiPolygon", "coordinates": polygons}
    )
    return transform_geom(grid.crs, "EPSG:4326", merged, precision=6)
