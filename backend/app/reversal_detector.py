"""Independent, non-executable early reversal diagnostics for GOLD.

Scores are heuristic evidence points, never probabilities or trade authorization.
"""
from __future__ import annotations

from typing import Any


_WEIGHTS = {"M5": 1.0, "M15": 1.5, "H1": 2.0, "H4": 2.5}


def _value(candle: Any, name: str) -> float:
    raw = candle.get(name) if isinstance(candle, dict) else getattr(candle, name)
    return float(raw)


def _ema(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    alpha = 2.0 / (period + 1.0)
    result = [values[0]]
    for value in values[1:]:
        result.append(alpha * value + (1.0 - alpha) * result[-1])
    return result


def _rsi(values: list[float], period: int = 14) -> list[float]:
    if len(values) < 2:
        return [50.0] * len(values)
    gains = [0.0]
    losses = [0.0]
    for previous, current in zip(values, values[1:]):
        change = current - previous
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))
    avg_gain = sum(gains[1:period + 1]) / period if len(values) > period else sum(gains[1:]) / max(1, len(gains) - 1)
    avg_loss = sum(losses[1:period + 1]) / period if len(values) > period else sum(losses[1:]) / max(1, len(losses) - 1)
    output = [50.0] * len(values)
    start = min(period, len(values) - 1)
    for index in range(start, len(values)):
        if index > start:
            avg_gain = ((period - 1) * avg_gain + gains[index]) / period
            avg_loss = ((period - 1) * avg_loss + losses[index]) / period
        output[index] = 100.0 if avg_loss == 0 else 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
    return output


def _confirmed_pivots(values: list[float], left: int = 3, right: int = 3, high: bool = True) -> list[int]:
    pivots: list[int] = []
    for index in range(left, len(values) - right):
        window = values[index - left:index + right + 1]
        pivot = values[index]
        if pivot == (max(window) if high else min(window)):
            # Resolve equal extrema deterministically to the first occurrence.
            if (window.index(pivot) == left):
                pivots.append(index)
    return pivots


def _frame_evidence(candles: list[Any]) -> dict[str, Any]:
    if len(candles) < 40:
        return {"available": False, "score": 0, "signals": ["تاريخ غير كافٍ (يلزم 40 شمعة على الأقل)"]}
    opens = [_value(c, "open") for c in candles]
    highs = [_value(c, "high") for c in candles]
    lows = [_value(c, "low") for c in candles]
    closes = [_value(c, "close") for c in candles]
    rsi = _rsi(closes)
    ema20 = _ema(closes, 20)
    ema50 = _ema(closes, 50)
    fast = _ema(closes, 12)
    slow = _ema(closes, 26)
    macd = [a - b for a, b in zip(fast, slow)]
    signal = _ema(macd, 9)
    hist = [a - b for a, b in zip(macd, signal)]
    high_pivots = _confirmed_pivots(highs, high=True)
    low_pivots = _confirmed_pivots(lows, high=False)
    signals: list[str] = []
    bullish = 0
    bearish = 0
    last = len(candles) - 1
    price = closes[last]
    recent_highs = high_pivots[-3:]
    recent_lows = low_pivots[-3:]
    bullish_sweep = bool(recent_lows and lows[last] < lows[recent_lows[-1]] and price > lows[recent_lows[-1]])
    bearish_sweep = bool(recent_highs and highs[last] > highs[recent_highs[-1]] and price < highs[recent_highs[-1]])
    bullish_choch = bool(recent_highs and price > highs[recent_highs[-1]] and closes[last - 1] <= highs[recent_highs[-1]])
    bearish_choch = bool(recent_lows and price < lows[recent_lows[-1]] and closes[last - 1] >= lows[recent_lows[-1]])
    bullish_divergence = False
    bearish_divergence = False
    if len(recent_lows) >= 2:
        a, b = recent_lows[-2], recent_lows[-1]
        bullish_divergence = lows[b] < lows[a] and rsi[b] > rsi[a] + 2.0
    if len(recent_highs) >= 2:
        a, b = recent_highs[-2], recent_highs[-1]
        bearish_divergence = highs[b] > highs[a] and rsi[b] < rsi[a] - 2.0
    bullish_momentum = rsi[last] > rsi[last - 1] > rsi[last - 2] and hist[last] > hist[last - 1] > hist[last - 2]
    bearish_momentum = rsi[last] < rsi[last - 1] < rsi[last - 2] and hist[last] < hist[last - 1] < hist[last - 2]
    bullish_reclaim = closes[last] > ema20[last] and closes[last - 1] <= ema20[last - 1]
    bearish_reclaim = closes[last] < ema20[last] and closes[last - 1] >= ema20[last - 1]
    bullish_candle = closes[last] > opens[last] and (closes[last] - lows[last]) / max(highs[last] - lows[last], 1e-12) >= 0.65
    bearish_candle = closes[last] < opens[last] and (closes[last] - lows[last]) / max(highs[last] - lows[last], 1e-12) <= 0.35

    for condition, points, message in (
        (bullish_divergence, 20, "دايفرجنس إيجابي: السعر صنع قاعاً أدنى وRSI قاعاً أعلى"),
        (bullish_sweep, 20, "سحب سيولة أسفل قاع مؤكد مع إغلاق فوقه"),
        (bullish_choch, 25, "CHoCH/BOS صاعد بإغلاق فوق آخر قمة مؤكدة"),
        (bullish_momentum, 15, "تحسن متتابع في RSI وMACD histogram"),
        (bullish_reclaim, 10, "استعادة EMA20 بإغلاق شمعة"),
        (bullish_candle, 5, "شمعة صاعدة أغلقت قرب قمتها"),
    ):
        if condition:
            bullish += points
            signals.append(message)
    for condition, points, message in (
        (bearish_divergence, 20, "دايفرجنس سلبي: السعر صنع قمة أعلى وRSI قمة أدنى"),
        (bearish_sweep, 20, "سحب سيولة أعلى قمة مؤكدة مع إغلاق تحتها"),
        (bearish_choch, 25, "CHoCH/BOS هابط بإغلاق تحت آخر قاع مؤكد"),
        (bearish_momentum, 15, "تراجع متتابع في RSI وMACD histogram"),
        (bearish_reclaim, 10, "فقدان EMA20 بإغلاق شمعة"),
        (bearish_candle, 5, "شمعة هابطة أغلقت قرب قاعها"),
    ):
        if condition:
            bearish += points
            signals.append(message)
    structure = "UPTREND" if len(recent_highs) >= 2 and len(recent_lows) >= 2 and highs[recent_highs[-1]] > highs[recent_highs[-2]] and lows[recent_lows[-1]] > lows[recent_lows[-2]] else "DOWNTREND" if len(recent_highs) >= 2 and len(recent_lows) >= 2 and highs[recent_highs[-1]] < highs[recent_highs[-2]] and lows[recent_lows[-1]] < lows[recent_lows[-2]] else "RANGE_OR_TRANSITION"
    return {
        "available": True, "bullish_score": min(100, bullish), "bearish_score": min(100, bearish),
        "rsi14": round(rsi[last], 2), "macd_histogram": round(hist[last], 6),
        "ema20": round(ema20[last], 5), "ema50": round(ema50[last], 5),
        "market_structure": structure, "bullish_choch": bullish_choch, "bearish_choch": bearish_choch,
        "bullish_sweep": bullish_sweep, "bearish_sweep": bearish_sweep,
        "last_confirmed_swing_high": round(highs[recent_highs[-1]], 5) if recent_highs else None,
        "last_confirmed_swing_low": round(lows[recent_lows[-1]], 5) if recent_lows else None,
        "last_close": round(price, 5), "signals": signals,
    }


def detect_early_reversal(frames: dict[str, list[Any]], live_h4: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return an independent reversal watch; never changes direction or entry gates."""
    details = {name: _frame_evidence(frames.get(name, [])) for name in _WEIGHTS}
    aggregate = {"BUY": 0.0, "SELL": 0.0}
    total_weight = 0.0
    for name, weight in _WEIGHTS.items():
        item = details[name]
        if not item.get("available"):
            continue
        aggregate["BUY"] += float(item["bullish_score"]) * weight
        aggregate["SELL"] += float(item["bearish_score"]) * weight
        total_weight += weight
    if total_weight == 0:
        return {
            "status": "UNAVAILABLE", "stage": "UNAVAILABLE", "direction": "WAIT",
            "score_0_to_100": 0, "score_type": "HEURISTIC_EVIDENCE_NOT_PROBABILITY",
            "summary": "لا توجد شموع كافية لتحليل الانعكاس.", "timeframes": details,
            "execution_enabled": False,
        }
    scores = {side: round(value / total_weight, 1) for side, value in aggregate.items()}
    direction = "BUY" if scores["BUY"] > scores["SELL"] else "SELL" if scores["SELL"] > scores["BUY"] else "WAIT"
    score = max(scores.values()) if direction != "WAIT" else 0.0
    supporting = [name for name, item in details.items() if item.get("available") and item.get("bullish_choch" if direction == "BUY" else "bearish_choch")]
    supporting_m15_h1 = all(details[name].get("available") and details[name].get("bullish_score" if direction == "BUY" else "bearish_score", 0) >= 25 for name in ("M15", "H1")) if direction != "WAIT" else False
    h4_live_note = "غير متاح؛ لم يضف أي نقاط"
    if live_h4 and live_h4.get("status") == "AVAILABLE" and direction in ("BUY", "SELL"):
        live_direction = str(live_h4.get("direction", "WAIT")).upper()
        if live_direction == direction:
            score = min(100.0, score + 3.0)
            h4_live_note = "زخم H4 الحي يؤيد بوادر الانعكاس (+3 نقاط استشارية)"
        elif live_direction in ("BUY", "SELL"):
            h4_live_note = "زخم H4 الحي يعاكس بوادر الانعكاس؛ يلزم الحذر"
    has_structure_break = any(bool(details[name].get("bullish_choch" if direction == "BUY" else "bearish_choch")) for name in ("M15", "H1", "H4")) if direction != "WAIT" else False
    if direction == "WAIT" or score < 25:
        stage = "NO_EARLY_SIGNAL"
        status = "WATCHING"
    elif score < 45:
        stage = "EARLY_WARNING"
        status = "WATCHING"
    elif score < 70 or not has_structure_break or not supporting_m15_h1:
        stage = "DEVELOPING"
        status = "DEVELOPING"
    else:
        stage = "CONFIRMED"
        status = "CONFIRMED"
    anchor = details["H4"] if details["H4"].get("available") else details["H1"]
    target = anchor.get("last_confirmed_swing_high" if direction == "BUY" else "last_confirmed_swing_low") if direction in ("BUY", "SELL") else None
    invalidation = anchor.get("last_confirmed_swing_low" if direction == "BUY" else "last_confirmed_swing_high") if direction in ("BUY", "SELL") else None
    signals = []
    for name in ("M5", "M15", "H1", "H4"):
        item = details[name]
        if item.get("available") and item.get("signals"):
            signals.extend(f"{name}: {message}" for message in item["signals"][:3])
    if direction == "WAIT":
        summary = "لا توجد أفضلية انعكاس واضحة حالياً."
    elif stage == "EARLY_WARNING":
        summary = "إنذار مبكر فقط؛ لم يكتمل تأكيد الهيكل، ولا يُعد إشارة دخول."
    elif stage == "DEVELOPING":
        summary = "الانعكاس يتطور؛ انتظر كسر الهيكل وتوافق الأطر قبل اعتباره مؤكداً."
    else:
        summary = "شروط الانعكاس الفني تحققت وفق القواعد الحالية؛ لا يعني ذلك ضمان استمرار الحركة."
    return {
        "status": status, "stage": stage, "direction": direction,
        "score_0_to_100": round(score, 1), "scores_by_direction": scores,
        "score_type": "HEURISTIC_EVIDENCE_NOT_PROBABILITY",
        "summary": summary, "supporting_structure_break_timeframes": supporting,
        "m15_h1_support": supporting_m15_h1, "live_h4_note": h4_live_note,
        "reference_target_level": target, "invalidation_level": invalidation,
        "signals": signals[:12], "timeframes": details,
        "policy": {"informational_only": True, "can_override_primary_trend": False, "can_authorize_entry": False},
        "execution_enabled": False,
    }
