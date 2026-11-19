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



def test_opportunity_log_preserves_exact_entry_engine_decision_context(monkeypatch):
    import json
    import app.learning_store as learning

    captured = {}

    class FakeResult:
        rowcount = 1

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, query, params=None):
            if params:
                captured["params"] = params
            return FakeResult()

    monkeypatch.setattr(learning, "_connect", lambda: FakeConnection())
    analysis = {
        "symbol": "GOLD",
        "decision": "WAIT",
        "generated_at": "2026-11-19T03:06:00Z",
        "blockers": ["ENTRY_CONFIRMATION_MISSING"],
        "market_data": {
            "latest_candle_times": {"M5": "2026-11-19T03:05:00Z"},
            "closed_candles_only": True,
        },
        "technical_dossier": {
            "confluence_score": 9.5,
            "strategy_scan": {"diagnostic_direction": "SELL"},
        },
        "entry_engine": {
            "direction": "WAIT",
            "selected_strategy": "MOMENTUM_CONTINUATION",
            "raw_entry_quality_score_0_to_100": 89,
            "effective_entry_quality_score_0_to_100": 9.5,
            "blockers": ["CROSS_LAYER_CONFLUENCE_BELOW_ENTRY_THRESHOLD"],
        },
        "setup_plan": None,
        "reasons": ["Weak multi-timeframe confluence."],
    }

    assert learning.save_opportunity(analysis) is True
    params = captured["params"]
    context = json.loads(params[-1])
    fingerprint = json.loads(json.dumps({
        "symbol": "GOLD",
        "decision": "WAIT",
        "blockers": ["ENTRY_CONFIRMATION_MISSING"],
        "latest_candle_times": {"M5": "2026-11-19T03:05:00Z"},
        "confluence_score": 9.5,
        "entry_score": 9.5,
        "entry_direction": "WAIT",
        "entry_strategy": "MOMENTUM_CONTINUATION",
        "strategy": None,
    }))
    assert context["entry_engine"]["raw_entry_quality_score_0_to_100"] == 89
    assert context["entry_engine"]["effective_entry_quality_score_0_to_100"] == 9.5
    assert context["technical_dossier"]["strategy_scan"]["diagnostic_direction"] == "SELL"
    assert context["blockers"] == ["ENTRY_CONFIRMATION_MISSING"]
