from fastapi.testclient import TestClient

from app.main import app


def test_health_reports_version_and_uptime():
    with TestClient(app) as client:
        body = client.get("/api/health").json()
    assert body["status"] == "ok"
    assert body["version"] == "3.0.0"
    assert body["uptime_seconds"] >= 0


def test_dashboard_is_served():
    with TestClient(app) as client:
        response = client.get("/")
    assert response.status_code == 200
    assert "SentinelWatch" in response.text
