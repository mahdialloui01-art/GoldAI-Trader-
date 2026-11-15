from __future__ import annotations

from datetime import date, datetime, timezone
from enum import Enum
from decimal import Decimal, ROUND_DOWN
import math
import statistics
from typing import Optional
from urllib.parse import quote

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
    economic_calendar_api_key: str = ""
    fred_api_key: str = ""
    economic_calendar_base_url: str = "https://api.tradingeconomics.com"
    news_blackout_before_minutes: int = Field(default=30, ge=0, le=240)
    news_blackout_after_minutes: int = Field(default=30, ge=0, le=240)


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
    tick_size: float | None = Field(default=None, gt=0)
    tick_value_loss: float | None = Field(default=None, gt=0)
    tick_value_profit: float | None = Field(default=None, ge=0)
    volume_min: float | None = Field(default=None, gt=0)
    volume_max: float | None = Field(default=None, gt=0)
    volume_step: float | None = Field(default=None, gt=0)
    contract_size: float | None = Field(default=None, gt=0)
    account_equity: float | None = Field(default=None, gt=0)
    account_currency: str | None = Field(default=None, min_length=3, max_length=8)


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



class PositionSizeRequest(BaseModel):
    stop_distance_points: float = Field(gt=0, le=10_000_000)
    risk_percent: float = Field(default=0.5, gt=0, le=1.0)
    account_equity: float | None = Field(default=None, gt=0)
    daily_loss_percent: float = Field(default=0, ge=0, le=100)


class RiskCheckResponse(BaseModel):
    approved: bool
    estimated_risk_percent: float
    max_allowed_risk_percent: float
    reasons: list[str]



@app.post("/api/v1/risk/position-size")
def calculate_position_size(request: PositionSizeRequest) -> dict:
    """Calculate a conservative GOLD lot size from live MT5 tick-value and volume specs."""
    quote = _fresh_mt5_quote()
    required = (
        "tick_size", "tick_value_loss", "volume_min", "volume_max",
        "volume_step", "contract_size", "account_equity", "account_currency",
    )
    if any(quote.get(field) is None for field in required):
        raise HTTPException(
            status_code=503,
            detail="Waiting for complete MT5 GOLD contract and account metadata; lot sizing is unavailable.",
        )
    if request.risk_percent > settings.max_risk_per_trade_percent:
        raise HTTPException(
            status_code=422,
            detail=f"Requested risk exceeds configured maximum of {settings.max_risk_per_trade_percent}%.",
        )
    if request.daily_loss_percent >= settings.max_daily_loss_percent:
        return {
            "approved": False, "recommended_lots": 0.0, "risk_amount": 0.0,
            "estimated_risk_percent": 0.0, "account_currency": quote["account_currency"],
            "reason": "Daily loss limit reached; new positions are locked.",
            "execution_enabled": False,
        }

    equity = request.account_equity or quote["account_equity"]
    price_distance = request.stop_distance_points * quote["point"]
    ticks_to_stop = price_distance / quote["tick_size"]
    loss_per_lot = ticks_to_stop * quote["tick_value_loss"]
    if not math.isfinite(loss_per_lot) or loss_per_lot <= 0:
        raise HTTPException(status_code=503, detail="MT5 tick-value metadata is invalid; lot sizing is paused.")

    risk_amount = equity * request.risk_percent / 100
    raw_lots = risk_amount / loss_per_lot
    step = Decimal(str(quote["volume_step"]))
    raw_steps = (Decimal(str(raw_lots)) / step).to_integral_value(rounding=ROUND_DOWN)
    lots = float(raw_steps * step)
    max_steps = (Decimal(str(quote["volume_max"])) / step).to_integral_value(rounding=ROUND_DOWN)
    max_lots = float(max_steps * step)
    lots = min(lots, max_lots)
    if lots < quote["volume_min"]:
        return {
            "approved": False, "recommended_lots": 0.0,
            "raw_lots_before_broker_limits": round(raw_lots, 8),
            "risk_amount": round(risk_amount, 2),
            "estimated_risk_percent": 0.0,
            "account_currency": quote["account_currency"],
            "reason": "The smallest broker-allowed lot would exceed the requested risk; no position is recommended.",
            "execution_enabled": False,
        }

    estimated_loss = lots * loss_per_lot
    estimated_percent = estimated_loss / equity * 100
    return {
        "approved": estimated_percent <= request.risk_percent + 1e-9,
        "recommended_lots": lots,
        "raw_lots_before_broker_limits": round(raw_lots, 8),
        "risk_amount": round(risk_amount, 2),
        "estimated_stop_loss_amount": round(estimated_loss, 2),
        "estimated_risk_percent": round(estimated_percent, 4),
        "requested_risk_percent": request.risk_percent,
        "stop_distance_points": request.stop_distance_points,
        "price_distance": round(price_distance, quote["digits"]),
        "tick_size": quote["tick_size"],
        "tick_value_loss_per_lot": quote["tick_value_loss"],
        "volume_min": quote["volume_min"],
        "volume_max": quote["volume_max"],
        "volume_step": quote["volume_step"],
        "contract_size": quote["contract_size"],
        "account_currency": quote["account_currency"],
        "symbol": quote["symbol"],
        "reason": "Volume rounded down to the broker step; no minimum-lot uplift was applied.",
        "execution_enabled": False,
    }


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
    spec_fields = (
        quote.tick_size, quote.tick_value_loss, quote.volume_min, quote.volume_max,
        quote.volume_step, quote.contract_size, quote.account_equity, quote.account_currency,
    )
    if any(value is not None for value in spec_fields) and not all(value is not None for value in spec_fields):
        raise HTTPException(status_code=422, detail="MT5 symbol and account metadata must be supplied as a complete set.")
    if quote.volume_min is not None and quote.volume_max < quote.volume_min:
        raise HTTPException(status_code=422, detail="MT5 maximum volume must be greater than or equal to minimum volume.")
    _latest_mt5_quote = {
        "symbol": settings.broker_gold_symbol.upper(), "bid": quote.bid, "ask": quote.ask, "last": quote.last,
        "mid": round((quote.bid + quote.ask) / 2, quote.digits),
        "spread_points": quote.spread_points, "digits": quote.digits,
        "point": quote.point, "source": "MetaTrader 5 broker feed",
        "source_updated_at": timestamp, "age_seconds": max(0, int(age)),
        "quote_type": "broker_bid_ask", "tradable_quote": True,
        "tick_size": quote.tick_size, "tick_value_loss": quote.tick_value_loss,
        "tick_value_profit": quote.tick_value_profit, "volume_min": quote.volume_min,
        "volume_max": quote.volume_max, "volume_step": quote.volume_step,
        "contract_size": quote.contract_size, "account_equity": quote.account_equity,
        "account_currency": quote.account_currency,
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




_FRED_GOLD_MACRO_SERIES = {
    "nominal_10y_yield": "DGS10",
    "real_10y_yield": "DFII10",
    "broad_us_dollar_index": "DTWEXBGS",
}


async def _fred_latest_observations(client: httpx.AsyncClient, series_id: str) -> list[dict]:
    api_key = settings.fred_api_key.strip()
    if not api_key:
        raise HTTPException(status_code=503, detail="FRED is not configured; macro analysis is unavailable.")
    start = (datetime.now(timezone.utc).date()).fromordinal(
        datetime.now(timezone.utc).date().toordinal() - 14
    ).isoformat()
    response = await client.get(
        "https://api.stlouisfed.org/fred/series/observations",
        params={
            "series_id": series_id,
            "api_key": api_key,
            "file_type": "json",
            "observation_start": start,
            "sort_order": "desc",
            "limit": 10,
        },
    )
    response.raise_for_status()
    payload = response.json()
    observations = payload.get("observations") if isinstance(payload, dict) else None
    if not isinstance(observations, list):
        raise ValueError(f"FRED returned an invalid observations payload for {series_id}.")
    valid = []
    for item in observations:
        raw_value = item.get("value")
        if raw_value in (None, "", "."):
            continue
        valid.append({"date": item["date"], "value": float(raw_value)})
        if len(valid) == 2:
            break
    if len(valid) < 2:
        raise ValueError(f"FRED has fewer than two valid observations for {series_id}.")
    return valid


@app.get("/api/v1/analysis/gold-macro")
async def gold_macro_analysis() -> dict:
    """Summarize USD and Treasury yield pressure using daily FRED observations."""
    if not settings.fred_api_key.strip():
        raise HTTPException(status_code=503, detail="FRED is not configured; macro analysis is unavailable.")
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            observations = {
                name: await _fred_latest_observations(client, series_id)
                for name, series_id in _FRED_GOLD_MACRO_SERIES.items()
            }
        now = datetime.now(timezone.utc)
        for name, rows in observations.items():
            latest_date = datetime.fromisoformat(rows[0]["date"]).date()
            age_days = (now.date() - latest_date).days
            if age_days < 0 or age_days > 7:
                raise ValueError(f"FRED series {name} is stale.")
            rows[0]["age_days"] = age_days

        nominal = observations["nominal_10y_yield"]
        real = observations["real_10y_yield"]
        dollar = observations["broad_us_dollar_index"]
        nominal_change_bp = (nominal[0]["value"] - nominal[1]["value"]) * 100
        real_change_bp = (real[0]["value"] - real[1]["value"]) * 100
        dollar_change_pct = (
            (dollar[0]["value"] - dollar[1]["value"]) / dollar[1]["value"] * 100
            if dollar[1]["value"] else 0.0
        )
        real_up = real_change_bp >= 1.0
        real_down = real_change_bp <= -1.0
        dollar_up = dollar_change_pct >= 0.05
        dollar_down = dollar_change_pct <= -0.05
        if real_up and dollar_up:
            bias = "GOLD_HEADWIND"
            explanation = "ارتفاع العائد الحقيقي والدولار يشكل ضغطاً تاريخياً محتملاً على الذهب."
        elif real_down and dollar_down:
            bias = "GOLD_SUPPORTIVE"
            explanation = "انخفاض العائد الحقيقي والدولار قد يدعم الذهب."
        else:
            bias = "MIXED_OR_NEUTRAL"
            explanation = "العوامل الكلية متباينة أو حركتها غير كافية لتحديد انحياز واضح."
        return {
            "bias": bias,
            "explanation": explanation,
            "observations": observations,
            "changes": {
                "nominal_10y_yield_basis_points": round(nominal_change_bp, 2),
                "real_10y_yield_basis_points": round(real_change_bp, 2),
                "broad_us_dollar_index_percent": round(dollar_change_pct, 4),
            },
            "source": "Federal Reserve Bank of St. Louis FRED",
            "generated_at": now,
            "execution_enabled": False,
            "note": "Daily macro context only; not an intraday signal or a calibrated predictor of gold returns.",
        }
    except HTTPException:
        raise
    except (httpx.HTTPError, ValueError, TypeError, KeyError) as exc:
        raise HTTPException(status_code=503, detail="FRED macro data is unavailable, stale, or invalid; macro filter is paused.") from exc


async def _load_us_high_impact_events(days_ahead: int = 7) -> list[dict]:
    """Fetch high-impact US calendar events; never substitute guessed or cached data."""
    if not 1 <= days_ahead <= 14:
        raise HTTPException(status_code=422, detail="days_ahead must be between 1 and 14.")
    api_key = settings.economic_calendar_api_key.strip()
    if not api_key:
        raise HTTPException(status_code=503, detail="Economic calendar is not configured; news-sensitive analysis is paused.")

    now = datetime.now(timezone.utc)
    start_date = now.date().isoformat()
    end_date = (now.date()).fromordinal(now.date().toordinal() + days_ahead).isoformat()
    base_url = settings.economic_calendar_base_url.rstrip("/")
    url = f"{base_url}/calendar/country/united%20states/{start_date}/{end_date}"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                url,
                params={"importance": "3", "f": "json"},
                headers={"Authorization": api_key},
            )
            response.raise_for_status()
            payload = response.json()
        if not isinstance(payload, list):
            raise ValueError("Calendar response must be a JSON array.")
        events = []
        for item in payload:
            if not isinstance(item, dict):
                raise ValueError("Calendar event has an invalid shape.")
            if int(item.get("Importance", 0) or 0) != 3:
                continue
            raw_date = item.get("Date")
            name = item.get("Event")
            if not raw_date or not name:
                raise ValueError("High-impact calendar event is missing its date or name.")
            event_time = datetime.fromisoformat(str(raw_date).replace("Z", "+00:00"))
            if event_time.tzinfo is None:
                event_time = event_time.replace(tzinfo=timezone.utc)
            events.append({
                "event_id": str(item.get("CalendarId", "")),
                "event": str(name),
                "category": str(item.get("Category", "")),
                "date": event_time.astimezone(timezone.utc),
                "importance": 3,
                "date_span": str(item.get("DateSpan", "0")),
                "actual": str(item.get("Actual", "")),
                "forecast": str(item.get("Forecast", "")),
                "previous": str(item.get("Previous", "")),
                "source": str(item.get("Source", "")),
                "source_url": str(item.get("SourceURL", "")),
            })
        return sorted(events, key=lambda event: event["date"])
    except HTTPException:
        raise
    except (httpx.HTTPError, ValueError, TypeError, KeyError) as exc:
        raise HTTPException(status_code=503, detail="Economic calendar provider failed or returned invalid data; news filter is paused.") from exc


@app.get("/api/v1/calendar/us-high-impact")
async def us_high_impact_calendar(days_ahead: int = 7) -> dict:
    events = await _load_us_high_impact_events(days_ahead)
    return {
        "country": "United States",
        "importance": 3,
        "timezone": "UTC",
        "days_ahead": days_ahead,
        "event_count": len(events),
        "events": events,
        "source": "Trading Economics",
        "execution_enabled": False,
        "note": "Economic calendar events can be revised; verify release times with the official source.",
    }


@app.get("/api/v1/analysis/news-filter")
async def economic_news_filter() -> dict:
    """Return WAIT during high-impact USD news blackout windows; fail closed on missing data."""
    events = await _load_us_high_impact_events(7)
    now = datetime.now(timezone.utc)
    before = settings.news_blackout_before_minutes
    after = settings.news_blackout_after_minutes
    active = []
    for event in events:
        delta_minutes = (event["date"] - now).total_seconds() / 60
        # An estimated-time event blocks its full UTC date because its exact time is unknown.
        same_estimated_day = event["date_span"] == "1" and event["date"].date() == now.date()
        if same_estimated_day or -after <= delta_minutes <= before:
            active.append({**event, "minutes_until_event": round(delta_minutes, 1)})
    blocked = bool(active)
    return {
        "decision": "WAIT" if blocked else "CLEAR",
        "trading_allowed": not blocked,
        "reason": "High-impact US event blackout is active." if blocked else "No high-impact US event is inside the configured blackout window.",
        "blackout_before_minutes": before,
        "blackout_after_minutes": after,
        "active_events": active,
        "checked_at": now,
        "execution_enabled": False,
        "fail_closed": True,
    }


class Candle(BaseModel):
    time: datetime
    open: float = Field(gt=0)
    high: float = Field(gt=0)
    low: float = Field(gt=0)
    close: float = Field(gt=0)
    tick_volume: int = Field(default=0, ge=0)


class BacktestRequest(BaseModel):
    symbol: str = "GOLD"
    timeframe: str = "M15"
    candles: list[Candle] = Field(min_length=100, max_length=5000)
    initial_balance: float = Field(default=10_000, gt=0)
    lots: float = Field(default=0.1, gt=0)
    contract_size: float = Field(default=100, gt=0)
    spread_points: float = Field(default=20, ge=0)
    point: float = Field(default=0.01, gt=0)
    slippage_points: float = Field(default=2, ge=0)
    commission_per_lot_round_turn: float = Field(default=0, ge=0)
    atr_stop_multiple: float = Field(default=1.5, ge=0.5, le=5)
    reward_risk_ratio: float = Field(default=2.0, ge=1, le=5)
    max_holding_bars: int = Field(default=100, ge=1, le=500)


class TechnicalAnalysisRequest(BaseModel):
    symbol: str = "GOLD"
    timeframe: str
    candles: list[Candle] = Field(min_length=60, max_length=500)


class MarketRegimeRequest(BaseModel):
    symbol: str = "GOLD"
    timeframe: str
    candles: list[Candle] = Field(min_length=60, max_length=500)


class MultiTimeframeAnalysisRequest(BaseModel):
    symbol: str = "GOLD"
    m5: list[Candle] = Field(min_length=60, max_length=500)
    m15: list[Candle] = Field(min_length=60, max_length=500)
    h1: list[Candle] = Field(min_length=60, max_length=500)
    h4: list[Candle] = Field(min_length=60, max_length=500)


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


def _rsi_wilder_series(values: list[float], period: int = 14) -> list[float | None]:
    result: list[float | None] = [None] * len(values)
    changes = [values[i] - values[i - 1] for i in range(1, len(values))]
    if len(changes) < period:
        return result
    gains = [max(change, 0.0) for change in changes]
    losses = [max(-change, 0.0) for change in changes]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    def value() -> float:
        if avg_loss == 0:
            return 100.0 if avg_gain > 0 else 50.0
        rs = avg_gain / avg_loss
        return 100 - 100 / (1 + rs)
    result[period] = value()
    for index in range(period, len(changes)):
        avg_gain = ((period - 1) * avg_gain + gains[index]) / period
        avg_loss = ((period - 1) * avg_loss + losses[index]) / period
        result[index + 1] = value()
    return result


def _atr_wilder_series(candles: list[Candle], period: int = 14) -> list[float | None]:
    result: list[float | None] = [None] * len(candles)
    ranges = _true_ranges(candles)
    if len(ranges) < period:
        return result
    current = sum(ranges[:period]) / period
    result[period] = current
    for index in range(period, len(ranges)):
        current = ((period - 1) * current + ranges[index]) / period
        result[index + 1] = current
    return result


def _adx_wilder(candles: list[Candle], period: int = 14) -> tuple[float, float, float]:
    if len(candles) < period + 2:
        raise ValueError("Not enough candles for ADX.")
    true_ranges = _true_ranges(candles)
    plus_dm = []
    minus_dm = []
    for index in range(1, len(candles)):
        up_move = candles[index].high - candles[index - 1].high
        down_move = candles[index - 1].low - candles[index].low
        plus_dm.append(up_move if up_move > down_move and up_move > 0 else 0.0)
        minus_dm.append(down_move if down_move > up_move and down_move > 0 else 0.0)

    tr_smooth = sum(true_ranges[:period])
    plus_smooth = sum(plus_dm[:period])
    minus_smooth = sum(minus_dm[:period])
    dx_values = []
    plus_di = minus_di = 0.0
    for index in range(period - 1, len(true_ranges)):
        if index >= period:
            tr_smooth = tr_smooth - tr_smooth / period + true_ranges[index]
            plus_smooth = plus_smooth - plus_smooth / period + plus_dm[index]
            minus_smooth = minus_smooth - minus_smooth / period + minus_dm[index]
        plus_di = 100 * plus_smooth / tr_smooth if tr_smooth else 0.0
        minus_di = 100 * minus_smooth / tr_smooth if tr_smooth else 0.0
        denominator = plus_di + minus_di
        dx_values.append(100 * abs(plus_di - minus_di) / denominator if denominator else 0.0)
    if len(dx_values) < period:
        raise ValueError("Not enough directional movement values for ADX.")
    adx = sum(dx_values[:period]) / period
    for dx in dx_values[period:]:
        adx = ((period - 1) * adx + dx) / period
    return adx, plus_di, minus_di


def _market_regime_analysis(candles: list[Candle]) -> dict:
    closes = [item.close for item in candles]
    atr = _atr_wilder(candles)
    atr_series = [value for value in _atr_wilder_series(candles) if value is not None]
    baseline = statistics.median(atr_series[-50:]) if atr_series else atr
    volatility_ratio = atr / baseline if baseline > 0 else 1.0
    adx, plus_di, minus_di = _adx_wilder(candles)
    if volatility_ratio >= 1.5:
        regime = "HIGH_VOLATILITY"
        action = "WAIT"
    elif adx >= 25 and plus_di > minus_di:
        regime = "TRENDING_UP"
        action = "TREND_FOLLOWING_SETUP_ONLY"
    elif adx >= 25 and minus_di > plus_di:
        regime = "TRENDING_DOWN"
        action = "TREND_FOLLOWING_SETUP_ONLY"
    elif adx < 20:
        regime = "RANGING"
        action = "RANGE_SETUP_ONLY"
    else:
        regime = "TRANSITION"
        action = "WAIT_FOR_CONFIRMATION"
    return {
        "market_regime": regime,
        "recommended_action": action,
        "indicators": {
            "adx14_wilder": round(adx, 2),
            "plus_di14": round(plus_di, 2),
            "minus_di14": round(minus_di, 2),
            "atr14_wilder": round(atr, 5),
            "atr_vs_recent_median": round(volatility_ratio, 3),
            "close": closes[-1],
        },
        "thresholds": {
            "strong_trend_adx": 25,
            "range_adx_below": 20,
            "high_volatility_atr_ratio": 1.5,
        },
        "note": "Regime labels are rule-based context, not a predictive model or trade authorization.",
    }


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



@app.post("/api/v1/backtest")
def run_backtest(request: BacktestRequest) -> dict:
    """Run a conservative, next-bar-entry EMA/RSI trend backtest without look-ahead."""
    if request.symbol.upper() != settings.broker_gold_symbol.upper():
        raise HTTPException(status_code=400, detail="Unsupported broker symbol.")
    timeframe = request.timeframe.upper()
    if timeframe not in _TIMEFRAME_SECONDS:
        raise HTTPException(status_code=422, detail="Unsupported timeframe.")
    candles = request.candles
    normalized_times = []
    for candle in candles:
        if candle.high < max(candle.open, candle.close, candle.low) or candle.low > min(candle.open, candle.close, candle.high):
            raise HTTPException(status_code=422, detail="Invalid candle high/low values.")
        stamp = candle.time if candle.time.tzinfo else candle.time.replace(tzinfo=timezone.utc)
        normalized_times.append(stamp.astimezone(timezone.utc))
    if any(current <= previous for previous, current in zip(normalized_times, normalized_times[1:])):
        raise HTTPException(status_code=422, detail="Candle timestamps must be strictly increasing and unique.")

    closes = [candle.close for candle in candles]
    ema20 = _ema_series(closes, 20)
    ema50 = _ema_series(closes, 50)
    rsi = _rsi_wilder_series(closes)
    atr = _atr_wilder_series(candles)
    spread_price = request.spread_points * request.point
    slippage_price = request.slippage_points * request.point
    equity = request.initial_balance
    peak_equity = equity
    max_drawdown = 0.0
    trades: list[dict] = []
    position = None
    pending = None
    equity_curve = [{"time": normalized_times[0], "equity": round(equity, 2)}]

    def close_position(exit_price: float, exit_index: int, reason: str) -> None:
        nonlocal equity, peak_equity, max_drawdown, position
        if position["direction"] == "BUY":
            gross = (exit_price - position["entry_price"]) * request.contract_size * request.lots
        else:
            gross = (position["entry_price"] - exit_price) * request.contract_size * request.lots
        commission = request.commission_per_lot_round_turn * request.lots
        net = gross - commission
        equity += net
        peak_equity = max(peak_equity, equity)
        drawdown = (peak_equity - equity) / peak_equity * 100 if peak_equity > 0 else 0
        max_drawdown = max(max_drawdown, drawdown)
        trades.append({
            "direction": position["direction"],
            "entry_time": normalized_times[position["entry_index"]],
            "exit_time": normalized_times[exit_index],
            "entry_price": round(position["entry_price"], 5),
            "exit_price": round(exit_price, 5),
            "lots": request.lots,
            "gross_pnl": round(gross, 2),
            "commission": round(commission, 2),
            "net_pnl": round(net, 2),
            "r_multiple": round(net / position["risk_amount"], 3) if position["risk_amount"] else 0,
            "exit_reason": reason,
            "bars_held": exit_index - position["entry_index"] + 1,
        })
        position = None
        equity_curve.append({"time": normalized_times[exit_index], "equity": round(equity, 2)})

    for index in range(50, len(candles)):
        candle = candles[index]
        if position is None and pending is not None:
            direction = pending["direction"]
            risk_distance = pending["atr"] * request.atr_stop_multiple
            if direction == "BUY":
                entry = candle.open + spread_price + slippage_price
                position = {
                    "direction": direction, "entry_index": index, "entry_price": entry,
                    "stop": entry - risk_distance,
                    "target": entry + risk_distance * request.reward_risk_ratio,
                    "risk_amount": risk_distance * request.contract_size * request.lots,
                }
            else:
                entry = candle.open - slippage_price
                position = {
                    "direction": direction, "entry_index": index, "entry_price": entry,
                    "stop": entry + risk_distance,
                    "target": entry - risk_distance * request.reward_risk_ratio,
                    "risk_amount": risk_distance * request.contract_size * request.lots,
                }
            pending = None

        if position is not None:
            held = index - position["entry_index"] + 1
            if position["direction"] == "BUY":
                if candle.open <= position["stop"]:
                    close_position(candle.open - slippage_price, index, "STOP_GAP")
                elif candle.low <= position["stop"]:
                    close_position(position["stop"] - slippage_price, index, "STOP")
                elif candle.open >= position["target"]:
                    close_position(candle.open - slippage_price, index, "TARGET_GAP")
                elif candle.high >= position["target"]:
                    close_position(position["target"] - slippage_price, index, "TARGET")
                elif held >= request.max_holding_bars:
                    close_position(candle.close - slippage_price, index, "TIME_EXIT")
            else:
                ask_open = candle.open + spread_price
                ask_high = candle.high + spread_price
                ask_low = candle.low + spread_price
                if ask_open >= position["stop"]:
                    close_position(ask_open + slippage_price, index, "STOP_GAP")
                elif ask_high >= position["stop"]:
                    close_position(position["stop"] + slippage_price, index, "STOP")
                elif ask_open <= position["target"]:
                    close_position(ask_open + slippage_price, index, "TARGET_GAP")
                elif ask_low <= position["target"]:
                    close_position(position["target"] + slippage_price, index, "TARGET")
                elif held >= request.max_holding_bars:
                    close_position(candle.close + spread_price + slippage_price, index, "TIME_EXIT")

        if position is None and index < len(candles) - 1 and atr[index] and rsi[index] is not None:
            ema_fast_value = ema20[index - 19]
            ema_slow_value = ema50[index - 49]
            if ema_fast_value > ema_slow_value and 52 <= rsi[index] <= 68:
                pending = {"direction": "BUY", "atr": atr[index]}
            elif ema_fast_value < ema_slow_value and 32 <= rsi[index] <= 48:
                pending = {"direction": "SELL", "atr": atr[index]}
            else:
                pending = None

    if position is not None:
        last_index = len(candles) - 1
        last = candles[-1]
        exit_price = (
            last.close - slippage_price if position["direction"] == "BUY"
            else last.close + spread_price + slippage_price
        )
        close_position(exit_price, last_index, "END_OF_DATA")

    wins = [trade["net_pnl"] for trade in trades if trade["net_pnl"] > 0]
    losses = [trade["net_pnl"] for trade in trades if trade["net_pnl"] < 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    net_profit = sum(trade["net_pnl"] for trade in trades)
    return {
        "symbol": settings.broker_gold_symbol.upper(),
        "timeframe": timeframe,
        "strategy": "EMA20/EMA50 trend with Wilder RSI confirmation; ATR stop and fixed reward/risk target",
        "trade_count": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_percent": round(len(wins) / len(trades) * 100, 2) if trades else 0.0,
        "profit_factor": round(gross_profit / gross_loss, 3) if gross_loss else None,
        "profit_factor_note": "Undefined because there are no losing trades in this sample." if gross_profit > 0 and gross_loss == 0 else None,
        "net_profit": round(net_profit, 2),
        "ending_balance": round(equity, 2),
        "return_percent": round(net_profit / request.initial_balance * 100, 3),
        "expectancy_per_trade": round(net_profit / len(trades), 2) if trades else 0.0,
        "average_win": round(gross_profit / len(wins), 2) if wins else 0.0,
        "average_loss": round(-gross_loss / len(losses), 2) if losses else 0.0,
        "max_drawdown_percent": round(max_drawdown, 3),
        "cost_model": {
            "spread_points": request.spread_points,
            "slippage_points_per_side": request.slippage_points,
            "commission_per_lot_round_turn": request.commission_per_lot_round_turn,
            "lots": request.lots,
            "contract_size": request.contract_size,
        },
        "equity_curve": equity_curve,
        "trades": trades,
        "lookahead_bias_controls": [
            "Signals are calculated only after a candle closes.",
            "Entries occur at the following candle open.",
            "If stop and target are both touched in one candle, the stop is assumed first.",
            "Spread, slippage and round-turn commission are included.",
        ],
        "execution_enabled": False,
        "disclaimer": "Historical simulation only. It does not guarantee future performance and does not model every broker fill, swap, financing charge, liquidity event or account-currency conversion.",
    }



@app.post("/api/v1/analysis/market-regime")
def market_regime_analysis(request: MarketRegimeRequest) -> dict:
    """Classify trend/range/volatility using Wilder ADX and ATR from broker candles."""
    if request.symbol.upper() != settings.broker_gold_symbol.upper():
        raise HTTPException(status_code=400, detail="Unsupported broker symbol.")
    timeframe = request.timeframe.upper()
    if timeframe not in _TIMEFRAME_SECONDS:
        raise HTTPException(status_code=422, detail="Unsupported timeframe.")
    quote = _fresh_mt5_quote()
    normalized_times = []
    for candle in request.candles:
        if candle.high < max(candle.open, candle.close, candle.low) or candle.low > min(candle.open, candle.close, candle.high):
            raise HTTPException(status_code=422, detail="Invalid candle high/low values.")
        stamp = candle.time if candle.time.tzinfo else candle.time.replace(tzinfo=timezone.utc)
        normalized_times.append(stamp.astimezone(timezone.utc))
    if any(current <= previous for previous, current in zip(normalized_times, normalized_times[1:])):
        raise HTTPException(status_code=422, detail="Candle timestamps must be strictly increasing and unique.")
    age = (datetime.now(timezone.utc) - normalized_times[-1]).total_seconds()
    if age < -30 or age > _TIMEFRAME_SECONDS[timeframe] * 2 + 60:
        raise HTTPException(status_code=422, detail="Latest candle is stale or from the future.")
    analysis = _market_regime_analysis(request.candles)
    return {
        "symbol": settings.broker_gold_symbol.upper(),
        "timeframe": timeframe,
        **analysis,
        "broker_spread_points": quote["spread_points"],
        "generated_at": datetime.now(timezone.utc),
        "execution_enabled": False,
    }


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


@app.post("/api/v1/analysis/multi-timeframe")
def multi_timeframe_analysis(request: MultiTimeframeAnalysisRequest) -> dict:
    """Require aligned M5 entry, M15 confirmation, H1 trend and H4 macro bias."""
    if request.symbol.upper() != settings.broker_gold_symbol.upper():
        raise HTTPException(status_code=400, detail="Unsupported broker symbol.")

    analyses = {
        "M5": technical_analysis(TechnicalAnalysisRequest(
            symbol=request.symbol, timeframe="M5", candles=request.m5
        )),
        "M15": technical_analysis(TechnicalAnalysisRequest(
            symbol=request.symbol, timeframe="M15", candles=request.m15
        )),
        "H1": technical_analysis(TechnicalAnalysisRequest(
            symbol=request.symbol, timeframe="H1", candles=request.h1
        )),
        "H4": technical_analysis(TechnicalAnalysisRequest(
            symbol=request.symbol, timeframe="H4", candles=request.h4
        )),
    }
    directions = {frame: result["direction"] for frame, result in analyses.items()}
    entry = directions["M5"]
    reasons: list[str] = []

    if entry == Direction.WAIT:
        final_direction = Direction.WAIT
        reasons.append("إطار M5 لا يقدم نقطة دخول مؤكدة.")
    elif any(direction != entry for direction in directions.values()):
        final_direction = Direction.WAIT
        reasons.append("تعارض بين M5 وM15 وH1 وH4؛ تم إلغاء الإشارة حتى تتوافق جميع الأطر.")
    else:
        final_direction = entry
        reasons.append("توافق M5 للدخول وM15 للتأكيد وH1 للاتجاه وH4 للسياق العام.")

    if any(result["spread_atr_ratio"] > 0.15 for result in analyses.values()):
        final_direction = Direction.WAIT
        reasons.append("فلتر السبريد منع الصفقة في إطار واحد على الأقل.")

    confidence_score = 0
    if final_direction != Direction.WAIT:
        confidence_score = min(
            85,
            round(sum(result["confidence_score"] for result in analyses.values()) / len(analyses)),
        )

    return {
        "symbol": settings.broker_gold_symbol.upper(),
        "direction": final_direction,
        "confidence_score": confidence_score,
        "confidence_note": "مؤشر توافق داخلي غير معاير، وليس احتمال نجاح الصفقة.",
        "timeframes": analyses,
        "alignment": directions,
        "reasons": reasons,
        "execution_enabled": False,
        "generated_at": datetime.now(timezone.utc),
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


class PriceActionRequest(BaseModel):
    symbol: str = "GOLD"
    timeframe: str
    candles: list[Candle] = Field(min_length=60, max_length=500)
    swing_left: int = Field(default=3, ge=2, le=10)
    swing_right: int = Field(default=3, ge=2, le=10)
    equal_level_tolerance_atr: float = Field(default=0.12, gt=0, le=0.5)


def _confirmed_swings(candles: list[Candle], left: int, right: int) -> tuple[list[dict], list[dict]]:
    """Find non-repainting pivots: a pivot is emitted only after right bars close."""
    highs: list[dict] = []
    lows: list[dict] = []
    for index in range(left, len(candles) - right):
        window = candles[index - left:index + right + 1]
        high = candles[index].high
        low = candles[index].low
        # Strict comparison against at least one neighbour avoids flat-window duplicates.
        if high == max(item.high for item in window) and any(high > item.high for j, item in enumerate(window) if j != left):
            highs.append({"index": index, "time": candles[index].time, "price": high})
        if low == min(item.low for item in window) and any(low < item.low for j, item in enumerate(window) if j != left):
            lows.append({"index": index, "time": candles[index].time, "price": low})
    return highs, lows


def _price_action_analysis(candles: list[Candle], left: int, right: int, equal_tolerance: float) -> dict:
    highs, lows = _confirmed_swings(candles, left, right)
    last = candles[-1]
    atr = _atr_wilder(candles)
    tolerance = max(atr * equal_tolerance, 1e-9)
    recent_highs, recent_lows = highs[-4:], lows[-4:]

    structure = "UNCONFIRMED"
    if len(recent_highs) >= 2 and len(recent_lows) >= 2:
        higher_highs = recent_highs[-1]["price"] > recent_highs[-2]["price"]
        higher_lows = recent_lows[-1]["price"] > recent_lows[-2]["price"]
        lower_highs = recent_highs[-1]["price"] < recent_highs[-2]["price"]
        lower_lows = recent_lows[-1]["price"] < recent_lows[-2]["price"]
        if higher_highs and higher_lows:
            structure = "UPTREND"
        elif lower_highs and lower_lows:
            structure = "DOWNTREND"
        else:
            structure = "RANGE_OR_TRANSITION"

    last_high = recent_highs[-1] if recent_highs else None
    last_low = recent_lows[-1] if recent_lows else None
    prior_close = candles[-2].close
    bullish_bos = bool(last_high and last.close > last_high["price"] and prior_close <= last_high["price"])
    bearish_bos = bool(last_low and last.close < last_low["price"] and prior_close >= last_low["price"])
    bullish_sweep = bool(last_high and last.high > last_high["price"] + tolerance and last.close < last_high["price"])
    bearish_sweep = bool(last_low and last.low < last_low["price"] - tolerance and last.close > last_low["price"])

    equal_highs = []
    equal_lows = []
    for i in range(len(recent_highs)):
        for j in range(i + 1, len(recent_highs)):
            a, b = recent_highs[i], recent_highs[j]
            if abs(a["price"] - b["price"]) <= tolerance:
                equal_highs.append({"price": round((a["price"] + b["price"]) / 2, 5), "first_index": a["index"], "second_index": b["index"]})
    for i in range(len(recent_lows)):
        for j in range(i + 1, len(recent_lows)):
            a, b = recent_lows[i], recent_lows[j]
            if abs(a["price"] - b["price"]) <= tolerance:
                equal_lows.append({"price": round((a["price"] + b["price"]) / 2, 5), "first_index": a["index"], "second_index": b["index"]})

    bullish_fvg = []
    bearish_fvg = []
    for i in range(2, len(candles)):
        first, third = candles[i - 2], candles[i]
        if third.low > first.high:
            bullish_fvg.append({"from_index": i - 2, "to_index": i, "lower": first.high, "upper": third.low, "mid": (first.high + third.low) / 2})
        if third.high < first.low:
            bearish_fvg.append({"from_index": i - 2, "to_index": i, "lower": third.high, "upper": first.low, "mid": (third.high + first.low) / 2})

    # A basic order-block candidate is the last opposing candle before a confirmed structure break.
    order_block = None
    if bullish_bos and last_high:
        for i in range(len(candles) - 2, max(-1, last_high["index"]), -1):
            if candles[i].close < candles[i].open:
                order_block = {"direction": "BULLISH", "index": i, "low": candles[i].low, "high": candles[i].high}
                break
    elif bearish_bos and last_low:
        for i in range(len(candles) - 2, max(-1, last_low["index"]), -1):
            if candles[i].close > candles[i].open:
                order_block = {"direction": "BEARISH", "index": i, "low": candles[i].low, "high": candles[i].high}
                break

    events = []
    if bullish_bos:
        events.append("BULLISH_BREAK_OF_STRUCTURE")
    if bearish_bos:
        events.append("BEARISH_BREAK_OF_STRUCTURE")
    if bullish_sweep:
        events.append("BUY_SIDE_LIQUIDITY_SWEEP")
    if bearish_sweep:
        events.append("SELL_SIDE_LIQUIDITY_SWEEP")
    if not events:
        events.append("NO_CONFIRMED_STRUCTURE_EVENT")

    return {
        "market_structure": structure,
        "swing_highs": recent_highs,
        "swing_lows": recent_lows,
        "last_confirmed_swing_high": last_high,
        "last_confirmed_swing_low": last_low,
        "events": events,
        "liquidity_pools": {"equal_highs": equal_highs, "equal_lows": equal_lows},
        "fair_value_gaps": {"bullish": bullish_fvg[-5:], "bearish": bearish_fvg[-5:]},
        "order_block_candidate": order_block,
        "atr14": round(atr, 5),
        "swing_confirmation": {"left_bars": left, "right_bars": right, "repainting": False},
        "notes": [
            "Swing points are confirmed only after the configured right-side candles close.",
            "Liquidity sweeps and structure breaks are rule-based candidates, not proof of institutional orders.",
            "FVG and order-block zones are heuristic price-action features and require backtesting before use.",
        ],
    }


@app.post("/api/v1/analysis/price-action")
def price_action_analysis(request: PriceActionRequest) -> dict:
    """Return deterministic, confirmed-candle market-structure and price-action features."""
    if request.symbol.upper() != settings.broker_gold_symbol.upper():
        raise HTTPException(status_code=400, detail="Unsupported broker symbol.")
    timeframe = request.timeframe.upper()
    if timeframe not in _TIMEFRAME_SECONDS:
        raise HTTPException(status_code=422, detail="Unsupported timeframe.")
    _fresh_mt5_quote()
    candles = request.candles
    normalized_times = []
    for candle in candles:
        if candle.high < max(candle.open, candle.close, candle.low) or candle.low > min(candle.open, candle.close, candle.high):
            raise HTTPException(status_code=422, detail="Invalid candle high/low values.")
        timestamp = candle.time if candle.time.tzinfo else candle.time.replace(tzinfo=timezone.utc)
        normalized_times.append(timestamp.astimezone(timezone.utc))
    if any(current <= previous for previous, current in zip(normalized_times, normalized_times[1:])):
        raise HTTPException(status_code=422, detail="Candle timestamps must be strictly increasing and unique.")
    age = (datetime.now(timezone.utc) - normalized_times[-1]).total_seconds()
    if age < -30 or age > _TIMEFRAME_SECONDS[timeframe] * 2 + 60:
        raise HTTPException(status_code=422, detail="Latest candle is stale or from the future.")
    analysis = _price_action_analysis(candles, request.swing_left, request.swing_right, request.equal_level_tolerance_atr)
    analysis.update({
        "symbol": settings.broker_gold_symbol.upper(),
        "timeframe": timeframe,
        "candle_count": len(candles),
        "latest_candle_time": normalized_times[-1],
        "generated_at": datetime.now(timezone.utc),
        "execution_enabled": False,
        "signal": "WAIT",
        "signal_note": "Price-action features are diagnostic only; no entry is authorized by this endpoint.",
    })
    return analysis


class PreReleaseNewsRequest(BaseModel):
    """Inputs from a separately validated event-forecast model; no invented forecasts."""
    symbol: str = "GOLD"
    event_id: str = Field(min_length=2, max_length=80)
    event_name: str = Field(min_length=3, max_length=160)
    release_at: datetime
    consensus: float
    model_estimate: float
    historical_error_std: float = Field(gt=0)
    historical_samples: int = Field(ge=30, le=100000)
    walk_forward_validation_passed: bool = False
    walk_forward_test_samples: int = Field(default=0, ge=0, le=100000)
    gold_direction_if_above: str = Field(pattern="^(BULLISH|BEARISH|MIXED)$")
    gold_direction_if_inline: str = Field(default="MIXED", pattern="^(BULLISH|BEARISH|MIXED)$")
    gold_direction_if_below: str = Field(pattern="^(BULLISH|BEARISH|MIXED)$")
    max_spread_points: float = Field(default=35, gt=0, le=10000)


@app.post("/api/v1/news/pre-release-analysis")
def pre_release_news_analysis(request: PreReleaseNewsRequest) -> dict:
    """Estimate pre-release scenario probabilities from a supplied, historically measured forecast model.

    This endpoint does not create the model estimate. The caller must provide an estimate,
    its historical error scale and sample count. Output is a paper-trading research candidate only.
    """
    if request.symbol.upper() != settings.broker_gold_symbol.upper():
        raise HTTPException(status_code=400, detail=f"Only {settings.broker_gold_symbol} is supported.")
    now = datetime.now(timezone.utc)
    release_at = request.release_at
    if release_at.tzinfo is None:
        raise HTTPException(status_code=422, detail="release_at must include a timezone.")
    minutes_to_release = (release_at.astimezone(timezone.utc) - now).total_seconds() / 60
    if minutes_to_release < 5 or minutes_to_release > 24 * 60:
        raise HTTPException(status_code=422, detail="Release must be between 5 minutes and 24 hours ahead.")

    quote = _fresh_mt5_quote()
    spread_ok = quote["spread_points"] <= request.max_spread_points

    # Model residuals are approximated as normal using the supplied out-of-sample error scale.
    # The ±0.25σ band defines 'in line'; it is explicit and testable, not a market fact.
    distribution = statistics.NormalDist(mu=request.model_estimate, sigma=request.historical_error_std)
    inline_band = request.historical_error_std * 0.25
    p_below = distribution.cdf(request.consensus - inline_band)
    p_above = 1.0 - distribution.cdf(request.consensus + inline_band)
    p_inline = max(0.0, 1.0 - p_above - p_below)
    total = p_above + p_inline + p_below
    probabilities = {
        "above_consensus": round(p_above / total, 6),
        "inline_with_consensus": round(p_inline / total, 6),
        "below_consensus": round(p_below / total, 6),
    }

    directional = {"BULLISH": 0.0, "BEARISH": 0.0, "MIXED": 0.0}
    directional[request.gold_direction_if_above] += probabilities["above_consensus"]
    directional[request.gold_direction_if_inline] += probabilities["inline_with_consensus"]
    directional[request.gold_direction_if_below] += probabilities["below_consensus"]
    ranked = sorted(directional.items(), key=lambda item: item[1], reverse=True)
    top_direction, top_probability = ranked[0]
    edge = top_probability - ranked[1][1]
    enough_edge = top_direction != "MIXED" and top_probability >= 0.65 and edge >= 0.15

    decision = "NO_TRADE"
    reasons = [
        "الاحتمالات محسوبة من تقدير نموذج خارجي ومقياس خطئه التاريخي؛ ليست معرفة مسبقة بنتيجة الخبر.",
        "الافتراض الإحصائي طبيعي، ويجب اختباره ومعايرته لكل نوع خبر قبل اعتباره صالحاً للتداول.",
    ]
    if request.historical_samples < 100:
        reasons.append("عدد العينات أقل من 100؛ الثقة البحثية محدودة.")
    # Never trust a client-supplied boolean as proof of validation. Until a server-side
    # model registry stores reproducible metrics and the exact model/data version, fail closed.
    validation_ok = False
    reasons.append(
        "التحقق من walk-forward غير موثق داخل سجل نماذج الخادم؛ "
        "الحقول المرسلة من التطبيق لا تُعد دليلاً، لذلك يمنع ترشيح الدخول قبل الخبر."
    )
    if not spread_ok:
        reasons.append("سبريد MT5 أعلى من الحد المسموح لهذا التحليل؛ لا يتم ترشيح صفقة.")
    if enough_edge and spread_ok and request.historical_samples >= 100 and validation_ok:
        decision = "PRE_NEWS_SETUP_CANDIDATE"
        reasons.append("تجاوزت الاحتمالات والميزة الحدين البحثيين؛ مرشح للمراجعة والتجربة الورقية فقط.")
    else:
        reasons.append("لا توجد أفضلية كافية أو بيانات/ظروف تنفيذ مناسبة؛ القرار NO TRADE.")

    return {
        "symbol": settings.broker_gold_symbol.upper(),
        "event_id": request.event_id,
        "event_name": request.event_name,
        "release_at": release_at,
        "minutes_to_release": round(minutes_to_release, 1),
        "forecast_inputs": {
            "consensus": request.consensus,
            "model_estimate": request.model_estimate,
            "historical_error_std": request.historical_error_std,
            "historical_samples": request.historical_samples,
            "walk_forward_validation_passed": False,
            "walk_forward_test_samples": 0,
            "client_validation_claim_ignored": True,
            "client_test_samples_claim_ignored": True,
            "server_side_model_registry_verified": False,
            "distribution_assumption": "normal_out_of_sample_residuals",
            "inline_band_std": 0.25,
        },
        "scenario_probabilities": probabilities,
        "gold_direction_probabilities": {key: round(value, 6) for key, value in directional.items()},
        "most_likely_gold_direction": top_direction if enough_edge else "UNCONFIRMED",
        "direction_probability": round(top_probability, 6),
        "probability_edge": round(edge, 6),
        "decision": decision,
        "market_conditions": {
            "quote_source": quote.get("source"),
            "spread_points": quote["spread_points"],
            "max_spread_points": request.max_spread_points,
            "spread_acceptable": spread_ok,
        },
        "reasons": reasons,
        "risk_policy": {
            "mode": "PAPER_ONLY",
            "suggested_max_risk_percent_for_future_testing": 0.25,
            "live_execution_enabled": False,
        },
        "generated_at": now,
        "disclaimer": "Probabilities are model-based estimates, not calibrated guarantees. This endpoint never places orders.",
        "execution_enabled": False,
    }

class HistoricalNewsObservation(BaseModel):
    event_id: str = Field(min_length=3, max_length=100)
    event_type: str = Field(min_length=2, max_length=80)
    released_at: datetime
    consensus: float
    actual: float
    surprise_scale: float = Field(gt=0, description="Historical standard deviation for this event's actual-minus-consensus surprise.")
    gold_before: float = Field(gt=0, description="Last executable broker mid immediately before release.")
    gold_1m: float = Field(gt=0)
    gold_5m: float = Field(gt=0)
    gold_15m: float = Field(gt=0)
    gold_60m: float = Field(gt=0)
    spread_points: float = Field(ge=0)
    point: float = Field(gt=0)
    slippage_points: float = Field(default=0, ge=0)


class HistoricalNewsStudyRequest(BaseModel):
    symbol: str = "GOLD"
    observations: list[HistoricalNewsObservation] = Field(min_length=1, max_length=2000)


def _pearson_correlation(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 3:
        return None
    mean_x, mean_y = statistics.mean(xs), statistics.mean(ys)
    covariance = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    variance_x = sum((x - mean_x) ** 2 for x in xs)
    variance_y = sum((y - mean_y) ** 2 for y in ys)
    denominator = math.sqrt(variance_x * variance_y)
    if denominator == 0:
        return None
    return round(covariance / denominator, 6)


def _compounded_max_drawdown_percent(returns_percent: list[float]) -> float:
    """Maximum peak-to-trough drawdown for a chronological sequence of net returns."""
    equity = peak = 1.0
    max_drawdown = 0.0
    for value in returns_percent:
        equity *= max(0.0, 1.0 + value / 100.0)
        peak = max(peak, equity)
        if peak > 0:
            max_drawdown = max(max_drawdown, (peak - equity) / peak * 100.0)
    return round(max_drawdown, 5)


@app.post("/api/v1/news/historical-study")
def historical_news_study(request: HistoricalNewsStudyRequest) -> dict:
    """Study historical news surprises against observed GOLD reactions, net of simple spread/slippage costs."""
    if request.symbol.upper() != settings.broker_gold_symbol.upper():
        raise HTTPException(status_code=400, detail=f"Only {settings.broker_gold_symbol} is supported.")

    ids = [item.event_id for item in request.observations]
    if len(ids) != len(set(ids)):
        raise HTTPException(status_code=422, detail="event_id values must be unique.")
    normalized_times = []
    for item in request.observations:
        if item.released_at.tzinfo is None:
            raise HTTPException(status_code=422, detail="Every released_at timestamp must include a timezone.")
        if not item.event_type.strip():
            raise HTTPException(status_code=422, detail="event_type must not be empty.")
        normalized_times.append(item.released_at.astimezone(timezone.utc))
        for horizon in ("gold_1m", "gold_5m", "gold_15m", "gold_60m"):
            if getattr(item, horizon) <= 0:
                raise HTTPException(status_code=422, detail=f"{horizon} must be positive.")

    # Sort by actual release time before any path-dependent risk metric is calculated.
    ordered_observations = sorted(
        zip(normalized_times, request.observations), key=lambda pair: pair[0]
    )
    overlap_pairs = sum(
        1 for index, (left_time, _) in enumerate(ordered_observations)
        for right_time, _ in ordered_observations[index + 1:]
        if (right_time - left_time).total_seconds() < 60 * 60
    )

    grouped: dict[str, list[dict]] = {}
    for released_at, item in ordered_observations:
        surprise_z = (item.actual - item.consensus) / item.surprise_scale
        cost_percent = ((item.spread_points + 2 * item.slippage_points) * item.point / item.gold_before) * 100
        row = {"released_at": released_at, "surprise_z": surprise_z, "cost_percent": cost_percent}
        for horizon in ("1m", "5m", "15m", "60m"):
            price = getattr(item, f"gold_{horizon}")
            row[horizon] = ((price / item.gold_before) - 1) * 100
        grouped.setdefault(item.event_type.strip().upper(), []).append(row)

    results = []
    for event_type, rows in sorted(grouped.items()):
        count = len(rows)
        horizons = {}
        for horizon in ("1m", "5m", "15m", "60m"):
            values = [row[horizon] for row in rows]
            net_values = [value - row["cost_percent"] for value, row in zip(values, rows)]
            horizons[horizon] = {
                "mean_gold_return_percent": round(statistics.mean(values), 5),
                "median_gold_return_percent": round(statistics.median(values), 5),
                "gross_positive_rate_percent": round(sum(value > 0 for value in values) / count * 100, 2),
                "mean_net_return_after_spread_slippage_percent": round(statistics.mean(net_values), 5),
                "median_net_return_percent": round(statistics.median(net_values), 5),
                "net_positive_rate_percent": round(sum(value > 0 for value in net_values) / count * 100, 2),
                "net_return_std_percent": round(statistics.stdev(net_values), 5) if count > 1 else None,
                "max_drawdown_on_chronological_compounded_net_returns_percent": _compounded_max_drawdown_percent(net_values),
            }
        correlation = _pearson_correlation(
            [row["surprise_z"] for row in rows],
            [row["5m"] for row in rows],
        )
        results.append({
            "event_type": event_type,
            "sample_count": count,
            "sample_quality": "MINIMUM_RESEARCH_SAMPLE_MET" if count >= 30 else "EXPLORATORY_ONLY",
            "sample_quality_note": "30 observations is only a minimum descriptive threshold; it is not enough to validate a predictive model.",
            "surprise_vs_gold_5m_correlation": correlation,
            "horizons": horizons,
            "notes": [
                "Correlation is descriptive and does not prove causation or predict the next release.",
                "Net return subtracts supplied spread and twice the supplied slippage estimate; swaps, commissions, gaps and execution failures are not included.",
            ],
        })

    return {
        "symbol": settings.broker_gold_symbol.upper(),
        "observation_count": len(request.observations),
        "event_type_count": len(results),
        "results_by_event_type": results,
        "data_policy": {
            "requires_actual_and_consensus": True,
            "requires_pre_release_and_post_release_gold_prices": True,
            "observations_sorted_by_release_time_for_risk_metrics": True,
            "overlapping_60m_event_windows": overlap_pairs,
            "overlap_warning": (
                "Some releases occur within 60 minutes of each other; their gold reactions may be confounded."
                if overlap_pairs else None
            ),
            "net_cost_model": "spread plus twice supplied slippage; commissions, swaps, gaps and execution failures excluded",
            "no_future_data_used_to_create_pre_release_forecasts": True,
            "execution_enabled": False,
            "mode": "RESEARCH_ONLY",
        },
        "generated_at": datetime.now(timezone.utc),
        "disclaimer": "Historical association is not a forecast. Results depend on timestamp quality, broker pricing and sample selection.",
    }


_HISTORICAL_US_GOLD_EVENTS = [
    "Non Farm Payrolls",
    "Inflation Rate YoY",
    "Core Inflation Rate YoY",
    "Retail Sales MoM",
    "GDP Growth Rate QoQ",
    "Interest Rate Decision",
    "Core PCE Price Index YoY",
    "Unemployment Rate",
    "Initial Jobless Claims",
]


@app.get("/api/v1/news/historical-calendar")
async def historical_news_calendar(start_date: date, end_date: date) -> dict:
    """Import point-in-time US consensus/actual calendar history for subsequent GOLD price alignment."""
    if end_date < start_date:
        raise HTTPException(status_code=422, detail="end_date must be on or after start_date.")
    if (end_date - start_date).days > 5 * 366:
        raise HTTPException(status_code=422, detail="Historical import is limited to five years per request.")
    api_key = settings.economic_calendar_api_key.strip()
    if not api_key:
        raise HTTPException(status_code=503, detail="Trading Economics API key is not configured.")
    base_url = settings.economic_calendar_base_url.rstrip("/")
    imported: dict[str, dict] = {}
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            for indicator in _HISTORICAL_US_GOLD_EVENTS:
                url = (
                    f"{base_url}/calendar/country/united%20states/indicator/"
                    f"{quote(indicator, safe='')}/{start_date.isoformat()}/{end_date.isoformat()}"
                )
                response = await client.get(
                    url,
                    params={"f": "json"},
                    headers={"Authorization": api_key},
                )
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, list):
                    raise ValueError("Historical calendar response must be a JSON array.")
                for item in payload:
                    if not isinstance(item, dict):
                        raise ValueError("Historical calendar event has an invalid shape.")
                    if int(item.get("Importance", 0) or 0) < 2:
                        continue
                    calendar_id = str(item.get("CalendarId", "")).strip()
                    event_date = str(item.get("Date", "")).strip()
                    if not calendar_id or not event_date:
                        continue
                    imported[calendar_id] = {
                        "event_id": calendar_id,
                        "event": str(item.get("Event", indicator)),
                        "category": str(item.get("Category", indicator)),
                        "provider_time_raw": event_date,
                        "actual": str(item.get("Actual", "")),
                        "forecast": str(item.get("Forecast", "")),
                        "te_forecast": str(item.get("TEForecast", "")),
                        "previous": str(item.get("Previous", "")),
                        "revised_previous": str(item.get("Revised", "")),
                        "importance": int(item.get("Importance", 0) or 0),
                        "unit": str(item.get("Unit", "")),
                        "source": str(item.get("Source", "")),
                        "source_url": str(item.get("SourceURL", "")),
                        "last_update_raw": str(item.get("LastUpdate", "")),
                        "ticker": str(item.get("Ticker", "")),
                    }
    except HTTPException:
        raise
    except (httpx.HTTPError, ValueError, TypeError, KeyError) as exc:
        raise HTTPException(
            status_code=503,
            detail="Point-in-time economic history provider failed; no partial dataset is returned.",
        ) from exc

    events = sorted(imported.values(), key=lambda item: (item["provider_time_raw"], item["event_id"]))
    return {
        "country": "United States",
        "start_date": start_date,
        "end_date": end_date,
        "event_count": len(events),
        "events": events,
        "source": "Trading Economics point-in-time calendar",
        "point_in_time": True,
        "price_alignment_required": True,
        "price_alignment_note": "Join each release to XM MT5 GOLD M1 candles using the broker server timezone and measured release timestamp. Do not assume provider timestamps are UTC.",
        "next_step": "Align MT5 GOLD prices immediately before release and at +1m, +5m, +15m and +60m, then submit the joined rows to /api/v1/news/historical-study.",
        "execution_enabled": False,
    }
