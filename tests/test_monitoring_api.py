from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app


def _aoi_payload() -> dict:
    return {
        "name": "Test forest boundary",
        "geometry": {
            "type": "Polygon",
            "coordinates": [
                [[77.59, 12.96], [77.60, 12.96], [77.60, 12.97], [77.59, 12.97], [77.59, 12.96]]
            ],
        },
        "categories": ["VEGETATION_LOSS", "CONSTRUCTION"],
        "max_cloud_cover": 20,
        "min_change_area_m2": 400,
        "sensitivity": "MEDIUM",
        "monitoring_interval_minutes": 1440,
    }


def test_aoi_crud_pause_resume_and_system_status():
    with TestClient(app) as client:
        create = client.post("/api/areas", json=_aoi_payload())
        assert create.status_code == 201
        area = create.json()
        assert area["bbox"] == [77.59, 12.96, 77.6, 12.97]
        assert area["status"] == "ACTIVE"
        assert client.get("/api/areas").json()[0]["id"] == area["id"]

        pause = client.post(f"/api/areas/{area['id']}/pause")
        assert pause.status_code == 200
        assert pause.json()["status"] == "PAUSED"
        blocked = client.post(f"/api/areas/{area['id']}/check-now")
        assert blocked.status_code == 409

        resume = client.post(f"/api/areas/{area['id']}/resume")
        assert resume.status_code == 200
        assert resume.json()["status"] == "ACTIVE"
        assert client.delete(f"/api/areas/{area['id']}").status_code == 204

        system = client.get("/api/system/status")
        assert system.status_code == 200
        assert system.json()["provider"] == "Copernicus Data Space Ecosystem"


def test_aoi_rejects_invalid_geometry_and_empty_categories():
    invalid = _aoi_payload()
    invalid["geometry"] = {"type": "Point", "coordinates": [77.59, 12.96]}
    with TestClient(app) as client:
        response = client.post("/api/areas", json=invalid)
        assert response.status_code == 422

        invalid = _aoi_payload()
        invalid["categories"] = []
        response = client.post("/api/areas", json=invalid)
        assert response.status_code == 422
