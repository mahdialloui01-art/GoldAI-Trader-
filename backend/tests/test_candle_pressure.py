from app.candle_pressure import classify_candle, classify_candle_series


def test_closed_bullish_candle_reports_buyer_pressure_without_claiming_delta():
    result = classify_candle({"open": 100, "high": 110, "low": 99, "close": 109, "tick_volume": 120})
    assert result["buyer_strength"] > result["seller_strength"]
    assert result["dominant_side"] == "BUYERS"
    assert result["tick_direction_is_aggressor_delta"] is False


def test_live_candle_uses_tick_direction_as_limited_proxy():
    result = classify_candle({
        "open": 100, "high": 104, "low": 99, "close": 101,
        "live": True, "tick_count": 20, "uptick_count": 15, "downtick_count": 2,
    }, live=True)
    assert result["tick_direction_proxy_percent"] > 0
    assert result["live"] is True
    assert "NOT_AGGRESSOR_DELTA" in result["score_type"]


def test_flat_or_invalid_candle_is_not_scored():
    result = classify_candle({"open": 100, "high": 100, "low": 100, "close": 100})
    assert result["status"] == "INVALID_OR_FLAT_CANDLE"
    assert result["buyer_strength"] is None


def test_series_keeps_each_candle_time_and_volume_context():
    rows = classify_candle_series([
        {"time": "t1", "open": 1, "high": 3, "low": 1, "close": 2, "tick_volume": 10},
        {"time": "t2", "open": 2, "high": 4, "low": 1, "close": 3.8, "tick_volume": 20},
    ])
    assert [row["time"] for row in rows] == ["t1", "t2"]
    assert rows[1]["tick_volume_participation_vs_recent_average"] == 2.0
