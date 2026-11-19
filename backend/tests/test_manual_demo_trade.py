from datetime import datetime, timezone

from fastapi.testclient import TestClient

import app.main as main
from app.main import app, Candle


client = TestClient(app)


def _ready_manual_demo(monkeypatch):
    monkeypatch.setattr(main.settings, "manual_trade_pin", "A" * 32)
    monkeypatch.setattr(main.settings, "demo_execution_enabled", False)
    monkeypatch.setattr(main.settings, "manual_demo_execution_enabled", True)
    monkeypatch.setattr(main.settings, "live_trading_enabled", False)
    monkeypatch.setattr(main.settings, "trading_mode", "paper")
    monkeypatch.setattr(main.settings, "demo_risk_per_trade_percent", 0.5)
    monkeypatch.setattr(main.settings, "risk_capital_usd", 80.0)
    monkeypatch.setattr(main.settings, "broker_gold_symbol", "GOLD")
    monkeypatch.setattr(main.settings, "max_daily_loss_percent", 2.0)
    main._latest_mt5_quote = {
        "symbol": "GOLD", "bid": 2350.00, "ask": 2350.20, "last": 0,
        "mid": 2350.10, "spread_points": 5, "digits": 2, "point": 0.01,
        "source": "MT5", "source_updated_at": datetime.now(timezone.utc),
        "age_seconds": 0, "quote_type": "broker_bid_ask", "tradable_quote": True,
        "tick_size": 0.01, "tick_value_loss": 1.0, "tick_value_profit": 1.0,
        "volume_min": 0.01, "volume_max": 10.0, "volume_step": 0.01,
        "contract_size": 100.0, "account_equity": 1000.0, "account_currency": "USD",
    }
    main._manual_trade_queue.clear()


def test_manual_demo_request_queues_even_without_analysis_conditions(monkeypatch):
    _ready_manual_demo(monkeypatch)
    response = client.post(
        "/api/v1/manual/demo/request",
        headers={"X-Manual-Trade-Pin": "A" * 32},
        json={"direction": "BUY", "lots": 0.01, "stop_loss_points": 20, "take_profit_points": 40},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["accepted"] is True
    assert body["queued"] is True
    assert body["direction"] == "BUY"
    assert body["estimated_risk_percent"] <= 0.5
    assert body["risk_capital_usd"] == 80.0


def test_manual_demo_request_rejects_wrong_pin(monkeypatch):
    _ready_manual_demo(monkeypatch)
    response = client.post(
        "/api/v1/manual/demo/request",
        headers={"X-Manual-Trade-Pin": "wrong"},
        json={"direction": "SELL", "lots": 0.01, "stop_loss_points": 100, "take_profit_points": 200},
    )
    assert response.status_code == 401


def test_manual_demo_request_rejects_risk_above_limit(monkeypatch):
    _ready_manual_demo(monkeypatch)
    response = client.post(
        "/api/v1/manual/demo/request",
        headers={"X-Manual-Trade-Pin": "A" * 32},
        json={"direction": "BUY", "lots": 0.30, "stop_loss_points": 1000, "take_profit_points": 2000},
    )
    assert response.status_code == 422
    assert "exceeds" in response.json()["detail"]


def test_manual_demo_command_requires_bridge_token(monkeypatch):
    _ready_manual_demo(monkeypatch)
    monkeypatch.setattr(main.settings, "mt5_bridge_token", "B" * 32)
    response = client.get("/api/v1/mt5/manual/next")
    assert response.status_code == 401


def test_manual_demo_command_is_delivered_only_to_authenticated_bridge(monkeypatch):
    _ready_manual_demo(monkeypatch)
    monkeypatch.setattr(main.settings, "mt5_bridge_token", "B" * 32)
    main._manual_trade_queue.append({
        "id": "manual-123", "symbol": "GOLD", "direction": "SELL",
        "lots": 0.01, "stop_loss_points": 100, "take_profit_points": 200,
        "risk_percent": 0.1, "created_at": datetime.now(timezone.utc).isoformat(),
    })
    response = client.get("/api/v1/mt5/manual/next", headers={"Authorization": "Bearer " + "B" * 32})
    assert response.status_code == 200
    assert response.json()["available"] is True
    assert response.json()["direction"] == "SELL"
    assert main._manual_trade_queue == []


def test_manual_demo_queue_rejects_duplicate_pending_request(monkeypatch):
    _ready_manual_demo(monkeypatch)
    main._manual_trade_queue.append({
        "id": "already-pending", "symbol": "GOLD", "direction": "BUY",
        "lots": 0.01, "stop_loss_points": 100, "take_profit_points": 200,
        "risk_percent": 0.1, "created_at": datetime.now(timezone.utc).isoformat(),
    })
    response = client.post(
        "/api/v1/manual/demo/request",
        headers={"X-Manual-Trade-Pin": "A" * 32},
        json={"direction": "SELL", "lots": 0.01, "stop_loss_points": 100, "take_profit_points": 200},
    )
    assert response.status_code == 429


def test_manual_demo_queue_discards_expired_request(monkeypatch):
    _ready_manual_demo(monkeypatch)
    monkeypatch.setattr(main.settings, "mt5_bridge_token", "B" * 32)
    main._manual_trade_queue.append({
        "id": "expired", "symbol": "GOLD", "direction": "BUY",
        "lots": 0.01, "stop_loss_points": 100, "take_profit_points": 200,
        "risk_percent": 0.1, "created_at": "2020-01-01T00:00:00+00:00",
    })
    response = client.get("/api/v1/mt5/manual/next", headers={"Authorization": "Bearer " + "B" * 32})
    assert response.status_code == 200
    assert response.json()["available"] is False
    assert response.json()["expired"] is True



def test_manual_defaults_auto_calculates_lot_sl_tp_at_two_percent(monkeypatch):
    _ready_manual_demo(monkeypatch)
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    rows = [
        Candle(time=now, open=2350.0, high=2350.05, low=2349.95, close=2350.0, tick_volume=100)
        for _ in range(20)
    ]
    main._latest_mt5_candles = {
        "symbol": "GOLD",
        "timeframes": {"M5": rows},
        "received_at": datetime.now(timezone.utc),
    }
    response = client.get("/api/v1/manual/demo/defaults")
    assert response.status_code == 200
    body = response.json()
    assert body["ready"] is True
    assert body["risk_limit_percent"] == 0.5
    assert body["stop_loss_points"] > 0
    assert body["take_profit_points"] == body["stop_loss_points"] * 2
    assert body["recommended_lots"] >= 0.01
    assert body["estimated_risk_percent"] <= 2.0


def test_manual_defaults_fail_closed_without_fresh_candles(monkeypatch):
    _ready_manual_demo(monkeypatch)
    main._latest_mt5_candles = None
    response = client.get("/api/v1/manual/demo/defaults")
    assert response.status_code == 503
