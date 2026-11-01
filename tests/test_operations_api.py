"""Deterministic contracts for priority discovery and local alert routing."""

from __future__ import annotations

from datetime import datetime

from fastapi.testclient import TestClient

from app.main import app
from app.satellite.provider import AcquisitionCandidate


def _region() -> dict:
    return {
        "region_name": "Test discovery region",
        "geometry": {
            "type": "Polygon",
            "coordinates": [[[77.59, 12.96], [77.61, 12.96], [77.61, 12.98], [77.59, 12.98], [77.59, 12.96]]],
        },
        "grid_size": 1,
        "max_cloud_cover": 20,
        "lookback_days": 30,
    }


def test_priority_discovery_is_persisted_and_can_be_approved(monkeypatch):
    class Provider:
        name = "copernicus-data-space"
        collection = "sentinel-2-l2a"

        def search_acquisitions(self, *_args, **_kwargs):
            return [
                AcquisitionCandidate(
                    product_id="REAL_METADATA_TEST_PRODUCT",
                    collection=self.collection,
                    acquisition_datetime=datetime.utcnow(),
                    cloud_cover=5.0,
                    geometry=None,
                    bbox=None,
                    satellite="S2A",
                    sensor="MSI",
                    processing_level="L2A",
                    source_url="https://example.invalid/product",
                    assets={},
                    raw_properties={},
                )
            ]

    monkeypatch.setattr("app.services.priority_discovery.CopernicusDataSpaceProvider", Provider)
    with TestClient(app) as client:
        discovered = client.post("/api/priority-candidates/discover", json=_region())
        assert discovered.status_code == 200
        candidate = discovered.json()[0]
        assert candidate["status"] == "PRELIMINARY"
        assert candidate["evidence"]["newest_product_id"] == "REAL_METADATA_TEST_PRODUCT"

        approved = client.post(
            f"/api/priority-candidates/{candidate['id']}/approve", json={"name": "Approved test AOI"}
        )
        assert approved.status_code == 200
        assert approved.json()["status"] == "APPROVED"
        assert approved.json()["monitoring_area_id"]

        status = client.get("/api/data-status")
        assert status.status_code == 200
        assert status.json()["database"] == "connected"


def test_department_assignment_records_truthful_notification_state():
    with TestClient(app) as client:
        department = client.post(
            "/api/departments",
            json={"name": "Forest review", "category": "vegetation", "recipient_email": "review@example.test"},
        )
        assert department.status_code == 201

        from app.database import SessionLocal
        from app.models import Alert, Case

        db = SessionLocal()
        try:
            case = Case(
                title="Routing test", before_image="/files/a.jpg", after_image="/files/b.jpg", mask_image="/files/m.jpg"
            )
            db.add(case)
            db.flush()
            alert = Alert(case_id=case.id, title="Review", message="Test", severity="medium")
            db.add(alert)
            db.commit()
            alert_id = alert.id
        finally:
            db.close()

        assigned = client.post(
            f"/api/alerts/{alert_id}/assign",
            json={"department_id": department.json()["id"], "notify_email": True},
        )
        assert assigned.status_code == 200
        statuses = {item["status"] for item in assigned.json()}
        assert "RECORDED" in statuses
        assert "NOT_CONFIGURED" in statuses or "SENT" in statuses or "FAILED" in statuses
