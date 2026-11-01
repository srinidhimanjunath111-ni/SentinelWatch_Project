"""Contract tests for the optional, truthful PDF case-report endpoint."""

from __future__ import annotations

from fastapi.testclient import TestClient

import app.main as main_module
from app.database import SessionLocal
from app.models import Case
from app.services.reporting import ReportDependencyUnavailable


def _stored_case_id() -> str:
    db = SessionLocal()
    try:
        case = Case(
            title="Report test case",
            location="Test AOI",
            before_date="2026-01-01",
            after_date="2026-02-01",
            source="Copernicus test metadata",
            analysis_method="spectral-baseline",
            overall_confidence=0.7,
            changed_area_px=10,
            changed_area_m2=1000.0,
            highest_severity="medium",
            before_image="/files/before.png",
            after_image="/files/after.png",
            mask_image="/files/evidence.png",
            data_origin="operational_monitoring",
            requires_human_verification=True,
        )
        db.add(case)
        db.commit()
        return case.id
    finally:
        db.close()


def test_case_report_downloads_when_renderer_returns_pdf(monkeypatch):
    case_id = _stored_case_id()

    monkeypatch.setattr(
        main_module, "build_case_investigation_report", lambda case, events: b"%PDF-test-report"
    )
    with TestClient(main_module.app) as client:
        response = client.get(f"/api/cases/{case_id}/report")

    assert response.status_code == 200
    assert response.content == b"%PDF-test-report"
    assert response.headers["content-type"].startswith("application/pdf")
    assert "attachment" in response.headers["content-disposition"]
    assert case_id in response.headers["content-disposition"]


def test_case_report_returns_clear_503_when_reportlab_is_unavailable(monkeypatch):
    case_id = _stored_case_id()

    def unavailable(*_args, **_kwargs):
        raise ReportDependencyUnavailable("PDF reporting requires ReportLab. Install requirements-operational.txt first.")

    monkeypatch.setattr(main_module, "build_case_investigation_report", unavailable)
    with TestClient(main_module.app) as client:
        response = client.get(f"/api/cases/{case_id}/report")

    assert response.status_code == 503
    assert "ReportLab" in response.json()["detail"]
