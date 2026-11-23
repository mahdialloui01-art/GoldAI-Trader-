"""Sideways/range regime detector and edge-entry planner for GOLD.

Closed-candle diagnostics only. It never authorizes or places orders.
"""
from __future__ import annotations

from typing import Any


def _v(c: Any, key: str) -> float:
    raw = c.get(key) if isinstance(c, dict) else getattr(c, key)
    return float(raw)


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _atr(highs: list[float], lows: list[float], closes: list[float], period: int = 14) -> float:
    trs = []
    for i in range(1, len(closes)):
        trs.append(max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1])))
    return _mean(trs[-period:])


def _adx(highs: list[float], lows: list[float], closes: list[float], period: int = 14) -> float:
    plus_dm, minus_dm, tr = [], [], []
    for i in range(1, len(closes)):
        up, down = highs[i] - highs[i-1], lows[i-1] - lows[i]
        plus_dm.append(up if up > down and up > 0 else 0.0)
        minus_dm.append(down if down > up and down > 0 else 0.0)
        tr.append(max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1])))
    if len(tr) < period:
        return 50.0
    smooth_tr = sum(tr[:period])
    smooth_plus = sum(plus_dm[:period])
    smooth_minus = sum(minus_dm[:period])
    dx = []
    for i in range(period, len(tr) + 1):
        if i > period:
            smooth_tr = smooth_tr - smooth_tr / period + tr[i-1]
            smooth_plus = smooth_plus - smooth_plus / period + plus_dm[i-1]
            smooth_minus = smooth_minus - smooth_minus / period + minus_dm[i-1]
        pdi = 100.0 * smooth_plus / smooth_tr if smooth_tr else 0.0
        mdi = 100.0 * smooth_minus / smooth_tr if smooth_tr else 0.0
        dx.append(100.0 * abs(pdi - mdi) / (pdi + mdi) if pdi + mdi else 0.0)
    return _mean(dx[-period:])


def _ema(values: list[float], period: int) -> float:
    if not values:
        return 0.0
    alpha = 2.0 / (period + 1.0)
    value = values[0]
    for item in values[1:]:
        value = alpha * item + (1.0 - alpha) * value
    return value


def _empty(reason: str) -> dict:
    return {
        "status": "UNAVAILABLE", "regime": "UNKNOWN", "direction": "WAIT",
        "reason": reason, "execution_enabled": False, "informational_only": True,
        "score_type": "HEURISTIC_EVIDENCE_NOT_PROBABILITY", "setup": None,
    }


def detect_range_market(candles: list[Any], digits: int = 2) -> dict:
    """Detect a balanced range and only propose edge setups after candle rejection."""
    if len(candles) < 60:
        return _empty("يلزم 60 شمعة مغلقة على الأقل لتقييم النطاق بثبات.")
    highs = [_v(c, "high") for c in candles]
    lows = [_v(c, "low") for c in candles]
    opens = [_v(c, "open") for c in candles]
    closes = [_v(c, "close") for c in candles]
    atr = _atr(highs, lows, closes)
    if atr <= 0:
        return _empty("ATR غير صالح.")
    window = 48
    hh, ll = max(highs[-window:]), min(lows[-window:])
    width = hh - ll
    width_atr = width / atr
    adx = _adx(highs, lows, closes)
    ema_now = _ema(closes[-30:], 20)
    ema_then = _ema(closes[-50:-20], 20)
    slope_atr = abs(ema_now - ema_then) / (atr * 30.0)
    net_move = abs(closes[-1] - closes[-21]) if len(closes) >= 21 else width
    path = sum(abs(closes[i] - closes[i-1]) for i in range(len(closes)-20, len(closes)))
    efficiency = net_move / path if path > 0 else 0.0
    # A range must show weak directional strength, flat EMA drift, low path efficiency,
    # and a plausible width. No single indicator can declare a range by itself.
    range_conditions = {
        "adx_below_22": adx < 22.0,
        "ema_slope_flat": slope_atr < 0.035,
        "low_directional_efficiency": efficiency < 0.34,
        "range_width_plausible": 2.0 <= width_atr <= 9.0,
    }
    range_score = round(100.0 * sum(range_conditions.values()) / len(range_conditions), 1)
    is_range = sum(range_conditions.values()) >= 3 and range_conditions["range_width_plausible"]
    last_o, last_h, last_l, last_c = opens[-1], highs[-1], lows[-1], closes[-1]
    edge_band = max(0.18 * atr, 0.08 * width)
    lower_zone = (ll, ll + edge_band)
    upper_zone = (hh - edge_band, hh)
    candle_span = max(last_h - last_l, 1e-12)
    close_position = (last_c - last_l) / candle_span
    bullish_rejection = last_l <= ll + edge_band and last_c > last_o and close_position >= 0.68
    bearish_rejection = last_h >= hh - edge_band and last_c < last_o and close_position <= 0.32
    swept_low_reclaimed = last_l < ll and last_c > ll
    swept_high_rejected = last_h > hh and last_c < hh
    # A close beyond the boundary is a breakout warning, not a range fade.
    breakout_up = last_c > hh + 0.10 * atr
    breakout_down = last_c < ll - 0.10 * atr
    direction = "WAIT"
    setup = None
    rationale = "انتظر الحافة؛ لا توجد شمعة رفض مؤكدة."
    if not is_range:
        regime = "TRANSITION" if range_score >= 50 else "TREND_OR_VOLATILE"
        rationale = "شروط السوق العرضي غير مكتملة؛ لا نستخدم استراتيجية ارتداد النطاق."
    elif breakout_up or breakout_down:
        regime = "RANGE_BREAKOUT_WATCH"
        rationale = "إغلاق خارج النطاق؛ ننتظر إعادة اختبار ناجحة قبل اعتماد الاختراق."
    else:
        regime = "SIDEWAYS"
        candidate = "BUY" if bullish_rejection or swept_low_reclaimed else "SELL" if bearish_rejection or swept_high_rejected else "WAIT"
        if candidate != "WAIT":
            entry_low, entry_high = lower_zone if candidate == "BUY" else upper_zone
            entry = last_c
            stop = ll - 0.22 * atr if candidate == "BUY" else hh + 0.22 * atr
            mid = (hh + ll) / 2.0
            target2 = hh - 0.12 * atr if candidate == "BUY" else ll + 0.12 * atr
            risk = entry - stop if candidate == "BUY" else stop - entry
            reward = mid - entry if candidate == "BUY" else entry - mid
            rr = reward / risk if risk > 0 else 0.0
            # Require the close to remain near the correct edge and a minimum RR.
            in_edge = lower_zone[0] - 0.10 * atr <= entry <= lower_zone[1] + 0.10 * atr if candidate == "BUY" else upper_zone[0] - 0.10 * atr <= entry <= upper_zone[1] + 0.10 * atr
            if in_edge and rr >= 1.25:
                direction = candidate
                rationale = "رفض سعري عند حافة النطاق مع إغلاق مؤكد؛ الصفقة المقترحة لا تزال بحاجة لإعادة التحقق من السعر والسبريد."
                setup = {
                    "strategy": "RANGE_EDGE_REJECTION",
                    "entry_zone": [round(entry_low, digits), round(entry_high, digits)],
                    "entry_reference_close": round(entry, digits),
                    "stop_loss": round(stop, digits),
                    "take_profit": {
                        "TP1_MID_RANGE": round(mid, digits),
                        "TP2_OPPOSITE_EDGE": round(target2, digits),
                    },
                    "reward_risk_to_mid": round(rr, 2),
                    "trigger": "Closed-candle rejection or liquidity sweep and reclaim at range edge",
                    "expiry": "Re-evaluate after the next closed M5 candle; cancel if price leaves the zone or range structure breaks",
                }
            else:
                rationale = "ظهرت بوادر رفض لكن العائد/المخاطرة أو موقع الإغلاق لا يبرر الدخول؛ انتظر."
    return {
        "status": "AVAILABLE", "regime": regime, "direction": direction,
        "range_score_0_to_100": range_score,
        "score_type": "HEURISTIC_EVIDENCE_NOT_PROBABILITY",
        "metrics": {
            "adx14": round(adx, 2), "ema_slope_atr_per_bar": round(slope_atr, 5),
            "directional_efficiency_20": round(efficiency, 3),
            "range_width_atr": round(width_atr, 2), "atr14": round(atr, digits),
            "conditions": range_conditions,
        },
        "levels": {
            "support": round(ll, digits), "resistance": round(hh, digits),
            "midpoint": round((hh + ll) / 2.0, digits),
            "buy_zone": [round(x, digits) for x in lower_zone],
            "sell_zone": [round(x, digits) for x in upper_zone],
        },
        "evidence": {
            "bullish_rejection": bullish_rejection, "bearish_rejection": bearish_rejection,
            "swept_low_reclaimed": swept_low_reclaimed, "swept_high_rejected": swept_high_rejected,
            "breakout_up": breakout_up, "breakout_down": breakout_down,
        },
        "reason": rationale, "setup": setup,
        "policy": {
            "informational_only": True, "requires_live_quote_revalidation": True,
            "does_not_override_trend_or_risk_gates": True,
            "no_entry_in_range_midpoint": True,
        },
        "execution_enabled": False,
    }
