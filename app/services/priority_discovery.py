"""Transparent automatic priority-area discovery from a user-supplied region.

Candidates are generated as a grid, then checked against the real CDSE catalogue.
They are deliberately labelled preliminary because catalogue availability and cloud
quality are not evidence that a physical environmental incident occurred.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.models import CatalogueRefresh, PriorityCandidate
from app.satellite.copernicus import CopernicusDataSpaceProvider
from app.satellite.geometry import geometry_bbox
from app.satellite.provider import ProviderError


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _grid_cells(geometry: dict[str, Any], grid_size: int) -> list[dict[str, Any]]:
    """Create WGS84 grid cells inside the bounding extent; each remains a candidate."""

    west, south, east, north = geometry_bbox(geometry)
    cells: list[dict[str, Any]] = []
    lon_step, lat_step = (east - west) / grid_size, (north - south) / grid_size
    for row in range(grid_size):
        for col in range(grid_size):
            x1, x2 = west + col * lon_step, west + (col + 1) * lon_step
            y1, y2 = south + row * lat_step, south + (row + 1) * lat_step
            cells.append(
                {
                    "type": "Polygon",
                    "coordinates": [[[x1, y1], [x2, y1], [x2, y2], [x1, y2], [x1, y1]]],
                }
            )
    return cells


def discover_priority_candidates(
    db: Session,
    *,
    region_name: str,
    geometry: dict[str, Any],
    grid_size: int,
    max_cloud_cover: float,
    lookback_days: int,
) -> list[PriorityCandidate]:
    """Persist candidates based on actual availability/freshness metadata when reachable."""

    provider = CopernicusDataSpaceProvider()
    end, start = _now(), _now() - timedelta(days=lookback_days)
    interval = f"{start.isoformat()}Z/{end.isoformat()}Z"
    created: list[PriorityCandidate] = []
    for position, cell in enumerate(_grid_cells(geometry, grid_size), start=1):
        bbox = geometry_bbox(cell)
        query = {
            "collection": provider.collection,
            "datetime": interval,
            "max_cloud_cover": max_cloud_cover,
            "geometry": cell,
        }
        try:
            observations = provider.search_acquisitions(cell, interval, max_cloud_cover, limit=10)
            newest = observations[0] if observations else None
            average_cloud = (
                sum(item.cloud_cover for item in observations if item.cloud_cover is not None)
                / max(1, sum(item.cloud_cover is not None for item in observations))
                if observations
                else None
            )
            freshness = max(0.0, 1.0 - ((end - newest.acquisition_datetime).days / lookback_days)) if newest else 0.0
            cloud_quality = 1.0 - min((average_cloud or max_cloud_cover) / 100.0, 1.0)
            availability = min(len(observations) / 4.0, 1.0)
            score = round(100 * (0.45 * freshness + 0.35 * cloud_quality + 0.20 * availability), 1)
            evidence = {
                "observation_count": len(observations),
                "newest_product_id": newest.product_id if newest else None,
                "newest_acquisition_at": newest.acquisition_datetime.isoformat() if newest else None,
                "average_catalogue_cloud_cover": average_cloud,
                "score_formula": "45% freshness + 35% cloud quality + 20% observation availability",
            }
            explanation = (
                "Grid-generated preliminary candidate scored from real catalogue freshness, cloud "
                "quality and observation availability. It is not a detected land-change incident."
            )
            refresh_status, refresh_message = "SUCCESS", f"Found {len(observations)} catalogue item(s)."
            newest_at = newest.acquisition_datetime if newest else None
        except ProviderError as error:
            score, evidence = 0.0, {"provider_error": str(error), "score_formula": "unavailable"}
            explanation = "Grid-generated preliminary candidate. Catalogue evidence is unavailable; no change claim was made."
            refresh_status, refresh_message, newest_at = "FAILED", str(error), None
        db.add(
            CatalogueRefresh(
                provider=provider.name,
                query_json=json.dumps(query),
                status=refresh_status,
                result_count=int(evidence.get("observation_count", 0)),
                newest_acquisition_at=newest_at,
                message=refresh_message,
            )
        )
        candidate = PriorityCandidate(
            geometry_json=json.dumps(cell),
            bbox_json=json.dumps(bbox),
            region_name=f"{region_name} / grid cell {position}",
            discovery_method="GRID_CATALOGUE_AVAILABILITY",
            score=score,
            explanation=explanation,
            evidence_json=json.dumps(evidence),
            status="PRELIMINARY",
        )
        db.add(candidate)
        created.append(candidate)
    db.commit()
    for candidate in created:
        db.refresh(candidate)
    return created
