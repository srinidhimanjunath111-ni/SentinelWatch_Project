from __future__ import annotations

import json
from datetime import datetime

from app.database import SessionLocal
from app.models import AnalysisRecord, AOIAcquisition, Case, MonitoringArea, MonitoringRun
from app.satellite.provider import AcquisitionCandidate, AcquisitionPair
from app.services.monitoring import execute_monitoring_run, queue_monitoring_run


def _candidate(product_id: str, day: int) -> AcquisitionCandidate:
    return AcquisitionCandidate(
        product_id=product_id,
        collection="sentinel-2-l2a",
        acquisition_datetime=datetime(2025, 1, day, 10, 0),
        cloud_cover=5.0,
        geometry=None,
        bbox=[77.59, 12.96, 77.60, 12.97],
        satellite="sentinel-2a",
        sensor="msi",
        processing_level="L2A",
        source_url=f"https://example.invalid/{product_id}",
    )


def test_monitoring_records_real_catalogue_metadata_then_blocks_without_credentials(monkeypatch):
    before, after = _candidate("S2_REAL_BEFORE", 1), _candidate("S2_REAL_AFTER", 25)

    class FakeProvider:
        stac_url = "https://stac.dataspace.copernicus.eu/v1"
        collection = "sentinel-2-l2a"
        has_credentials = False

        def search_acquisitions(self, *args, **kwargs):
            return [after, before]

        def select_pair(self, *args, **kwargs):
            return AcquisitionPair(before=before, after=after, rationale="test pair")

    monkeypatch.setattr("app.services.monitoring.CopernicusDataSpaceProvider", FakeProvider)
    db = SessionLocal()
    area = MonitoringArea(
        name="Service test AOI",
        geometry_json=json.dumps(
            {"type": "Polygon", "coordinates": [[[77.59, 12.96], [77.60, 12.96], [77.60, 12.97], [77.59, 12.97], [77.59, 12.96]]]}
        ),
        bbox_json="[77.59, 12.96, 77.60, 12.97]",
        center_lat=12.965,
        center_lon=77.595,
        categories_json='["VEGETATION_LOSS"]',
    )
    db.add(area)
    db.commit()
    db.refresh(area)
    area_id = area.id
    run = queue_monitoring_run(db, area)
    db.close()

    execute_monitoring_run(run.id)

    db = SessionLocal()
    stored = db.get(MonitoringRun, run.id)
    assert stored is not None
    assert stored.status == "BLOCKED_CREDENTIALS"
    assert stored.before_acquisition_id is not None
    assert stored.after_acquisition_id is not None
    assert db.query(AOIAcquisition).filter(AOIAcquisition.aoi_id == area_id).count() == 2
    assert db.query(AnalysisRecord).count() == 0
    assert db.query(Case).count() == 0
    assert stored.error_code == "COPERNICUS_CREDENTIALS_MISSING"
    assert "metadata was recorded" in stored.message
    db.close()
