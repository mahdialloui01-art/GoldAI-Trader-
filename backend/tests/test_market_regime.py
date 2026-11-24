from app.market_regime import analyze_market_regime


def _range_candles(count=100):
    import math
    rows = []
    for i in range(count):
        center = 2400 + 4.5 * math.sin(i * 0.42)
        rows.append({
            "open": center - 0.25,
            "high": center + 0.75,
            "low": center - 0.75,
            "close": center + 0.25,
        })
    # A rejection candle at the lower boundary, with a strong close in its upper part.
    rows[-1] = {"open": 2396.0, "high": 2398.0, "low": 2394.7, "close": 2397.5}
    return rows


def test_range_classifier_finds_range_and_edge_setup_without_execution():
    frames = {name: _range_candles() for name in ("M5", "M15", "H1", "H4")}
    result = analyze_market_regime(frames)
    assert result["status"] == "AVAILABLE"
    assert result["market_regime"] in {"RANGE", "TRANSITION"}
    assert result["execution_enabled"] is False
    assert result["policy"]["informational_only"] is True
    assert set(result["timeframes"]) == {"M5", "M15", "H1", "H4"}
    for item in result["timeframes"].values():
        if item["available"]:
            assert item["support"] < item["resistance"]
            assert item["range_score_0_to_100"] <= 100


def test_closed_candle_downside_breakout_is_not_hidden_by_range_recalculation():
    rows = _range_candles()[:-1]
    prior_lows = [row["low"] for row in rows[-48:]]
    prior_closes = [row["close"] for row in rows[-48:]]
    support = sorted(prior_lows)[int((len(prior_lows) - 1) * 0.08)]
    # A decisive closed candle below the prior range boundary.
    close = support - 1.8
    rows.append({
        "open": close + 0.9,
        "high": close + 1.0,
        "low": close - 0.2,
        "close": close,
    })
    result = analyze_market_regime({name: rows for name in ("M5", "M15", "H1", "H4")})
    assert result["breakout_state"] == "DOWNSIDE_BREAKOUT_CONFIRMED"
    assert result["range_trade_watch"] == "DOWNSIDE_BREAKOUT_CONFIRMED"
    assert result["timeframes"]["M15"]["confirmed_downside_breakout"] is True
    assert result["timeframes"]["M15"]["breakout_reference_support"] < prior_closes[-1]
    assert result["execution_enabled"] is False


def test_deep_wick_that_closes_back_inside_range_is_not_confirmed_breakout():
    rows = _range_candles()[:-1]
    prior_lows = [row["low"] for row in rows[-48:]]
    support = sorted(prior_lows)[int((len(prior_lows) - 1) * 0.08)]
    close = support + 0.3
    rows.append({
        "open": close + 0.05,
        "high": close + 0.2,
        "low": support - 2.0,
        "close": close,
    })
    result = analyze_market_regime({name: rows for name in ("M5", "M15", "H1", "H4")})
    assert result["breakout_state"] != "DOWNSIDE_BREAKOUT_CONFIRMED"
    assert result["timeframes"]["M15"]["confirmed_downside_breakout"] is False
    assert result["execution_enabled"] is False


def test_range_classifier_fails_closed_on_insufficient_history():
    result = analyze_market_regime({name: _range_candles(30) for name in ("M5", "M15", "H1", "H4")})
    assert result["status"] == "UNAVAILABLE"
    assert result["market_regime"] == "UNKNOWN"
    assert result["execution_enabled"] is False


def test_range_classifier_never_suggests_range_trade_in_trend():
    rows = []
    for i in range(100):
        close = 2300 + i * 1.2
        rows.append({"open": close - 0.2, "high": close + 0.5, "low": close - 0.5, "close": close})
    result = analyze_market_regime({name: rows for name in ("M5", "M15", "H1", "H4")})
    assert result["market_regime"] in {"TREND_UP", "TRANSITION"}
    assert result["range_entry_candidates"] == []
    assert result["execution_enabled"] is False


def test_m15_range_edge_is_retained_when_higher_frames_are_not_range():
    frames = {name: _range_candles() for name in ("M5", "M15", "H1", "H4")}
    trend = []
    for i in range(100):
        close = 2400 + i * 0.8
        trend.append({"open": close - 0.2, "high": close + 0.5, "low": close - 0.5, "close": close})
    frames["H1"] = trend
    frames["H4"] = trend
    result = analyze_market_regime(frames)
    assert result["timeframes"]["M15"]["regime"] == "RANGE"
    assert result["range_entry_candidates"]
    assert result["range_entry_candidates"][0]["timeframe"] == "M15"
    assert result["execution_enabled"] is False
