from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

import app.main as main
from app.main import app

client = TestClient(app)


def test_health_reports_paper_mode_by_default():
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["mode"] == "paper"
    assert body["live_trading_enabled"] is False


def test_config_does_not_expose_secrets():
    response = client.get("/api/v1/config")
    assert response.status_code == 200
    body = response.json()
    assert body["trading_mode"] == "paper"
    assert body["live_trading_enabled"] is False
    assert "password" not in body
    assert "api_key" not in body


def test_market_snapshot_returns_provider_price_and_marks_it_non_tradable(monkeypatch):
    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {
                "price": 4012.34,
                "currency": "USD",
                "updatedAt": datetime.now(timezone.utc).isoformat(),
            }

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url):
            assert url == main.settings.gold_spot_api_url
            return FakeResponse()

    monkeypatch.setattr(main.httpx, "AsyncClient", lambda **kwargs: FakeClient())
    response = client.get("/api/v1/market/spot-reference")
    assert response.status_code == 200
    body = response.json()
    assert body["price"] == 4012.34
    assert body["quote_type"] == "indicative_spot"
    assert body["tradable_quote"] is False
    assert body["source"] == "gold-api.com"


def test_market_snapshot_rejects_stale_provider_data(monkeypatch):
    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {
                "price": 4012.34,
                "currency": "USD",
                "updatedAt": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
            }

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url):
            return FakeResponse()

    monkeypatch.setattr(main.httpx, "AsyncClient", lambda **kwargs: FakeClient())
    response = client.get("/api/v1/market/spot-reference")
    assert response.status_code == 503
    assert "stale" in response.json()["detail"]


def test_market_snapshot_handles_provider_failure(monkeypatch):
    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url):
            raise main.httpx.ConnectError("offline")

    monkeypatch.setattr(main.httpx, "AsyncClient", lambda **kwargs: FakeClient())
    response = client.get("/api/v1/market/spot-reference")
    assert response.status_code == 503


def test_signal_waits_when_filters_are_not_aligned():
    response = client.post(
        "/api/v1/signals/evaluate",
        json={
            "symbol": "XAUUSD",
            "timeframe": "M15",
            "current_price": 2350.0,
            "ema_fast": 2351.0,
            "ema_slow": 2349.0,
            "rsi": 45.0,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["direction"] == "WAIT"
    assert body["confidence"] == 0
    assert body["disclaimer"]


def test_signal_rejects_unsupported_symbol():
    response = client.post(
        "/api/v1/signals/evaluate",
        json={"symbol": "EURUSD", "current_price": 1.08},
    )
    assert response.status_code == 400


def test_risk_check_blocks_position_over_risk_limit():
    response = client.post(
        "/api/v1/risk/check",
        json={
            "account_equity": 1000,
            "stop_distance_points": 100,
            "point_value_per_lot": 1,
            "proposed_lots": 0.1,
            "daily_loss_percent": 0,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["approved"] is False
    assert body["estimated_risk_percent"] == 1.0


def test_risk_check_blocks_when_daily_loss_limit_is_reached():
    response = client.post(
        "/api/v1/risk/check",
        json={
            "account_equity": 10000,
            "stop_distance_points": 10,
            "point_value_per_lot": 1,
            "proposed_lots": 0.1,
            "daily_loss_percent": 2.0,
        },
    )
    assert response.status_code == 200
    assert response.json()["approved"] is False


def test_mt5_quote_requires_token_and_becomes_primary_market_snapshot(monkeypatch):
    main._latest_mt5_quote = None
    monkeypatch.setattr(main.settings, "mt5_bridge_token", "test-secret")
    payload = {
        "symbol": "XAUUSD", "bid": 4010.10, "ask": 4010.35, "last": 0,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "digits": 2, "point": 0.01, "spread_points": 25,
    }
    denied = client.post("/api/v1/mt5/quote", json=payload)
    assert denied.status_code == 401
    accepted = client.post(
        "/api/v1/mt5/quote", json=payload,
        headers={"Authorization": "Bearer test-secret"},
    )
    assert accepted.status_code == 200
    snapshot = client.get("/api/v1/market/spot-reference")
    assert snapshot.status_code == 200
    body = snapshot.json()
    assert body["source"] == "MetaTrader 5 broker feed"
    assert body["bid"] == 4010.10
    assert body["ask"] == 4010.35
    assert body["tradable_quote"] is True


def test_market_snapshot_does_not_fall_back_to_public_spot():
    main._latest_mt5_quote = None
    response = client.get("/api/v1/market/spot-reference")
    assert response.status_code == 503
    assert "MetaTrader 5" in response.json()["detail"]


def test_mt5_quote_rejects_wrong_symbol(monkeypatch):
    monkeypatch.setattr(main.settings, "mt5_bridge_token", "test-secret")
    payload = {
        "symbol": "EURUSD", "bid": 1.1, "ask": 1.2,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "digits": 5, "point": 0.00001, "spread_points": 10000,
    }
    response = client.post(
        "/api/v1/mt5/quote", json=payload,
        headers={"Authorization": "Bearer test-secret"},
    )
    assert response.status_code == 400
