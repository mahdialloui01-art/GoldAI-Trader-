"""Market-regime and range-boundary diagnostics for GOLD.

Informational only: range setups are candidates, not execution instructions.
All levels are derived from supplied closed OHLC candles.
"""
from __future__ import annotations

from typing import Any
import math


def _v(row: Any, key: str) -> float:
    value = row.get(key) if isinstance(row, dict) else getattr(row, key)
    return float(value)


def _ema(values: list[float], period: int) -> list[float]:
    alpha = 2.0 / (period + 1.0)
    out = [values[0]]
    for value in values[1:]:
        out.append(alpha * value + (1.0 - alpha) * out[-1])
    return out


def _atr(highs: list[float], lows: list[float], closes: list[float], period: int = 14) -> float:
    trs = []
    for i in range(1, len(closes)):
        trs.append(max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1])))
    if not trs:
        return 0.0
    seed = sum(trs[:period]) / min(period, len(trs))
    value = seed
    for tr in trs[period:]:
        value = (value * (period - 1) + tr) / period
    return value


def _adx(highs: list[float], lows: list[float], closes: list[float], period: int = 14) -> float:
    trs, plus, minus = [], [], []
    for i in range(1, len(closes)):
        up, down = highs[i] - highs[i - 1], lows[i - 1] - lows[i]
        plus.append(up if up > down and up > 0 else 0.0)
        minus.append(down if down > up and down > 0 else 0.0)
        trs.append(max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1])))
    if len(trs) < period * 2:
        return 50.0
    smooth_tr = sum(trs[:period])
    smooth_p = sum(plus[:period])
    smooth_m = sum(minus[:period])
    dx_values = []
    for i in range(period, len(trs)):
        smooth_tr = smooth_tr - smooth_tr / period + trs[i]
        smooth_p = smooth_p - smooth_p / period + plus[i]
        smooth_m = smooth_m - smooth_m / period + minus[i]
        if smooth_tr <= 0:
            continue
        pdi, mdi = 100 * smooth_p / smooth_tr, 100 * smooth_m / smooth_tr
        dx_values.append(100 * abs(pdi - mdi) / max(pdi + mdi, 1e-9))
    if not dx_values:
        return 50.0
    return sum(dx_values[-period:]) / len(dx_values[-period:])


def _quantile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    pos = (len(ordered) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo)


def analyze_market_regime(frames: dict[str, list[Any]]) -> dict[str, Any]:
    """Classify regime and derive cautious range-edge candidates per timeframe."""
    weights = {"M5": 1.0, "M15": 1.5, "H1": 2.0, "H4": 2.5}
    details: dict[str, Any] = {}
    for frame, weight in weights.items():
        rows = frames.get(frame, [])
        if len(rows) < 60:
            details[frame] = {"available": False, "reason": "يلزم 60 شمعة مغلقة على الأقل"}
            continue
        try:
            opens = [_v(x, "open") for x in rows]
            highs = [_v(x, "high") for x in rows]
            lows = [_v(x, "low") for x in rows]
            closes = [_v(x, "close") for x in rows]
        except (AttributeError, KeyError, TypeError, ValueError):
            details[frame] = {"available": False, "reason": "بيانات OHLC غير صالحة"}
            continue
        if not all(math.isfinite(x) for seq in (opens, highs, lows, closes) for x in seq):
            details[frame] = {"available": False, "reason": "بيانات غير منتهية"}
            continue
        atr = _atr(highs, lows, closes)
        if atr <= 0:
            details[frame] = {"available": False, "reason": "ATR غير صالح"}
            continue
        adx = _adx(highs, lows, closes)
        e20, e50 = _ema(closes, 20), _ema(closes, 50)
        lookback = 48
        rh, rl, rc = highs[-lookback:], lows[-lookback:], closes[-lookback:]
        upper, lower = _quantile(rh, 0.92), _quantile(rl, 0.08)
        width = upper - lower
        efficiency = abs(rc[-1] - rc[0]) / max(sum(abs(b - a) for a, b in zip(rc, rc[1:])), 1e-9)
        slope_atr = abs(e20[-1] - e20[-6]) / (5 * atr)
        range_width_atr = width / atr
        range_score = 0
        range_score += 30 if adx < 18 else 20 if adx < 22 else 0
        range_score += 25 if efficiency < 0.22 else 15 if efficiency < 0.32 else 0
        range_score += 25 if slope_atr < 0.08 else 15 if slope_atr < 0.15 else 0
        range_score += 20 if 2.0 <= range_width_atr <= 10.0 else 0

        if range_score >= 65:
            regime = "RANGE"
        elif e20[-1] > e50[-1] and closes[-1] > e20[-1] and adx >= 20:
            regime = "TREND_UP"
        elif e20[-1] < e50[-1] and closes[-1] < e20[-1] and adx >= 20:
            regime = "TREND_DOWN"
        else:
            regime = "TRANSITION"

        price = closes[-1]
        last_open, last_high, last_low = opens[-1], highs[-1], lows[-1]
        candle_range = max(last_high - last_low, 1e-9)
        close_location = (price - last_low) / candle_range
        lower_wick = min(last_open, price) - last_low
        upper_wick = last_high - max(last_open, price)
        support_touch = last_low <= lower + 0.30 * atr and price > lower
        resistance_touch = last_high >= upper - 0.30 * atr and price < upper
        bull_rejection = support_touch and close_location >= 0.62 and lower_wick >= 0.25 * candle_range
        bear_rejection = resistance_touch and close_location <= 0.38 and upper_wick >= 0.25 * candle_range

        prior_inside = lower <= closes[-2] <= upper
        bull_break = prior_inside and price > upper + 0.12 * atr and close_location >= 0.70
        bear_break = prior_inside and price < lower - 0.12 * atr and close_location <= 0.30
        near_support = abs(price - lower) <= 0.40 * atr or support_touch
        near_resistance = abs(upper - price) <= 0.40 * atr or resistance_touch

        setup = None
        if regime == "RANGE" and bull_rejection:
            setup = {
                "direction": "BUY", "entry_zone": [round(lower + 0.05 * atr, 5), round(lower + 0.35 * atr, 5)],
                "stop_invalidation": round(lower - 0.30 * atr, 5),
                "target_1": round((lower + upper) / 2, 5), "target_2": round(upper - 0.15 * atr, 5),
                "confirmation": "إغلاق رفض صاعد قرب دعم النطاق",
            }
        elif regime == "RANGE" and bear_rejection:
            setup = {
                "direction": "SELL", "entry_zone": [round(upper - 0.35 * atr, 5), round(upper - 0.05 * atr, 5)],
                "stop_invalidation": round(upper + 0.30 * atr, 5),
                "target_1": round((lower + upper) / 2, 5), "target_2": round(lower + 0.15 * atr, 5),
                "confirmation": "إغلاق رفض هابط قرب مقاومة النطاق",
            }

        details[frame] = {
            "available": True, "regime": regime, "range_score_0_to_100": range_score,
            "adx14": round(adx, 2), "efficiency_ratio": round(efficiency, 3),
            "ema20_slope_atr_per_bar": round(slope_atr, 4), "atr14": round(atr, 5),
            "range_width_atr": round(range_width_atr, 2),
            "support": round(lower, 5), "resistance": round(upper, 5),
            "price": round(price, 5), "near_support": near_support,
            "near_resistance": near_resistance, "bullish_rejection": bull_rejection,
            "bearish_rejection": bear_rejection, "confirmed_upside_breakout": bull_break,
            "confirmed_downside_breakout": bear_break, "range_setup": setup,
            "weight": weight,
        }

    available = {k: v for k, v in details.items() if v.get("available")}
    if not available:
        return {
            "status": "UNAVAILABLE", "market_regime": "UNKNOWN",
            "summary": "لا توجد بيانات كافية لتصنيف حالة السوق.",
            "timeframes": details, "execution_enabled": False,
        }
    total_weight = sum(v["weight"] for v in available.values())
    range_weight = sum(v["weight"] for v in available.values() if v["regime"] == "RANGE")
    up_weight = sum(v["weight"] for v in available.values() if v["regime"] == "TREND_UP")
    down_weight = sum(v["weight"] for v in available.values() if v["regime"] == "TREND_DOWN")
    if range_weight / total_weight >= 0.55:
        overall = "RANGE"
    elif up_weight / total_weight >= 0.55:
        overall = "TREND_UP"
    elif down_weight / total_weight >= 0.55:
        overall = "TREND_DOWN"
    else:
        overall = "TRANSITION"

    # M15 is the execution-analysis frame for range edges. Do not require the
    # aggregate regime or H1 to also be RANGE: a valid M15 boundary rejection
    # can occur while higher frames are trending or transitioning. The caller
    # applies a separate strong-opposition safety check before qualifying it.
    m15 = details.get("M15", {})
    h1 = details.get("H1", {})
    candidates = []
    if m15.get("available") and m15.get("regime") == "RANGE" and m15.get("range_setup"):
        candidates.append({"timeframe": "M15", **m15["range_setup"]})
    if m15.get("available") and m15.get("regime") == "RANGE" and not candidates:
        if m15.get("near_support"):
            watch = "WAIT_FOR_BULLISH_REJECTION_AT_SUPPORT"
        elif m15.get("near_resistance"):
            watch = "WAIT_FOR_BEARISH_REJECTION_AT_RESISTANCE"
        else:
            watch = "WAIT_FOR_RANGE_EDGE"
    else:
        watch = "NO_RANGE_ENTRY"
    if any(v.get("confirmed_upside_breakout") for v in available.values()):
        breakout = "UPSIDE_BREAKOUT_CONFIRMED"
    elif any(v.get("confirmed_downside_breakout") for v in available.values()):
        breakout = "DOWNSIDE_BREAKOUT_CONFIRMED"
    else:
        breakout = "NO_CONFIRMED_BREAKOUT"

    return {
        "status": "AVAILABLE", "market_regime": overall,
        "range_consensus_0_to_100": round(range_weight / total_weight * 100, 1),
        "breakout_state": breakout, "range_trade_watch": watch,
        "range_entry_candidates": candidates,
        "summary": (
            "إطار M15 في نطاق؛ ننتظر رفضاً مؤكداً عند أحد الحدين ولا ندخل من منتصف النطاق."
            if m15.get("available") and m15.get("regime") == "RANGE" else
            "اتجاه صاعد؛ لا نستخدم استراتيجية البيع من المقاومة داخل النطاق."
            if overall == "TREND_UP" else
            "اتجاه هابط؛ لا نستخدم استراتيجية الشراء من الدعم داخل النطاق."
            if overall == "TREND_DOWN" else
            "السوق انتقالي أو غير محسوم؛ الأفضل انتظار وضوح البنية."
        ),
        "timeframes": details,
        "policy": {"informational_only": True, "can_override_primary_trend": False, "can_authorize_entry": False},
        "execution_enabled": False,
    }
