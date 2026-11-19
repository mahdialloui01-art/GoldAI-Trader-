from datetime import datetime, timedelta, timezone
import math

from fastapi.testclient import TestClient

import app.main as main
from app.main import app

client = TestClient(app)


def _candles(count: int, step_seconds: int) -> list[dict]:
    now_epoch = int(datetime.now(timezone.utc).timestamp())
    latest_closed_epoch = (now_epoch // step_seconds - 1) * step_seconds
    latest_closed_bar = datetime.fromtimestamp(latest_closed_epoch, tz=timezone.utc)
    rows = []
    base = 2300.0
    for index in range(count):
        close = base + index * 0.08 + math.sin(index / 2.5) * 0.25
        open_price = close - 0.04
        rows.append({
            "time": (latest_closed_bar - timedelta(seconds=(count - index - 1) * step_seconds)).isoformat(),
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


def test_mt5_candle_feed_requires_bridge_authentication(monkeypatch):
    monkeypatch.setattr(main.settings, "mt5_bridge_token", "a-long-test-bridge-token-123")
    payload = {
        "symbol": "GOLD",
        "m5": _candles(60, 300),
        "m15": _candles(60, 900),
        "h1": _candles(60, 3600),
        "h4": _candles(60, 14400),
    }
    response = client.post("/api/v1/mt5/candles", json=payload)
    assert response.status_code == 401


def test_authenticated_mt5_candles_power_live_expert_endpoint(monkeypatch):
    main._latest_mt5_candles = None
    monkeypatch.setattr(main.settings, "mt5_bridge_token", "a-long-test-bridge-token-123")
    main._latest_mt5_quote = {
        "symbol": "GOLD", "bid": 2306.10, "ask": 2306.11, "last": 0,
        "mid": 2306.105, "spread_points": 1, "digits": 2, "point": 0.01,
        "source": "MetaTrader 5 broker feed",
        "source_updated_at": datetime.now(timezone.utc),
        "age_seconds": 0, "quote_type": "broker_bid_ask", "tradable_quote": True,
    }

    async def fake_gold_decision(request):
        assert main._ALLOW_H4_SESSION_GAP.get() is True
        mtf = main.multi_timeframe_analysis(request)
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
            "blockers": ["NEWS_BLACKOUT"],
            "reasons": ["News filter blocked the candidate."],
            "components": {"multi_timeframe": mtf},
            "policy": {"mode": "PAPER_ONLY", "live_trading_enabled": False, "execution_enabled": False},
        }

    monkeypatch.setattr(main, "gold_decision_analysis", fake_gold_decision)
    payload = {
        "symbol": "GOLD",
        "m5": _candles(80, 300),
        "m15": _candles(80, 900),
        "h1": _candles(80, 3600),
        "h4": [
            {
                **candle,
                "time": (
                    datetime.fromisoformat(candle["time"]) - timedelta(days=3)
                ).isoformat(),
            }
            for candle in _candles(80, 14400)
        ],
    }
    accepted = client.post(
        "/api/v1/mt5/candles",
        json=payload,
        headers={"Authorization": "Bearer a-long-test-bridge-token-123"},
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["accepted"] is True
    assert accepted.json()["candle_counts"] == {"M5": 80, "M15": 80, "H1": 80, "H4": 80}
    assert accepted.json()["data_quality"]["H4"]["h4_session_reopen_gap"] is True

    live = client.get("/api/v1/analysis/expert/live")
    assert live.status_code == 200, live.text
    body = live.json()
    assert body["decision"] == "WAIT"
    assert body["market_data"]["closed_candles_only"] is True
    assert body["market_data"]["source"].startswith("Authenticated MetaTrader 5")
    assert body["setup_plan"] is None
    assert body["policy"]["execution_enabled"] is False


def test_strategy_scan_identifies_confirmed_breakout_without_claiming_probability():
    base_time = datetime.now(timezone.utc) - timedelta(minutes=500)
    candles = []
    for index in range(100):
        close = 100.0 + (index % 8 if index % 8 < 4 else 8 - index % 8) * 0.2
        candles.append(main.Candle(
            time=base_time + timedelta(minutes=index * 5),
            open=close - 0.05, high=close + 0.1, low=close - 0.1, close=close,
        ))
    previous = candles[-1]
    candles.append(main.Candle(
        time=base_time + timedelta(minutes=500),
        open=previous.close, high=103.0, low=previous.close - 0.1, close=102.8,
    ))
    frames = {"M5": candles, "M15": candles, "H1": candles, "H4": candles}
    reports = {"M5": {
        "microstructure": {"closed_candle_pattern": "BULLISH_ENGULFING", "impulse_direction": "BULLISH"},
        "market_structure": "UPTREND", "market_regime": "NORMAL",
        "atr14": 1.0, "ema20": 102.0,
    }}
    result = main._classify_entry_setups("BUY", frames, reports, 0.02)
    assert any(item["strategy"] == "BREAKOUT" for item in result["candidates"])
    assert result["selected"]["strategy"] == "BREAKOUT"
    assert "not win probability" in result["score_note"]


def test_strategy_scan_never_selects_when_direction_is_wait():
    candles = [main.Candle(
        time=datetime.now(timezone.utc) - timedelta(minutes=500 - i * 5),
        open=100, high=101, low=99, close=100,
    ) for i in range(100)]
    frames = {"M5": candles, "M15": candles, "H1": candles, "H4": candles}
    result = main._classify_entry_setups("WAIT", frames, {}, 0.01)
    assert result["selected"] is None
    assert result["candidates"] == []


def test_wait_scan_exposes_diagnostic_sell_bias_without_selecting_trade():
    candles = [main.Candle(
        time=datetime.now(timezone.utc) - timedelta(minutes=500 - i * 5),
        open=100, high=101, low=99, close=100,
    ) for i in range(100)]
    frames = {"M5": candles, "M15": candles, "H1": candles, "H4": candles}
    reports = {
        frame: {"direction": "SELL", "market_structure": "DOWNTREND"}
        for frame in ("M5", "M15", "H1", "H4")
    }
    result = main._classify_entry_setups("WAIT", frames, reports, 0.01)
    assert result["direction"] == "WAIT"
    assert result["diagnostic_direction"] == "SELL"
    assert result["selected"] is None
    assert result["diagnostic_selected"] is None


def test_pullback_uses_confirmation_from_recent_closed_m5_candles(monkeypatch):
    candles = [main.Candle(
        time=datetime.now(timezone.utc) - timedelta(minutes=500 - i * 5),
        open=100.0, high=100.2, low=99.8, close=100.0,
    ) for i in range(100)]
    # Make the final closed candle a genuine bearish EMA20 reclaim after a
    # recent retest; flat candles alone must not qualify as a pullback.
    candles[-1] = candles[-1].model_copy(update={
        "open": 100.0, "high": 100.2, "low": 99.4, "close": 99.5,
    })

    def fake_microstructure(window):
        if len(window) == 99:
            return {"closed_candle_pattern": "BEARISH_REJECTION", "impulse_direction": "NONE"}
        return {"closed_candle_pattern": "NONE", "impulse_direction": "NONE"}

    monkeypatch.setattr(main, "_candle_microstructure_analysis", fake_microstructure)
    frames = {"M5": candles, "M15": candles, "H1": candles, "H4": candles}
    reports = {"M5": {
        "microstructure": {"closed_candle_pattern": "NONE", "impulse_direction": "NONE"},
        "market_structure": "DOWNTREND", "market_regime": "NORMAL",
        "atr14": 1.0, "ema20": 100.0,
    }}
    result = main._classify_entry_setups("SELL", frames, reports, 0.01)
    pullback = next(item for item in result["candidates"] if item["strategy"] == "PULLBACK")
    assert pullback["score"] == 5
    assert any("آخر 1 شمعة" in item for item in pullback["evidence"])
