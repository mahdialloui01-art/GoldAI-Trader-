"""Independent support/resistance reversal signal lane.

This module only produces a separate research signal. It never changes the
primary trend decision, confluence score, blockers, or execution permissions.
"""
from __future__ import annotations

from typing import Any


def _number(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
        return result if result == result and abs(result) != float("inf") else default
    except (TypeError, ValueError):
        return default


def analyze_support_resistance_reversal(
    market_analysis: dict[str, Any],
    quote: dict[str, Any] | None,
    m5_report: dict[str, Any] | None,
) -> dict[str, Any]:
    """Return an independent support/resistance bounce watch with its own score."""
    empty = {
        "status": "NO_SETUP",
        "direction": "WAIT",
        "score": 0,
        "score_type": "HEURISTIC_SETUP_STRENGTH_NOT_WIN_PROBABILITY",
        "entry_zone": None,
        "expected_move_start": None,
        "expected_move_end": None,
        "entry_authorized": False,
        "independent_from_primary_signal": True,
    }
    if not quote or not m5_report or market_analysis.get("status") != "AVAILABLE":
        return {**empty, "reason": "يلزم سعر MT5 حي وبيانات أطر زمنية صالحة."}

    frames = market_analysis.get("timeframes", {})
    m15 = frames.get("M15", {})
    h1 = frames.get("H1", {})
    if not m15.get("available") or not h1.get("available"):
        return {**empty, "status": "DATA_INSUFFICIENT", "reason": "يلزم توفر M15 وH1."}

    atr = _number(m15.get("atr14"))
    support = _number(m15.get("support"))
    resistance = _number(m15.get("resistance"))
    if atr <= 0 or resistance <= support:
        return {**empty, "status": "DATA_INSUFFICIENT", "reason": "حدود الدعم والمقاومة أو ATR غير صالحة."}

    bid, ask = _number(quote.get("bid")), _number(quote.get("ask"))
    if bid <= 0 or ask < bid:
        return {**empty, "status": "DATA_INSUFFICIENT", "reason": "سعر الوسيط غير صالح."}
    price = (bid + ask) / 2.0
    digits = int(_number(quote.get("digits"), 2))
    h1_support = _number(h1.get("support"))
    h1_resistance = _number(h1.get("resistance"))
    h1_atr = max(_number(h1.get("atr14")), atr)

    support_zone = (support - 0.10 * atr, support + 0.35 * atr)
    resistance_zone = (resistance - 0.35 * atr, resistance + 0.10 * atr)
    support_distance = abs(price - support)
    resistance_distance = abs(price - resistance)
    support_near = support_distance <= 0.65 * atr
    resistance_near = resistance_distance <= 0.65 * atr

    # If price is between both zones, prefer the closer valid boundary.
    side = None
    if support_near and resistance_near:
        side = "SUPPORT" if support_distance <= resistance_distance else "RESISTANCE"
    elif support_near:
        side = "SUPPORT"
    elif resistance_near:
        side = "RESISTANCE"
    if side is None:
        return {
            **empty,
            "status": "WAITING_FOR_ZONE",
            "support": round(support, digits),
            "resistance": round(resistance, digits),
            "current_price": round(price, digits),
            "reason": "السعر بعيد عن مناطق الدعم والمقاومة؛ ننتظر اقترابه.",
        }

    is_support = side == "SUPPORT"
    direction = "BUY" if is_support else "SELL"
    zone = support_zone if is_support else resistance_zone
    in_zone = zone[0] <= price <= zone[1]
    level = support if is_support else resistance
    h1_level = h1_support if is_support else h1_resistance
    h1_distance = abs(h1_level - level)
    h1_agrees = h1_distance <= max(0.75 * atr, 0.35 * h1_atr)
    m15_rejection = bool(m15.get("bullish_rejection") if is_support else m15.get("bearish_rejection"))
    h1_rejection = bool(h1.get("bullish_rejection") if is_support else h1.get("bearish_rejection"))

    micro = m5_report.get("microstructure", {})
    pattern = str(micro.get("closed_candle_pattern", ""))
    impulse = str(micro.get("impulse_direction", ""))
    m5_confirmation = (
        pattern in {"BULLISH_ENGULFING", "BULLISH_REJECTION"} or impulse == "BULLISH"
        if is_support else
        pattern in {"BEARISH_ENGULFING", "BEARISH_REJECTION"} or impulse == "BEARISH"
    )
    m5_regime_ok = m5_report.get("market_regime") != "HIGH_VOLATILITY"
    m5_extension = _number(m5_report.get("price_extension_from_ema20_atr"), 99.0)
    spread_price = _number(quote.get("spread_points")) * _number(quote.get("point"))
    spread_ratio = spread_price / atr if atr > 0 else 99.0
    spread_ok = spread_ratio <= 0.15
    not_broken = not bool(
        m15.get("confirmed_downside_breakout") if is_support
        else m15.get("confirmed_upside_breakout")
    )

    # Dedicated 0-100 setup-strength score; deliberately not merged into main score.
    score = 0
    proximity_ratio = max(0.0, 1.0 - (support_distance if is_support else resistance_distance) / (0.65 * atr))
    score += round(20 * proximity_ratio)
    score += 20 if h1_agrees else 0
    score += 20 if m15_rejection else 0
    score += 15 if h1_rejection else 0
    score += 15 if m5_confirmation else 0
    score += 5 if in_zone else 0
    score += 5 if m5_regime_ok and m5_extension <= 1.5 and spread_ok and not_broken else 0
    score = min(100, score)

    if is_support:
        stop = support - 0.40 * atr
        midpoint = (support + resistance) / 2.0
        far_target = resistance - 0.15 * atr
        target1, target2 = midpoint, far_target
    else:
        stop = resistance + 0.40 * atr
        midpoint = (support + resistance) / 2.0
        far_target = support + 0.15 * atr
        target1, target2 = midpoint, far_target

    risk = abs(price - stop)
    reward = abs(target2 - price)
    rr = reward / risk if risk > 0 else 0.0
    valid_safety = m5_regime_ok and m5_extension <= 1.5 and spread_ok and not_broken and rr >= 1.0

    if not not_broken:
        status = "ZONE_BROKEN_WAIT"
    elif in_zone and score >= 70 and m15_rejection and m5_confirmation and valid_safety:
        status = "CONFIRMED_REVERSAL_CANDIDATE"
    elif in_zone and score >= 55 and valid_safety:
        status = "POTENTIAL_REVERSAL"
    else:
        status = "WATCHING_ZONE"

    return {
        "status": status,
        "direction": direction,
        "zone_type": side,
        "score": score,
        "score_type": "HEURISTIC_SETUP_STRENGTH_NOT_WIN_PROBABILITY",
        "current_price": round(price, digits),
        "support": round(support, digits),
        "resistance": round(resistance, digits),
        "entry_zone": [round(zone[0], digits), round(zone[1], digits)],
        "expected_move_start": round(level, digits),
        "expected_move_end": round(target2, digits),
        "entry_reference": round(price, digits) if in_zone else None,
        "stop_invalidation": round(stop, digits),
        "target_1": round(target1, digits),
        "target_2": round(target2, digits),
        "reward_risk_to_target_2": round(rr, 2),
        "evidence": {
            "m15_rejection": m15_rejection,
            "h1_rejection": h1_rejection,
            "h1_level_alignment": h1_agrees,
            "m5_confirmation": m5_confirmation,
            "m5_pattern": pattern,
            "price_inside_zone": in_zone,
            "spread_ok": spread_ok,
            "volatility_ok": m5_regime_ok,
            "anti_chasing_ok": m5_extension <= 1.5,
            "boundary_not_broken": not_broken,
        },
        "entry_authorized": False,
        "execution_policy": "research_alert_only_no_order_authorization",
        "independent_from_primary_signal": True,
    }
