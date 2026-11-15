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


def test_multi_timeframe_fails_closed_when_one_frame_is_stale():
    _set_fresh_quote()
    payload = {
        "symbol": "GOLD",
        "m5": _technical_payload("M5")["candles"],
        "m15": _technical_payload("M15")["candles"],
        "h1": _technical_payload("H1")["candles"],
        "h4": _technical_payload("H4", start_offset_seconds=48 * 3600)["candles"],
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
        "h4": _technical_payload("H4")["candles"],
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


def test_price_action_rejects_stale_candles_and_never_uses_them():
    _set_fresh_quote()
    payload = _technical_payload("M5", start_offset_seconds=3600)
    response = client.post("/api/v1/analysis/price-action", json=payload)
    assert response.status_code == 422
    assert "stale" in response.json()["detail"]


def test_price_action_fails_closed_without_mt5_quote():
    main._latest_mt5_quote = None
    response = client.post("/api/v1/analysis/price-action", json=_technical_payload("M5"))
    assert response.status_code == 503


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



def test_economic_calendar_returns_only_high_impact_us_events(monkeypatch):
    monkeypatch.setattr(main.settings, "economic_calendar_api_key", "calendar-test-key")
    future = (datetime.now(timezone.utc) + timedelta(hours=2)).replace(tzinfo=None).isoformat()
    class FakeResponse:
        def raise_for_status(self):
            pass
        def json(self):
            return [
                {"CalendarId": "1", "Date": future, "Event": "CPI", "Category": "Inflation",
                 "Importance": 3, "DateSpan": "0", "Forecast": "2.5%", "Source": "BLS"},
                {"CalendarId": "2", "Date": future, "Event": "Low impact", "Importance": 1},
            ]
    class FakeClient:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return None
        async def get(self, url, params, headers):
            assert "/calendar/country/united%20states/" in url
            assert params["importance"] == "3"
            assert headers["Authorization"] == "calendar-test-key"
            return FakeResponse()
    monkeypatch.setattr(main.httpx, "AsyncClient", lambda **kwargs: FakeClient())
    response = client.get("/api/v1/calendar/us-high-impact?days_ahead=3")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["event_count"] == 1
    assert body["events"][0]["event"] == "CPI"
    assert body["events"][0]["importance"] == 3
    assert body["execution_enabled"] is False


def test_news_filter_blocks_inside_high_impact_blackout_window(monkeypatch):
    monkeypatch.setattr(main.settings, "economic_calendar_api_key", "calendar-test-key")
    event_time = (datetime.now(timezone.utc) + timedelta(minutes=10)).replace(tzinfo=None).isoformat()
    class FakeResponse:
        def raise_for_status(self):
            pass
        def json(self):
            return [{"CalendarId": "9", "Date": event_time, "Event": "FOMC Rate Decision",
                     "Importance": 3, "DateSpan": "0"}]
    class FakeClient:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return None
        async def get(self, *args, **kwargs):
            return FakeResponse()
    monkeypatch.setattr(main.httpx, "AsyncClient", lambda **kwargs: FakeClient())
    response = client.get("/api/v1/analysis/news-filter")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["decision"] == "WAIT"
    assert body["trading_allowed"] is False
    assert body["active_events"][0]["event"] == "FOMC Rate Decision"
    assert body["fail_closed"] is True


def test_news_filter_fails_closed_when_calendar_is_not_configured(monkeypatch):
    monkeypatch.setattr(main.settings, "economic_calendar_api_key", "")
    response = client.get("/api/v1/analysis/news-filter")
    assert response.status_code == 503
    assert "not configured" in response.json()["detail"]


def test_news_filter_fails_closed_when_provider_is_unavailable(monkeypatch):
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
    assert response.status_code == 503
    assert "paused" in response.json()["detail"]


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
        "stop_distance_points": 100,
        "risk_percent": 0.5,
    })
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["approved"] is True
    assert body["recommended_lots"] == 0.5
    assert body["estimated_stop_loss_amount"] == 5.0
    assert body["estimated_risk_percent"] == 0.5
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
        "daily_loss_percent": 2.0,
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


def test_market_regime_endpoint_requires_fresh_broker_quote():
    main._latest_mt5_quote = None
    payload = _technical_payload("M5", start_offset_seconds=300)
    response = client.post("/api/v1/analysis/market-regime", json=payload)
    assert response.status_code == 503


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
    assert body["decision"] == "PRE_NEWS_SETUP_CANDIDATE"
    assert body["most_likely_gold_direction"] == "BEARISH"
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
    base = datetime(2025, 1, 1, tzinfo=timezone.utc)
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
    horizon = result["horizons"]["5m"]
    assert "net_positive_rate_percent" in horizon
    assert "max_drawdown_on_chronological_compounded_net_returns_percent" in horizon
    assert body["data_policy"]["observations_sorted_by_release_time_for_risk_metrics"] is True


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
