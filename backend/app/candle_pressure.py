"""Per-candle buyer/seller pressure estimates from observable OHLC structure.

These are candle-structure/tick-direction proxies, not exchange aggressor delta.
Tick volume is participation context only and is never assigned a buyer/seller side.
"""
from __future__ import annotations

from math import isfinite


def _number(value, default=0.0):
    try:
        result = float(value)
        return result if isfinite(result) else default
    except (TypeError, ValueError):
        return default


def classify_candle(candle: dict, *, previous_tick_volumes=None, live=False) -> dict:
    """Return a transparent 0..100 directional pressure estimate for one candle."""
    get = candle.get
    o, h, low, close = (_number(get(k)) for k in ("open", "high", "low", "close"))
    span = max(0.0, h - low)
    if min(o, h, low, close) <= 0 or h < max(o, close) or low > min(o, close) or span <= 0:
        return {
            "status": "INVALID_OR_FLAT_CANDLE", "buyer_strength": None,
            "seller_strength": None, "dominant_side": "UNKNOWN",
            "score_type": "OHLC_PRESSURE_PROXY_NOT_AGGRESSOR_DELTA",
        }

    close_location = min(1.0, max(0.0, (close - low) / span))
    signed_body = min(1.0, max(-1.0, (close - o) / span))
    upper_wick = max(0.0, h - max(o, close)) / span
    lower_wick = max(0.0, min(o, close) - low) / span
    structure_buy = (
        0.50 * close_location
        + 0.30 * ((signed_body + 1.0) / 2.0)
        + 0.20 * ((1.0 + lower_wick - upper_wick) / 2.0)
    )

    tick_count = max(0, int(_number(get("tick_count"))))
    up = max(0, int(_number(get("uptick_count"))))
    down = max(0, int(_number(get("downtick_count"))))
    directional = up + down
    tick_proxy_available = directional > 0
    tick_buy = up / directional if directional else 0.5
    # A live candle incorporates actual sequential Bid changes, but still not
    # true aggressor side. Closed candles rely on their OHLC structure.
    combined_buy = 0.70 * structure_buy + 0.30 * tick_buy if live and tick_proxy_available else structure_buy
    buyer = int(round(100 * min(1.0, max(0.0, combined_buy))))
    seller = 100 - buyer
    difference = abs(buyer - seller)
    side = "BUYERS" if buyer >= 58 else "SELLERS" if seller >= 58 else "BALANCED"
    volumes = [max(0.0, _number(v)) for v in (previous_tick_volumes or []) if _number(v) > 0]
    tick_volume = max(0.0, _number(get("tick_volume")))
    avg_volume = sum(volumes[-20:]) / len(volumes[-20:]) if volumes else 0.0
    participation = (
        round(tick_volume / avg_volume, 2) if avg_volume > 0 and tick_volume > 0 else None
    )
    if live and tick_count < 3:
        quality = "LOW"
    elif difference >= 50 and (not live or directional >= 5):
        quality = "STRONG"
    elif difference >= 20:
        quality = "MODERATE"
    else:
        quality = "WEAK_OR_BALANCED"

    return {
        "status": "AVAILABLE",
        "buyer_strength": buyer,
        "seller_strength": seller,
        "dominant_side": side,
        "pressure_strength": difference,
        "pressure_quality": quality,
        "close_location_percent": round(close_location * 100, 1),
        "body_percent_of_range": round(abs(close - o) / span * 100, 1),
        "upper_wick_percent": round(upper_wick * 100, 1),
        "lower_wick_percent": round(lower_wick * 100, 1),
        "tick_direction_proxy_percent": round((up - down) / directional * 100, 1) if directional else None,
        "tick_direction_is_aggressor_delta": False,
        "tick_count": tick_count if live else None,
        "tick_volume": int(tick_volume) if tick_volume else None,
        "tick_volume_participation_vs_recent_average": participation,
        "score_type": "OHLC_AND_LIVE_TICK_DIRECTION_PROXY_NOT_AGGRESSOR_DELTA",
        "live": bool(live),
    }


def classify_candle_series(candles: list[dict]) -> list[dict]:
    volumes = [_number(item.get("tick_volume")) for item in candles]
    results = []
    for index, candle in enumerate(candles):
        result = classify_candle(
            candle,
            previous_tick_volumes=volumes[max(0, index - 20):index],
            live=bool(candle.get("live", False)),
        )
        results.append({"time": candle.get("time"), **result})
    return results
