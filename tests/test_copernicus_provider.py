from __future__ import annotations

from datetime import datetime

from app.satellite.copernicus import CopernicusDataSpaceProvider


def _feature(product_id: str, timestamp: str, cloud_cover: float, platform: str = "sentinel-2a") -> dict:
    return {
        "type": "Feature",
        "id": product_id,
        "collection": "sentinel-2-l2a",
        "geometry": {
            "type": "Polygon",
            "coordinates": [[[77.59, 12.96], [77.60, 12.96], [77.60, 12.97], [77.59, 12.97], [77.59, 12.96]]],
        },
        "bbox": [77.59, 12.96, 77.60, 12.97],
        "properties": {
            "datetime": timestamp,
            "eo:cloud_cover": cloud_cover,
            "platform": platform,
            "instruments": ["msi"],
            "processing:level": "L2A",
        },
        "assets": {"B04": {"href": "https://example.invalid/b04.jp2"}},
        "links": [{"rel": "self", "href": f"https://example.invalid/items/{product_id}"}],
    }


def test_parse_and_quality_select_a_realistic_sentinel_pair():
    provider = CopernicusDataSpaceProvider(client_id="", client_secret="")
    older = provider._parse_feature(_feature("OLDER", "2025-01-01T10:00:00Z", 4.0))
    newer = provider._parse_feature(_feature("NEWER", "2025-02-15T10:00:00Z", 11.0))
    too_close = provider._parse_feature(_feature("CLOSE", "2025-02-25T10:00:00Z", 1.0))
    pair = provider.select_pair([too_close, older, newer], min_gap_days=20)
    assert pair is not None
    assert pair.before.product_id == "OLDER"
    assert pair.after.product_id == "CLOSE"
    assert pair.before.acquisition_datetime == datetime(2025, 1, 1, 10, 0)
    assert pair.after.collection == "sentinel-2-l2a"


def test_search_sends_official_stac_filters(monkeypatch):
    captured: dict = {}

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"features": [_feature("SCENE", "2025-01-01T10:00:00Z", 8.0)]}

    def fake_post(url, json, timeout):
        captured.update({"url": url, "payload": json, "timeout": timeout})
        return FakeResponse()

    monkeypatch.setattr("app.satellite.copernicus.httpx.post", fake_post)
    provider = CopernicusDataSpaceProvider(
        stac_url="https://stac.dataspace.copernicus.eu/v1", client_id="", client_secret=""
    )
    geometry = _feature("X", "2025-01-01T10:00:00Z", 1.0)["geometry"]
    items = provider.search_acquisitions(geometry, "2025-01-01/2025-02-01", 20.0)
    assert captured["url"] == "https://stac.dataspace.copernicus.eu/v1/search"
    assert captured["payload"]["collections"] == ["sentinel-2-l2a"]
    assert captured["payload"]["query"] == {"eo:cloud_cover": {"lte": 20.0}}
    assert items[0].product_id == "SCENE"
