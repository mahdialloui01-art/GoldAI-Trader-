from backend.app.support_resistance_reversal import analyze_support_resistance_reversal


def _inputs():
    market = {
        "status": "AVAILABLE",
        "timeframes": {
            "M15": {
                "available": True, "atr14": 1.0, "support": 100.0, "resistance": 110.0,
                "bullish_rejection": True, "bearish_rejection": False,
                "confirmed_downside_breakout": False, "confirmed_upside_breakout": False,
            },
            "H1": {
                "available": True, "atr14": 1.5, "support": 100.2, "resistance": 110.2,
                "bullish_rejection": True, "bearish_rejection": False,
            },
        },
    }
    quote = {"bid": 100.08, "ask": 100.10, "digits": 2, "spread_points": 2, "point": 0.01}
    m5 = {
        "market_regime": "RANGE",
        "price_extension_from_ema20_atr": 0.2,
        "microstructure": {"closed_candle_pattern": "BULLISH_REJECTION", "impulse_direction": "BULLISH"},
    }
    return market, quote, m5


def test_support_reversal_is_separate_and_reports_levels():
    market, quote, m5 = _inputs()
    result = analyze_support_resistance_reversal(market, quote, m5)

    assert result["status"] == "CONFIRMED_REVERSAL_CANDIDATE"
    assert result["direction"] == "BUY"
    assert result["zone_type"] == "SUPPORT"
    assert result["score"] >= 70
    assert result["entry_zone"] == [99.9, 100.35]
    assert result["expected_move_start"] == 100.0
    assert result["expected_move_end"] == 109.85
    assert result["entry_authorized"] is False
    assert result["independent_from_primary_signal"] is True


def test_no_primary_market_quote_means_no_signal():
    market, _, m5 = _inputs()
    result = analyze_support_resistance_reversal(market, None, m5)
    assert result["status"] == "NO_SETUP"
    assert result["score"] == 0
    assert result["entry_authorized"] is False


def test_broken_support_does_not_show_buy_reversal():
    market, quote, m5 = _inputs()
    market["timeframes"]["M15"]["confirmed_downside_breakout"] = True
    result = analyze_support_resistance_reversal(market, quote, m5)
    assert result["status"] == "ZONE_BROKEN_WAIT"
    assert result["direction"] == "BUY"
    assert result["entry_authorized"] is False
