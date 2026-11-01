"""Regression tests for auditable, non-hallucinated monitoring outcomes."""

from __future__ import annotations

import json
from datetime import datetime

from fastapi.testclient import TestClient

from app.database import SessionLocal
from app.main import app
from app.models import (
    AnalysisRecord,
    AOIAcquisition,
    AuditLog,
    Case,
    CaseReview,
    MonitoringArea,
    MonitoringRun,
    SatelliteAcquisition,
)
from app.satellite.provider import (
    AcquisitionCandidate,
    AcquisitionPair,
    ProviderAuthRequired,
    ProviderError,
)
from app.services.monitoring import execute_monitoring_run, queue_monitoring_run


def _area_geometry() -> dict:
    return {
        "type": "Polygon",
        "coordinates": [
            [[77.59, 12.96], [77.60, 12.96], [77.60, 12.97], [77.59, 12.97], [77.59, 12.96]]
        ],
    }


def _queue_area(*, status: str = "ACTIVE", name: str = "Truthful-state AOI") -> tuple[str, str]:
    db = SessionLocal()
    try:
        area = MonitoringArea(
            name=name,
            geometry_json=json.dumps(_area_geometry()),
            bbox_json="[77.59, 12.96, 77.60, 12.97]",
            center_lat=12.965,
            center_lon=77.595,
            categories_json='["VEGETATION_LOSS"]',
            status=status,
        )
        db.add(area)
        db.commit()
        db.refresh(area)
        run = queue_monitoring_run(db, area)
        return area.id, run.id
    finally:
        db.close()


def _candidate(product_id: str, day: int) -> AcquisitionCandidate:
    return AcquisitionCandidate(
        product_id=product_id,
        collection="sentinel-2-l2a",
        acquisition_datetime=datetime(2025, 2, day, 10, 0),
        cloud_cover=4.0,
        geometry=None,
        bbox=[77.59, 12.96, 77.60, 12.97],
        satellite="sentinel-2a",
        sensor="msi",
        processing_level="L2A",
        source_url=f"https://example.invalid/{product_id}",
    )


def test_no_catalogue_candidates_is_a_terminal_auditable_non_detection(monkeypatch):
    class EmptyCatalogueProvider:
        stac_url = "https://stac.dataspace.copernicus.eu/v1"
        collection = "sentinel-2-l2a"
        has_credentials = False

        def search_acquisitions(self, *args, **kwargs):
            return []

        def select_pair(self, *args, **kwargs):
            raise AssertionError("Pair selection must not run without two acquisitions.")

    monkeypatch.setattr("app.services.monitoring.CopernicusDataSpaceProvider", EmptyCatalogueProvider)
    area_id, run_id = _queue_area(name="Empty catalogue AOI")

    execute_monitoring_run(run_id)

    db = SessionLocal()
    try:
        run = db.get(MonitoringRun, run_id)
        area = db.get(MonitoringArea, area_id)
        assert run is not None
        assert area is not None
        assert run.status == "NO_SUITABLE_ACQUISITION"
        assert run.stage == "NO_SUITABLE_ACQUISITION"
        assert run.progress_percent == 100
        assert run.completed_at is not None
        assert run.catalogue_query_json is not None
        assert area.last_checked_at is not None
        assert db.query(SatelliteAcquisition).count() == 0
        assert db.query(AnalysisRecord).count() == 0
        assert db.query(Case).count() == 0
    finally:
        db.close()


def test_provider_error_is_persisted_without_creating_evidence(monkeypatch):
    class UnavailableProvider:
        stac_url = "https://stac.dataspace.copernicus.eu/v1"
        collection = "sentinel-2-l2a"
        has_credentials = False

        def search_acquisitions(self, *args, **kwargs):
            raise ProviderError("STAC service did not respond")

    monkeypatch.setattr("app.services.monitoring.CopernicusDataSpaceProvider", UnavailableProvider)
    _, run_id = _queue_area(name="Unavailable provider AOI")

    execute_monitoring_run(run_id)

    db = SessionLocal()
    try:
        run = db.get(MonitoringRun, run_id)
        assert run is not None
        assert run.status == "FAILED"
        assert run.stage == "FAILED"
        assert run.progress_percent == 100
        assert run.error_code == "PROVIDER_ERROR"
        assert run.error_detail == "STAC service did not respond"
        assert run.before_acquisition_id is None
        assert run.after_acquisition_id is None
        assert db.query(SatelliteAcquisition).count() == 0
        assert db.query(AnalysisRecord).count() == 0
        assert db.query(Case).count() == 0
    finally:
        db.close()


def test_paused_area_skips_provider_access_and_records_reason(monkeypatch):
    def unexpected_provider():
        raise AssertionError("A paused AOI must not contact the satellite provider.")

    monkeypatch.setattr("app.services.monitoring.CopernicusDataSpaceProvider", unexpected_provider)
    _, run_id = _queue_area(status="PAUSED", name="Paused AOI")

    execute_monitoring_run(run_id)

    db = SessionLocal()
    try:
        run = db.get(MonitoringRun, run_id)
        assert run is not None
        assert run.status == "SKIPPED_PAUSED"
        assert run.error_code is None
        assert "no external data request" in run.message
    finally:
        db.close()


def test_auth_failure_during_retrieval_never_claims_a_completed_analysis(monkeypatch):
    before, after = _candidate("S2_AUTH_BEFORE", 1), _candidate("S2_AUTH_AFTER", 25)

    class ExpiredCredentialProvider:
        stac_url = "https://stac.dataspace.copernicus.eu/v1"
        collection = "sentinel-2-l2a"
        has_credentials = True

        def search_acquisitions(self, *args, **kwargs):
            return [before, after]

        def select_pair(self, *args, **kwargs):
            return AcquisitionPair(before=before, after=after, rationale="test pair")

        def download_multiband(self, *args, **kwargs):
            raise ProviderAuthRequired("OAuth token was rejected")

    monkeypatch.setattr(
        "app.services.monitoring.CopernicusDataSpaceProvider", ExpiredCredentialProvider
    )
    _, run_id = _queue_area(name="Expired credentials AOI")

    execute_monitoring_run(run_id)

    db = SessionLocal()
    try:
        run = db.get(MonitoringRun, run_id)
        assert run is not None
        assert run.status == "BLOCKED_CREDENTIALS"
        assert run.stage == "BLOCKED_CREDENTIALS"
        assert run.error_code == "COPERNICUS_AUTH_FAILED"
        assert run.error_detail == "OAuth token was rejected"
        assert run.before_acquisition_id is not None
        assert run.after_acquisition_id is not None
        assert db.query(AOIAcquisition).count() == 2
        assert db.query(AnalysisRecord).count() == 0
        assert db.query(Case).count() == 0
    finally:
        db.close()


def test_model_endpoint_reports_untrained_state_without_invented_metrics(monkeypatch):
    def not_trained_state() -> tuple[str, str, str]:
        return "NOT_TRAINED", "cpu", "No validated checkpoint has been registered."

    monkeypatch.setattr("app.services.system.current_model_state", not_trained_state)
    with TestClient(app) as client:
        models = client.get("/api/models")
        system = client.get("/api/system/status")

    assert models.status_code == 200
    assert len(models.json()) == 1
    model = models.json()[0]
    assert model["status"] == "NOT_TRAINED"
    assert model["metrics"] == {}
    assert model["training_dataset"] is None
    assert system.status_code == 200
    assert system.json()["model_status"] == "NOT_TRAINED"
    assert "No validated checkpoint" in system.json()["message"]


def test_case_review_endpoint_preserves_human_decision_and_audit_event():
    with TestClient(app) as client:
        db = SessionLocal()
        try:
            case = Case(
                title="Reviewable monitoring lead",
                source="Copernicus Data Space / Sentinel-2 L2A",
                before_image="/files/before.jpg",
                after_image="/files/after.jpg",
                mask_image="/files/mask.jpg",
                data_origin="copernicus_operational",
            )
            db.add(case)
            db.commit()
            db.refresh(case)
            case_id = case.id
        finally:
            db.close()

        response = client.post(
            f"/api/cases/{case_id}/reviews",
            json={
                "action": "verified",
                "note": "Field team should validate this lead.",
                "reviewer": "project-supervisor",
            },
        )
        detail = client.get(f"/api/cases/{case_id}")

    assert response.status_code == 201
    assert response.json()["action"] == "verified"
    assert detail.status_code == 200
    assert detail.json()["status"] == "verified"
    assert detail.json()["review_note"] == "Field team should validate this lead."
    assert len(detail.json()["reviews"]) == 1

    db = SessionLocal()
    try:
        assert db.query(CaseReview).filter(CaseReview.case_id == case_id).count() == 1
        audit = (
            db.query(AuditLog)
            .filter(AuditLog.entity_id == case_id, AuditLog.event_type == "human_review_recorded")
            .one()
        )
        assert json.loads(audit.payload_json)["action"] == "verified"
    finally:
        db.close()
