"""Independent micro-reversal scalping research lane for broker GOLD candles.

Detects small, closed-candle liquidity-sweep reversals. It never authorizes
execution and never changes the primary signal or risk controls.
"""
from __future__ import annotations

from math import isfinite
from typing import Any


def _v(row: Any, key: str) -> float:
    raw = row.get(key) if isinstance(row, dict) else getattr(row, key)
    value = float(raw)
    if not isfinite(value):
        raise ValueError(key)
    return value


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def analyze_micro_scalp_reversal(
    frames: dict[str, list[Any]], quote: dict[str, Any] | None,
) -> dict[str, Any]:
    """Return a short-horizon reversal candidate using only closed M5 candles."""
    empty = {
        "status": "NO_SETUP", "direction": "WAIT", "score": 0,
        "score_type": "HEURISTIC_SETUP_STRENGTH_NOT_WIN_PROBABILITY",
        "entry_authorized": False, "execution_enabled": False,
        "independent_from_primary_signal": True,
    }
    if not quote:
        return {**empty, "status": "DATA_INSUFFICIENT", "reason": "يلزم سعر MT5 حي."}
    rows = frames.get("M5") or []
    if len(rows) < 35:
        return {**empty, "status": "DATA_INSUFFICIENT", "reason": "يلزم 35 شمعة M5 مغلقة على الأقل."}
    try:
        opens = [_v(x, "open") for x in rows]
        highs = [_v(x, "high") for x in rows]
        lows = [_v(x, "low") for x in rows]
        closes = [_v(x, "close") for x in rows]
        bid, ask = float(quote["bid"]), float(quote["ask"])
        point = float(quote.get("point") or 0.01)
        spread_points = float(quote.get("spread_points") or 0)
    except (KeyError, TypeError, ValueError, AttributeError):
        return {**empty, "status": "DATA_INSUFFICIENT", "reason": "بيانات الشموع أو سعر الوسيط غير صالحة."}
    if not all(isfinite(x) for seq in (opens, highs, lows, closes) for x in seq) or bid <= 0 or ask < bid or point <= 0:
        return {**empty, "status": "DATA_INSUFFICIENT", "reason": "بيانات السوق غير صالحة."}

    tr = []
    for i in range(1, len(rows)):
        tr.append(max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1])))
    atr = sum(tr[-14:]) / min(14, len(tr))
    if atr <= 0:
        return {**empty, "status": "DATA_INSUFFICIENT", "reason": "ATR غير صالح."}

    # Compare the latest CLOSED candle with the preceding 12 closed candles.
    last = len(rows) - 1
    prior_low, prior_high = min(lows[last-12:last]), max(highs[last-12:last])
    candle_range = max(highs[last] - lows[last], 1e-9)
    body = abs(closes[last] - opens[last])
    lower_wick = min(opens[last], closes[last]) - lows[last]
    upper_wick = highs[last] - max(opens[last], closes[last])
    close_pos = (closes[last] - lows[last]) / candle_range
    bull_sweep = lows[last] < prior_low and closes[last] > prior_low and closes[last] > opens[last]
    bear_sweep = highs[last] > prior_high and closes[last] < prior_high and closes[last] < opens[last]
    bull_reject = closes[last] > opens[last] and lower_wick >= max(body * 1.15, candle_range * 0.30) and close_pos >= 0.68
    bear_reject = closes[last] < opens[last] and upper_wick >= max(body * 1.15, candle_range * 0.30) and close_pos <= 0.32
    bull_engulf = closes[last] > opens[last] and closes[last-1] < opens[last-1] and closes[last] >= opens[last-1] and opens[last] <= closes[last-1]
    bear_engulf = closes[last] < opens[last] and closes[last-1] > opens[last-1] and closes[last] <= opens[last-1] and opens[last] >= closes[last-1]

    direction = "BUY" if bull_sweep else "SELL" if bear_sweep else "WAIT"
    if direction == "WAIT":
        if bull_reject or bull_engulf:
            direction = "BUY"
        elif bear_reject or bear_engulf:
            direction = "SELL"
    if direction == "WAIT":
        return {**empty, "status": "WATCHING", "reason": "لا يوجد سحب سيولة أو رفض سعري مكتمل على آخر شمعة M5 مغلقة."}

    is_buy = direction == "BUY"
    sweep = bull_sweep if is_buy else bear_sweep
    rejection = bull_reject if is_buy else bear_reject
    engulf = bull_engulf if is_buy else bear_engulf
    reclaim = closes[last] > prior_low if is_buy else closes[last] < prior_high
    # M15 provides context, not a rigid same-direction requirement.
    m15 = frames.get("M15") or []
    m15_support = m15_resistance = None
    if len(m15) >= 20:
        try:
            m15_support = min(_v(x, "low") for x in m15[-20:-1])
            m15_resistance = max(_v(x, "high") for x in m15[-20:-1])
        except (TypeError, ValueError, AttributeError):
            pass

    spread = (ask - bid) if ask > bid else spread_points * point
    spread_ok = spread <= min(0.18 * atr, 0.45)
    price = (bid + ask) / 2
    extension = abs(closes[last] - price)
    not_chasing = extension <= 0.55 * atr
    # Keep targets deliberately short and volatility-aware; no forced fixed-dollar target.
    target_distance = _clamp(0.80 * atr, 1.20, 5.00)
    stop_distance = max(0.55 * atr, 2.5 * spread, 0.80)
    rr = target_distance / stop_distance
    score = 0
    score += 30 if sweep else 0
    score += 20 if rejection else 0
    score += 15 if engulf else 0
    score += 15 if reclaim else 0
    score += 10 if spread_ok else 0
    score += 10 if not_chasing else 0
    score = min(100, score)

    entry = ask if is_buy else bid
    extreme = lows[last] if is_buy else highs[last]
    stop = min(extreme - 0.12 * atr, entry - stop_distance) if is_buy else max(extreme + 0.12 * atr, entry + stop_distance)
    actual_risk = abs(entry - stop)
    target = entry + target_distance if is_buy else entry - target_distance
    actual_rr = abs(target - entry) / actual_risk if actual_risk > 0 else 0
    if not spread_ok:
        status = "SPREAD_BLOCKED"
    elif not not_chasing:
        status = "MISSED_MOVE_NO_CHASE"
    elif score >= 75 and (sweep or (rejection and engulf)) and actual_rr >= 1.0:
        status = "CONFIRMED_MICRO_REVERSAL_CANDIDATE"
    elif score >= 45:
        status = "EARLY_WATCH"
    else:
        status = "NO_SETUP"

    digits = int(quote.get("digits", 2))
    rnd = lambda x: round(float(x), digits)
    return {
        "status": status, "direction": direction, "score": score,
        "score_type": "HEURISTIC_SETUP_STRENGTH_NOT_WIN_PROBABILITY",
        "timeframe": "M5_CLOSED_CANDLES", "current_price": rnd(price),
        "entry_reference": rnd(entry) if status == "CONFIRMED_MICRO_REVERSAL_CANDIDATE" else None,
        "entry_zone": [rnd(entry - 0.12*atr), rnd(entry + 0.12*atr)],
        "stop_invalidation": rnd(stop), "target": rnd(target),
        "expected_move_dollars": round(target_distance, 2),
        "reward_risk": round(actual_rr, 2),
        "evidence": {
            "liquidity_sweep": sweep, "rejection_candle": rejection,
            "engulfing_confirmation": engulf, "close_reclaimed_prior_extreme": reclaim,
            "spread_ok": spread_ok, "anti_chasing_ok": not_chasing,
            "m15_support": rnd(m15_support) if m15_support is not None else None,
            "m15_resistance": rnd(m15_resistance) if m15_resistance is not None else None,
        },
        "entry_authorized": False, "execution_enabled": False,
        "execution_policy": "research_alert_only_no_order_authorization",
        "independent_from_primary_signal": True,
    }
