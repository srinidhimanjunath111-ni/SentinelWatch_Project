"""Monitored sites and authorised zones, driven by files in app/data/.

Seeding is an upsert keyed on each site's stable ``key``, so an edited bbox or a renamed site
updates its row in place. A row only changes when its file entry changed, so edits made
through the API survive a restart. A site or zone deleted through the API stays deleted.

Run ``python -m app.seed_sites`` to apply the files to an existing database.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any

from app.config import settings
from app.database import SessionLocal
from app.models import AuditLog, AuthorisedZone, MonitoringArea
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


def _entry_hash(site: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(site, sort_keys=True).encode()).hexdigest()


def _apply_site(area: MonitoringArea, site: dict[str, Any]) -> None:
    bbox = [float(value) for value in site["bbox"]]
    geometry = validate_geometry(bbox_polygon(bbox))
    latitude, longitude = geometry_center(geometry)
    area.site_key = site["key"]
    area.seed_hash = _entry_hash(site)
    area.name = site["name"]
    area.geometry_json = json.dumps(geometry)
    area.bbox_json = json.dumps(bbox)
    area.center_lat = latitude
    area.center_lon = longitude
    area.categories_json = json.dumps(site.get("categories") or [])
    area.max_cloud_cover = float(site.get("max_cloud_cover", 40.0))
    area.min_change_area_m2 = float(site.get("min_change_area_m2", 5000.0))
    area.sensitivity = site.get("sensitivity", "MEDIUM")
    area.monitoring_interval_minutes = int(
        site.get("interval", settings.default_monitoring_interval_minutes)
    )
    area.state = site.get("state")
    area.district = site.get("district")
    area.site_type = site.get("site_type")
    area.description = site.get("description")
    area.sources_json = json.dumps(site.get("sources") or [])
    area.suggested_replay_json = json.dumps(site["replay"]) if site.get("replay") else None


def _deleted_through_api(db, event_type: str, field: str) -> set[str]:
    rows = db.query(AuditLog.payload_json).filter(AuditLog.event_type == event_type).all()
    return {str(json.loads(payload).get(field)) for (payload,) in rows}


def seed_sites(sites: list[dict[str, Any]] | None = None) -> int:
    """Insert or update listed sites. Returns how many rows were added or changed."""

    db = SessionLocal()
    changed = 0
    try:
        deleted = _deleted_through_api(db, "aoi_deleted", "site_key")
        for site in load_sites() if sites is None else sites:
            if site["key"] in deleted:
                continue
            area = db.query(MonitoringArea).filter(MonitoringArea.site_key == site["key"]).first()
            if area is None:
                # A row from before site keys existed is adopted by its name.
                area = db.query(MonitoringArea).filter(MonitoringArea.name == site["name"]).first()
            if area is None:
                area = MonitoringArea(status="ACTIVE", next_check_at=utcnow())
                db.add(area)
                event = "aoi_seeded"
            elif area.seed_hash == _entry_hash(site):
                continue
            else:
                event = "aoi_reseeded"
            _apply_site(area, site)
            db.flush()
            record_audit(db, "monitoring_area", area.id, event, {"site_key": site["key"]})
            changed += 1
        _seed_zones(db)
        db.commit()
    finally:
        db.close()
    return changed


def _seed_zones(db) -> None:
    if not ZONES_FILE.is_file():
        return
    deleted = _deleted_through_api(db, "zone_deleted", "name")
    for entry in json.loads(ZONES_FILE.read_text(encoding="utf-8")):
        if entry["name"] in deleted:
            continue
        zone = AuthorisedZoneCreate(**entry)
        values = zone.model_dump(exclude={"geometry"})
        values["geometry_json"] = json.dumps(zone.geometry)
        existing = db.query(AuthorisedZone).filter(AuthorisedZone.name == zone.name).first()
        if existing is None:
            db.add(AuthorisedZone(**values))
        else:
            for name, value in values.items():
                setattr(existing, name, value)


def apply_seed_files() -> int:
    if not settings.seed_sites_on_first_run:
        return 0
    count = seed_sites()
    if count:
        logger.info("Applied %d monitored-site change(s) from the seed file.", count)
    return count


if __name__ == "__main__":
    from app.migrations import migrate_schema

    settings.ensure_directories()
    migrate_schema()
    print(f"Added or updated {seed_sites()} site(s).")
