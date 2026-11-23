from datetime import datetime, timedelta, timezone

from app.reversal_detector import detect_early_reversal


def _candles(count=100, reverse_at=75):
    start = datetime.now(timezone.utc) - timedelta(minutes=count * 5)
    rows = []
    for index in range(count):
        # A sustained decline followed by a controlled recovery.
        if index < reverse_at:
            close = 2400.0 - index * 0.35
        else:
            close = 2400.0 - reverse_at * 0.35 + (index - reverse_at) * 0.62
        open_price = close - (0.18 if index >= reverse_at else 0.10)
        rows.append({
            "time": start + timedelta(minutes=index * 5),
            "open": open_price,
            "high": max(open_price, close) + 0.16,
            "low": min(open_price, close) - 0.16,
            "close": close,
        })
    return rows


def test_reversal_detector_returns_separate_non_executable_diagnostic():
    frames = {name: _candles() for name in ("M5", "M15", "H1", "H4")}
    result = detect_early_reversal(frames)
    assert result["direction"] in {"BUY", "SELL", "WAIT"}
    assert result["stage"] in {"NO_EARLY_SIGNAL", "EARLY_WARNING", "DEVELOPING", "CONFIRMED"}
    assert result["score_type"] == "HEURISTIC_EVIDENCE_NOT_PROBABILITY"
    assert result["execution_enabled"] is False
    assert result["policy"]["informational_only"] is True
    assert result["policy"]["can_override_primary_trend"] is False
    assert set(result["timeframes"]) == {"M5", "M15", "H1", "H4"}


def test_reversal_detector_marks_insufficient_history_unavailable():
    frames = {name: _candles(20) for name in ("M5", "M15", "H1", "H4")}
    result = detect_early_reversal(frames)
    assert result["status"] == "UNAVAILABLE"
    assert result["direction"] == "WAIT"
    assert result["score_0_to_100"] == 0
    assert result["execution_enabled"] is False


def test_live_h4_confirmation_is_only_a_small_advisory_adjustment():
    frames = {name: _candles() for name in ("M5", "M15", "H1", "H4")}
    without_live = detect_early_reversal(frames)
    with_live = detect_early_reversal(frames, {"status": "AVAILABLE", "direction": "BUY"})
    assert with_live["score_0_to_100"] <= without_live["score_0_to_100"] + 3.0
    assert with_live["execution_enabled"] is False
