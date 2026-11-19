import httpx

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

import app.main as main
import app.learning_store as learning_store
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
    assert body["analysis_engine_version"] == "0.6.10"
    assert body["adaptive_risk_policy"]["enabled"] is True
    assert body["adaptive_risk_policy"]["live_trading_enabled"] is False
    assert "closed_candle_microstructure" in body["analysis_layers"]
    assert "optional_tick_volume_participation" in body["analysis_layers"]
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
    # The account has $1,000, but the risk denominator is capped at $80.
    assert body["estimated_risk_percent"] == 12.5


def test_risk_check_distinguishes_risk_approval_from_execution_authorization():
    response = client.post(
        "/api/v1/risk/check",
        json={
            "account_equity": 10000,
            "stop_distance_points": 10,
            "point_value_per_lot": 1,
            "proposed_lots": 0.01,
            "daily_loss_percent": 0,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["approved"] is True
    assert body["estimated_risk_percent"] == 0.125
    assert body["execution_authorized"] is False
    assert any("Execution is disabled" in reason for reason in body["reasons"])


def test_risk_check_blocks_when_daily_loss_limit_is_reached():
    response = client.post(
        "/api/v1/risk/check",
        json={
            "account_equity": 10000,
            "stop_distance_points": 10,
            "point_value_per_lot": 1,
            "proposed_lots": 0.1,
            "daily_loss_usd": 15.0,
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



def test_multi_timeframe_requires_all_four_frames_to_align():
    _set_fresh_quote()
    main._latest_mt5_quote["spread_points"] = 1
    def strong_bullish_candles(timeframe):
        candles = _technical_payload(timeframe)["candles"]
        last = candles[-1]
        last["open"] = last["close"] - 0.8
        last["low"] = last["open"] - 0.05
        last["high"] = last["close"] + 0.05
        return candles

    payload = {
        "symbol": "GOLD",
        "m5": strong_bullish_candles("M5"),
        "m15": strong_bullish_candles("M15"),
        "h1": strong_bullish_candles("H1"),
        "h4": strong_bullish_candles("H4"),
    }
    response = client.post("/api/v1/analysis/multi-timeframe", json=payload)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["execution_enabled"] is False
    assert set(body["timeframes"]) == {"M5", "M15", "H1", "H4"}
    assert body["alignment"]["M5"] == "BUY"
    assert body["alignment"]["M15"] == "BUY"
    assert body["alignment"]["H1"] == "BUY"
    assert body["alignment"]["H4"] == "BUY"
    assert body["direction"] == "BUY"


def test_multi_timeframe_keeps_historical_candles_during_session_break():
    main._latest_mt5_quote = None
    payload = {
        "symbol": "GOLD",
        "m5": _technical_payload("M5", start_offset_seconds=48 * 3600)["candles"],
        "m15": _technical_payload("M15", start_offset_seconds=48 * 3600)["candles"],
        "h1": _technical_payload("H1", start_offset_seconds=48 * 3600)["candles"],
        "h4": _technical_payload("H4", start_offset_seconds=48 * 3600)["candles"],
    }
    response = client.post("/api/v1/analysis/multi-timeframe", json=payload)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["timeframes"]["H1"]["analysis_mode"] == "HISTORICAL_CLOSED_MARKET"
    assert body["execution_enabled"] is False


def test_multi_timeframe_runs_historical_analysis_without_live_quote():
    main._latest_mt5_quote = None
    payload = {
        "symbol": "GOLD",
        "m5": _technical_payload("M5")["candles"],
        "m15": _technical_payload("M15")["candles"],
        "h1": _technical_payload("H1")["candles"],
        "h4": _technical_payload("H4")["candles"],
    }
    response = client.post("/api/v1/analysis/multi-timeframe", json=payload)
    assert response.status_code == 200, response.text
    assert response.json()["timeframes"]["H1"]["analysis_mode"] == "HISTORICAL_CLOSED_MARKET"


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


def test_technical_analysis_accepts_closed_historical_candles():
    main._latest_mt5_quote = None
    payload = _technical_payload(start_offset_seconds=48 * 3600)
    response = client.post("/api/v1/analysis/technical", json=payload)
    assert response.status_code == 200, response.text
    assert response.json()["analysis_mode"] == "HISTORICAL_CLOSED_MARKET"
    assert response.json()["execution_enabled"] is False


def test_technical_analysis_runs_without_live_quote_but_does_not_authorize_execution():
    main._latest_mt5_quote = None
    response = client.post("/api/v1/analysis/technical", json=_technical_payload())
    assert response.status_code == 200, response.text
    assert response.json()["broker_quote"] is None
    assert response.json()["execution_enabled"] is False


def test_price_action_endpoint_returns_structure_features_without_authorizing_trades():
    _set_fresh_quote()
    payload = _technical_payload("M5", count=120)
    response = client.post("/api/v1/analysis/price-action", json=payload)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["symbol"] == "GOLD"
    assert body["timeframe"] == "M5"
    assert body["market_structure"] in {"UPTREND", "DOWNTREND", "RANGE_OR_TRANSITION", "UNCONFIRMED"}
    assert "swing_highs" in body and "swing_lows" in body
    assert "liquidity_pools" in body
    assert "fair_value_gaps" in body
    assert body["signal"] == "WAIT"
    assert body["execution_enabled"] is False
    assert body["swing_confirmation"]["repainting"] is False


def test_confirmed_swings_never_include_unconfirmed_right_edge():
    candles = [
        main.Candle(time=datetime.now(timezone.utc) - timedelta(minutes=100 - i),
                    open=100 + (i % 3), high=100 + (i % 3) + (5 if i == 10 else 0),
                    low=95 + (i % 3), close=99 + (i % 3))
        for i in range(100)
    ]
    highs, lows = main._confirmed_swings(candles, 3, 3)
    assert all(item["index"] <= len(candles) - 4 for item in highs + lows)
    assert all(item["index"] >= 3 for item in highs + lows)


def test_price_action_keeps_historical_structure_during_market_closure():
    main._latest_mt5_quote = None
    payload = _technical_payload("M5", start_offset_seconds=48 * 3600)
    response = client.post("/api/v1/analysis/price-action", json=payload)
    assert response.status_code == 200, response.text
    assert response.json()["execution_enabled"] is False


def test_price_action_is_available_without_live_quote_for_historical_analysis():
    main._latest_mt5_quote = None
    response = client.post("/api/v1/analysis/price-action", json=_technical_payload("M5"))
    assert response.status_code == 200, response.text
    assert response.json()["execution_enabled"] is False


def test_price_action_rejects_unsupported_symbol():
    _set_fresh_quote()
    payload = _technical_payload("M5")
    payload["symbol"] = "XAUUSD"
    response = client.post("/api/v1/analysis/price-action", json=payload)
    assert response.status_code == 400



def test_price_action_detects_bullish_and_bearish_fair_value_gaps():
    base_time = datetime.now(timezone.utc) - timedelta(minutes=60)
    candles = []
    for index in range(60):
        candles.append(main.Candle(
            time=base_time + timedelta(minutes=index),
            open=100.0, high=100.5, low=99.5, close=100.1,
        ))

    # Three-candle gap: candle 3 low is above candle 1 high.
    candles[28] = main.Candle(time=candles[28].time, open=100.0, high=100.5, low=99.5, close=100.1)
    candles[30] = main.Candle(time=candles[30].time, open=101.6, high=102.0, low=101.5, close=101.8)

    # Three-candle bearish gap: candle 3 high is below candle 1 low.
    candles[38] = main.Candle(time=candles[38].time, open=100.0, high=100.5, low=99.5, close=100.1)
    candles[40] = main.Candle(time=candles[40].time, open=98.0, high=98.5, low=97.8, close=98.2)

    result = main._price_action_analysis(candles, left=3, right=3, equal_tolerance=0.12)
    bullish = result["fair_value_gaps"]["bullish"]
    bearish = result["fair_value_gaps"]["bearish"]
    assert any(item["from_index"] == 28 and item["to_index"] == 30 for item in bullish)
    assert any(item["from_index"] == 38 and item["to_index"] == 40 for item in bearish)
    assert all(item["upper"] > item["lower"] for item in bullish + bearish)


def test_price_action_uses_only_confirmed_pivots_for_break_levels():
    candles = []
    base_time = datetime.now(timezone.utc) - timedelta(minutes=100)
    for index in range(100):
        # A controlled oscillating range creates confirmed local highs and lows.
        close = 100 + (index % 8 if index % 8 < 4 else 8 - index % 8) * 0.2
        candles.append(main.Candle(
            time=base_time + timedelta(minutes=index),
            open=close - 0.05, high=close + 0.1, low=close - 0.1, close=close,
        ))
    highs, lows = main._confirmed_swings(candles, left=3, right=3)
    assert highs and lows
    assert max(point["index"] for point in highs + lows) <= len(candles) - 4
    # No pivot at the currently forming/right-edge candles may become a break level.
    assert all(point["index"] < len(candles) - 3 for point in highs + lows)



def _sample_xoomar_event(event_name="CPI", event_time=None):
    return {
        "event_id": "bls:test-event",
        "event": event_name,
        "category": "October 2026",
        "date": event_time or (datetime.now(timezone.utc) + timedelta(hours=2)),
        "importance": 3,
        "date_span": "0",
        "actual": "",
        "forecast": "",
        "previous": "",
        "source": "bls",
        "source_url": "https://xoomar.com/markets/api/calendar",
        "unit": "%",
        "forecast_available": False,
        "data_provider": "XOOMAR",
        "source_attribution": "Release schedules and figures from the issuing US agencies.",
        "data_updated_at": datetime.now(timezone.utc),
    }


def _reset_news_engine_cache_for_test():
    main._NEWS_CALENDAR_CACHE = []
    main._NEWS_HEADLINES_CACHE = []
    main._NEWS_CALENDAR_LAST_ATTEMPT = None
    main._NEWS_CALENDAR_LAST_SUCCESS = None
    main._NEWS_CALENDAR_LAST_ERROR = None
    main._NEWS_HEADLINES_LAST_SUCCESS = None
    main._NEWS_HEADLINES_ERRORS = []


def test_economic_calendar_returns_only_high_impact_us_events(monkeypatch):
    _reset_news_engine_cache_for_test()
    async def fake_load(days_ahead=7, api_key=""):
        assert days_ahead == 3
        return [_sample_xoomar_event()]
    monkeypatch.setattr(main, "load_xoomar_high_impact_events", fake_load)
    response = client.get("/api/v1/calendar/us-high-impact?days_ahead=3")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["event_count"] == 1
    assert body["events"][0]["event"] == "CPI"
    assert body["events"][0]["importance"] == 3
    assert body["source"].startswith("XOOMAR")
    assert body["forecast_available"] is False
    assert body["execution_enabled"] is False


def test_news_filter_treats_high_impact_window_as_advisory(monkeypatch):
    _reset_news_engine_cache_for_test()
    event_time = datetime.now(timezone.utc) + timedelta(minutes=10)
    async def fake_load(days_ahead=7, api_key=""):
        return [_sample_xoomar_event("FOMC Rate Decision", event_time)]
    monkeypatch.setattr(main, "load_xoomar_high_impact_events", fake_load)
    response = client.get("/api/v1/analysis/news-filter")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["decision"] == "ADVISORY"
    assert body["trading_allowed"] is True
    assert body["active_events"][0]["event"] == "FOMC Rate Decision"
    assert body["fail_closed"] is False
    assert body["calendar_source"] == "XOOMAR"


def test_news_filter_is_advisory_when_calendar_is_unavailable(monkeypatch):
    _reset_news_engine_cache_for_test()
    async def fail_load(days_ahead=7, api_key=""):
        raise httpx.ConnectError("provider unavailable")
    monkeypatch.setattr(main, "load_xoomar_high_impact_events", fail_load)
    response = client.get("/api/v1/analysis/news-filter")
    assert response.status_code == 200
    assert response.json()["status"] == "UNAVAILABLE"
    assert response.json()["trading_allowed"] is True
    assert response.json()["fail_closed"] is False

def test_news_filter_is_advisory_when_provider_is_unavailable(monkeypatch):
    _reset_news_engine_cache_for_test()
    monkeypatch.setattr(main.settings, "economic_calendar_api_key", "calendar-test-key")
    class FakeClient:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return None
        async def get(self, *args, **kwargs):
            raise main.httpx.ConnectError("offline")
    monkeypatch.setattr(main.httpx, "AsyncClient", lambda **kwargs: FakeClient())
    response = client.get("/api/v1/analysis/news-filter")
    assert response.status_code == 200
    assert response.json()["status"] == "UNAVAILABLE"
    assert response.json()["trading_allowed"] is True
    assert response.json()["fail_closed"] is False


def test_economic_calendar_rejects_excessive_date_window():
    response = client.get("/api/v1/calendar/us-high-impact?days_ahead=30")
    assert response.status_code == 422



def test_gold_macro_analysis_detects_joint_real_yield_and_dollar_headwind(monkeypatch):
    monkeypatch.setattr(main.settings, "fred_api_key", "fred-test-key")
    today = datetime.now(timezone.utc).date()
    yesterday = today - timedelta(days=1)
    values = {
        "DGS10": [("4.50", today.isoformat()), ("4.40", yesterday.isoformat())],
        "DFII10": [("2.50", today.isoformat()), ("2.40", yesterday.isoformat())],
        "DTWEXBGS": [("102.0", today.isoformat()), ("101.0", yesterday.isoformat())],
    }
    class FakeResponse:
        def __init__(self, series_id):
            self.series_id = series_id
        def raise_for_status(self):
            pass
        def json(self):
            return {"observations": [
                {"date": date, "value": value} for value, date in values[self.series_id]
            ]}
    class FakeClient:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return None
        async def get(self, url, params):
            assert url.endswith("/fred/series/observations")
            assert params["api_key"] == "fred-test-key"
            return FakeResponse(params["series_id"])
    monkeypatch.setattr(main.httpx, "AsyncClient", lambda **kwargs: FakeClient())
    response = client.get("/api/v1/analysis/gold-macro")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["bias"] == "GOLD_HEADWIND"
    assert body["changes"]["real_10y_yield_basis_points"] == 10.0
    assert body["changes"]["broad_us_dollar_index_percent"] > 0
    assert body["execution_enabled"] is False


def test_gold_macro_analysis_fails_closed_without_fred_key(monkeypatch):
    monkeypatch.setattr(main.settings, "fred_api_key", "")
    response = client.get("/api/v1/analysis/gold-macro")
    assert response.status_code == 503
    assert "not configured" in response.json()["detail"]


def test_gold_macro_analysis_rejects_stale_observations(monkeypatch):
    monkeypatch.setattr(main.settings, "fred_api_key", "fred-test-key")
    old = (datetime.now(timezone.utc).date() - timedelta(days=10)).isoformat()
    older = (datetime.now(timezone.utc).date() - timedelta(days=11)).isoformat()
    class FakeResponse:
        def raise_for_status(self):
            pass
        def json(self):
            return {"observations": [
                {"date": old, "value": "4.0"},
                {"date": older, "value": "3.9"},
            ]}
    class FakeClient:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return None
        async def get(self, *args, **kwargs):
            return FakeResponse()
    monkeypatch.setattr(main.httpx, "AsyncClient", lambda **kwargs: FakeClient())
    response = client.get("/api/v1/analysis/gold-macro")
    assert response.status_code == 503
    assert "stale" in response.json()["detail"]



def _set_quote_with_xm_contract(equity=1000.0, volume_min=0.1):
    _set_fresh_quote()
    main._latest_mt5_quote.update({
        "tick_size": 0.1,
        "tick_value_loss": 1.0,
        "tick_value_profit": 1.0,
        "volume_min": volume_min,
        "volume_max": 100.0,
        "volume_step": 0.1,
        "contract_size": 100.0,
        "account_equity": equity,
        "account_currency": "USD",
    })


def test_mt5_quote_accepts_complete_contract_metadata(monkeypatch):
    monkeypatch.setattr(main.settings, "mt5_bridge_token", "test-secret")
    payload = {
        "symbol": "GOLD", "bid": 2300.0, "ask": 2300.2,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "digits": 2, "point": 0.01, "spread_points": 20,
        "tick_size": 0.1, "tick_value_loss": 1.0, "tick_value_profit": 1.0,
        "volume_min": 0.1, "volume_max": 100.0, "volume_step": 0.1,
        "contract_size": 100.0, "account_equity": 1000.0, "account_currency": "USD",
    }
    response = client.post(
        "/api/v1/mt5/quote", json=payload,
        headers={"Authorization": "Bearer test-secret"},
    )
    assert response.status_code == 200, response.text
    assert main._latest_mt5_quote["tick_value_loss"] == 1.0
    assert main._latest_mt5_quote["account_currency"] == "USD"


def test_mt5_quote_rejects_partial_contract_metadata(monkeypatch):
    monkeypatch.setattr(main.settings, "mt5_bridge_token", "test-secret")
    payload = {
        "symbol": "GOLD", "bid": 2300.0, "ask": 2300.2,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "digits": 2, "point": 0.01, "spread_points": 20,
        "tick_size": 0.1,
    }
    response = client.post(
        "/api/v1/mt5/quote", json=payload,
        headers={"Authorization": "Bearer test-secret"},
    )
    assert response.status_code == 422
    assert "complete set" in response.json()["detail"]


def test_position_size_uses_broker_tick_value_and_rounds_down_to_volume_step():
    _set_quote_with_xm_contract(equity=1000.0)
    response = client.post("/api/v1/risk/position-size", json={
        "stop_distance_points": 10,
        "risk_percent": 0.5,
    })
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["approved"] is True
    assert body["recommended_lots"] == 0.4
    assert body["estimated_stop_loss_amount"] == 0.4
    assert body["estimated_risk_percent"] == 0.5
    assert body["risk_capital_usd"] == 80.0
    assert body["account_currency"] == "USD"
    assert body["execution_enabled"] is False


def test_position_size_never_rounds_up_to_broker_minimum_lot():
    _set_quote_with_xm_contract(equity=100.0, volume_min=0.1)
    response = client.post("/api/v1/risk/position-size", json={
        "stop_distance_points": 100,
        "risk_percent": 0.5,
    })
    assert response.status_code == 200
    body = response.json()
    assert body["approved"] is False
    assert body["recommended_lots"] == 0.0
    assert "smallest broker-allowed lot" in body["reason"]


def test_position_size_blocks_when_daily_loss_limit_is_reached():
    _set_quote_with_xm_contract()
    response = client.post("/api/v1/risk/position-size", json={
        "stop_distance_points": 100,
        "risk_percent": 0.5,
        "daily_loss_usd": 15.0,
    })
    assert response.status_code == 200
    assert response.json()["recommended_lots"] == 0.0
    assert response.json()["approved"] is False


def test_position_size_fails_closed_without_contract_metadata():
    _set_fresh_quote()
    response = client.post("/api/v1/risk/position-size", json={
        "stop_distance_points": 100,
        "risk_percent": 0.5,
    })
    assert response.status_code == 503
    assert "metadata" in response.json()["detail"]


def test_position_size_rejects_risk_above_configured_limit():
    _set_quote_with_xm_contract()
    response = client.post("/api/v1/risk/position-size", json={
        "stop_distance_points": 100,
        "risk_percent": 0.75,
    })
    assert response.status_code == 422
    assert "configured maximum" in response.json()["detail"]



def test_backtest_enters_on_next_bar_and_assumes_stop_first(monkeypatch):
    candles = []
    start = datetime.now(timezone.utc) - timedelta(minutes=100)
    for index in range(100):
        candles.append({
            "time": (start + timedelta(minutes=index)).isoformat(),
            "open": 100.0, "high": 104.0, "low": 96.0, "close": 100.0,
            "tick_volume": 100,
        })
    monkeypatch.setattr(main, "_ema_series", lambda values, period: (
        [101.0] * (len(values) - 19) if period == 20 else [100.0] * (len(values) - 49)
    ))
    monkeypatch.setattr(main, "_rsi_wilder_series", lambda values, period=14: [60.0] * len(values))
    monkeypatch.setattr(main, "_atr_wilder_series", lambda values, period=14: [1.0] * len(values))
    response = client.post("/api/v1/backtest", json={
        "symbol": "GOLD", "timeframe": "M5", "candles": candles,
        "initial_balance": 10000, "lots": 0.1, "contract_size": 100,
        "spread_points": 20, "point": 0.01, "slippage_points": 2,
        "commission_per_lot_round_turn": 0,
    })
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["trade_count"] > 0
    assert datetime.fromisoformat(body["trades"][0]["entry_time"]) == datetime.fromisoformat(candles[51]["time"])
    assert body["trades"][0]["exit_reason"] == "STOP"
    assert body["trades"][0]["net_pnl"] < 0
    assert body["execution_enabled"] is False
    assert any("following candle open" in item for item in body["lookahead_bias_controls"])


def test_backtest_rejects_unsupported_symbol():
    response = client.post("/api/v1/backtest", json={
        "symbol": "EURUSD", "timeframe": "M15",
        "candles": _technical_payload("M15")["candles"],
    })
    assert response.status_code == 400


def test_backtest_rejects_non_increasing_timestamps():
    candles = _technical_payload("M5", count=100)["candles"]
    candles[-1]["time"] = candles[-2]["time"]
    response = client.post("/api/v1/backtest", json={
        "symbol": "GOLD", "timeframe": "M5", "candles": candles,
    })
    assert response.status_code == 422
    assert "strictly increasing" in response.json()["detail"]


def test_backtest_flat_market_returns_zero_trades_without_fake_win_rate():
    candles = _technical_payload("M5", count=100)["candles"]
    response = client.post("/api/v1/backtest", json={
        "symbol": "GOLD", "timeframe": "M5", "candles": candles,
    })
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["trade_count"] == 0
    assert body["win_rate_percent"] == 0
    assert body["net_profit"] == 0
    assert body["ending_balance"] == 10000
    assert body["execution_enabled"] is False



def _regime_candles(kind="uptrend", count=100):
    start = datetime.now(timezone.utc) - timedelta(minutes=count * 5)
    candles = []
    for index in range(count):
        if kind == "uptrend":
            close = 100 + index * 0.1
            open_price = close - 0.02
            high, low = close + 0.2, open_price - 0.2
        elif kind == "downtrend":
            close = 100 - index * 0.1
            open_price = close + 0.02
            high, low = open_price + 0.2, close - 0.2
        elif kind == "range":
            close = 100.0 + (0.2 if index % 2 else 0.0)
            open_price = close + (0.02 if index % 2 else -0.02)
            high, low = max(open_price, close) + 0.1, min(open_price, close) - 0.1
        else:
            close = 100.0
            open_price = 100.0
            high, low = (105.0, 95.0) if index >= count - 10 else (100.2, 99.8)
        candles.append(main.Candle(
            time=start + timedelta(minutes=index * 5),
            open=open_price, high=high, low=low, close=close,
        ))
    return candles


def test_market_regime_detects_strong_uptrend():
    result = main._market_regime_analysis(_regime_candles("uptrend"))
    assert result["market_regime"] == "TRENDING_UP"
    assert result["indicators"]["adx14_wilder"] >= 25
    assert result["indicators"]["plus_di14"] > result["indicators"]["minus_di14"]


def test_market_regime_detects_range():
    result = main._market_regime_analysis(_regime_candles("range"))
    assert result["market_regime"] == "RANGING"
    assert result["recommended_action"] == "RANGE_SETUP_ONLY"


def test_market_regime_prioritizes_high_volatility():
    result = main._market_regime_analysis(_regime_candles("high_volatility"))
    assert result["market_regime"] == "HIGH_VOLATILITY"
    assert result["recommended_action"] == "WAIT"
    assert result["indicators"]["atr_vs_recent_median"] >= 1.5


def test_market_regime_endpoint_analyzes_closed_bars_without_live_quote():
    main._latest_mt5_quote = None
    payload = _technical_payload("M5", start_offset_seconds=48 * 3600)
    response = client.post("/api/v1/analysis/market-regime", json=payload)
    assert response.status_code == 200, response.text
    assert response.json()["analysis_mode"] == "HISTORICAL_CLOSED_MARKET"
    assert response.json()["broker_spread_points"] is None


def test_market_regime_endpoint_returns_analysis_only():
    _set_fresh_quote()
    payload = _technical_payload("M5", start_offset_seconds=300)
    response = client.post("/api/v1/analysis/market-regime", json=payload)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["symbol"] == "GOLD"
    assert body["execution_enabled"] is False
    assert "adx14_wilder" in body["indicators"]
    assert "atr_vs_recent_median" in body["indicators"]



def test_market_regime_detects_strong_downtrend():
    result = main._market_regime_analysis(_regime_candles("downtrend"))
    assert result["market_regime"] == "TRENDING_DOWN"
    assert result["indicators"]["adx14_wilder"] >= 25
    assert result["indicators"]["minus_di14"] > result["indicators"]["plus_di14"]


def test_pre_release_news_analysis_returns_probabilities_and_paper_candidate():
    _set_fresh_quote()
    release = datetime.now(timezone.utc) + timedelta(minutes=60)
    response = client.post("/api/v1/news/pre-release-analysis", json={
        "symbol": "GOLD",
        "event_id": "US_RETAIL_SALES_2026_11",
        "event_name": "US Retail Sales",
        "release_at": release.isoformat(),
        "consensus": 0.2,
        "model_estimate": 0.8,
        "historical_error_std": 0.2,
        "historical_samples": 150,
        "walk_forward_validation_passed": True,
        "walk_forward_test_samples": 120,
        "gold_direction_if_above": "BEARISH",
        "gold_direction_if_inline": "MIXED",
        "gold_direction_if_below": "BULLISH",
    })
    assert response.status_code == 200, response.text
    body = response.json()
    probs = body["scenario_probabilities"]
    assert abs(sum(probs.values()) - 1.0) < 0.00001
    # Client-supplied validation claims must never unlock a trade candidate.
    assert body["decision"] == "NO_TRADE"
    assert body["most_likely_gold_direction"] == "BEARISH"  # descriptive scenario, not an authorized trade
    assert body["forecast_inputs"]["client_validation_claim_ignored"] is True
    assert body["forecast_inputs"]["client_test_samples_claim_ignored"] is True
    assert body["forecast_inputs"]["walk_forward_test_samples"] == 0
    assert body["forecast_inputs"]["server_side_model_registry_verified"] is False
    assert body["execution_enabled"] is False
    assert body["risk_policy"]["mode"] == "PAPER_ONLY"


def test_pre_release_news_analysis_fails_closed_without_quote():
    main._latest_mt5_quote = None
    response = client.post("/api/v1/news/pre-release-analysis", json={
        "event_id": "US_RETAIL", "event_name": "US Retail Sales",
        "release_at": (datetime.now(timezone.utc) + timedelta(minutes=60)).isoformat(),
        "consensus": 0.2, "model_estimate": 0.8, "historical_error_std": 0.2,
        "historical_samples": 150, "gold_direction_if_above": "BEARISH",
        "gold_direction_if_below": "BULLISH",
    })
    assert response.status_code == 503


def test_pre_release_news_analysis_rejects_release_too_close():
    _set_fresh_quote()
    response = client.post("/api/v1/news/pre-release-analysis", json={
        "event_id": "US_RETAIL", "event_name": "US Retail Sales",
        "release_at": (datetime.now(timezone.utc) + timedelta(minutes=2)).isoformat(),
        "consensus": 0.2, "model_estimate": 0.8, "historical_error_std": 0.2,
        "historical_samples": 150, "gold_direction_if_above": "BEARISH",
        "gold_direction_if_below": "BULLISH",
    })
    assert response.status_code == 422


def test_pre_release_news_analysis_refuses_candidate_without_walk_forward_validation():
    _set_fresh_quote()
    response = client.post("/api/v1/news/pre-release-analysis", json={
        "event_id": "US_RETAIL", "event_name": "US Retail Sales",
        "release_at": (datetime.now(timezone.utc) + timedelta(minutes=60)).isoformat(),
        "consensus": 0.2, "model_estimate": 0.8, "historical_error_std": 0.2,
        "historical_samples": 150, "gold_direction_if_above": "BEARISH",
        "gold_direction_if_below": "BULLISH",
    })
    assert response.status_code == 200
    assert response.json()["decision"] == "NO_TRADE"
    assert any("walk-forward" in reason for reason in response.json()["reasons"])


def test_pre_release_news_analysis_blocks_small_sample_and_wide_spread():
    _set_fresh_quote()
    main._latest_mt5_quote["spread_points"] = 100
    response = client.post("/api/v1/news/pre-release-analysis", json={
        "event_id": "US_RETAIL", "event_name": "US Retail Sales",
        "release_at": (datetime.now(timezone.utc) + timedelta(minutes=60)).isoformat(),
        "consensus": 0.2, "model_estimate": 0.8, "historical_error_std": 0.2,
        "historical_samples": 40, "gold_direction_if_above": "BEARISH",
        "gold_direction_if_below": "BULLISH",
    })
    assert response.status_code == 200
    assert response.json()["decision"] == "NO_TRADE"
    assert response.json()["execution_enabled"] is False



def test_historical_news_study_groups_events_and_deducts_spread_slippage():
    # Keep the entire synthetic event series safely in the past as time advances.
    base = datetime.now(timezone.utc) - timedelta(days=1000)
    observations = []
    for index in range(30):
        before = 2600.0
        observations.append({
            "event_id": f"CPI_{index}",
            "event_type": "US_CPI",
            "released_at": (base + timedelta(days=index * 30)).isoformat(),
            "consensus": 3.0,
            "actual": 3.1 if index % 2 == 0 else 2.9,
            "surprise_scale": 0.1,
            "gold_before": before,
            "gold_1m": before - 1,
            "gold_5m": before - 2,
            "gold_15m": before - 3,
            "gold_60m": before - 4,
            "spread_points": 20,
            "point": 0.01,
            "slippage_points": 5,
        })
    response = client.post("/api/v1/news/historical-study", json={
        "symbol": "GOLD", "observations": observations,
    })
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["observation_count"] == 30
    result = body["results_by_event_type"][0]
    assert result["sample_quality"] == "MINIMUM_RESEARCH_SAMPLE_MET"
    assert result["horizons"]["5m"]["mean_gold_return_percent"] < 0
    assert result["horizons"]["5m"]["mean_net_return_after_spread_slippage_percent"] < result["horizons"]["5m"]["mean_gold_return_percent"]
    assert body["data_policy"]["execution_enabled"] is False
    assert body["data_policy"]["forecast_leakage_verified"] is False
    assert body["data_policy"]["data_provenance"] == "CLIENT_SUPPLIED_OBSERVATIONS_NOT_INDEPENDENTLY_VERIFIED"
    assert body["data_policy"]["price_timestamp_integrity_verified"] is False
    horizon = result["horizons"]["5m"]
    assert "net_positive_rate_percent" in horizon
    assert "max_drawdown_on_chronological_compounded_net_returns_percent" in horizon
    assert body["data_policy"]["observations_sorted_by_release_time_for_risk_metrics"] is True


def test_historical_news_study_rejects_future_release_timestamp():
    observation = {
        "event_id": "FUTURE_CPI", "event_type": "US_CPI",
        "released_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        "consensus": 3, "actual": 3.1, "surprise_scale": 0.1,
        "gold_before": 2600, "gold_1m": 2601, "gold_5m": 2602,
        "gold_15m": 2603, "gold_60m": 2604,
        "spread_points": 20, "point": 0.01, "slippage_points": 3,
    }
    response = client.post("/api/v1/news/historical-study", json={"observations": [observation]})
    assert response.status_code == 422
    assert "future release timestamps" in response.text


def test_historical_news_study_marks_small_samples_exploratory():
    observation = {
        "event_id": "NFP_1", "event_type": "US_NFP",
        "released_at": datetime(2025, 1, 10, tzinfo=timezone.utc).isoformat(),
        "consensus": 150000, "actual": 175000, "surprise_scale": 30000,
        "gold_before": 2600, "gold_1m": 2601, "gold_5m": 2602,
        "gold_15m": 2603, "gold_60m": 2604,
        "spread_points": 20, "point": 0.01, "slippage_points": 3,
    }
    response = client.post("/api/v1/news/historical-study", json={"observations": [observation]})
    assert response.status_code == 200, response.text
    assert response.json()["results_by_event_type"][0]["sample_quality"] == "EXPLORATORY_ONLY"


def test_historical_news_study_rejects_duplicate_event_ids():
    observation = {
        "event_id": "CPI_DUP", "event_type": "US_CPI",
        "released_at": datetime(2025, 1, 10, tzinfo=timezone.utc).isoformat(),
        "consensus": 3, "actual": 3.1, "surprise_scale": 0.1,
        "gold_before": 2600, "gold_1m": 2601, "gold_5m": 2602,
        "gold_15m": 2603, "gold_60m": 2604,
        "spread_points": 20, "point": 0.01,
    }
    response = client.post("/api/v1/news/historical-study", json={
        "observations": [observation, observation],
    })
    assert response.status_code == 422



def test_historical_news_calendar_imports_point_in_time_events(monkeypatch):
    main.settings.economic_calendar_api_key = "test-key"

    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return [{
                "CalendarId": "12345", "Date": "2025-01-10T13:30:00",
                "Event": "Non Farm Payrolls", "Category": "Employment",
                "Actual": "175K", "Forecast": "150K", "TEForecast": "152K",
                "Previous": "140K", "Revised": "145K", "Importance": 3,
                "Unit": "K", "Source": "BLS", "SourceURL": "https://bls.gov",
                "LastUpdate": "2025-01-10T13:30:00", "Ticker": "NFP",
            }]

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url, params, headers):
            assert "calendar/country/united%20states/indicator/" in url
            assert headers["Authorization"] == "test-key"
            return FakeResponse()

    monkeypatch.setattr(main.httpx, "AsyncClient", FakeClient)
    response = client.get("/api/v1/news/historical-calendar?start_date=2025-01-01&end_date=2025-02-01")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["point_in_time"] is True
    assert body["event_count"] == 1
    assert body["events"][0]["actual"] == "175K"
    assert body["events"][0]["forecast"] == "150K"
    assert body["price_alignment_required"] is True
    assert body["execution_enabled"] is False


def test_historical_news_calendar_requires_configured_provider(monkeypatch):
    monkeypatch.setattr(main.settings, "economic_calendar_api_key", "")
    response = client.get("/api/v1/news/historical-calendar?start_date=2025-01-01&end_date=2025-02-01")
    assert response.status_code == 503


def test_historical_news_calendar_rejects_invalid_range():
    response = client.get("/api/v1/news/historical-calendar?start_date=2025-02-01&end_date=2025-01-01")
    assert response.status_code == 422


def test_historical_news_calendar_rejects_future_end_date():
    future = (datetime.now(timezone.utc).date() + timedelta(days=1)).isoformat()
    response = client.get(
        f"/api/v1/news/historical-calendar?start_date={future}&end_date={future}"
    )
    assert response.status_code == 422
    assert "cannot be in the future" in response.text


def _microstructure_test_candles(volume=100):
    start = datetime.now(timezone.utc) - timedelta(minutes=60 * 5)
    candles = []
    for index in range(60):
        close = 100.0 + index * 0.02
        open_price = close - 0.01
        candles.append(main.Candle(
            time=start + timedelta(minutes=index * 5),
            open=open_price,
            high=close + 0.05,
            low=open_price - 0.05,
            close=close,
            tick_volume=volume,
        ))
    candles[-2] = main.Candle(
        time=candles[-2].time, open=101.0, high=101.1,
        low=100.4, close=100.5, tick_volume=volume,
    )
    candles[-1] = main.Candle(
        time=candles[-1].time, open=100.4, high=101.3,
        low=100.3, close=101.2, tick_volume=250 if volume else 0,
    )
    return candles


def test_microstructure_detects_closed_bullish_engulfing_and_volume_confirmation():
    result = main._candle_microstructure_analysis(_microstructure_test_candles())
    assert result["closed_candle_pattern"] == "BULLISH_ENGULFING"
    assert result["impulse_direction"] == "BULLISH"
    assert result["tick_volume"]["state"] == "EXPANSION"
    assert result["tick_volume"]["directional_confirmation"] == "BULLISH"
    assert result["tick_volume"]["ratio_to_median"] == 2.5


def test_microstructure_does_not_invent_volume_confirmation_when_tick_volume_is_missing():
    result = main._candle_microstructure_analysis(_microstructure_test_candles(volume=0))
    assert result["tick_volume"]["state"] == "UNAVAILABLE"
    assert result["tick_volume"]["ratio_to_median"] is None
    assert result["tick_volume"]["directional_confirmation"] == "NONE"



def test_entry_plan_requires_confirmed_multitimeframe_trigger_and_returns_explainable_plan(monkeypatch):
    _set_fresh_quote()
    main._latest_mt5_quote.update({
        "bid": 2300.0, "ask": 2300.1, "spread_points": 2,
        "point": 0.01, "digits": 2,
    })

    def stable_candles(timeframe):
        candles = _technical_payload(timeframe, count=100)["candles"]
        for index, candle in enumerate(candles):
            close = 2299.0 + index * 0.001
            candle.update({
                "open": close - 0.05,
                "high": close + 0.10,
                "low": close - 0.10,
                "close": close,
                "tick_volume": 200,
            })
        candles[-1].update({
            "open": 2299.70, "high": 2300.20,
            "low": 2299.50, "close": 2300.00,
            "tick_volume": 300,
        })
        return candles

    payload = {
        "symbol": "GOLD",
        "m5": stable_candles("M5"),
        "m15": stable_candles("M15"),
        "h1": stable_candles("H1"),
        "h4": stable_candles("H4"),
    }

    monkeypatch.setattr(main, "_fresh_mt5_quote", lambda: main._latest_mt5_quote)
    monkeypatch.setattr(
        main, "technical_analysis",
        lambda request: {
            "direction": "BUY",
            "confidence_score": 90,
            "candle_count": len(request.candles),
        },
    )
    monkeypatch.setattr(
        main, "_ema_series",
        lambda values, period: [2299.9] if period == 20 else [2299.5],
    )
    monkeypatch.setattr(main, "_adx_wilder", lambda candles: (28.0, 30.0, 10.0))
    monkeypatch.setattr(main, "_atr_wilder", lambda candles: 1.0)
    monkeypatch.setattr(
        main, "_market_regime_analysis",
        lambda candles: {
            "market_regime": "TRENDING_UP",
            "indicators": {"atr_vs_recent_median": 1.0},
        },
    )

    def price_action(candles, left, right, tolerance):
        if candles is payload["m15"]:
            return {"market_structure": "UPTREND", "events": []}
        return {
            "market_structure": "UPTREND",
            "events": ["BULLISH_BREAK_OF_STRUCTURE"],
        }

    monkeypatch.setattr(main, "_price_action_analysis", price_action)
    monkeypatch.setattr(
        main, "_candle_microstructure_analysis",
        lambda candles: {
            "candle_direction": "BULLISH",
            "close_location_0_to_1": 0.9,
            "closed_candle_pattern": "BULLISH_ENGULFING",
            "tick_volume": {
                "state": "EXPANSION",
                "directional_confirmation": "BULLISH",
            },
        },
    )

    response = client.post("/api/v1/analysis/entry-plan", json=payload)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["direction"] == "BUY"
    assert body["selected_strategy"] == "BREAKOUT"
    assert body["entry_quality_score_0_to_100"] >= 82
    assert body["score_threshold"] == 82
    assert body["setup"]["stop_loss"] < body["setup"]["entry_reference"]
    assert body["setup"]["take_profit"]["TP2_2R"] > body["setup"]["entry_reference"]
    assert body["execution_enabled"] is False


def test_entry_plan_waits_without_confirmed_m5_trigger(monkeypatch):
    _set_fresh_quote()
    payload = {
        "symbol": "GOLD",
        "m5": _technical_payload("M5")["candles"],
        "m15": _technical_payload("M15")["candles"],
        "h1": _technical_payload("H1")["candles"],
        "h4": _technical_payload("H4")["candles"],
    }
    monkeypatch.setattr(main, "_fresh_mt5_quote", lambda: main._latest_mt5_quote)
    monkeypatch.setattr(
        main, "technical_analysis",
        lambda request: {
            "direction": "BUY",
            "confidence_score": 90,
            "candle_count": len(request.candles),
        },
    )
    monkeypatch.setattr(main, "_ema_series", lambda values, period: [2299.9] if period == 20 else [2299.5])
    monkeypatch.setattr(main, "_adx_wilder", lambda candles: (28.0, 30.0, 10.0))
    monkeypatch.setattr(main, "_atr_wilder", lambda candles: 1.0)
    monkeypatch.setattr(
        main, "_market_regime_analysis",
        lambda candles: {"market_regime": "TRENDING_UP", "indicators": {"atr_vs_recent_median": 1.0}},
    )
    def conflicting_m15_structure(candles, left, right, tolerance):
        timeframe_seconds = (candles[1].time - candles[0].time).total_seconds()
        return {
            "market_structure": "UPTREND",
            "events": (
                ["BEARISH_BREAK_OF_STRUCTURE"]
                if timeframe_seconds >= 900
                else ["NO_CONFIRMED_STRUCTURE_EVENT"]
            ),
        }

    monkeypatch.setattr(main, "_price_action_analysis", conflicting_m15_structure)
    monkeypatch.setattr(
        main, "_candle_microstructure_analysis",
        lambda candles: {
            "candle_direction": "NEUTRAL",
            "close_location_0_to_1": 0.5,
            "closed_candle_pattern": "NONE",
            "tick_volume": {"state": "UNAVAILABLE", "directional_confirmation": "NONE"},
        },
    )
    response = client.post("/api/v1/analysis/entry-plan", json=payload)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["direction"] == "WAIT"
    assert body["selected_strategy"] is None
    assert body["setup"] is None
    assert body["execution_enabled"] is False
    assert any("محفز دخول مكتمل" in reason for reason in body["blockers"])


def test_adaptive_demo_risk_scales_with_setup_quality_and_caps(monkeypatch):
    monkeypatch.setattr(main.settings, "auto_demo_risk_per_trade_percent", 0.5)
    monkeypatch.setattr(main.settings, "max_risk_per_trade_percent", 0.5)
    monkeypatch.setattr(main.settings, "max_daily_loss_percent", 10.0)

    def analysis(score):
        return {
            "technical_dossier": {"confluence_score": score},
            "entry_engine": {"entry_quality_score_0_to_100": score},
        }

    exceptional = main._adaptive_demo_risk_policy(analysis(97), 0.0)
    strong = main._adaptive_demo_risk_policy(analysis(88), 0.0)
    assert exceptional["approved"] is True
    assert exceptional["risk_percent"] == 0.5
    assert exceptional["quality_band"] == "EXCEPTIONAL"
    assert strong["risk_percent"] == 0.3
    assert strong["quality_band"] == "STRONG"


def test_adaptive_demo_risk_reduces_after_daily_loss_and_respects_server_cap(monkeypatch):
    monkeypatch.setattr(main.settings, "auto_demo_risk_per_trade_percent", 0.5)
    monkeypatch.setattr(main.settings, "max_risk_per_trade_percent", 0.5)
    monkeypatch.setattr(main.settings, "max_daily_loss_percent", 10.0)
    analysis = {
        "technical_dossier": {"confluence_score": 93},
        "entry_engine": {"entry_quality_score_0_to_100": 93},
    }
    reduced = main._adaptive_demo_risk_policy(analysis, 7.5)
    assert reduced["risk_percent"] == 0.2
    assert reduced["daily_loss_headroom"] == "REDUCED"

    monkeypatch.setattr(main.settings, "auto_demo_risk_per_trade_percent", 0.25)
    capped = main._adaptive_demo_risk_policy(analysis, 0.0)
    assert capped["risk_percent"] == 0.25
    assert capped["configured_cap_percent"] == 0.25


def test_adaptive_demo_risk_locks_at_daily_loss_limit(monkeypatch):
    monkeypatch.setattr(main.settings, "max_daily_loss_usd", 15.0)
    analysis = {
        "technical_dossier": {"confluence_score": 99},
        "entry_engine": {"entry_quality_score_0_to_100": 99},
    }
    result = main._adaptive_demo_risk_policy(analysis, 15.0)
    assert result["approved"] is False
    assert result["risk_percent"] == 0.0


def test_entry_scanner_finds_momentum_continuation_without_waiting_for_swing_bos(monkeypatch):
    raw = _technical_payload("M5", count=100)["candles"]
    candles = [main.Candle(**item) for item in raw]
    candles[-2] = candles[-2].model_copy(update={
        "open": 2299.8, "high": 2300.5, "low": 2299.7, "close": 2300.1,
    })
    candles[-1] = candles[-1].model_copy(update={
        "open": 2300.1, "high": 2301.2, "low": 2300.0, "close": 2301.0,
    })
    monkeypatch.setattr(main, "_ema_series", lambda values, period: [2300.0] * len(values))
    monkeypatch.setattr(main, "_price_action_analysis", lambda *args, **kwargs: {
        "market_structure": "UPTREND", "events": [],
    })
    monkeypatch.setattr(main, "_candle_microstructure_analysis", lambda values: {
        "closed_candle_pattern": "NONE", "impulse_direction": "BULLISH",
    })
    report = {
        "market_structure": "UPTREND",
        "microstructure": {"closed_candle_pattern": "NONE", "impulse_direction": "BULLISH"},
        "atr14": 1.0, "ema20": 2300.0, "market_regime": "TRENDING_UP",
    }
    scan = main._classify_entry_setups_for_direction(
        "BUY", {"M5": candles}, {"M5": report}, 0.05,
    )
    assert any(item["strategy"] == "MOMENTUM_CONTINUATION" for item in scan["candidates"])
    assert scan["selected"]["strategy"] == "MOMENTUM_CONTINUATION"


def test_entry_scanner_detects_closed_ema20_reclaim(monkeypatch):
    raw = _technical_payload("M5", count=100)["candles"]
    candles = [main.Candle(**item) for item in raw]
    candles[-2] = candles[-2].model_copy(update={
        "open": 2300.1, "high": 2300.3, "low": 2299.8, "close": 2300.0,
    })
    candles[-1] = candles[-1].model_copy(update={
        "open": 2300.0, "high": 2300.9, "low": 2299.95, "close": 2300.8,
    })
    def controlled_ema(values, period):
        return [2300.5 if len(values) == len(candles) else 2300.2] * len(values)
    monkeypatch.setattr(main, "_ema_series", controlled_ema)
    monkeypatch.setattr(main, "_price_action_analysis", lambda *args, **kwargs: {
        "market_structure": "UPTREND", "events": [],
    })
    monkeypatch.setattr(main, "_candle_microstructure_analysis", lambda values: {
        "closed_candle_pattern": "NONE", "impulse_direction": "BULLISH",
    })
    report = {
        "market_structure": "UPTREND",
        "microstructure": {"closed_candle_pattern": "NONE", "impulse_direction": "BULLISH"},
        "atr14": 1.0, "ema20": 2300.5, "market_regime": "TRENDING_UP",
    }
    scan = main._classify_entry_setups_for_direction(
        "BUY", {"M5": candles}, {"M5": report}, 0.05,
    )
    assert any(item["strategy"] == "EMA_RECLAIM" for item in scan["candidates"])


def test_demo_quality_gate_accepts_new_strategies_only_with_strong_setup_score():
    # The legacy strategy scan is not sufficient to authorize a demo order.
    # A complete, matching dedicated entry-engine decision is required.
    analysis = {
        "decision": "BUY",
        "technical_dossier": {
            "confluence_score": 86,
            "strategy_scan": {
                "blockers": [],
                "selected": {
                    "strategy": "EMA_RECLAIM", "score": 5, "max_score": 5,
                },
            },
        },
        "entry_engine": {
            "direction": "BUY",
            "selected_strategy": "EMA_RECLAIM",
            "effective_entry_quality_score_0_to_100": 90,
            "score_threshold": 85,
            "blockers": [],
            "entry_setup_scan": {
                "blockers": [],
                "selected": {
                    "strategy": "EMA_RECLAIM", "score": 5, "max_score": 5,
                },
            },
            "setup": {
                "strategy": "EMA_RECLAIM",
                "entry": 2320.0,
                "stop_loss": 2318.0,
                "take_profit": 2324.0,
            },
        },
    }
    approved, _, details = main._demo_candidate_quality_gate(analysis)
    assert approved is True
    assert details["selected_strategy"] == "EMA_RECLAIM"


def test_entry_scanner_keeps_recent_closed_bos_as_candidate(monkeypatch):
    raw = _technical_payload("M5", count=100)["candles"]
    candles = [main.Candle(**item) for item in raw]
    candles[-1] = candles[-1].model_copy(update={
        "open": 2319.7, "high": 2320.1, "low": 2319.6, "close": 2320.0,
    })
    def structure_with_recent_bos(values, *args, **kwargs):
        return {
            "market_structure": "UPTREND",
            "events": ["BULLISH_BREAK_OF_STRUCTURE"] if len(values) == len(candles) - 1 else [],
        }
    monkeypatch.setattr(main, "_ema_series", lambda values, period: [2319.8] * len(values))
    monkeypatch.setattr(main, "_price_action_analysis", structure_with_recent_bos)
    monkeypatch.setattr(main, "_candle_microstructure_analysis", lambda values: {
        "closed_candle_pattern": "BULLISH_ENGULFING",
        "impulse_direction": "BULLISH",
    })
    report = {
        "market_structure": "UPTREND",
        "microstructure": {"closed_candle_pattern": "BULLISH_ENGULFING", "impulse_direction": "BULLISH"},
        "atr14": 1.0, "ema20": 2319.8, "market_regime": "TRENDING_UP",
    }
    scan = main._classify_entry_setups_for_direction(
        "BUY", {"M5": candles}, {"M5": report}, 0.05,
    )
    assert any(item["strategy"] == "BREAKOUT" for item in scan["candidates"])


def test_mt5_candle_cache_restores_closed_history_after_restart(monkeypatch):
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    restored = {}
    for timeframe in ("M5", "M15", "H1", "H4"):
        seconds = main._TIMEFRAME_SECONDS[timeframe]
        latest_bucket = int(now.timestamp() // seconds) * seconds - seconds
        latest = datetime.fromtimestamp(latest_bucket, timezone.utc)
        restored[timeframe] = [
            {
                "time": (latest - timedelta(seconds=(59 - index) * seconds)).isoformat(),
                "open": 2300.0 + index * 0.1,
                "high": 2300.5 + index * 0.1,
                "low": 2299.5 + index * 0.1,
                "close": 2300.1 + index * 0.1,
                "tick_volume": 100,
            }
            for index in range(60)
        ]
    monkeypatch.setattr(main, "load_mt5_candle_history", lambda *args, **kwargs: restored)
    main._latest_mt5_candles = None
    assert main._restore_mt5_candle_cache() is True
    assert main._latest_mt5_candles["source"].startswith("PostgreSQL")
    assert all(len(main._latest_mt5_candles["timeframes"][frame]) == 60 for frame in restored)



def test_mt5_candle_history_persistence_uses_connection_execute(monkeypatch):
    from types import SimpleNamespace

    calls = []

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def execute(self, statement, params=None):
            calls.append((statement, params))
            return SimpleNamespace(rowcount=1)

    monkeypatch.setattr(learning_store, "_connect", lambda: FakeConnection())
    candle = SimpleNamespace(
        time=datetime.now(timezone.utc),
        open=2300.0, high=2301.0, low=2299.0, close=2300.5,
        tick_volume=123,
    )
    assert learning_store.save_mt5_candle_history("GOLD", {"M5": [candle]}) == 1
    assert len(calls) == 2
    assert "INSERT INTO mt5_closed_candles" in calls[0][0]
    assert "DELETE FROM mt5_closed_candles" in calls[1][0]
