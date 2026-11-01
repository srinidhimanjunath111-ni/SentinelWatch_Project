"""First-run monitoring sites and authorised zones, driven by files in app/data/.

Run ``python -m app.seed_sites`` to add any listed sites missing from an existing database.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from app.config import settings
from app.database import SessionLocal
from app.models import AuthorisedZone, MonitoringArea
from app.satellite.geometry import bbox_polygon, geometry_center, validate_geometry
from app.schemas import AuthorisedZoneCreate
from app.services.audit import record_audit
from app.timeutil import utcnow

logger = logging.getLogger(__name__)

_DATA_DIR = Path(__file__).resolve().parent / "data"
SITES_FILE = _DATA_DIR / "monitored_sites.json"
ZONES_FILE = _DATA_DIR / "authorised_zones.json"


def load_sites(path: Path = SITES_FILE) -> list[dict[str, Any]]:
    return json.loads(path.read_text(encoding="utf-8"))


def _area_from_site(site: dict[str, Any]) -> MonitoringArea:
    bbox = [float(value) for value in site["bbox"]]
    geometry = validate_geometry(bbox_polygon(bbox))
    latitude, longitude = geometry_center(geometry)
    return MonitoringArea(
        name=site["name"],
        geometry_json=json.dumps(geometry),
        bbox_json=json.dumps(bbox),
        center_lat=latitude,
        center_lon=longitude,
        categories_json=json.dumps(site.get("categories") or []),
        max_cloud_cover=float(site.get("max_cloud_cover", 40.0)),
        min_change_area_m2=float(site.get("min_change_area_m2", 5000.0)),
        sensitivity=site.get("sensitivity", "MEDIUM"),
        monitoring_interval_minutes=int(
            site.get("interval", settings.default_monitoring_interval_minutes)
        ),
        status="ACTIVE",
        state=site.get("state"),
        district=site.get("district"),
        site_type=site.get("site_type"),
        description=site.get("description"),
        sources_json=json.dumps(site.get("sources") or []),
        next_check_at=utcnow(),
    )


def seed_sites(sites: list[dict[str, Any]] | None = None) -> int:
    """Add listed sites and zones not already present, matched by name."""

    db = SessionLocal()
    added = 0
    try:
        existing = {name for (name,) in db.query(MonitoringArea.name).all()}
        for site in load_sites() if sites is None else sites:
            if site["name"] in existing:
                continue
            area = _area_from_site(site)
            db.add(area)
            db.flush()
            record_audit(db, "monitoring_area", area.id, "aoi_seeded", {"name": area.name})
            added += 1
        known_zones = {name for (name,) in db.query(AuthorisedZone.name).all()}
        for entry in json.loads(ZONES_FILE.read_text(encoding="utf-8")) if ZONES_FILE.is_file() else []:
            if entry["name"] in known_zones:
                continue
            zone = AuthorisedZoneCreate(**entry)
            db.add(
                AuthorisedZone(
                    **zone.model_dump(exclude={"geometry"}), geometry_json=json.dumps(zone.geometry)
                )
            )
        db.commit()
    finally:
        db.close()
    return added


def seed_sites_if_empty() -> int:
    if not settings.seed_sites_on_first_run:
        return 0
    db = SessionLocal()
    try:
        if db.query(MonitoringArea.id).first() is not None:
            return 0
    finally:
        db.close()
    count = seed_sites()
    logger.info("Seeded %d monitored site(s) on first run.", count)
    return count


if __name__ == "__main__":
    from app.migrations import migrate_schema

    settings.ensure_directories()
    migrate_schema()
    print(f"Added {seed_sites()} site(s).")
