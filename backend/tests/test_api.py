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
    main._latest_mt5_quote = {
        "symbol": "GOLD", "bid": 2350.0, "ask": 2350.2, "last": 0,
        "mid": 2350.1, "spread_points": 20, "digits": 2, "point": 0.01,
        "source": "MetaTrader 5 broker feed",
        "source_updated_at": datetime.now(timezone.utc),
        "age_seconds": 0, "quote_type": "broker_bid_ask", "tradable_quote": True,
    }
    response = client.post(
        "/api/v1/signals/evaluate",
        json={
            "symbol": "GOLD",
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


def test_signal_endpoint_fails_closed_without_fresh_mt5_quote():
    main._latest_mt5_quote = None
    response = client.post(
        "/api/v1/signals/evaluate",
        json={"symbol": "GOLD", "timeframe": "M5", "current_price": 2350.0},
    )
    assert response.status_code == 503
    assert "MetaTrader 5" in response.json()["detail"]


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
        "symbol": "GOLD", "bid": 4010.10, "ask": 4010.35, "last": 0,
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
    snapshot = client.get("/api/v1/market/snapshot")
    assert snapshot.status_code == 200
    body = snapshot.json()
    assert body["source"] == "MetaTrader 5 broker feed"
    assert body["symbol"] == "GOLD"
    assert body["bid"] == 4010.10
    assert body["ask"] == 4010.35
    assert body["tradable_quote"] is True


def test_market_snapshot_does_not_fall_back_to_public_spot():
    main._latest_mt5_quote = None
    response = client.get("/api/v1/market/snapshot")
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


def _technical_payload(timeframe="M5", count=100, start_offset_seconds=0):
    now = datetime.now(timezone.utc) - timedelta(seconds=start_offset_seconds)
    seconds = main._TIMEFRAME_SECONDS[timeframe]
    candles = []
    for index in range(count):
        close = 2300.0 + index * 0.2
        open_price = close - 0.1
        candles.append({
            "time": (now - timedelta(seconds=(count - index - 1) * seconds)).isoformat(),
            "open": open_price,
            "high": close + 0.4,
            "low": open_price - 0.4,
            "close": close,
            "tick_volume": 100,
        })
    return {"symbol": "GOLD", "timeframe": timeframe, "candles": candles}


def _set_fresh_quote():
    main._latest_mt5_quote = {
        "symbol": "GOLD", "bid": 2300.0, "ask": 2300.2, "last": 0,
        "mid": 2300.1, "spread_points": 20, "digits": 2, "point": 0.01,
        "source": "MetaTrader 5 broker feed",
        "source_updated_at": datetime.now(timezone.utc),
        "age_seconds": 0, "quote_type": "broker_bid_ask", "tradable_quote": True,
    }



def test_multi_timeframe_requires_all_three_frames_to_align():
    _set_fresh_quote()
    main._latest_mt5_quote["spread_points"] = 1
    payload = {
        "symbol": "GOLD",
        "m5": _technical_payload("M5")["candles"],
        "m15": _technical_payload("M15")["candles"],
        "h1": _technical_payload("H1")["candles"],
    }
    response = client.post("/api/v1/analysis/multi-timeframe", json=payload)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["execution_enabled"] is False
    assert set(body["timeframes"]) == {"M5", "M15", "H1"}
    assert body["alignment"]["M5"] == "BUY"
    assert body["alignment"]["M15"] == "BUY"
    assert body["alignment"]["H1"] == "BUY"
    assert body["direction"] == "BUY"


def test_multi_timeframe_fails_closed_when_one_frame_is_stale():
    _set_fresh_quote()
    payload = {
        "symbol": "GOLD",
        "m5": _technical_payload("M5")["candles"],
        "m15": _technical_payload("M15")["candles"],
        "h1": _technical_payload("H1", start_offset_seconds=24 * 3600)["candles"],
    }
    response = client.post("/api/v1/analysis/multi-timeframe", json=payload)
    assert response.status_code == 422
    assert "stale" in response.json()["detail"]


def test_multi_timeframe_fails_closed_without_mt5_quote():
    main._latest_mt5_quote = None
    payload = {
        "symbol": "GOLD",
        "m5": _technical_payload("M5")["candles"],
        "m15": _technical_payload("M15")["candles"],
        "h1": _technical_payload("H1")["candles"],
    }
    response = client.post("/api/v1/analysis/multi-timeframe", json=payload)
    assert response.status_code == 503


def test_technical_analysis_returns_real_indicator_fields():
    _set_fresh_quote()
    response = client.post("/api/v1/analysis/technical", json=_technical_payload())
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["symbol"] == "GOLD"
    assert body["execution_enabled"] is False
    assert "rsi14_wilder" in body["indicators"]
    assert "macd_histogram" in body["indicators"]
    assert "bollinger_upper" in body["indicators"]
    assert "confidence_note" in body


def test_technical_analysis_rejects_invalid_ohlc():
    _set_fresh_quote()
    payload = _technical_payload()
    payload["candles"][-1]["high"] = payload["candles"][-1]["open"] - 1
    response = client.post("/api/v1/analysis/technical", json=payload)
    assert response.status_code == 422


def test_technical_analysis_rejects_non_increasing_timestamps():
    _set_fresh_quote()
    payload = _technical_payload()
    payload["candles"][-1]["time"] = payload["candles"][-2]["time"]
    response = client.post("/api/v1/analysis/technical", json=payload)
    assert response.status_code == 422
    assert "strictly increasing" in response.json()["detail"]


def test_technical_analysis_rejects_unsupported_timeframe():
    _set_fresh_quote()
    payload = _technical_payload()
    payload["timeframe"] = "W1"
    response = client.post("/api/v1/analysis/technical", json=payload)
    assert response.status_code == 422


def test_technical_analysis_rejects_stale_candles():
    _set_fresh_quote()
    payload = _technical_payload(start_offset_seconds=3600)
    response = client.post("/api/v1/analysis/technical", json=payload)
    assert response.status_code == 422
    assert "stale" in response.json()["detail"]


def test_technical_analysis_fails_closed_without_mt5_quote():
    main._latest_mt5_quote = None
    response = client.post("/api/v1/analysis/technical", json=_technical_payload())
    assert response.status_code == 503
