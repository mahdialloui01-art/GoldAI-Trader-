from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Optional

import httpx
import secrets
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "development"
    trading_mode: str = "paper"
    live_trading_enabled: bool = False
    max_risk_per_trade_percent: float = Field(default=0.5, gt=0, le=1.0)
    max_daily_loss_percent: float = Field(default=2.0, gt=0, le=5.0)
    gold_spot_api_url: str = "https://api.gold-api.com/price/XAU"
    market_quote_max_age_seconds: int = Field(default=300, ge=30, le=3600)
    mt5_bridge_token: str = ""
    broker_gold_symbol: str = "GOLD"


settings = Settings()
app = FastAPI(
    title="GoldAI Trader API",
    description="Demo-first XM GOLD analysis and risk-control service.",
    version="0.3.0",
)


class Direction(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    WAIT = "WAIT"


class BrokerQuote(BaseModel):
    symbol: str = "GOLD"
    bid: float = Field(gt=0)
    ask: float = Field(gt=0)
    last: float = Field(default=0, ge=0)
    timestamp: datetime
    digits: int = Field(ge=0, le=10)
    point: float = Field(gt=0)
    spread_points: float = Field(ge=0)


_latest_mt5_quote: dict | None = None


class SignalRequest(BaseModel):
    symbol: str = "GOLD"
    timeframe: str = "M15"
    current_price: float = Field(gt=0)
    ema_fast: Optional[float] = Field(default=None, gt=0)
    ema_slow: Optional[float] = Field(default=None, gt=0)
    rsi: Optional[float] = Field(default=None, ge=0, le=100)


class SignalResponse(BaseModel):
    symbol: str
    timeframe: str
    direction: Direction
    confidence: int
    reasons: list[str]
    generated_at: datetime
    disclaimer: str = "Analytical signal only; not a guarantee of profit."


class RiskCheckRequest(BaseModel):
    account_equity: float = Field(gt=0)
    stop_distance_points: float = Field(gt=0)
    point_value_per_lot: float = Field(gt=0)
    proposed_lots: float = Field(gt=0)
    daily_loss_percent: float = Field(default=0, ge=0)


class RiskCheckResponse(BaseModel):
    approved: bool
    estimated_risk_percent: float
    max_allowed_risk_percent: float
    reasons: list[str]


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "service": "GoldAI Trader",
        "mode": settings.trading_mode,
        "live_trading_enabled": settings.live_trading_enabled,
        "timestamp": datetime.now(timezone.utc),
    }


@app.get("/api/v1/config")
def get_config() -> dict:
    # Expose safe operational settings only; never expose credentials.
    return {
        "trading_mode": settings.trading_mode,
        "live_trading_enabled": settings.live_trading_enabled,
        "max_risk_per_trade_percent": settings.max_risk_per_trade_percent,
        "max_daily_loss_percent": settings.max_daily_loss_percent,
        "supported_initial_symbol": settings.broker_gold_symbol,
    }


@app.post("/api/v1/mt5/quote")
def receive_mt5_quote(quote: BrokerQuote, authorization: str | None = Header(default=None)) -> dict:
    """Receive the broker's live GOLD quote from the authenticated MT5 EA."""
    global _latest_mt5_quote
    expected = settings.mt5_bridge_token
    if not expected or not authorization or not secrets.compare_digest(
        authorization.removeprefix("Bearer ").strip(), expected
    ):
        raise HTTPException(status_code=401, detail="Invalid or unconfigured MT5 bridge token.")
    if quote.symbol.upper() != settings.broker_gold_symbol.upper():
        raise HTTPException(status_code=400, detail=f"Only {settings.broker_gold_symbol} quotes are accepted.")
    if quote.ask < quote.bid:
        raise HTTPException(status_code=422, detail="Ask must be greater than or equal to Bid.")
    timestamp = quote.timestamp
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - timestamp.astimezone(timezone.utc)).total_seconds()
    if age < -30 or age > settings.market_quote_max_age_seconds:
        raise HTTPException(status_code=422, detail="MT5 quote timestamp is outside the accepted freshness window.")
    _latest_mt5_quote = {
        "symbol": settings.broker_gold_symbol.upper(), "bid": quote.bid, "ask": quote.ask, "last": quote.last,
        "mid": round((quote.bid + quote.ask) / 2, quote.digits),
        "spread_points": quote.spread_points, "digits": quote.digits,
        "point": quote.point, "source": "MetaTrader 5 broker feed",
        "source_updated_at": timestamp, "age_seconds": max(0, int(age)),
        "quote_type": "broker_bid_ask", "tradable_quote": True,
    }
    return {"accepted": True, "symbol": settings.broker_gold_symbol.upper(), "source": "MT5"}


def _fresh_mt5_quote() -> dict:
    """Return a fresh broker quote or fail closed when MT5 is unavailable."""
    if _latest_mt5_quote is None:
        raise HTTPException(status_code=503, detail="Waiting for a fresh GOLD quote from MetaTrader 5.")
    age = max(0, int((datetime.now(timezone.utc) - _latest_mt5_quote["source_updated_at"].astimezone(timezone.utc)).total_seconds()))
    if age > settings.market_quote_max_age_seconds:
        raise HTTPException(status_code=503, detail="MT5 quote is stale; market signals must remain paused.")
    return {**_latest_mt5_quote, "age_seconds": age}


@app.get("/api/v1/market/snapshot")
def market_snapshot() -> dict:
    """Return only a fresh broker quote; never silently fall back to public spot."""
    return _fresh_mt5_quote()


@app.get("/api/v1/market/spot-reference")
async def market_spot_reference() -> dict:
    """Return a public indicative spot reference for comparison only."""
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            response = await client.get(settings.gold_spot_api_url)
            response.raise_for_status()
            payload = response.json()
        price = float(payload["price"])
        updated_at = datetime.fromisoformat(
            str(payload["updatedAt"]).replace("Z", "+00:00")
        )
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=timezone.utc)
        age_seconds = max(
            0, int((datetime.now(timezone.utc) - updated_at.astimezone(timezone.utc)).total_seconds())
        )
        if price <= 0:
            raise ValueError("Provider returned a non-positive price.")
        if age_seconds > settings.market_quote_max_age_seconds:
            raise HTTPException(
                status_code=503,
                detail="Gold spot quote is stale; no price is being presented as live.",
            )
        return {
            "symbol": settings.broker_gold_symbol.upper(),
            "reference_symbol": "XAU spot",
            "price": price,
            "currency": payload.get("currency", "USD"),
            "unit": "troy_ounce",
            "source": "gold-api.com",
            "source_updated_at": updated_at,
            "age_seconds": age_seconds,
            "quote_type": "indicative_spot",
            "tradable_quote": False,
            "warning": (
                "Indicative spot reference only. It may differ from your broker's "
                "XAUUSD bid/ask and must not be used alone to execute trades."
            ),
        }
    except HTTPException:
        raise
    except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=503,
            detail="Gold spot provider is unavailable or returned invalid data.",
        ) from exc


class Candle(BaseModel):
    time: datetime
    open: float = Field(gt=0)
    high: float = Field(gt=0)
    low: float = Field(gt=0)
    close: float = Field(gt=0)
    tick_volume: int = Field(default=0, ge=0)


class TechnicalAnalysisRequest(BaseModel):
    symbol: str = "GOLD"
    timeframe: str
    candles: list[Candle] = Field(min_length=60, max_length=500)


_TIMEFRAME_SECONDS = {
    "M1": 60, "M5": 300, "M15": 900, "M30": 1800,
    "H1": 3600, "H4": 14400, "D1": 86400,
}


def _ema_series(values: list[float], period: int) -> list[float]:
    if len(values) < period:
        raise ValueError("Not enough values for EMA.")
    seed = sum(values[:period]) / period
    result = [seed]
    alpha = 2 / (period + 1)
    for value in values[period:]:
        result.append(alpha * value + (1 - alpha) * result[-1])
    return result


def _rsi_wilder(values: list[float], period: int = 14) -> float:
    changes = [values[i] - values[i - 1] for i in range(1, len(values))]
    if len(changes) < period:
        raise ValueError("Not enough values for RSI.")
    gains = [max(change, 0.0) for change in changes]
    losses = [max(-change, 0.0) for change in changes]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for gain, loss in zip(gains[period:], losses[period:]):
        avg_gain = ((period - 1) * avg_gain + gain) / period
        avg_loss = ((period - 1) * avg_loss + loss) / period
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def _true_ranges(candles: list[Candle]) -> list[float]:
    return [
        max(
            candle.high - candle.low,
            abs(candle.high - candles[index - 1].close),
            abs(candle.low - candles[index - 1].close),
        )
        for index, candle in enumerate(candles) if index > 0
    ]


def _atr_wilder(candles: list[Candle], period: int = 14) -> float:
    ranges = _true_ranges(candles)
    if len(ranges) < period:
        raise ValueError("Not enough candles for ATR.")
    value = sum(ranges[:period]) / period
    for current in ranges[period:]:
        value = ((period - 1) * value + current) / period
    return value


def _macd(closes: list[float]) -> tuple[float, float, float]:
    fast = _ema_series(closes, 12)
    slow = _ema_series(closes, 26)
    # Align both EMA streams to the same trailing candle.
    offset = 26 - 12
    macd_line = [fast[index + offset] - slow[index] for index in range(len(slow))]
    signal_line = _ema_series(macd_line, 9)
    line = macd_line[-1]
    signal = signal_line[-1]
    return line, signal, line - signal


@app.post("/api/v1/analysis/technical")
def technical_analysis(request: TechnicalAnalysisRequest) -> dict:
    """Broker-candle analysis. It deliberately returns WAIT when inputs are unsafe."""
    timeframe = request.timeframe.upper()
    if request.symbol.upper() != settings.broker_gold_symbol.upper():
        raise HTTPException(status_code=400, detail="Unsupported broker symbol.")
    if timeframe not in _TIMEFRAME_SECONDS:
        raise HTTPException(status_code=422, detail="Unsupported timeframe. Use M1, M5, M15, M30, H1, H4 or D1.")
    quote = _fresh_mt5_quote()
    candles = request.candles

    normalized_times = []
    for candle in candles:
        if candle.high < max(candle.open, candle.close, candle.low) or candle.low > min(candle.open, candle.close, candle.high):
            raise HTTPException(status_code=422, detail="Invalid candle high/low values.")
        timestamp = candle.time
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        normalized_times.append(timestamp.astimezone(timezone.utc))
    if any(current <= previous for previous, current in zip(normalized_times, normalized_times[1:])):
        raise HTTPException(status_code=422, detail="Candle timestamps must be strictly increasing and unique.")

    now = datetime.now(timezone.utc)
    latest_age = (now - normalized_times[-1]).total_seconds()
    freshness_limit = _TIMEFRAME_SECONDS[timeframe] * 2 + 60
    if latest_age < -30 or latest_age > freshness_limit:
        raise HTTPException(status_code=422, detail="Latest candle is stale or from the future; refresh MT5 candle data.")

    closes = [item.close for item in candles]
    ema20 = _ema_series(closes, 20)[-1]
    ema50 = _ema_series(closes, 50)[-1]
    rsi14 = _rsi_wilder(closes)
    atr14 = _atr_wilder(candles)
    macd_line, macd_signal, macd_histogram = _macd(closes)
    bb_window = closes[-20:]
    bb_mid = sum(bb_window) / 20
    bb_std = (sum((value - bb_mid) ** 2 for value in bb_window) / 20) ** 0.5
    bb_upper, bb_lower = bb_mid + 2 * bb_std, bb_mid - 2 * bb_std
    last = candles[-1]

    score = 0
    reasons: list[str] = []
    if ema20 > ema50 and last.close > ema20:
        score += 2
        reasons.append("الاتجاه العام صاعد: EMA20 فوق EMA50 والسعر فوق EMA20.")
    elif ema20 < ema50 and last.close < ema20:
        score -= 2
        reasons.append("الاتجاه العام هابط: EMA20 تحت EMA50 والسعر تحت EMA20.")
    else:
        reasons.append("الاتجاه غير مؤكد؛ المتوسطات لا تعطي توافقاً واضحاً.")

    if 52 <= rsi14 <= 68:
        score += 1
        reasons.append("RSI يدعم الزخم الشرائي دون تشبع.")
    elif 32 <= rsi14 <= 48:
        score -= 1
        reasons.append("RSI يدعم الزخم البيعي دون تشبع.")
    elif rsi14 >= 70 or rsi14 <= 30:
        reasons.append("RSI في منطقة تشبع؛ لا تطارد الحركة.")

    if macd_histogram > 0:
        score += 1
        reasons.append("MACD histogram موجب ويدعم الزخم الصاعد.")
    elif macd_histogram < 0:
        score -= 1
        reasons.append("MACD histogram سالب ويدعم الزخم الهابط.")

    candle_range = max(last.high - last.low, 1e-12)
    body_ratio = abs(last.close - last.open) / candle_range
    if last.close > last.open and body_ratio >= 0.6:
        score += 1
        reasons.append("آخر شمعة صاعدة بجسم قوي نسبياً.")
    elif last.close < last.open and body_ratio >= 0.6:
        score -= 1
        reasons.append("آخر شمعة هابطة بجسم قوي نسبياً.")

    spread_price = quote["spread_points"] * quote["point"]
    spread_atr_ratio = spread_price / atr14 if atr14 > 0 else float("inf")
    if spread_atr_ratio > 0.15:
        reasons.append("السبريد مرتفع مقارنة بالتذبذب؛ تم منع إصدار اتجاه تداول.")
        direction = Direction.WAIT
    else:
        direction = Direction.BUY if score >= 4 else Direction.SELL if score <= -4 else Direction.WAIT
    if direction == Direction.WAIT:
        reasons.append("لا توجد موافقات كافية؛ القرار الآمن هو الانتظار.")

    confidence_score = min(85, 50 + abs(score) * 6) if direction != Direction.WAIT else 0
    return {
        "symbol": settings.broker_gold_symbol.upper(),
        "timeframe": timeframe,
        "direction": direction,
        "confidence_score": confidence_score,
        "confidence_note": "مؤشر توافق داخلي غير معاير، وليس احتمال نجاح الصفقة.",
        "indicators": {
            "ema20": round(ema20, 5), "ema50": round(ema50, 5),
            "rsi14_wilder": round(rsi14, 2), "atr14_wilder": round(atr14, 5),
            "macd": round(macd_line, 5), "macd_signal": round(macd_signal, 5),
            "macd_histogram": round(macd_histogram, 5),
            "bollinger_upper": round(bb_upper, 5), "bollinger_middle": round(bb_mid, 5),
            "bollinger_lower": round(bb_lower, 5),
        },
        "broker_quote": {"bid": quote["bid"], "ask": quote["ask"], "spread_points": quote["spread_points"]},
        "spread_atr_ratio": round(spread_atr_ratio, 4),
        "candle_count": len(candles),
        "reasons": reasons,
        "risk_note": "ATR مقياس للتذبذب وليس وقف خسارة مضموناً. يلزم توافق الأطر الأعلى واختبار المخاطر والأخبار قبل أي صفقة.",
        "generated_at": now,
        "execution_enabled": False,
    }


@app.post("/api/v1/signals/evaluate", response_model=SignalResponse)
def evaluate_signal(request: SignalRequest) -> SignalResponse:
    if request.symbol.upper() != settings.broker_gold_symbol.upper():
        raise HTTPException(status_code=400, detail=f"The initial release supports {settings.broker_gold_symbol} only.")
    # Never evaluate a trade setup without a fresh quote from the configured broker.
    _fresh_mt5_quote()

    reasons: list[str] = []
    score = 0
    direction = Direction.WAIT

    if request.ema_fast is not None and request.ema_slow is not None:
        if request.ema_fast > request.ema_slow:
            score += 1
            reasons.append("Fast EMA is above slow EMA (bullish trend filter).")
        elif request.ema_fast < request.ema_slow:
            score -= 1
            reasons.append("Fast EMA is below slow EMA (bearish trend filter).")

    if request.rsi is not None:
        if 50 < request.rsi < 70:
            score += 1
            reasons.append("RSI supports bullish momentum without being overbought.")
        elif 30 < request.rsi < 50:
            score -= 1
            reasons.append("RSI supports bearish momentum without being oversold.")
        elif request.rsi >= 70:
            reasons.append("RSI is overbought; avoid chasing a BUY.")
        elif request.rsi <= 30:
            reasons.append("RSI is oversold; avoid chasing a SELL.")

    if score >= 2:
        direction = Direction.BUY
    elif score <= -2:
        direction = Direction.SELL
    else:
        reasons.append("Filters are not aligned; wait for confirmation.")

    confidence = min(90, 50 + abs(score) * 15) if direction != Direction.WAIT else 0
    return SignalResponse(
        symbol=settings.broker_gold_symbol.upper(),
        timeframe=request.timeframe,
        direction=direction,
        confidence=confidence,
        reasons=reasons,
        generated_at=datetime.now(timezone.utc),
    )


@app.post("/api/v1/risk/check", response_model=RiskCheckResponse)
def check_risk(request: RiskCheckRequest) -> RiskCheckResponse:
    estimated_loss = (
        request.stop_distance_points
        * request.point_value_per_lot
        * request.proposed_lots
    )
    risk_percent = estimated_loss / request.account_equity * 100
    reasons: list[str] = []

    if risk_percent > settings.max_risk_per_trade_percent:
        reasons.append("Proposed position exceeds the configured per-trade risk limit.")
    if request.daily_loss_percent >= settings.max_daily_loss_percent:
        reasons.append("Daily loss limit reached; trading must remain locked.")
    if not settings.live_trading_enabled:
        reasons.append("Execution is disabled; this service is in analysis/paper mode.")

    approved = (
        risk_percent <= settings.max_risk_per_trade_percent
        and request.daily_loss_percent < settings.max_daily_loss_percent
    )
    return RiskCheckResponse(
        approved=approved,
        estimated_risk_percent=round(risk_percent, 4),
        max_allowed_risk_percent=settings.max_risk_per_trade_percent,
        reasons=reasons,
    )
