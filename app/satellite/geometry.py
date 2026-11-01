"""Small GeoJSON helpers used before making a remote satellite request."""

from __future__ import annotations

from collections.abc import Iterable
from copy import deepcopy
from typing import Any


class GeometryValidationError(ValueError):
    """Raised when a submitted AOI cannot safely be used as a WGS84 geometry."""


def _position(value: Any) -> list[float]:
    if not isinstance(value, (list, tuple)) or len(value) < 2:
        raise GeometryValidationError("Each coordinate must be a [longitude, latitude] pair.")
    try:
        longitude, latitude = float(value[0]), float(value[1])
    except (TypeError, ValueError) as error:
        raise GeometryValidationError("AOI coordinates must be numeric.") from error
    if not -180 <= longitude <= 180 or not -90 <= latitude <= 90:
        raise GeometryValidationError("AOI coordinates must use WGS84 longitude/latitude ranges.")
    return [round(longitude, 8), round(latitude, 8)]


def _normalise_ring(ring: Any) -> list[list[float]]:
    if not isinstance(ring, list) or len(ring) < 3:
        raise GeometryValidationError("Each polygon ring needs at least three vertices.")
    positions = [_position(position) for position in ring]
    if positions[0] != positions[-1]:
        positions.append(positions[0])
    if len(positions) < 4:
        raise GeometryValidationError("Each polygon ring needs at least three distinct vertices.")
    if len({tuple(point) for point in positions[:-1]}) < 3:
        raise GeometryValidationError("Each polygon ring needs at least three distinct vertices.")
    return positions


def _normalise_polygon(polygon: Any) -> list[list[list[float]]]:
    if not isinstance(polygon, list) or not polygon:
        raise GeometryValidationError("A polygon must contain at least one ring.")
    return [_normalise_ring(ring) for ring in polygon]


def validate_geometry(value: dict[str, Any]) -> dict[str, Any]:
    """Return a safe GeoJSON Polygon/MultiPolygon, accepting a GeoJSON Feature too."""

    if not isinstance(value, dict):
        raise GeometryValidationError("AOI geometry must be a GeoJSON object.")
    geometry = deepcopy(value.get("geometry")) if value.get("type") == "Feature" else deepcopy(value)
    if not isinstance(geometry, dict):
        raise GeometryValidationError("The GeoJSON Feature does not contain a geometry.")
    kind = geometry.get("type")
    coordinates = geometry.get("coordinates")
    if kind == "Polygon":
        normalised = [_normalise_polygon(coordinates)]
    elif kind == "MultiPolygon":
        if not isinstance(coordinates, list) or not coordinates:
            raise GeometryValidationError("A MultiPolygon needs one or more polygons.")
        normalised = [_normalise_polygon(polygon) for polygon in coordinates]
    else:
        raise GeometryValidationError("Only GeoJSON Polygon and MultiPolygon AOIs are supported.")

    points = list(iter_positions({"type": "MultiPolygon", "coordinates": normalised}))
    west, east = min(point[0] for point in points), max(point[0] for point in points)
    south, north = min(point[1] for point in points), max(point[1] for point in points)
    if west == east or south == north:
        raise GeometryValidationError("AOI must cover a non-zero geographic area.")
    if kind == "Polygon":
        return {"type": "Polygon", "coordinates": normalised[0]}
    return {"type": "MultiPolygon", "coordinates": normalised}


def iter_positions(geometry: dict[str, Any]) -> Iterable[list[float]]:
    """Yield exterior and interior coordinates from a validated geometry."""

    if geometry.get("type") == "Polygon":
        polygons = [geometry.get("coordinates", [])]
    elif geometry.get("type") == "MultiPolygon":
        polygons = geometry.get("coordinates", [])
    else:
        raise GeometryValidationError("Only Polygon and MultiPolygon AOIs are supported.")
    for polygon in polygons:
        for ring in polygon:
            for position in ring:
                yield position


def geometry_bbox(geometry: dict[str, Any]) -> list[float]:
    points = list(iter_positions(geometry))
    if not points:
        raise GeometryValidationError("AOI geometry has no positions.")
    return [
        min(point[0] for point in points),
        min(point[1] for point in points),
        max(point[0] for point in points),
        max(point[1] for point in points),
    ]


def geometry_center(geometry: dict[str, Any]) -> tuple[float, float]:
    west, south, east, north = geometry_bbox(geometry)
    return ((south + north) / 2, (west + east) / 2)


def bbox_polygon(bbox: list[float]) -> dict[str, Any]:
    west, south, east, north = bbox
    return {
        "type": "Polygon",
        "coordinates": [
            [[west, south], [east, south], [east, north], [west, north], [west, south]]
        ],
    }
