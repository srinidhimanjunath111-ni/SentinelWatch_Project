"""External context for a case: rainfall, disaster feeds, OpenStreetMap and terrain.

All of it is optional. Each source fails independently, is recorded as unavailable, and
can never fail a run. Responses are cached on disk, so a replay is deterministic and a
repeated query does not lean on a public service.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import httpx
import numpy as np

from app.config import settings
from app.satellite.grid import AOIGrid, rasterize_geometry

logger = logging.getLogger(__name__)

OPEN_METEO_ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"
OPEN_METEO_FORECAST = "https://api.open-meteo.com/v1/forecast"
EONET_EVENTS = "https://eonet.gsfc.nasa.gov/api/v3/events"
GDACS_EVENTS = "https://www.gdacs.org/gdacsapi/api/events/geteventlist/SEARCH"
OVERPASS_MIRRORS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
)
DEM_URL = (
    "https://copernicus-dem-30m.s3.amazonaws.com/"
    "Copernicus_DSM_COG_10_{ns}{lat:02d}_00_{ew}{lon:03d}_00_DEM/"
    "Copernicus_DSM_COG_10_{ns}{lat:02d}_00_{ew}{lon:03d}_00_DEM.tif"
)
RAIN_WINDOW_DAYS = 14
RAIN_PRIOR_YEARS = 3
DISASTER_WINDOW_DAYS = 30
DISASTER_RADIUS_KM = 150.0
# Past data does not change; recent data is refreshed in case the feed has caught up.
_FINAL_AFTER_DAYS = 10
_RECENT_TTL_SECONDS = 6 * 3600
_OSM_TTL_SECONDS = 30 * 86400

_FLOOD_WORDS = ("flood",)
_FIRE_WORDS = ("wildfire", "fire")
_LANDSLIDE_WORDS = ("landslide",)


def _cache_path(kind: str, key: str) -> Path:
    digest = hashlib.sha1(key.encode()).hexdigest()[:24]
    return settings.context_cache_dir / kind / f"{digest}.json"


def _cache_get(kind: str, key: str, ttl_seconds: float | None) -> Any | None:
    path = _cache_path(kind, key)
    try:
        if ttl_seconds is not None and time.time() - path.stat().st_mtime > ttl_seconds:
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _cache_put(kind: str, key: str, value: Any) -> None:
    path = _cache_path(kind, key)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(value), encoding="utf-8")
        temporary.replace(path)
    except OSError as error:
        logger.warning("Could not cache %s context: %s", kind, error)


def _ttl_for(end: date) -> float | None:
    return None if (date.today() - end).days >= _FINAL_AFTER_DAYS else _RECENT_TTL_SECONDS


def _get_json(url: str, params: dict[str, Any], *, attempts: int = 2) -> Any:
    error: Exception | None = None
    for attempt in range(attempts):
        try:
            response = httpx.get(
                url,
                params=params,
                timeout=settings.context_timeout_seconds,
                headers={"User-Agent": settings.context_user_agent},
                follow_redirects=True,
            )
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as caught:
            error = caught
            if attempt + 1 < attempts:
                time.sleep(1.0)
    raise RuntimeError(str(error))


def _distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi, dlam = phi2 - phi1, math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return 6371.0 * 2 * math.asin(min(1.0, math.sqrt(a)))


# Rainfall -----------------------------------------------------------------------------


def _daily_precipitation(lat: float, lon: float, start: date, end: date) -> list[float | None]:
    key = f"{lat:.3f},{lon:.3f},{start},{end}"
    cached = _cache_get("rain", key, _ttl_for(end))
    if cached is not None:
        return cached
    body = _get_json(
        OPEN_METEO_ARCHIVE,
        {
            "latitude": f"{lat:.4f}",
            "longitude": f"{lon:.4f}",
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "daily": "precipitation_sum",
            "timezone": "UTC",
        },
    )
    values = (body.get("daily") or {}).get("precipitation_sum") or []
    # The archive lags by a few days, so recent gaps are filled from the forecast API.
    if any(value is None for value in values) or not values:
        past_days = (date.today() - start).days + 1
        if past_days <= 92:
            try:
                recent = _get_json(
                    OPEN_METEO_FORECAST,
                    {
                        "latitude": f"{lat:.4f}",
                        "longitude": f"{lon:.4f}",
                        "past_days": past_days,
                        "forecast_days": 1,
                        "daily": "precipitation_sum",
                        "timezone": "UTC",
                    },
                )
                daily = recent.get("daily") or {}
                by_day = dict(zip(daily.get("time") or [], daily.get("precipitation_sum") or [], strict=False))
                span = (end - start).days + 1
                days = [(start + timedelta(days=offset)).isoformat() for offset in range(span)]
                archive = dict(zip((body.get("daily") or {}).get("time") or days, values, strict=False))
                values = [
                    archive.get(day) if archive.get(day) is not None else by_day.get(day) for day in days
                ]
            except RuntimeError as error:
                logger.info("Recent rainfall fallback unavailable: %s", error)
    _cache_put("rain", key, values)
    return values


def fetch_rainfall(lat: float, lon: float, after_date: date) -> dict[str, Any]:
    start = after_date - timedelta(days=RAIN_WINDOW_DAYS - 1)
    daily = _daily_precipitation(lat, lon, start, after_date)
    present = [float(value) for value in daily if value is not None]
    if not present:
        raise RuntimeError("Open-Meteo returned no rainfall values for this window.")
    result: dict[str, Any] = {
        "available": True,
        "source": "Open-Meteo",
        "window_days": RAIN_WINDOW_DAYS,
        "start_date": start.isoformat(),
        "end_date": after_date.isoformat(),
        "days_with_data": len(present),
        "total_mm": round(sum(present), 1),
        "max_daily_mm": round(max(present), 1),
        "prior_years": [],
        "normal_mm": None,
        "anomaly_percent": None,
    }
    totals: list[float] = []
    for years_back in range(1, RAIN_PRIOR_YEARS + 1):
        try:
            prior_start = start.replace(year=start.year - years_back)
            prior_end = after_date.replace(year=after_date.year - years_back)
        except ValueError:  # 29 February.
            prior_start = start - timedelta(days=365 * years_back)
            prior_end = after_date - timedelta(days=365 * years_back)
        try:
            values = _daily_precipitation(lat, lon, prior_start, prior_end)
        except RuntimeError:
            break
        known = [float(value) for value in values if value is not None]
        if known:
            total = round(sum(known), 1)
            totals.append(total)
            result["prior_years"].append({"year": prior_end.year, "total_mm": total})
    if totals:
        normal = sum(totals) / len(totals)
        result["normal_mm"] = round(normal, 1)
        if normal >= 1.0:
            result["anomaly_percent"] = round(100.0 * (result["total_mm"] - normal) / normal, 1)
    return result


# Disaster feeds -----------------------------------------------------------------------


def _event_type(title: str, types: list[str]) -> str:
    text = " ".join([title, *types]).lower()
    if any(word in text for word in _FLOOD_WORDS):
        return "flood"
    if any(word in text for word in _LANDSLIDE_WORDS):
        return "landslide"
    if any(word in text for word in _FIRE_WORDS):
        return "fire"
    return types[0].lower() if types else "other"


def _point_from_geometry(geometry: dict[str, Any] | None) -> tuple[float, float] | None:
    if not isinstance(geometry, dict):
        return None
    coordinates = geometry.get("coordinates")
    while isinstance(coordinates, list) and coordinates and isinstance(coordinates[0], list):
        coordinates = coordinates[0]
    if isinstance(coordinates, list) and len(coordinates) >= 2:
        try:
            return float(coordinates[1]), float(coordinates[0])
        except (TypeError, ValueError):
            return None
    return None


def fetch_eonet(lat: float, lon: float, after_date: date) -> list[dict[str, Any]]:
    start = after_date - timedelta(days=DISASTER_WINDOW_DAYS)
    west, north, east, south = lon - 1.5, lat + 1.5, lon + 1.5, lat - 1.5
    key = f"{lat:.2f},{lon:.2f},{start},{after_date}"
    body = _cache_get("eonet", key, _ttl_for(after_date))
    if body is None:
        body = _get_json(
            EONET_EVENTS,
            {
                "bbox": f"{west:.3f},{north:.3f},{east:.3f},{south:.3f}",
                "start": start.isoformat(),
                "end": after_date.isoformat(),
                "status": "all",
            },
        )
        _cache_put("eonet", key, body)
    events = []
    for event in body.get("events") or []:
        categories = [item.get("title", "") for item in event.get("categories") or []]
        geometries = event.get("geometry") or []
        point = _point_from_geometry(geometries[-1]) if geometries else None
        distance = _distance_km(lat, lon, *point) if point else None
        if distance is not None and distance > DISASTER_RADIUS_KM:
            continue
        title = str(event.get("title") or "Unnamed event")
        events.append(
            {
                "source": "NASA EONET",
                "title": title,
                "type": _event_type(title, categories),
                "date": (geometries[-1].get("date") or "")[:10] if geometries else None,
                "distance_km": round(distance, 1) if distance is not None else None,
                "url": event.get("link"),
            }
        )
    return events


def fetch_gdacs(lat: float, lon: float, after_date: date) -> list[dict[str, Any]]:
    start = after_date - timedelta(days=DISASTER_WINDOW_DAYS)
    key = f"{start},{after_date}"
    body = _cache_get("gdacs", key, _ttl_for(after_date))
    if body is None:
        body = _get_json(
            GDACS_EVENTS,
            {
                "country": "India",
                "fromDate": start.isoformat(),
                "toDate": after_date.isoformat(),
                "eventlist": "FL;WF;TC;EQ",
            },
        )
        _cache_put("gdacs", key, body)
    events = []
    for feature in body.get("features") or []:
        properties = feature.get("properties") or {}
        point = _point_from_geometry(feature.get("geometry"))
        distance = _distance_km(lat, lon, *point) if point else None
        if distance is None or distance > DISASTER_RADIUS_KM:
            continue
        kind = {"FL": "flood", "WF": "fire", "TC": "cyclone", "EQ": "earthquake"}.get(
            str(properties.get("eventtype")), "other"
        )
        events.append(
            {
                "source": "GDACS",
                "title": str(properties.get("name") or properties.get("htmldescription") or kind.title()),
                "type": kind,
                "date": str(properties.get("fromdate") or "")[:10] or None,
                "distance_km": round(distance, 1),
                "alert_level": properties.get("alertlevel"),
                "url": (properties.get("url") or {}).get("report")
                if isinstance(properties.get("url"), dict)
                else None,
            }
        )
    return events


# OpenStreetMap ------------------------------------------------------------------------


def _stitch_rings(ways: list[list[list[float]]]) -> list[list[list[float]]]:
    """Join open way fragments of a relation into closed rings."""

    rings: list[list[list[float]]] = []
    pending = [list(way) for way in ways if len(way) >= 2]
    while pending:
        current = pending.pop()
        extended = True
        while extended and current[0] != current[-1]:
            extended = False
            for index, way in enumerate(pending):
                if way[0] == current[-1]:
                    current.extend(way[1:])
                elif way[-1] == current[-1]:
                    current.extend(reversed(way[:-1]))
                elif way[-1] == current[0]:
                    current[:0] = way[:-1]
                elif way[0] == current[0]:
                    current[:0] = list(reversed(way[1:]))
                else:
                    continue
                pending.pop(index)
                extended = True
                break
        if len(current) >= 4 and current[0] == current[-1]:
            rings.append(current)
    return rings


def _element_geometry(element: dict[str, Any]) -> dict[str, Any] | None:
    def ring_of(nodes: list[dict[str, float]]) -> list[list[float]]:
        return [[node["lon"], node["lat"]] for node in nodes if "lon" in node and "lat" in node]

    if element.get("type") == "way":
        ring = ring_of(element.get("geometry") or [])
        if len(ring) >= 4 and ring[0] == ring[-1]:
            return {"type": "Polygon", "coordinates": [ring]}
        return None
    if element.get("type") == "relation":
        outers = [
            ring_of(member.get("geometry") or [])
            for member in element.get("members") or []
            if member.get("type") == "way" and member.get("role") in ("outer", "")
        ]
        rings = _stitch_rings(outers)
        if rings:
            return {"type": "MultiPolygon", "coordinates": [[ring] for ring in rings]}
    return None


def fetch_osm_features(bbox: list[float]) -> list[dict[str, Any]]:
    """Quarries and protected areas inside the AOI, with geometry for raster overlap."""

    west, south, east, north = bbox
    key = f"{west:.4f},{south:.4f},{east:.4f},{north:.4f}"
    cached = _cache_get("osm", key, _OSM_TTL_SECONDS)
    if cached is not None:
        return cached
    box = f"{south:.5f},{west:.5f},{north:.5f},{east:.5f}"
    query = (
        "[out:json][timeout:25];("
        f'way["landuse"="quarry"]({box});relation["landuse"="quarry"]({box});'
        f'way["boundary"="protected_area"]({box});relation["boundary"="protected_area"]({box});'
        f'way["leisure"="nature_reserve"]({box});relation["leisure"="nature_reserve"]({box});'
        ");out tags geom;"
    )
    errors: list[str] = []
    body = None
    for mirror in OVERPASS_MIRRORS:
        try:
            response = httpx.post(
                mirror,
                data={"data": query},
                timeout=max(settings.context_timeout_seconds, 30.0),
                headers={"User-Agent": settings.context_user_agent},
            )
            response.raise_for_status()
            body = response.json()
            break
        except (httpx.HTTPError, ValueError) as error:
            errors.append(f"{mirror}: {error}")
    if body is None:
        raise RuntimeError("All Overpass mirrors failed. " + " | ".join(errors))
    features = []
    for element in body.get("elements") or []:
        tags = element.get("tags") or {}
        if tags.get("landuse") == "quarry":
            kind = "quarry"
        elif tags.get("boundary") == "protected_area" or tags.get("leisure") == "nature_reserve":
            kind = "protected_area"
        else:
            continue
        features.append(
            {
                "kind": kind,
                "name": tags.get("name") or tags.get("name:en"),
                "osm_id": f"{element.get('type')}/{element.get('id')}",
                "geometry": _element_geometry(element),
            }
        )
    _cache_put("osm", key, features)
    return features


# Terrain ------------------------------------------------------------------------------


def _dem_tiles(grid: AOIGrid) -> list[str]:
    west, south, east, north = grid.wgs84_bounds_inner()
    urls = []
    for lat in range(math.floor(south), math.floor(north) + 1):
        for lon in range(math.floor(west), math.floor(east) + 1):
            urls.append(
                DEM_URL.format(
                    ns="N" if lat >= 0 else "S",
                    lat=abs(lat),
                    ew="E" if lon >= 0 else "W",
                    lon=abs(lon),
                )
            )
    return urls


def read_slope_degrees(grid: AOIGrid) -> np.ndarray | None:
    """Slope from the public Copernicus 30 m DEM on the AOI grid; None if unreachable."""

    import rasterio
    from rasterio.enums import Resampling
    from rasterio.errors import RasterioError
    from rasterio.vrt import WarpedVRT

    from app.satellite.stac import GDAL_ENV

    path = settings.context_cache_dir / "dem" / f"{grid.key}.npy"
    try:
        if path.is_file():
            return np.load(path)
    except (OSError, ValueError):
        path.unlink(missing_ok=True)
    elevation = np.full(grid.shape, np.nan, dtype=np.float32)
    try:
        with rasterio.Env(**GDAL_ENV):
            for url in _dem_tiles(grid):
                with rasterio.open(url) as source, WarpedVRT(
                    source,
                    crs=grid.crs,
                    transform=grid.transform,
                    width=grid.width,
                    height=grid.height,
                    resampling=Resampling.bilinear,
                    src_nodata=-32767,
                    nodata=-32767,
                ) as warped:
                    tile = warped.read(1).astype(np.float32)
                    elevation = np.where(tile > -32000, tile, elevation)
    except (RasterioError, OSError) as error:
        logger.info("Terrain unavailable: %s", error)
        return None
    if np.isnan(elevation).all():
        return None
    elevation = np.nan_to_num(elevation, nan=float(np.nanmedian(elevation)))
    dy, dx = np.gradient(elevation, grid.pixel_size)
    slope = np.degrees(np.arctan(np.hypot(dx, dy))).astype(np.float32)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("wb") as handle:
            np.save(handle, slope)
    except OSError as error:
        logger.info("Could not cache terrain: %s", error)
    return slope


# Assembly -----------------------------------------------------------------------------


def fetch_case_context(
    lat: float, lon: float, bbox: list[float], after_date: date
) -> dict[str, Any]:
    """Query every source concurrently, recording each failure instead of raising it."""

    context: dict[str, Any] = {
        "rainfall": {"available": False},
        "disasters": [],
        "protected_areas": [],
        "osm_features": [],
        "unavailable": [],
    }
    jobs = {
        "rainfall": lambda: fetch_rainfall(lat, lon, after_date),
        "eonet": lambda: fetch_eonet(lat, lon, after_date),
        "gdacs": lambda: fetch_gdacs(lat, lon, after_date),
        "osm": lambda: fetch_osm_features(bbox),
    }
    with ThreadPoolExecutor(max_workers=len(jobs), thread_name_prefix="context") as pool:
        futures = {name: pool.submit(job) for name, job in jobs.items()}
        for name, future in futures.items():
            try:
                value = future.result()
            except Exception as error:  # Context is optional by design; record and continue.
                logger.info("Context source %s unavailable: %s", name, error)
                context["unavailable"].append({"source": name, "reason": str(error)[:300]})
                continue
            if name == "rainfall":
                context["rainfall"] = value
            elif name in ("eonet", "gdacs"):
                context["disasters"].extend(value)
            else:
                context["osm_features"] = value
    return context


def rasterize_osm(
    features: list[dict[str, Any]], grid: AOIGrid, kind: str
) -> tuple[np.ndarray | None, list[str]]:
    mask: np.ndarray | None = None
    names: list[str] = []
    for feature in features:
        if feature.get("kind") != kind or not feature.get("geometry"):
            continue
        try:
            burned = rasterize_geometry(feature["geometry"], grid)
        except Exception as error:  # Community geometry can be malformed; skip that feature.
            logger.info("Skipping unusable OSM geometry %s: %s", feature.get("osm_id"), error)
            continue
        if burned.any():
            mask = burned if mask is None else mask | burned
            if feature.get("name"):
                names.append(str(feature["name"]))
    return mask, names


def public_context(context: dict[str, Any]) -> dict[str, Any]:
    """The stored and served form: names and flags, without bulky OSM geometry."""

    def strip(feature: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in feature.items() if key != "geometry"}

    osm = context.get("osm_features") or []
    return {
        "rainfall": context.get("rainfall") or {"available": False},
        "disasters": context.get("disasters") or [],
        "protected_areas": [strip(f) for f in osm if f.get("kind") == "protected_area"],
        "osm_features": [strip(f) for f in osm if f.get("kind") != "protected_area"],
        "authorised_zones": context.get("authorised_zones") or [],
        "unavailable": context.get("unavailable") or [],
    }
