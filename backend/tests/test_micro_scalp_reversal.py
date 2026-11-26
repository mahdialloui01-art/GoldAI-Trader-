from app.micro_scalp_reversal import analyze_micro_scalp_reversal


def _bars():
    rows = []
    for i in range(40):
        base = 100.0 + (i % 3) * 0.05
        rows.append({"open": base, "high": base + 0.6, "low": base - 0.6, "close": base + 0.1})
    # Sweep the preceding lows, reclaim the level, and close with a clear lower wick.
    prior_low = min(row["low"] for row in rows[-12:])
    rows.append({
        "open": prior_low + 0.5, "high": prior_low + 0.95,
        "low": prior_low - 0.6, "close": prior_low + 0.8,
    })
    return rows


def test_micro_reversal_is_independent_and_never_authorizes_execution():
    result = analyze_micro_scalp_reversal(
        {"M5": _bars(), "M15": []},
        {"bid": 100.8, "ask": 100.85, "point": 0.01, "spread_points": 5, "digits": 2},
    )
    assert result["direction"] == "BUY"
    assert result["evidence"]["liquidity_sweep"] is True
    assert result["entry_authorized"] is False
    assert result["execution_enabled"] is False
    assert result["independent_from_primary_signal"] is True


def test_micro_reversal_fails_closed_without_fresh_quote_or_candles():
    assert analyze_micro_scalp_reversal({"M5": _bars()}, None)["status"] == "DATA_INSUFFICIENT"
    assert analyze_micro_scalp_reversal({"M5": _bars()[:20]}, {"bid": 1, "ask": 1.01})["status"] == "DATA_INSUFFICIENT"
