"""External context is optional: it stays inside its budget and can never fail a run."""

from __future__ import annotations

import json
import threading
import time
from datetime import date

import httpx
import pytest

from app.config import settings
from app.database import SessionLocal
from app.models import Case
from app.services import context as context_module
from app.services.context import (
    fetch_case_context,
    fetch_eonet,
    fetch_gdacs,
    fetch_osm_features,
    fetch_rainfall,
    public_context,
    update_case_context,
)

DAY = date(2024, 8, 1)
BBOX = [76.1, 11.4, 76.2, 11.5]


class _Response:
    def __init__(self, body, status=200):
        self.status_code = status
        self._body = body
        self.content = b"" if body is None else b"x"

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("bad", request=None, response=self)

    def json(self):
        return self._body


def _series_response(start, end):
    days = (end - start).days + 1
    from datetime import timedelta

    return {
        "daily": {
            "time": [(start + timedelta(days=n)).isoformat() for n in range(days)],
            "precipitation_sum": [10.0] * days,
        }
    }


def test_rainfall_comes_from_one_request_with_anomaly_and_gap_peak(monkeypatch):
    calls = []

    def fake_get(url, params, **_):
        calls.append(params)
        return _Response(_series_response(date.fromisoformat(params["start_date"]), date.fromisoformat(params["end_date"])))

    monkeypatch.setattr(context_module.httpx, "get", fake_get)
    result = fetch_rainfall(11.5, 76.1, DAY, date(2024, 2, 5))
    assert len(calls) == 1
    assert result["total_mm"] == 140.0 and result["window_days"] == 14
    assert result["normal_mm"] == 140.0 and result["anomaly_percent"] == 0.0
    assert len(result["prior_years"]) == 3
    assert result["peak_14d_mm"] == 140.0
    fetch_rainfall(11.5, 76.1, DAY, date(2024, 2, 5))
    assert len(calls) == 1, "A second call is served from the disk cache"


def test_rainfall_with_no_values_raises_for_the_caller_to_record(monkeypatch):
    monkeypatch.setattr(context_module.httpx, "get", lambda *a, **k: _Response({"daily": {}}))
    with pytest.raises(RuntimeError):
        fetch_rainfall(11.5, 76.1, DAY)


def test_gdacs_empty_body_means_no_events(monkeypatch):
    monkeypatch.setattr(context_module.httpx, "get", lambda *a, **k: _Response(None))
    assert fetch_gdacs(11.5, 76.1, DAY) == []


def test_disaster_feeds_keep_only_nearby_events(monkeypatch):
    near = {
        "properties": {"eventtype": "FL", "name": "Kerala flood", "fromdate": "2024-07-30", "alertlevel": "Orange"},
        "geometry": {"type": "Point", "coordinates": [76.2, 11.6]},
    }
    far = {"properties": {"eventtype": "FL", "name": "Assam flood"}, "geometry": {"type": "Point", "coordinates": [92.0, 26.0]}}
    monkeypatch.setattr(context_module.httpx, "get", lambda *a, **k: _Response({"features": [near, far]}))
    events = fetch_gdacs(11.5, 76.1, DAY)
    assert [event["title"] for event in events] == ["Kerala flood"]
    assert events[0]["type"] == "flood" and events[0]["distance_km"] < 150

    eonet = {
        "events": [
            {
                "title": "Wildfire near site",
                "categories": [{"title": "Wildfires"}],
                "geometry": [{"date": "2024-07-20T00:00:00Z", "type": "Point", "coordinates": [76.1, 11.5]}],
                "link": "https://example.invalid/event",
            }
        ]
    }
    monkeypatch.setattr(context_module.httpx, "get", lambda *a, **k: _Response(eonet))
    assert fetch_eonet(11.5, 76.1, DAY)[0]["type"] == "fire"


def _ring(*points):
    return [{"lat": lat, "lon": lon} for lon, lat in points]


def test_overpass_falls_back_across_mirrors_and_sends_a_user_agent(monkeypatch):
    calls = []
    body = {
        "elements": [
            {
                "type": "way",
                "id": 1,
                "tags": {"landuse": "quarry", "name": "Rawa quarry"},
                "geometry": _ring((76.1, 11.4), (76.2, 11.4), (76.2, 11.5), (76.1, 11.4)),
            },
            {
                "type": "relation",
                "id": 2,
                "tags": {"boundary": "protected_area", "name": "Reserve"},
                "members": [
                    {"type": "way", "role": "outer", "geometry": _ring((76.1, 11.4), (76.2, 11.4))},
                    {"type": "way", "role": "outer", "geometry": _ring((76.2, 11.4), (76.2, 11.5), (76.1, 11.4))},
                ],
            },
        ]
    }

    def fake_post(url, data, timeout, headers):
        calls.append((url, headers))
        if len(calls) == 1:
            return _Response(None, status=406)
        return _Response(body)

    monkeypatch.setattr(context_module.httpx, "post", fake_post)
    features = fetch_osm_features(BBOX)
    assert len(calls) == 2 and calls[0][0] != calls[1][0]
    assert "SentinelWatch" in calls[0][1]["User-Agent"]
    assert {feature["kind"] for feature in features} == {"quarry", "protected_area"}
    assert all(feature["geometry"] for feature in features)
    fetch_osm_features(BBOX)
    assert len(calls) == 2, "Results are cached on disk"


def test_failed_overpass_is_left_alone_for_a_while(monkeypatch):
    calls = []

    def down(*a, **k):
        calls.append(1)
        raise httpx.ConnectTimeout("down")

    monkeypatch.setattr(context_module.httpx, "post", down)
    with pytest.raises(RuntimeError):
        fetch_osm_features(BBOX)
    first = len(calls)
    with pytest.raises(RuntimeError):
        fetch_osm_features(BBOX)
    assert len(calls) == first


def _stub_sources(monkeypatch, *, rainfall=None, eonet=None, gdacs=None, osm=None):
    monkeypatch.setattr(context_module, "fetch_rainfall", rainfall or (lambda *a: {"available": True, "total_mm": 1.0}))
    monkeypatch.setattr(context_module, "fetch_eonet", eonet or (lambda *a: []))
    monkeypatch.setattr(context_module, "fetch_gdacs", gdacs or (lambda *a: []))
    monkeypatch.setattr(context_module, "fetch_osm_features", osm or (lambda *a: []))


def test_a_failing_source_is_recorded_and_the_rest_are_kept(monkeypatch):
    def broken(*_):
        raise RuntimeError("feed down")

    _stub_sources(monkeypatch, eonet=broken)
    context, late = fetch_case_context(11.5, 76.1, BBOX, DAY)
    assert context["rainfall"]["total_mm"] == 1.0
    assert context["unavailable"] == [{"source": "eonet", "reason": "feed down"}]
    assert late._futures == {}


def test_slow_source_cannot_hold_up_the_alert_and_fills_in_the_case_afterwards(monkeypatch):
    monkeypatch.setattr(settings, "context_budget_seconds", 0.1)
    release = threading.Event()
    feature = {"kind": "quarry", "name": "Late quarry", "osm_id": "way/1", "geometry": {"type": "Polygon"}}

    def slow_osm(*_):
        release.wait(5)
        return [feature]

    _stub_sources(monkeypatch, osm=slow_osm)
    started = time.monotonic()
    context, late = fetch_case_context(11.5, 76.1, BBOX, DAY)
    assert time.monotonic() - started < 2, "The budget holds even with a stuck source"
    assert context["unavailable"][0]["source"] == "osm"
    assert context["rainfall"]["available"] is True

    db = SessionLocal()
    case = Case(
        title="c", before_image="/b", after_image="/a", mask_image="/m",
        context_json=json.dumps(public_context(context)),
    )
    db.add(case)
    db.commit()
    case_id = case.id
    db.close()

    late.finish_later(case_id)
    release.set()
    deadline = time.monotonic() + 5
    stored = {}
    while time.monotonic() < deadline:
        db = SessionLocal()
        stored = json.loads(db.get(Case, case_id).context_json)
        db.close()
        if stored["osm_features"]:
            break
        time.sleep(0.02)
    assert [item["name"] for item in stored["osm_features"]] == ["Late quarry"]
    assert "geometry" not in stored["osm_features"][0], "Bulky geometry stays out of the record"
    assert stored["unavailable"] == []


def test_late_failure_keeps_the_source_marked_unavailable():
    db = SessionLocal()
    case = Case(
        title="c", before_image="/b", after_image="/a", mask_image="/m",
        context_json=json.dumps({"unavailable": [{"source": "osm", "reason": "first"}]}),
    )
    db.add(case)
    db.commit()
    case_id = case.id
    db.close()
    update_case_context(case_id, {}, {"osm": "All mirrors failed"})
    db = SessionLocal()
    stored = json.loads(db.get(Case, case_id).context_json)
    db.close()
    assert stored["unavailable"] == [{"source": "osm", "reason": "All mirrors failed"}]
