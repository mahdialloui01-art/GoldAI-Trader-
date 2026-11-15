from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def _payload():
    now = datetime.now(timezone.utc)
    release = now - timedelta(minutes=10)
    return {
        "symbol": "GOLD",
        "event_id": "US_RETAIL_2026_11",
        "event_name": "US Retail Sales",
        "release_at": release.isoformat(),
        "actual": "0.4",
        "previous": "0.2",
        "unit": "%",
        "reference_price": 4000.0,
        "reference_price_time": (release - timedelta(minutes=1)).isoformat(),
        "spread_points": 20,
        "candles": [
            {"close_time": (release + timedelta(minutes=1)).isoformat(), "open": 4000.0, "high": 4001.0, "low": 3999.8, "close": 4000.2},
            {"close_time": (release + timedelta(minutes=3)).isoformat(), "open": 4000.2, "high": 4002.0, "low": 4000.1, "close": 4001.0},
            {"close_time": (release + timedelta(minutes=5)).isoformat(), "open": 4001.0, "high": 4003.0, "low": 4000.9, "close": 4002.0},
            {"close_time": (release + timedelta(minutes=7)).isoformat(), "open": 4002.0, "high": 4004.0, "low": 4001.9, "close": 4003.0},
            {"close_time": (release + timedelta(minutes=9)).isoformat(), "open": 4003.0, "high": 4005.0, "low": 4002.9, "close": 4004.0},
        ],
    }


def test_post_release_analysis_reports_observed_direction_without_trade():
    response = client.post("/api/v1/news/post-release-analysis", json=_payload())
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["event"]["actual_vs_previous"] == "ABOVE_PREVIOUS"
    assert "not consensus surprise" in body["event"]["comparison_note"]
    assert body["observed_gold_reaction"]["direction_from_reference"] == "UP"
    assert body["decision"] == "REACTION_CONFIRMED_FOR_RESEARCH"
    assert body["execution_enabled"] is False
    assert body["data_policy"]["mode"] == "RESEARCH_ONLY"


def test_post_release_analysis_does_not_confirm_non_monotonic_move():
    payload = _payload()
    payload["candles"][-1]["close"] = 4001.5
    response = client.post("/api/v1/news/post-release-analysis", json=payload)
    assert response.status_code == 200
    assert response.json()["decision"] == "WAIT_FOR_MORE_DATA_OR_CONFIRMATION"


def test_post_release_analysis_rejects_candle_before_release():
    payload = _payload()
    payload["candles"][0]["close_time"] = (
        datetime.fromisoformat(payload["release_at"]) - timedelta(seconds=1)
    ).isoformat()
    response = client.post("/api/v1/news/post-release-analysis", json=payload)
    assert response.status_code == 422
    assert "strictly after" in response.json()["detail"]


def test_post_release_analysis_blocks_wide_spread():
    payload = _payload()
    payload["spread_points"] = 100
    response = client.post("/api/v1/news/post-release-analysis", json=payload)
    assert response.status_code == 200
    assert response.json()["decision"] == "WAIT_FOR_MORE_DATA_OR_CONFIRMATION"
    assert response.json()["market_conditions"]["spread_acceptable"] is False
