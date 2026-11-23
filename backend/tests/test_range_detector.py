from datetime import datetime, timedelta, timezone

from app.range_detector import detect_range_market


def _range_candles(count=100, center=2400.0, width=4.0):
    start = datetime.now(timezone.utc) - timedelta(minutes=count * 5)
    rows = []
    for i in range(count):
        # Stable oscillation with small candles and repeated boundary tests.
        import math
        close = center + (width / 2.0 - 0.25) * math.sin(i * 0.42)
        open_price = center + (width / 2.0 - 0.25) * math.sin((i - 0.45) * 0.42)
        rows.append({
            "time": start + timedelta(minutes=i * 5),
            "open": open_price,
            "high": max(open_price, close) + 0.16,
            "low": min(open_price, close) - 0.16,
            "close": close,
        })
    return rows


def test_range_detector_is_non_executable_and_returns_levels():
    result = detect_range_market(_range_candles())
    assert result["execution_enabled"] is False
    assert result["policy"]["informational_only"] is True
    assert result["regime"] in {"SIDEWAYS", "TRANSITION", "TREND_OR_VOLATILE", "RANGE_BREAKOUT_WATCH"}
    assert result["levels"]["support"] < result["levels"]["resistance"]
    assert result["setup"] is None or result["setup"]["strategy"] == "RANGE_EDGE_REJECTION"


def test_range_detector_requires_sufficient_closed_history():
    result = detect_range_market(_range_candles(30))
    assert result["status"] == "UNAVAILABLE"
    assert result["direction"] == "WAIT"
    assert result["setup"] is None
