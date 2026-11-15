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


settings = Settings()
app = FastAPI(
    title="GoldAI Trader API",
    description="Demo-first XAUUSD analysis and risk-control service.",
    version="0.2.0",
)


class Direction(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    WAIT = "WAIT"


class BrokerQuote(BaseModel):
    symbol: str = "XAUUSD"
    bid: float = Field(gt=0)
    ask: float = Field(gt=0)
    last: float = Field(default=0, ge=0)
    timestamp: datetime
    digits: int = Field(ge=0, le=10)
    point: float = Field(gt=0)
    spread_points: float = Field(ge=0)


_latest_mt5_quote: dict | None = None


class SignalRequest(BaseModel):
    symbol: str = "XAUUSD"
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
        "supported_initial_symbol": "XAUUSD",
    }


@app.post("/api/v1/mt5/quote")
def receive_mt5_quote(quote: BrokerQuote, authorization: str | None = Header(default=None)) -> dict:
    """Receive the broker's live XAUUSD quote from the authenticated MT5 EA."""
    global _latest_mt5_quote
    expected = settings.mt5_bridge_token
    if not expected or not authorization or not secrets.compare_digest(
        authorization.removeprefix("Bearer ").strip(), expected
    ):
        raise HTTPException(status_code=401, detail="Invalid or unconfigured MT5 bridge token.")
    if quote.symbol.upper() != "XAUUSD":
        raise HTTPException(status_code=400, detail="Only XAUUSD quotes are accepted.")
    if quote.ask < quote.bid:
        raise HTTPException(status_code=422, detail="Ask must be greater than or equal to Bid.")
    timestamp = quote.timestamp
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - timestamp.astimezone(timezone.utc)).total_seconds()
    if age < -30 or age > settings.market_quote_max_age_seconds:
        raise HTTPException(status_code=422, detail="MT5 quote timestamp is outside the accepted freshness window.")
    _latest_mt5_quote = {
        "symbol": "XAUUSD", "bid": quote.bid, "ask": quote.ask, "last": quote.last,
        "mid": round((quote.bid + quote.ask) / 2, quote.digits),
        "spread_points": quote.spread_points, "digits": quote.digits,
        "point": quote.point, "source": "MetaTrader 5 broker feed",
        "source_updated_at": timestamp, "age_seconds": max(0, int(age)),
        "quote_type": "broker_bid_ask", "tradable_quote": True,
    }
    return {"accepted": True, "symbol": "XAUUSD", "source": "MT5"}


@app.get("/api/v1/market/snapshot")
def market_snapshot() -> dict:
    """Return only a fresh broker quote; never silently fall back to public spot."""
    if _latest_mt5_quote is None:
        raise HTTPException(status_code=503, detail="Waiting for a fresh XAUUSD quote from MetaTrader 5.")
    age = max(0, int((datetime.now(timezone.utc) - _latest_mt5_quote["source_updated_at"].astimezone(timezone.utc)).total_seconds()))
    if age > settings.market_quote_max_age_seconds:
        raise HTTPException(status_code=503, detail="MT5 quote is stale; market signals must remain paused.")
    return {**_latest_mt5_quote, "age_seconds": age}


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
            "symbol": "XAUUSD",
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


@app.post("/api/v1/signals/evaluate", response_model=SignalResponse)
def evaluate_signal(request: SignalRequest) -> SignalResponse:
    if request.symbol.upper() != "XAUUSD":
        raise HTTPException(status_code=400, detail="The initial release supports XAUUSD only.")

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
        symbol="XAUUSD",
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
