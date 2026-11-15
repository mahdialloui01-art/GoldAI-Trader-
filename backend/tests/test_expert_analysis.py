from datetime import datetime, timedelta, timezone
import math

from fastapi.testclient import TestClient

import app.main as main
from app.main import app

client = TestClient(app)


def _candles(count: int, step_seconds: int) -> list[dict]:
    now = datetime.now(timezone.utc)
    rows = []
    base = 2300.0
    for index in range(count):
        close = base + index * 0.08 + math.sin(index / 2.5) * 0.25
        open_price = close - 0.04
        rows.append({
            "time": (now - timedelta(seconds=(count - index - 1) * step_seconds + 20)).isoformat(),
            "open": open_price,
            "high": max(open_price, close) + 0.18,
            "low": min(open_price, close) - 0.18,
            "close": close,
            "tick_volume": 100 + index,
        })
    return rows


def test_expert_endpoint_returns_explainable_paper_only_dossier(monkeypatch):
    main._latest_mt5_quote = {
        "symbol": "GOLD", "bid": 2306.10, "ask": 2306.30, "last": 0,
        "mid": 2306.20, "spread_points": 20, "digits": 2, "point": 0.01,
        "source": "MetaTrader 5 broker feed",
        "source_updated_at": datetime.now(timezone.utc),
        "age_seconds": 0, "quote_type": "broker_bid_ask", "tradable_quote": True,
    }

    async def fake_gold_decision(request):
        timeframes = {
            frame: {
                "direction": "BUY",
                "indicators": {"rsi14_wilder": 62.0, "macd_histogram": 0.12},
            }
            for frame in ("M5", "M15", "H1", "H4")
        }
        return {
            "decision": "WAIT",
            "status": "NO_TRADE",
            "blockers": ["HIGH_IMPACT_NEWS_BLACKOUT"],
            "reasons": ["News blackout is active."],
            "components": {"multi_timeframe": {"timeframes": timeframes}},
            "policy": {"mode": "PAPER_ONLY", "live_trading_enabled": False, "execution_enabled": False},
        }

    monkeypatch.setattr(main, "gold_decision_analysis", fake_gold_decision)
    payload = {
        "symbol": "GOLD",
        "m5": _candles(80, 300),
        "m15": _candles(80, 900),
        "h1": _candles(80, 3600),
        "h4": _candles(80, 14400),
    }
    response = client.post("/api/v1/analysis/expert", json=payload)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["decision"] == "WAIT"
    assert body["status"] == "NO_TRADE"
    assert body["technical_dossier"]["score_type"] == "HEURISTIC_CONFLUENCE_NOT_PROBABILITY"
    assert set(body["technical_dossier"]["timeframes"]) == {"M5", "M15", "H1", "H4"}
    assert body["setup_plan"] is None
    assert body["policy"]["live_trading_enabled"] is False
    assert body["policy"]["execution_enabled"] is False


def test_expert_endpoint_fails_closed_without_validated_multi_timeframe_analysis(monkeypatch):
    main._latest_mt5_quote = {
        "symbol": "GOLD", "bid": 2300.0, "ask": 2300.2, "last": 0,
        "mid": 2300.1, "spread_points": 20, "digits": 2, "point": 0.01,
        "source": "MetaTrader 5 broker feed",
        "source_updated_at": datetime.now(timezone.utc),
        "age_seconds": 0, "quote_type": "broker_bid_ask", "tradable_quote": True,
    }

    async def fake_gold_decision(request):
        return {
            "decision": "WAIT",
            "blockers": ["BROKER_TECHNICAL_DATA_UNAVAILABLE"],
            "reasons": [],
            "components": {},
            "policy": {"mode": "PAPER_ONLY", "live_trading_enabled": False, "execution_enabled": False},
        }

    monkeypatch.setattr(main, "gold_decision_analysis", fake_gold_decision)
    payload = {
        "symbol": "GOLD",
        "m5": _candles(60, 300),
        "m15": _candles(60, 900),
        "h1": _candles(60, 3600),
        "h4": _candles(60, 14400),
    }
    response = client.post("/api/v1/analysis/expert", json=payload)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["decision"] == "WAIT"
    assert body["analysis_level"] == "EXPERT_DOSSIER_UNAVAILABLE"
    assert body["technical_dossier"] is None
    assert body["setup_plan"] is None
    assert body["policy"]["execution_enabled"] is False


def test_expert_endpoint_builds_reference_plan_only_after_all_gates_pass(monkeypatch):
    main._latest_mt5_quote = {
        "symbol": "GOLD", "bid": 2306.10, "ask": 2306.11, "last": 0,
        "mid": 2306.105, "spread_points": 1, "digits": 2, "point": 0.01,
        "source": "MetaTrader 5 broker feed",
        "source_updated_at": datetime.now(timezone.utc),
        "age_seconds": 0, "quote_type": "broker_bid_ask", "tradable_quote": True,
    }

    async def fake_gold_decision(request):
        timeframes = {
            frame: {
                "direction": "BUY",
                "indicators": {"rsi14_wilder": 62.0, "macd_histogram": 0.12},
            }
            for frame in ("M5", "M15", "H1", "H4")
        }
        return {
            "decision": "BUY",
            "status": "PAPER_CANDIDATE",
            "blockers": [],
            "reasons": ["All base gates passed."],
            "components": {"multi_timeframe": {"timeframes": timeframes}},
            "policy": {"mode": "PAPER_ONLY", "live_trading_enabled": False, "execution_enabled": False},
        }

    monkeypatch.setattr(main, "gold_decision_analysis", fake_gold_decision)
    payload = {
        "symbol": "GOLD",
        "m5": _candles(80, 300),
        "m15": _candles(80, 900),
        "h1": _candles(80, 3600),
        "h4": _candles(80, 14400),
    }
    response = client.post("/api/v1/analysis/expert", json=payload)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["decision"] == "BUY"
    assert body["setup_plan"]["status"] == "PAPER_RESEARCH_ONLY"
    assert body["setup_plan"]["stop_loss_reference"] < body["setup_plan"]["entry_reference"]
    assert body["setup_plan"]["targets"]["target_2R"] > body["setup_plan"]["entry_reference"]
    assert body["policy"]["execution_enabled"] is False
