from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_learning_journal_requires_mt5_bridge_token(monkeypatch):
    monkeypatch.setenv("MT5_BRIDGE_TOKEN", "bridge-secret-for-test")
    response = client.post(
        "/api/v1/learning/trades/journal",
        json={"deals": [{
            "deal_ticket": 101, "position_id": 202,
            "deal_time": "2026-11-16T20:00:00Z", "symbol": "GOLD",
            "direction": "BUY", "volume": 0.01, "price": 2350.0,
            "profit": 2.5, "commission": -0.1, "swap": 0, "reason": 1,
        }]},
    )
    assert response.status_code == 401


def test_learning_journal_rejects_non_gold_symbol_before_database_write(monkeypatch):
    monkeypatch.setenv("MT5_BRIDGE_TOKEN", "bridge-secret-for-test")
    response = client.post(
        "/api/v1/learning/trades/journal",
        headers={"Authorization": "Bearer bridge-secret-for-test"},
        json={"deals": [{
            "deal_ticket": 101, "position_id": 202,
            "deal_time": "2026-11-16T20:00:00Z", "symbol": "EURUSD",
            "direction": "BUY", "volume": 0.01, "price": 1.1,
            "profit": 2.5, "commission": -0.1, "swap": 0, "reason": 1,
        }]},
    )
    assert response.status_code == 400


def test_learning_journal_rejects_timezone_naive_deal_timestamp(monkeypatch):
    monkeypatch.setenv("MT5_BRIDGE_TOKEN", "bridge-secret-for-test")
    response = client.post(
        "/api/v1/learning/trades/journal",
        headers={"Authorization": "Bearer bridge-secret-for-test"},
        json={"deals": [{
            "deal_ticket": 101, "position_id": 202,
            "deal_time": "2026-11-16T20:00:00", "symbol": "GOLD",
            "direction": "BUY", "volume": 0.01, "price": 2350.0,
            "profit": 2.5, "commission": -0.1, "swap": 0, "reason": 1,
        }]},
    )
    assert response.status_code == 422
