from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "development"
    trading_mode: str = "paper"
    live_trading_enabled: bool = False
    max_risk_per_trade_percent: float = Field(default=0.5, gt=0, le=1.0)
    max_daily_loss_percent: float = Field(default=2.0, gt=0, le=5.0)


settings = Settings()
app = FastAPI(
    title="GoldAI Trader API",
    description="Demo-first XAUUSD analysis and risk-control service.",
    version="0.1.0",
)


class Direction(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    WAIT = "WAIT"


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


@app.get("/api/v1/market/snapshot")
def market_snapshot() -> dict:
    raise HTTPException(
        status_code=503,
        detail="No market-data provider is connected yet. Do not use placeholder prices for trading.",
    )


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
