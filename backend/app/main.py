from __future__ import annotations

from datetime import date, datetime, timezone, timedelta
from contextvars import ContextVar
import asyncio
from enum import Enum
from decimal import Decimal, ROUND_DOWN
import math
import statistics
import logging
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from html import unescape
import re
from typing import Optional
from urllib.parse import quote

import httpx
import jwt
import secrets
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from .xoomar_calendar import load_xoomar_high_impact_events

logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "development"
    trading_mode: str = "paper"
    live_trading_enabled: bool = False
    max_risk_per_trade_percent: float = Field(default=0.5, gt=0, le=1.0)
    max_daily_loss_percent: float = Field(default=10.0, gt=0, le=10.0)
    demo_execution_enabled: bool = False
    demo_risk_per_trade_percent: float = Field(default=0.25, gt=0, le=0.5)
    gold_spot_api_url: str = "https://api.gold-api.com/price/XAU"
    market_quote_max_age_seconds: int = Field(default=300, ge=30, le=3600)
    mt5_bridge_token: str = ""
    manual_trade_pin: str = ""  # Required for authenticated manual DEMO requests from the Android app.
    broker_gold_symbol: str = "GOLD"
    economic_calendar_api_key: str = ""  # Legacy key for point-in-time historical imports only.
    xoomar_api_key: str = ""  # Optional; keyless calendar access is supported.
    fred_api_key: str = ""
    economic_calendar_base_url: str = "https://api.tradingeconomics.com"
    news_blackout_before_minutes: int = Field(default=30, ge=0, le=240)
    news_blackout_after_minutes: int = Field(default=30, ge=0, le=240)
    news_refresh_interval_seconds: int = Field(default=300, ge=60, le=3600)
    news_cache_max_age_seconds: int = Field(default=1200, ge=300, le=7200)


settings = Settings()
app = FastAPI(
    title="GoldAI Trader API",
    description="Demo-first XM GOLD analysis and risk-control service.",
    version="0.6.2",
)

from .news_reaction import router as news_reaction_router

app.include_router(news_reaction_router)


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
_ALLOW_H4_SESSION_GAP: ContextVar[bool] = ContextVar("allow_h4_session_gap", default=False)


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


class ManualDemoTradeRequest(BaseModel):
    direction: Direction
    lots: float = Field(gt=0, le=100)
    stop_loss_points: float = Field(gt=0, le=10_000_000)
    take_profit_points: float = Field(gt=0, le=10_000_000)


_manual_trade_queue: list[dict] = []


def _require_manual_trade_pin(pin: str | None) -> None:
    configured = settings.manual_trade_pin.strip()
    if len(configured) < 16 or not pin or not secrets.compare_digest(pin, configured):
        raise HTTPException(status_code=401, detail="Manual trading is not configured or the security PIN is invalid.")


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
    execution_authorized: bool = False
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
        "demo_execution_enabled": settings.demo_execution_enabled,
        "demo_risk_per_trade_percent": settings.demo_risk_per_trade_percent,
        "manual_demo_trading_configured": len(settings.manual_trade_pin.strip()) >= 16,
        "supported_initial_symbol": settings.broker_gold_symbol,
        "analysis_engine_version": "0.6.2",
        "analysis_layers": [
            "multi_timeframe_m5_m15_h1_h4",
            "wilder_rsi_macd_adx_atr",
            "confirmed_market_structure_bos_liquidity_fvg",
            "closed_candle_microstructure",
            "mt5_closed_candle_integrity_and_monotonic_feed",
            "optional_tick_volume_participation",
            "explainable_weighted_signal_fusion",
            "freshness_adjusted_macro_pressure_gate",
            "macro_and_high_impact_news_fail_closed_gates",
        ],
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
            # The broad trade-weighted dollar index is released on a weekly
            # schedule (often Friday data with the next FRED update on Monday).
            # Allow a narrow 10-calendar-day window for this series only;
            # keep the stricter 7-day limit for daily Treasury yield series.
            max_age_days = 10 if name == "broad_us_dollar_index" else 7
            if age_days < 0 or age_days > max_age_days:
                raise ValueError(
                    f"FRED series {name} is stale (age_days={age_days}, max_age_days={max_age_days})."
                )
            rows[0]["age_days"] = age_days
            rows[0]["max_age_days"] = max_age_days

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

        # Transparent heuristic pressure score. This is deliberately NOT a
        # probability and is not calibrated against forward GOLD returns.
        def clamp_unit(value: float) -> float:
            return max(-1.0, min(1.0, value))

        real_component = clamp_unit(real_change_bp / 10.0)
        dollar_component = clamp_unit(dollar_change_pct / 0.20)
        nominal_component = clamp_unit(nominal_change_bp / 15.0)
        raw_pressure = (
            0.50 * real_component
            + 0.35 * dollar_component
            + 0.15 * nominal_component
        )
        freshness_factors = {
            name: max(0.0, 1.0 - 0.35 * rows[0]["age_days"] / rows[0]["max_age_days"])
            for name, rows in observations.items()
        }
        freshness_factor = (
            0.50 * freshness_factors["real_10y_yield"]
            + 0.35 * freshness_factors["broad_us_dollar_index"]
            + 0.15 * freshness_factors["nominal_10y_yield"]
        )
        adjusted_pressure = raw_pressure * freshness_factor
        pressure_score = round(50.0 + 50.0 * adjusted_pressure, 1)
        strength_score = round(abs(adjusted_pressure) * 100.0, 1)
        strength_label = (
            "STRONG_HEURISTIC" if strength_score >= 60
            else "MODERATE_HEURISTIC" if strength_score >= 30
            else "WEAK_HEURISTIC"
        )
        if adjusted_pressure > 0.10:
            scored_bias = "GOLD_HEADWIND"
        elif adjusted_pressure < -0.10:
            scored_bias = "GOLD_SUPPORTIVE"
        else:
            scored_bias = "MIXED_OR_NEUTRAL"

        return {
            "bias": bias,
            "explanation": explanation,
            "observations": observations,
            "changes": {
                "nominal_10y_yield_basis_points": round(nominal_change_bp, 2),
                "real_10y_yield_basis_points": round(real_change_bp, 2),
                "broad_us_dollar_index_percent": round(dollar_change_pct, 4),
            },
            "confidence_engine": {
                "version": "heuristic_v1",
                "directional_bias": scored_bias,
                "pressure_score_0_to_100": pressure_score,
                "strength_score_0_to_100": strength_score,
                "strength_label": strength_label,
                "freshness_factor_0_to_1": round(freshness_factor, 3),
                "series_freshness_factors": {key: round(value, 3) for key, value in freshness_factors.items()},
                "component_scores_minus1_to_plus1": {
                    "real_yield": round(real_component, 3),
                    "broad_us_dollar": round(dollar_component, 3),
                    "nominal_yield": round(nominal_component, 3),
                },
                "weights": {"real_yield": 0.50, "broad_us_dollar": 0.35, "nominal_yield": 0.15},
                "calibration_status": "NOT_CALIBRATED",
                "is_probability": False,
                "is_trade_entry_signal": False,
                "note": "Heuristic daily macro pressure score only. Requires out-of-sample calibration against broker GOLD returns before predictive use.",
            },
            "source": "Federal Reserve Bank of St. Louis FRED",
            "generated_at": now,
            "execution_enabled": False,
            "note": "Daily macro context only; not an intraday signal or a calibrated predictor of gold returns.",
        }
    except HTTPException:
        raise
    except httpx.HTTPStatusError as exc:
        # Never log the request URL: FRED API keys are sent as query parameters.
        logger.error("FRED macro request failed with HTTP status %s.", exc.response.status_code)
        raise HTTPException(status_code=503, detail="FRED macro data is unavailable, stale, or invalid; macro filter is paused.") from exc
    except httpx.HTTPError as exc:
        logger.error("FRED macro network request failed (%s).", type(exc).__name__)
        raise HTTPException(status_code=503, detail="FRED macro data is unavailable, stale, or invalid; macro filter is paused.") from exc
    except (ValueError, TypeError, KeyError) as exc:
        # The validation reason is safe: it contains series/date metadata, never credentials.
        logger.error("FRED macro data validation failed: %s", str(exc)[:240])
        raise HTTPException(status_code=503, detail="FRED macro data is unavailable, stale, or invalid; macro filter is paused.") from exc



_NEWS_CALENDAR_CACHE: list[dict] = []
_NEWS_HEADLINES_CACHE: list[dict] = []
_NEWS_CALENDAR_LAST_ATTEMPT: datetime | None = None
_NEWS_CALENDAR_LAST_SUCCESS: datetime | None = None
_NEWS_CALENDAR_LAST_ERROR: str | None = None
_NEWS_HEADLINES_LAST_SUCCESS: datetime | None = None
_NEWS_HEADLINES_ERRORS: list[str] = []
_NEWS_REFRESH_LOCK = asyncio.Lock()
_NEWS_FEEDS = [
    ("BLS Employment Situation", "https://www.bls.gov/feed/empsit.rss", "BLS", "LABOR"),
    ("BLS Consumer Price Index", "https://www.bls.gov/feed/cpi.rss", "BLS", "INFLATION"),
    ("BLS Producer Price Index", "https://www.bls.gov/feed/ppi.rss", "BLS", "INFLATION"),
    ("BLS Job Openings and Labor Turnover", "https://www.bls.gov/feed/jolts.rss", "BLS", "LABOR"),
    ("Federal Reserve Monetary Policy", "https://www.federalreserve.gov/feeds/press_monetary.xml", "Federal Reserve", "MONETARY_POLICY"),
]


def _parse_news_feed_xml(xml_text: str, feed_name: str, source: str, category: str) -> list[dict]:
    """Parse RSS 2.0 or Atom headlines without executing embedded markup."""
    root = ET.fromstring(xml_text)
    entries = [node for node in root.iter() if node.tag.rsplit("}", 1)[-1].lower() in {"item", "entry"}]
    parsed: list[dict] = []
    for entry in entries[:40]:
        fields: dict[str, str] = {}
        for node in entry.iter():
            key = node.tag.rsplit("}", 1)[-1].lower()
            value = (node.text or "").strip()
            if key == "link" and not value:
                value = str(node.attrib.get("href") or "").strip()
            if value and key not in fields:
                fields[key] = value
        title = fields.get("title")
        link = fields.get("link") or fields.get("guid") or fields.get("id")
        raw_date = fields.get("pubdate") or fields.get("published") or fields.get("updated") or fields.get("date")
        if not title or not link or not raw_date:
            continue
        published = None
        try:
            published = datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
        except ValueError:
            try:
                published = parsedate_to_datetime(raw_date)
            except (TypeError, ValueError, OverflowError):
                continue
        if published is None or published.tzinfo is None:
            continue
        published = published.astimezone(timezone.utc)
        if published > datetime.now(timezone.utc) + timedelta(minutes=10):
            continue
        summary = fields.get("description") or fields.get("summary") or fields.get("encoded") or ""
        summary = re.sub(r"<[^>]*>", " ", unescape(summary))
        summary = re.sub(r"\s+", " ", summary).strip()[:360]
        parsed.append({
            "id": link,
            "title": title[:240],
            "summary": summary,
            "url": link,
            "published_at": published,
            "source": source,
            "feed": feed_name,
            "category": category,
            "importance": "HIGH",
            "directional_bias": "NOT_ASSESSED",
            "directional_note": "عنوان رسمي؛ لا يتم استنتاج اتجاه الذهب من العنوان وحده.",
        })
    return parsed


async def _load_official_economic_headlines(client: httpx.AsyncClient) -> tuple[list[dict], list[str], int]:
    async def fetch_one(feed: tuple[str, str, str, str]) -> tuple[list[dict], str | None]:
        name, url, source, category = feed
        try:
            response = await client.get(url, headers={"Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml"})
            response.raise_for_status()
            return _parse_news_feed_xml(response.text, name, source, category), None
        except (httpx.HTTPError, ET.ParseError, ValueError):
            logger.warning("Official economic news feed failed: %s", name)
            return [], name

    results = await asyncio.gather(*(fetch_one(feed) for feed in _NEWS_FEEDS))
    all_items: dict[str, dict] = {}
    errors: list[str] = []
    successful_feeds = 0
    for items, error in results:
        if error:
            errors.append(error)
        else:
            successful_feeds += 1
        for item in items:
            all_items[item["id"]] = item
    headlines = sorted(all_items.values(), key=lambda item: item["published_at"], reverse=True)[:100]
    return headlines, errors, successful_feeds


def _news_cache_age_seconds(at: datetime | None, now: datetime | None = None) -> int | None:
    if at is None:
        return None
    current = now or datetime.now(timezone.utc)
    return max(0, int((current - at).total_seconds()))


def _news_engine_status() -> dict:
    now = datetime.now(timezone.utc)
    calendar_age = _news_cache_age_seconds(_NEWS_CALENDAR_LAST_SUCCESS, now)
    headlines_age = _news_cache_age_seconds(_NEWS_HEADLINES_LAST_SUCCESS, now)
    calendar_stale = calendar_age is None or calendar_age > settings.news_cache_max_age_seconds
    headline_stale = headlines_age is None or headlines_age > settings.news_cache_max_age_seconds
    future_events = [event for event in _NEWS_CALENDAR_CACHE if event["date"] > now]
    next_event = min(future_events, key=lambda event: event["date"]) if future_events else None
    return {
        "engine": "GoldAI Economic News Engine",
        "scheduler": "GitHub Actions scheduled worker + in-process safety refresh",
        "poll_interval_seconds": settings.news_refresh_interval_seconds,
        "calendar_source": "XOOMAR (agency-published US release schedules)",
        "calendar_last_attempt_at": _NEWS_CALENDAR_LAST_ATTEMPT,
        "calendar_last_success_at": _NEWS_CALENDAR_LAST_SUCCESS,
        "calendar_age_seconds": calendar_age,
        "calendar_stale": calendar_stale,
        "calendar_event_count": len(_NEWS_CALENDAR_CACHE),
        "calendar_last_error": _NEWS_CALENDAR_LAST_ERROR,
        "headlines_sources": ["BLS official RSS", "Federal Reserve monetary-policy RSS"],
        "headlines_last_success_at": _NEWS_HEADLINES_LAST_SUCCESS,
        "headlines_age_seconds": headlines_age,
        "headlines_stale": headline_stale,
        "headline_count": len(_NEWS_HEADLINES_CACHE),
        "headline_feed_errors": _NEWS_HEADLINES_ERRORS,
        "next_high_impact_event": next_event,
        "calendar_healthy": not calendar_stale,
        "headlines_degraded": headline_stale or bool(_NEWS_HEADLINES_ERRORS),
        "overall_status": "STALE" if calendar_stale else "DEGRADED" if headline_stale or _NEWS_HEADLINES_ERRORS else "HEALTHY",
        "healthy": not calendar_stale and not headline_stale,
        "execution_enabled": False,
    }


async def _refresh_news_engine(force: bool = False, days_ahead: int = 14) -> dict:
    """Refresh the calendar and official headlines, protected against overlapping workers."""
    global _NEWS_CALENDAR_CACHE, _NEWS_HEADLINES_CACHE
    global _NEWS_CALENDAR_LAST_ATTEMPT, _NEWS_CALENDAR_LAST_SUCCESS
    global _NEWS_CALENDAR_LAST_ERROR, _NEWS_HEADLINES_LAST_SUCCESS, _NEWS_HEADLINES_ERRORS

    async with _NEWS_REFRESH_LOCK:
        now = datetime.now(timezone.utc)
        attempt_age = _news_cache_age_seconds(_NEWS_CALENDAR_LAST_ATTEMPT, now)
        interval = settings.news_refresh_interval_seconds
        if not force and attempt_age is not None and attempt_age < interval - 30:
            return _news_engine_status()

        _NEWS_CALENDAR_LAST_ATTEMPT = now
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            calendar_error: str | None = None
            try:
                events = await load_xoomar_high_impact_events(
                    days_ahead=days_ahead,
                    api_key=settings.xoomar_api_key.strip(),
                )
                _NEWS_CALENDAR_CACHE = events
                _NEWS_CALENDAR_LAST_SUCCESS = datetime.now(timezone.utc)
                _NEWS_CALENDAR_LAST_ERROR = None
            except ValueError as exc:
                calendar_error = f"VALIDATION: {str(exc)[:180]}"
            except httpx.HTTPStatusError as exc:
                calendar_error = f"HTTP_STATUS: {exc.response.status_code}"
            except httpx.HTTPError as exc:
                calendar_error = f"NETWORK: {type(exc).__name__}"
            except Exception as exc:
                calendar_error = f"UNEXPECTED: {type(exc).__name__}"
            if calendar_error:
                _NEWS_CALENDAR_LAST_ERROR = calendar_error
                logger.warning("Economic calendar refresh failed: %s", calendar_error)

            headlines, feed_errors, successful_feeds = await _load_official_economic_headlines(client)
            if successful_feeds > 0:
                _NEWS_HEADLINES_CACHE = headlines
                _NEWS_HEADLINES_LAST_SUCCESS = datetime.now(timezone.utc)
            _NEWS_HEADLINES_ERRORS = feed_errors

        if calendar_error:
            raise HTTPException(
                status_code=503,
                detail="Economic calendar refresh failed; news-sensitive decisions remain blocked.",
            )
        return _news_engine_status()


async def _load_us_high_impact_events(days_ahead: int = 7) -> list[dict]:
    """Return recently refreshed events; never treat a stale cache as a clear calendar."""
    if not 1 <= days_ahead <= 14:
        raise HTTPException(status_code=422, detail="days_ahead must be between 1 and 14.")
    age = _news_cache_age_seconds(_NEWS_CALENDAR_LAST_SUCCESS)
    if age is None or age >= settings.news_refresh_interval_seconds - 30:
        try:
            await _refresh_news_engine(force=False, days_ahead=days_ahead)
        except HTTPException:
            pass
    age = _news_cache_age_seconds(_NEWS_CALENDAR_LAST_SUCCESS)
    if age is None or age > settings.news_cache_max_age_seconds:
        raise HTTPException(
            status_code=503,
            detail="Economic calendar is unavailable or stale; news-sensitive analysis is paused (fail-closed).",
        )
    now = datetime.now(timezone.utc)
    return [
        event for event in _NEWS_CALENDAR_CACHE
        if now - timedelta(days=1) <= event["date"] <= now + timedelta(days=days_ahead)
    ]


async def _verify_github_news_refresh_token(token: str) -> dict:
    """Verify GitHub Actions OIDC and pin the caller to this repository/workflow/main branch."""
    if not token:
        raise HTTPException(status_code=401, detail="Missing GitHub Actions identity token.")
    try:
        header = jwt.get_unverified_header(token)
        if header.get("alg") != "RS256" or not header.get("kid"):
            raise ValueError("Unsupported token header.")
        async with httpx.AsyncClient(timeout=8.0) as client:
            response = await client.get("https://token.actions.githubusercontent.com/.well-known/jwks")
            response.raise_for_status()
            keys = response.json().get("keys", [])
        jwk = next((item for item in keys if item.get("kid") == header["kid"]), None)
        if not jwk:
            raise ValueError("Unknown signing key.")
        public_key = jwt.algorithms.RSAAlgorithm.from_jwk(__import__("json").dumps(jwk))
        claims = jwt.decode(
            token,
            public_key,
            algorithms=["RS256"],
            audience="goldai-trader-news-refresh",
            issuer="https://token.actions.githubusercontent.com",
            leeway=60,
            options={"require": ["exp", "iat", "iss", "aud"]},
        )
        if claims.get("repository") != "mahdialloui01-art/GoldAI-Trader-":
            raise ValueError("Untrusted repository.")
        if claims.get("ref") != "refs/heads/main":
            raise ValueError("Untrusted branch.")
        if claims.get("workflow_ref") != "mahdialloui01-art/GoldAI-Trader-/.github/workflows/ci.yml@refs/heads/main":
            raise ValueError("Untrusted workflow.")
        if claims.get("event_name") not in {"schedule", "workflow_dispatch"}:
            raise ValueError("Untrusted workflow event.")
        return claims
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("News refresh identity validation failed: %s", type(exc).__name__)
        raise HTTPException(status_code=401, detail="GitHub Actions identity token is invalid or not authorized.") from exc


@app.on_event("startup")
async def _start_news_background_refresh() -> None:
    async def worker() -> None:
        while True:
            try:
                await _refresh_news_engine(force=False)
            except Exception as exc:
                logger.warning("Background news refresh did not complete: %s", type(exc).__name__)
            await asyncio.sleep(settings.news_refresh_interval_seconds)
    asyncio.create_task(worker())


@app.get("/api/v1/news/status")
def economic_news_engine_status() -> dict:
    """Public health/status endpoint; exposes no credentials."""
    return _news_engine_status()


@app.get("/api/v1/news/feed")
async def economic_news_headlines() -> dict:
    """Return automatically collected headlines from official US institutions."""
    age = _news_cache_age_seconds(_NEWS_HEADLINES_LAST_SUCCESS)
    if age is None or age > settings.news_cache_max_age_seconds:
        try:
            await _refresh_news_engine(force=False)
        except HTTPException:
            pass
    age = _news_cache_age_seconds(_NEWS_HEADLINES_LAST_SUCCESS)
    return {
        "status": "OK" if age is not None and age <= settings.news_cache_max_age_seconds else "DEGRADED",
        "headline_count": len(_NEWS_HEADLINES_CACHE),
        "headlines": _NEWS_HEADLINES_CACHE[:50],
        "last_success_at": _NEWS_HEADLINES_LAST_SUCCESS,
        "age_seconds": age,
        "feed_errors": _NEWS_HEADLINES_ERRORS,
        "directional_bias": "NOT_ASSESSED",
        "note": "Official releases and headlines are monitored automatically. Headline text alone does not determine BUY or SELL.",
        "execution_enabled": False,
    }


@app.post("/api/v1/news/refresh")
async def refresh_economic_news_engine(authorization: str | None = Header(default=None)) -> dict:
    """Refresh endpoint restricted to this repository's GitHub Actions OIDC identity."""
    token = (authorization or "").removeprefix("Bearer ").strip()
    await _verify_github_news_refresh_token(token)
    status = await _refresh_news_engine(force=False)
    return {"accepted": True, "refresh": status}


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
        "source": "XOOMAR (aggregated US agency schedules)",
        "source_url": "https://xoomar.com/markets/api/calendar",
        "attribution": "Data: XOOMAR. " + (events[0]["source_attribution"] if events else "Release schedules are aggregated from the issuing US agencies."),
        "forecast_available": False,
        "execution_enabled": False,
        "note": "Forecast consensus is not provided. Verify release times with the issuing agency; this calendar is not an order signal.",
    }


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
        "source": "XOOMAR (aggregated US agency schedules)",
        "source_url": "https://xoomar.com/markets/api/calendar",
        "attribution": "Data: XOOMAR. " + (events[0]["source_attribution"] if events else "Release schedules are aggregated from the issuing US agencies."),
        "forecast_available": False,
        "execution_enabled": False,
        "note": "Forecast consensus is not provided. Verify release times with the issuing agency; this calendar is not an order signal.",
    }


@app.get("/api/v1/analysis/news-filter")
async def economic_news_filter() -> dict:
    """Provide economic-calendar context as advisory information, never as a standalone entry gate."""
    now = datetime.now(timezone.utc)
    before = settings.news_blackout_before_minutes
    after = settings.news_blackout_after_minutes
    try:
        events = await _load_us_high_impact_events(7)
    except HTTPException as exc:
        return {
            "status": "UNAVAILABLE",
            "decision": "ADVISORY",
            "trading_allowed": True,
            "reason": "Economic calendar unavailable; technical and risk checks remain authoritative.",
            "warning": f"Calendar provider returned HTTP {exc.status_code}.",
            "blackout_before_minutes": before,
            "blackout_after_minutes": after,
            "active_events": [],
            "upcoming_events": [],
            "calendar_source": "XOOMAR",
            "checked_at": now,
            "execution_enabled": False,
            "fail_closed": False,
        }
    active = []
    for event in events:
        delta_minutes = (event["date"] - now).total_seconds() / 60
        same_estimated_day = event["date_span"] == "1" and event["date"].date() == now.date()
        if same_estimated_day or -after <= delta_minutes <= before:
            active.append({**event, "minutes_until_event": round(delta_minutes, 1)})
    upcoming = [
        {**event, "minutes_until_event": round((event["date"] - now).total_seconds() / 60, 1)}
        for event in events
        if (event["date"] - now).total_seconds() > 0
    ][:5]
    return {
        "status": "AVAILABLE",
        "decision": "ADVISORY" if active else "CLEAR",
        "trading_allowed": True,
        "reason": "High-impact US event is near; review volatility and spread." if active else "No high-impact US event is inside the configured advisory window.",
        "blackout_before_minutes": before,
        "blackout_after_minutes": after,
        "active_events": active,
        "upcoming_events": upcoming,
        "calendar_source": "XOOMAR",
        "calendar_source_url": "https://xoomar.com/markets/api/calendar",
        "calendar_attribution": "Data: XOOMAR. " + (events[0]["source_attribution"] if events else "Release schedules are aggregated from the issuing US agencies."),
        "checked_at": now,
        "execution_enabled": False,
        "fail_closed": False,
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
    strategy: str = Field(default="EMA_TREND", pattern="^(EMA_TREND|PULLBACK|BREAKOUT|LIQUIDITY_SWEEP)$")
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



def _candle_microstructure_analysis(candles: list[Candle]) -> dict:
    """Describe closed-candle impulse, EMA geometry and tick-volume participation.

    Tick volume is optional: missing/zero broker volume is reported as unavailable,
    never interpreted as low participation. All features are descriptive heuristics.
    """
    if len(candles) < 26:
        raise ValueError("At least 26 candles are required for microstructure context.")

    last = candles[-1]
    previous = candles[-2]
    candle_range = max(last.high - last.low, 1e-12)
    body = abs(last.close - last.open)
    body_ratio = body / candle_range
    upper_wick = max(0.0, last.high - max(last.open, last.close))
    lower_wick = max(0.0, min(last.open, last.close) - last.low)
    close_location = (last.close - last.low) / candle_range
    candle_direction = (
        "BULLISH" if last.close > last.open
        else "BEARISH" if last.close < last.open
        else "NEUTRAL"
    )

    bullish_engulfing = (
        previous.close < previous.open
        and last.close > last.open
        and last.open <= previous.close
        and last.close >= previous.open
    )
    bearish_engulfing = (
        previous.close > previous.open
        and last.close < last.open
        and last.open >= previous.close
        and last.close <= previous.open
    )
    bullish_pin_bar = (
        lower_wick / candle_range >= 0.55
        and body_ratio <= 0.35
        and close_location >= 0.65
    )
    bearish_pin_bar = (
        upper_wick / candle_range >= 0.55
        and body_ratio <= 0.35
        and close_location <= 0.35
    )

    if bullish_engulfing:
        pattern = "BULLISH_ENGULFING"
    elif bearish_engulfing:
        pattern = "BEARISH_ENGULFING"
    elif bullish_pin_bar:
        pattern = "BULLISH_REJECTION"
    elif bearish_pin_bar:
        pattern = "BEARISH_REJECTION"
    else:
        pattern = "NONE"

    impulse_direction = "NONE"
    if candle_direction == "BULLISH" and body_ratio >= 0.65 and close_location >= 0.80:
        impulse_direction = "BULLISH"
    elif candle_direction == "BEARISH" and body_ratio >= 0.65 and close_location <= 0.20:
        impulse_direction = "BEARISH"

    volumes = [max(0, int(item.tick_volume)) for item in candles[-21:-1]]
    positive_volumes = [value for value in volumes if value > 0]
    current_volume = max(0, int(last.tick_volume))
    if positive_volumes and current_volume > 0:
        median_volume = float(statistics.median(positive_volumes))
        volume_ratio = current_volume / median_volume if median_volume > 0 else None
        if volume_ratio is None:
            volume_state = "UNAVAILABLE"
        elif volume_ratio >= 1.5:
            volume_state = "EXPANSION"
        elif volume_ratio >= 1.1:
            volume_state = "ABOVE_AVERAGE"
        elif volume_ratio <= 0.75:
            volume_state = "BELOW_AVERAGE"
        else:
            volume_state = "NORMAL"
    else:
        median_volume = None
        volume_ratio = None
        volume_state = "UNAVAILABLE"

    volume_confirmation = "NONE"
    if volume_ratio is not None and volume_ratio >= 1.25:
        if candle_direction == "BULLISH":
            volume_confirmation = "BULLISH"
        elif candle_direction == "BEARISH":
            volume_confirmation = "BEARISH"

    closes = [item.close for item in candles]
    ema20_series = _ema_series(closes, 20)
    ema50_series = _ema_series(closes, 50) if len(closes) >= 50 else None
    atr = _atr_wilder(candles)
    ema20_slope_atr = (
        (ema20_series[-1] - ema20_series[-6]) / atr
        if atr > 0 and len(ema20_series) >= 6 else 0.0
    )
    ema_separation_atr = (
        abs(ema20_series[-1] - ema50_series[-1]) / atr
        if atr > 0 and ema50_series else None
    )
    if abs(ema20_slope_atr) >= 0.25 and ema_separation_atr is not None and ema_separation_atr >= 0.15:
        trend_quality = "STRONG"
    elif abs(ema20_slope_atr) >= 0.10 or (
        ema_separation_atr is not None and ema_separation_atr >= 0.10
    ):
        trend_quality = "MODERATE"
    else:
        trend_quality = "WEAK"

    return {
        "candle_direction": candle_direction,
        "body_to_range_ratio": round(body_ratio, 3),
        "close_location_0_to_1": round(close_location, 3),
        "upper_wick_to_range_ratio": round(upper_wick / candle_range, 3),
        "lower_wick_to_range_ratio": round(lower_wick / candle_range, 3),
        "closed_candle_pattern": pattern,
        "impulse_direction": impulse_direction,
        "tick_volume": {
            "current": current_volume,
            "median_previous_20": round(median_volume, 2) if median_volume is not None else None,
            "ratio_to_median": round(volume_ratio, 3) if volume_ratio is not None else None,
            "state": volume_state,
            "directional_confirmation": volume_confirmation,
        },
        "ema20_slope_atr_5_bars": round(ema20_slope_atr, 4),
        "ema20_ema50_separation_atr": (
            round(ema_separation_atr, 4) if ema_separation_atr is not None else None
        ),
        "trend_quality": trend_quality,
        "note": "Closed-candle and broker tick-volume heuristics; volume may be unavailable and none of these fields is a probability.",
    }


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
            direction = None
            if request.strategy == "EMA_TREND":
                if ema_fast_value > ema_slow_value and 52 <= rsi[index] <= 68:
                    direction = "BUY"
                elif ema_fast_value < ema_slow_value and 32 <= rsi[index] <= 48:
                    direction = "SELL"
            elif request.strategy == "PULLBACK":
                extension = abs(candle.close - ema_fast_value) / atr[index] if atr[index] else float("inf")
                if extension <= 0.45 and candle.close > candle.open and ema_fast_value > ema_slow_value and rsi[index] >= 50:
                    direction = "BUY"
                elif extension <= 0.45 and candle.close < candle.open and ema_fast_value < ema_slow_value and rsi[index] <= 50:
                    direction = "SELL"
            elif request.strategy in ("BREAKOUT", "LIQUIDITY_SWEEP"):
                prefix = candles[:index + 1]
                pa = _price_action_analysis(prefix, 3, 3, 0.12) if len(prefix) >= 60 else {}
                events = set(pa.get("events", []))
                micro = _candle_microstructure_analysis(prefix) if len(prefix) >= 60 else {}
                if request.strategy == "BREAKOUT":
                    if "BULLISH_BREAK_OF_STRUCTURE" in events and ema_fast_value >= ema_slow_value:
                        direction = "BUY"
                    elif "BEARISH_BREAK_OF_STRUCTURE" in events and ema_fast_value <= ema_slow_value:
                        direction = "SELL"
                else:
                    if "SELL_SIDE_LIQUIDITY_SWEEP" in events and micro.get("impulse_direction") == "BULLISH":
                        direction = "BUY"
                    elif "BUY_SIDE_LIQUIDITY_SWEEP" in events and micro.get("impulse_direction") == "BEARISH":
                        direction = "SELL"
            pending = {"direction": direction, "atr": atr[index]} if direction else None

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
        "strategy": request.strategy,
        "strategy_description": {
            "EMA_TREND": "EMA20/EMA50 with Wilder RSI confirmation",
            "PULLBACK": "EMA20 retest within 0.45 ATR with directional candle",
            "BREAKOUT": "Confirmed BOS close with EMA trend filter",
            "LIQUIDITY_SWEEP": "Confirmed opposing-liquidity sweep with directional impulse",
        }[request.strategy],
        "execution_model": "Signal on closed candle; enter at following candle open; stop-first when SL and TP touch in same bar",
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
    h4_session_gap_allowed = (
        timeframe == "H4"
        and _ALLOW_H4_SESSION_GAP.get()
        and latest_age <= 7 * 24 * 60 * 60
    )
    if latest_age < -30 or (latest_age > freshness_limit and not h4_session_gap_allowed):
        raise HTTPException(status_code=422, detail=f"Latest {timeframe} candle is stale or from the future; age_seconds={int(latest_age)}, limit_seconds={freshness_limit}.")

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
            symbol=request.symbol, timeframe="H4", candles=request.h4,
        )),
    }
    directions = {frame: result["direction"] for frame, result in analyses.items()}
    reasons: list[str] = []

    # Role-based alignment: H4/H1 establish the bias, M15 validates structure,
    # and M5 remains the mandatory entry trigger. M15 may be technically neutral
    # only when its confirmed price structure independently supports the bias.
    higher_bias = directions["H4"]
    h1_bias = directions["H1"]
    m15_direction = directions["M15"]
    m5_trigger = directions["M5"]
    final_direction = Direction.WAIT
    alignment_details = {
        "higher_timeframe_bias": higher_bias.value if isinstance(higher_bias, Direction) else str(higher_bias),
        "h1_confirms_h4": h1_bias == higher_bias and higher_bias in (Direction.BUY, Direction.SELL),
        "m15_technical_direction": m15_direction.value if isinstance(m15_direction, Direction) else str(m15_direction),
        "m5_entry_trigger": m5_trigger.value if isinstance(m5_trigger, Direction) else str(m5_trigger),
        "policy": "H4+H1 bias; M15 direction or confirmed structure; M5 trigger required",
    }

    if higher_bias not in (Direction.BUY, Direction.SELL) or h1_bias != higher_bias:
        reasons.append("H4 وH1 لا يحددان اتجاهاً واحداً مؤكداً؛ لا توجد أفضلية إطار أعلى.")
    elif m5_trigger != higher_bias:
        reasons.append("إشارة M5 لا تؤكد اتجاه H4/H1؛ ننتظر محفز دخول واضحاً على M5.")
    elif m15_direction == higher_bias:
        final_direction = higher_bias
        reasons.append("H4 وH1 متوافقان، وM15 يؤكد الاتجاه، وM5 يعطي محفز الدخول.")
    elif m15_direction == Direction.WAIT:
        m15_structure = _price_action_analysis(request.m15, 3, 3, 0.12)
        m15_events = m15_structure.get("events", [])
        structure_matches = (
            (higher_bias == Direction.BUY and (
                m15_structure.get("market_structure") == "UPTREND"
                or "BULLISH_BREAK_OF_STRUCTURE" in m15_events
            ))
            or (higher_bias == Direction.SELL and (
                m15_structure.get("market_structure") == "DOWNTREND"
                or "BEARISH_BREAK_OF_STRUCTURE" in m15_events
            ))
        )
        alignment_details["m15_structure"] = m15_structure.get("market_structure")
        alignment_details["m15_structure_confirms"] = bool(structure_matches)
        if structure_matches:
            final_direction = higher_bias
            reasons.append("M15 محايد بالمؤشرات لكنه يؤكد الاتجاه عبر بنية السعر؛ وM5 أعطى محفز الدخول.")
        else:
            reasons.append("M15 محايد ولا توجد بنية سعر مؤكدة تدعم اتجاه H4/H1.")
    else:
        reasons.append("M15 يعاكس اتجاه H4/H1؛ تم رفض الإشارة رغم توافق بقية الأطر.")

    if any(result["spread_atr_ratio"] > 0.15 for result in analyses.values()):
        final_direction = Direction.WAIT
        reasons.append("فلتر السبريد منع الصفقة لأن تكلفته مرتفعة مقارنة بتذبذب أحد الأطر.")

    confidence_score = 0
    if final_direction != Direction.WAIT:
        confidence_weights = {"M5": 0.35, "M15": 0.25, "H1": 0.20, "H4": 0.20}
        confidence_score = min(85, round(sum(
            float(analyses[frame]["confidence_score"]) * weight
            for frame, weight in confidence_weights.items()
        )))

    return {
        "symbol": settings.broker_gold_symbol.upper(),
        "direction": final_direction,
        "confidence_score": confidence_score,
        "confidence_note": "مؤشر توافق داخلي موزون حسب دور الإطار؛ ليس احتمال نجاح أو نسبة ربح.",
        "timeframes": analyses,
        "alignment": directions,
        "alignment_details": alignment_details,
        "reasons": reasons,
        "execution_enabled": False,
        "generated_at": datetime.now(timezone.utc),
    }


@app.post("/api/v1/analysis/signal-fusion")
async def signal_fusion_analysis(request: MultiTimeframeAnalysisRequest) -> dict:
    """Explainable multi-layer GOLD confluence score; never authorizes order execution."""
    if request.symbol.upper() != settings.broker_gold_symbol.upper():
        raise HTTPException(status_code=400, detail="Unsupported broker symbol.")

    # Reuse the existing validated technical engine and closed-candle price-action layer.
    mtf = multi_timeframe_analysis(request)
    analyses = mtf["timeframes"]
    candle_sets = {"M5": request.m5, "M15": request.m15, "H1": request.h1, "H4": request.h4}
    weights = {"M5": 0.15, "M15": 0.20, "H1": 0.30, "H4": 0.35}
    signed_components: dict[str, float] = {}
    price_action: dict[str, dict] = {}
    microstructure: dict[str, dict] = {}
    reasons: list[str] = []

    for frame, candles in candle_sets.items():
        result = analyses[frame]
        direction = result["direction"]
        sign = 1.0 if direction == Direction.BUY else -1.0 if direction == Direction.SELL else 0.0
        technical_strength = float(result.get("confidence_score", 0)) / 100.0
        signed_components[frame] = sign * technical_strength * weights[frame]

        pa = _price_action_analysis(candles, 3, 3, 0.12)
        micro = _candle_microstructure_analysis(candles)
        price_action[frame] = {
            "market_structure": pa["market_structure"],
            "events": pa["events"],
            "order_block_candidate": pa["order_block_candidate"],
            "latest_fvg": {
                "bullish": pa["fair_value_gaps"]["bullish"][-1:] ,
                "bearish": pa["fair_value_gaps"]["bearish"][-1:] ,
            },
        }
        microstructure[frame] = micro

        structure_sign = 1.0 if pa["market_structure"] == "UPTREND" else -1.0 if pa["market_structure"] == "DOWNTREND" else 0.0
        signed_components[frame] += structure_sign * weights[frame] * 0.12

        pattern = micro["closed_candle_pattern"]
        if pattern in ("BULLISH_ENGULFING", "BULLISH_REJECTION") or micro["impulse_direction"] == "BULLISH":
            signed_components[frame] += weights[frame] * 0.06
        elif pattern in ("BEARISH_ENGULFING", "BEARISH_REJECTION") or micro["impulse_direction"] == "BEARISH":
            signed_components[frame] -= weights[frame] * 0.06

        volume_bias = micro["tick_volume"]["directional_confirmation"]
        if volume_bias == "BULLISH":
            signed_components[frame] += weights[frame] * 0.04
        elif volume_bias == "BEARISH":
            signed_components[frame] -= weights[frame] * 0.04

    raw_score = max(-1.0, min(1.0, sum(signed_components.values())))
    score_0_100 = round(50.0 + 50.0 * raw_score, 1)
    directions = {frame: analyses[frame]["direction"].value for frame in ("M5", "M15", "H1", "H4")}

    # Higher-timeframe conflict is a hard veto; lower timeframes cannot overrule H4/H1.
    higher_aligned = directions["H4"] in ("BUY", "SELL") and directions["H1"] == directions["H4"]
    entry_aligned = directions["M15"] == directions["H4"] and directions["M5"] in (directions["H4"], "WAIT")
    final_direction = Direction.WAIT
    if not higher_aligned:
        reasons.append("H4 وH1 غير متوافقين؛ تم إيقاف الإشارة.")
    elif not entry_aligned:
        reasons.append("M15 لا يؤكد اتجاه الإطار الأعلى أو M5 يعاكسه.")
    elif raw_score >= 0.25 and directions["H4"] == "BUY":
        final_direction = Direction.BUY
    elif raw_score <= -0.25 and directions["H4"] == "SELL":
        final_direction = Direction.SELL
    else:
        reasons.append("قوة توافق الطبقات أقل من الحد المطلوب؛ الانتظار أفضل.")

    # News and macro gates are mandatory. Missing/stale data blocks directional output.
    gate_status: dict[str, object] = {}
    try:
        news = await economic_news_filter()
        gate_status["news"] = news["decision"]
        if news["decision"] != "CLEAR":
            final_direction = Direction.WAIT
            reasons.append("فلتر الأخبار عالي التأثير يمنع إصدار إشارة تداول.")
    except HTTPException:
        gate_status["news"] = "UNAVAILABLE"
        final_direction = Direction.WAIT
        reasons.append("تقويم الأخبار غير متاح أو قديم؛ تم إيقاف الإشارة احتياطياً.")

    try:
        macro = await gold_macro_analysis()
        macro_engine = macro.get("confidence_engine", {})
        # Prefer the weighted, freshness-adjusted macro assessment; retain the
        # broad bias only as a defensive fallback for older response formats.
        macro_bias = macro_engine.get("directional_bias") or macro.get("bias", "MIXED_OR_NEUTRAL")
        gate_status["macro"] = {
            "directional_bias": macro_bias,
            "pressure_score_0_to_100": macro_engine.get("pressure_score_0_to_100"),
            "strength_score_0_to_100": macro_engine.get("strength_score_0_to_100"),
            "freshness_factor_0_to_1": macro_engine.get("freshness_factor_0_to_1"),
            "calibration_status": macro_engine.get("calibration_status", "UNKNOWN"),
        }
        if macro_bias == "GOLD_HEADWIND" and final_direction == Direction.BUY:
            final_direction = Direction.WAIT
            reasons.append("درجة الضغط الاقتصادي المعدّلة بحداثة البيانات تعاكس الشراء؛ تم إيقاف الإشارة.")
        elif macro_bias == "GOLD_SUPPORTIVE" and final_direction == Direction.SELL:
            final_direction = Direction.WAIT
            reasons.append("درجة الدعم الاقتصادي المعدّلة بحداثة البيانات تعاكس البيع؛ تم إيقاف الإشارة.")
    except HTTPException:
        gate_status["macro"] = "UNAVAILABLE"
        final_direction = Direction.WAIT
        reasons.append("بيانات FRED الاقتصادية غير متاحة أو قديمة؛ تم إيقاف الإشارة احتياطياً.")

    quote = _fresh_mt5_quote()
    spread_block = any(float(item["spread_atr_ratio"]) > 0.15 for item in analyses.values())
    if spread_block:
        final_direction = Direction.WAIT
        reasons.append("السبريد مرتفع مقارنة بـ ATR في إطار واحد على الأقل.")

    if final_direction != Direction.WAIT:
        reasons.insert(0, "توافق الاتجاه والأطر الزمنية والبنية والشموع اجتاز الفلاتر.")
    else:
        reasons.append("WAIT لا يعني انعكاساً؛ يعني عدم توفر شروط كافية لإشارة موثوقة.")

    return {
        "symbol": settings.broker_gold_symbol.upper(),
        "direction": final_direction,
        "confluence_score_0_to_100": score_0_100,
        "score_note": "درجة توافق قواعد داخلية وليست احتمال ربح أو نسبة نجاح.",
        "timeframe_weights": weights,
        "timeframe_directions": directions,
        "technical_components": {key: round(value, 4) for key, value in signed_components.items()},
        "price_action": price_action,
        "closed_candle_microstructure": microstructure,
        "safety_gates": gate_status,
        "broker_spread_points": quote["spread_points"],
        "reasons": reasons,
        "execution_enabled": False,
        "generated_at": datetime.now(timezone.utc),
        "disclaimer": "بحث وتحليل تجريبي فقط؛ لا يرسل أوامر ولا يضمن النتائج.",
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
        event_time = item.released_at.astimezone(timezone.utc)
        if event_time > datetime.now(timezone.utc):
            raise HTTPException(status_code=422, detail="Historical study cannot include future release timestamps.")
        normalized_times.append(event_time)
        numeric_values = (
            item.consensus, item.actual, item.surprise_scale, item.gold_before,
            item.gold_1m, item.gold_5m, item.gold_15m, item.gold_60m,
            item.spread_points, item.point, item.slippage_points,
        )
        if not all(math.isfinite(value) for value in numeric_values):
            raise HTTPException(status_code=422, detail="Historical observations must contain finite numeric values.")
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
            "forecast_leakage_verified": False,
            "data_provenance": "CLIENT_SUPPLIED_OBSERVATIONS_NOT_INDEPENDENTLY_VERIFIED",
            "price_timestamp_integrity_verified": False,
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
    if end_date > datetime.now(timezone.utc).date():
        raise HTTPException(status_code=422, detail="Historical calendar end_date cannot be in the future.")
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


@app.post("/api/v1/analysis/gold-decision")
async def gold_decision_analysis(request: MultiTimeframeAnalysisRequest) -> dict:
    """Combine broker multi-timeframe, structure, regime, macro and news gates into one paper-only decision."""
    if request.symbol.upper() != settings.broker_gold_symbol.upper():
        raise HTTPException(status_code=400, detail=f"Only {settings.broker_gold_symbol} is supported.")

    reasons: list[str] = []
    components: dict = {}
    blockers: list[str] = []

    # Price/technical components are based exclusively on fresh broker candles and quotes.
    try:
        mtf = multi_timeframe_analysis(request)
        components["multi_timeframe"] = mtf
        direction = str(mtf["direction"].value if isinstance(mtf["direction"], Direction) else mtf["direction"])
        if direction == "WAIT":
            blockers.append("MULTI_TIMEFRAME_NOT_ALIGNED")
            reasons.extend(mtf.get("reasons", []))
    except HTTPException as exc:
        direction = "WAIT"
        blockers.append("BROKER_TECHNICAL_DATA_UNAVAILABLE")
        reasons.append(f"تعذر التحقق من التحليل الفني لدى الوسيط (HTTP {exc.status_code}).")

    try:
        regime = market_regime_analysis(MarketRegimeRequest(
            symbol=request.symbol, timeframe="H1", candles=request.h1
        ))
        components["h1_market_regime"] = regime
        if regime["market_regime"] == "HIGH_VOLATILITY":
            blockers.append("HIGH_VOLATILITY")
            reasons.append("تقلب H1 مرتفع جداً؛ تم إيقاف ترشيح الدخول.")
        elif direction == "BUY" and regime["market_regime"] == "TRENDING_DOWN":
            blockers.append("H1_TREND_CONFLICT")
            reasons.append("اتجاه H1 هابط ويتعارض مع شراء M5.")
        elif direction == "SELL" and regime["market_regime"] == "TRENDING_UP":
            blockers.append("H1_TREND_CONFLICT")
            reasons.append("اتجاه H1 صاعد ويتعارض مع بيع M5.")
    except HTTPException as exc:
        blockers.append("H1_REGIME_UNAVAILABLE")
        reasons.append(f"تعذر التحقق من نظام السوق H1 (HTTP {exc.status_code}).")

    try:
        structure = price_action_analysis(PriceActionRequest(
            symbol=request.symbol, timeframe="M15", candles=request.m15
        ))
        components["m15_price_action"] = structure
        market_structure = structure.get("market_structure")
        events = structure.get("events", [])
        if direction == "BUY" and market_structure == "DOWNTREND":
            blockers.append("M15_STRUCTURE_CONFLICT")
            reasons.append("بنية M15 هابطة وتتعارض مع إشارة الشراء.")
        elif direction == "SELL" and market_structure == "UPTREND":
            blockers.append("M15_STRUCTURE_CONFLICT")
            reasons.append("بنية M15 صاعدة وتتعارض مع إشارة البيع.")
        if direction == "BUY" and "BEARISH_BREAK_OF_STRUCTURE" in events:
            blockers.append("M15_BEARISH_STRUCTURE_BREAK")
            reasons.append("ظهر كسر هيكل هابط مؤكد على M15.")
        if direction == "SELL" and "BULLISH_BREAK_OF_STRUCTURE" in events:
            blockers.append("M15_BULLISH_STRUCTURE_BREAK")
            reasons.append("ظهر كسر هيكل صاعد مؤكد على M15.")
    except HTTPException as exc:
        blockers.append("M15_PRICE_ACTION_UNAVAILABLE")
        reasons.append(f"تعذر التحقق من بنية السعر M15 (HTTP {exc.status_code}).")

    # Macro and calendar are fail-closed: missing/stale inputs can never become a green light.
    try:
        macro = await gold_macro_analysis()
        components["macro"] = macro
        macro_bias = macro.get("confidence_engine", {}).get("directional_bias", macro.get("bias"))
        if direction == "BUY" and macro_bias == "GOLD_HEADWIND":
            blockers.append("MACRO_OPPOSES_BUY")
            reasons.append("الضغط الكلي الحالي معاكس للشراء؛ ننتظر توافقاً أفضل.")
        elif direction == "SELL" and macro_bias == "GOLD_SUPPORTIVE":
            blockers.append("MACRO_OPPOSES_SELL")
            reasons.append("الضغط الكلي الحالي داعم للذهب ويتعارض مع البيع.")
        elif macro_bias == "MIXED_OR_NEUTRAL":
            reasons.append("السياق الكلي محايد أو متباين ولا يمنح أفضلية اتجاهية.")
    except HTTPException as exc:
        components["macro"] = {"status": "UNAVAILABLE", "http_status": exc.status_code}
        blockers.append("MACRO_DATA_UNAVAILABLE")
        reasons.append("بيانات FRED الكلية غير متاحة أو قديمة؛ القرار WAIT.")

    try:
        news = await economic_news_filter()
        components["news_filter"] = news
        if news.get("status") == "UNAVAILABLE":
            reasons.append("تنبيه: التقويم الاقتصادي غير متاح؛ لن يمنع الدخول وحده، وتبقى شروط التحليل الفني والمخاطر إلزامية.")
        elif news.get("active_events"):
            reasons.append("تنبيه: يوجد خبر أمريكي عالي التأثير قريب؛ راقب السبريد والتذبذب، لكنه ليس مانع دخول مستقلاً.")
    except HTTPException as exc:
        components["news_filter"] = {"status": "UNAVAILABLE", "http_status": exc.status_code, "decision": "ADVISORY", "trading_allowed": True, "fail_closed": False}
        reasons.append("تنبيه: تعذر تحميل التقويم الاقتصادي؛ لن يمنع الدخول وحده، وتبقى شروط التحليل الفني والمخاطر إلزامية.")

    final_direction = direction if not blockers else "WAIT"
    if final_direction == "WAIT" and not reasons:
        reasons.append("لا توجد موافقات كافية لإصدار قرار.")
    if final_direction != "WAIT":
        reasons.append("اجتازت الإشارة فلاتر الأطر الزمنية والبنية والسياق الكلي والأخبار؛ تبقى مرشحاً ورقياً فقط.")

    return {
        "symbol": settings.broker_gold_symbol.upper(),
        "decision": final_direction,
        "status": "PAPER_CANDIDATE" if final_direction != "WAIT" else "NO_TRADE",
        "blockers": sorted(set(blockers)),
        "reasons": reasons,
        "components": components,
        "policy": {
            "mode": "PAPER_ONLY",
            "live_trading_enabled": False,
            "execution_enabled": False,
            "heuristic_scores_are_probabilities": False,
            "entry_is_not_guaranteed": True,
        },
        "generated_at": datetime.now(timezone.utc),
        "disclaimer": "قرار تحليلي تجريبي فقط. لا يضمن الربح ولا يرسل أوامر. يجب التحقق من أسعار وبيانات الوسيط واختبار الأداء التاريخي قبل أي استخدام مالي.",
    }


def _classify_entry_setups(direction: str, frames: dict[str, list[Candle]], reports: dict[str, dict], spread_atr_ratio: float) -> dict:
    """Rank deterministic closed-candle setup candidates; scores are not probabilities."""
    if direction not in ("BUY", "SELL"):
        return {"selected": None, "candidates": [], "note": "No directional bias; setup scan is diagnostic only."}
    m5 = frames["M5"]
    report = reports["M5"]
    structure = _price_action_analysis(m5, 3, 3, 0.12)
    micro = report.get("microstructure", {})
    atr = float(report.get("atr14") or 0.0)
    close = float(m5[-1].close)
    ema20 = float(report.get("ema20") or close)
    extension = abs(close - ema20) / atr if atr > 0 else float("inf")
    events = set(structure.get("events", []))
    pattern = micro.get("closed_candle_pattern", "NONE")
    impulse = micro.get("impulse_direction", "NONE")
    bullish = direction == "BUY"
    wanted_bos = "BULLISH_BREAK_OF_STRUCTURE" if bullish else "BEARISH_BREAK_OF_STRUCTURE"
    wanted_sweep = "SELL_SIDE_LIQUIDITY_SWEEP" if bullish else "BUY_SIDE_LIQUIDITY_SWEEP"
    wanted_pattern = ("BULLISH_ENGULFING", "BULLISH_REJECTION") if bullish else ("BEARISH_ENGULFING", "BEARISH_REJECTION")
    candidates = []

    # Pullback: price returns near EMA20 without excessive extension and closes with directional intent.
    pullback_score = 0
    if extension <= 0.45:
        pullback_score += 2
    if pattern in wanted_pattern or impulse == ("BULLISH" if bullish else "BEARISH"):
        pullback_score += 2
    if report.get("market_structure") == ("UPTREND" if bullish else "DOWNTREND"):
        pullback_score += 1
    if pullback_score:
        candidates.append({"strategy": "PULLBACK", "score": pullback_score, "max_score": 5,
                           "evidence": ["M5 قريب من EMA20" if extension <= 0.45 else "السعر ليس داخل منطقة إعادة الاختبار المثالية",
                                        "شمعة/اندفاع مؤيد" if pullback_score >= 2 and (pattern in wanted_pattern or impulse == ("BULLISH" if bullish else "BEARISH")) else "تأكيد الشمعة غير مكتمل"]})

    # Breakout: only a confirmed close through a previously confirmed swing level.
    if wanted_bos in events:
        candidates.append({"strategy": "BREAKOUT", "score": 5, "max_score": 5,
                           "evidence": ["كسر هيكل مؤكد بإغلاق شمعة مكتملة"]})

    # Liquidity sweep: wick takes the opposing pool then closes back through the level.
    if wanted_sweep in events and (pattern in wanted_pattern or impulse == ("BULLISH" if bullish else "BEARISH")):
        candidates.append({"strategy": "LIQUIDITY_SWEEP", "score": 5, "max_score": 5,
                           "evidence": ["سحب سيولة معاكس ثم إغلاق/اندفاع مؤيد"]})

    # Reversal is deliberately informational only; it cannot override H4/H1 trend bias.
    opposite_structure = "DOWNTREND" if bullish else "UPTREND"
    if report.get("market_structure") == opposite_structure and wanted_sweep in events:
        candidates.append({"strategy": "REVERSAL_WATCH", "score": 3, "max_score": 5,
                           "evidence": ["احتمال انعكاس بعد سحب سيولة؛ لا يدخل عكس اتجاه H4/H1"]})

    candidates.sort(key=lambda item: (item["score"], item["strategy"] == "BREAKOUT"), reverse=True)
    selected = candidates[0] if candidates else None
    blockers = []
    if spread_atr_ratio > 0.15:
        blockers.append("SPREAD_TOO_HIGH")
    if report.get("market_regime") == "HIGH_VOLATILITY":
        blockers.append("EXTREME_M5_VOLATILITY")
    if extension > 1.5:
        blockers.append("M5_OVEREXTENDED")
    if blockers:
        selected = None
    return {
        "selected": selected,
        "candidates": candidates,
        "direction": direction,
        "spread_to_m5_atr_ratio": round(spread_atr_ratio, 4),
        "m5_extension_from_ema20_atr": round(extension, 3) if math.isfinite(extension) else None,
        "blockers": blockers,
        "score_note": "Rule-based setup quality points, not win probability; requires walk-forward backtesting.",
        "strategy_policy": {
            "pullback": "trend-following retest near EMA20 with closed-candle confirmation",
            "breakout": "confirmed BOS only; no intrabar breakout entries",
            "liquidity_sweep": "sweep of opposing liquidity followed by directional close/impulse",
            "reversal_watch": "watchlist only; cannot reverse H4/H1 bias",
        },
    }


@app.post("/api/v1/analysis/expert")
async def expert_technical_analysis(request: MultiTimeframeAnalysisRequest) -> dict:
    """Produce a conservative, explainable multi-layer GOLD technical dossier.

    Scores are confluence points, never probabilities. The endpoint only emits a
    paper candidate when the existing technical, structure, macro and news gates
    all pass. It never places orders.
    """
    if request.symbol.upper() != settings.broker_gold_symbol.upper():
        raise HTTPException(status_code=400, detail=f"Only {settings.broker_gold_symbol} is supported.")

    # Reuse the existing full decision pipeline; this also validates all four
    # broker candle streams and applies the fail-closed macro/news filters.
    base = await gold_decision_analysis(request)
    quote = _fresh_mt5_quote()
    blockers = list(base.get("blockers", []))
    reasons = list(base.get("reasons", []))
    mtf = base.get("components", {}).get("multi_timeframe")
    if not isinstance(mtf, dict) or not isinstance(mtf.get("timeframes"), dict):
        return {
            **base,
            "analysis_level": "EXPERT_DOSSIER_UNAVAILABLE",
            "technical_dossier": None,
            "setup_plan": None,
            "blockers": sorted(set(blockers + ["TECHNICAL_DOSSIER_UNAVAILABLE"])),
            "policy": {
                "mode": "PAPER_ONLY", "live_trading_enabled": False,
                "execution_enabled": False, "scores_are_probabilities": False,
            },
        }

    frames = {
        "M5": request.m5, "M15": request.m15,
        "H1": request.h1, "H4": request.h4,
    }
    weights = {"M5": 1.0, "M15": 1.5, "H1": 2.0, "H4": 2.5}
    frame_reports: dict[str, dict] = {}
    weighted_score = 0.0
    total_weight = sum(weights.values())
    for timeframe, candles in frames.items():
        technical = mtf["timeframes"][timeframe]
        direction_value = technical.get("direction", "WAIT")
        direction = direction_value.value if isinstance(direction_value, Direction) else str(direction_value)
        regime = _market_regime_analysis(candles)
        structure = _price_action_analysis(candles, 3, 3, 0.12)
        microstructure = _candle_microstructure_analysis(candles)
        indicators = technical.get("indicators", {})
        rsi = float(indicators.get("rsi14_wilder", 50.0))
        histogram = float(indicators.get("macd_histogram", 0.0))
        adx = float(regime["indicators"]["adx14_wilder"])
        atr_value = float(regime["indicators"]["atr14_wilder"])
        last_close = float(candles[-1].close)
        ema20_value = float(indicators.get("ema20", last_close))
        extension_atr = abs(last_close - ema20_value) / atr_value if atr_value > 0 else 0.0
        frame_score = 0.0
        evidence: list[str] = []

        if direction == "BUY":
            frame_score += 2.0
            evidence.append("المؤشرات الأساسية تميل للصعود")
        elif direction == "SELL":
            frame_score -= 2.0
            evidence.append("المؤشرات الأساسية تميل للهبوط")

        if regime["market_regime"] == "TRENDING_UP":
            frame_score += 1.0
            evidence.append("ADX يؤكد بيئة اتجاه صاعد")
        elif regime["market_regime"] == "TRENDING_DOWN":
            frame_score -= 1.0
            evidence.append("ADX يؤكد بيئة اتجاه هابط")

        if structure["market_structure"] == "UPTREND":
            frame_score += 1.0
            evidence.append("القمم والقيعان المؤكدة تصاعدية")
        elif structure["market_structure"] == "DOWNTREND":
            frame_score -= 1.0
            evidence.append("القمم والقيعان المؤكدة تنازلية")

        if rsi >= 55 and histogram > 0:
            frame_score += 0.75
            evidence.append("الزخم RSI وMACD إيجابي")
        elif rsi <= 45 and histogram < 0:
            frame_score -= 0.75
            evidence.append("الزخم RSI وMACD سلبي")
        elif 45 < rsi < 55:
            evidence.append("الزخم محايد نسبياً")

        if microstructure["impulse_direction"] == "BULLISH":
            frame_score += 0.25
            evidence.append("شمعة صاعدة اندفاعية أغلقت قرب القمة")
        elif microstructure["impulse_direction"] == "BEARISH":
            frame_score -= 0.25
            evidence.append("شمعة هابطة اندفاعية أغلقت قرب القاع")

        volume_confirmation = microstructure["tick_volume"]["directional_confirmation"]
        if volume_confirmation == "BULLISH":
            frame_score += 0.25
            evidence.append("حجم التداول النسبي يؤكد ضغط الشراء")
        elif volume_confirmation == "BEARISH":
            frame_score -= 0.25
            evidence.append("حجم التداول النسبي يؤكد ضغط البيع")

        if microstructure["trend_quality"] == "WEAK":
            evidence.append("فصل EMA والزخم النسبي ضعيفان؛ الاتجاه يحتاج حذراً")
        if microstructure["tick_volume"]["state"] == "UNAVAILABLE":
            evidence.append("حجم Tick غير متوفر؛ لم يُستخدم كعامل تأكيد")

        if regime["market_regime"] == "HIGH_VOLATILITY":
            evidence.append("تحذير: تقلب مرتفع في هذا الإطار")
        if "BUY_SIDE_LIQUIDITY_SWEEP" in structure["events"]:
            evidence.append("رصد سحب سيولة فوق قمة مؤكدة؛ يحتاج تأكيداً")
        if "SELL_SIDE_LIQUIDITY_SWEEP" in structure["events"]:
            evidence.append("رصد سحب سيولة تحت قاع مؤكد؛ يحتاج تأكيداً")
        if "BULLISH_BREAK_OF_STRUCTURE" in structure["events"]:
            evidence.append("كسر هيكل صاعد مؤكد بالإغلاق")
        if "BEARISH_BREAK_OF_STRUCTURE" in structure["events"]:
            evidence.append("كسر هيكل هابط مؤكد بالإغلاق")

        weighted_score += frame_score * weights[timeframe]
        frame_reports[timeframe] = {
            "direction": direction,
            "weight": weights[timeframe],
            "confluence_points": round(frame_score, 2),
            "market_regime": regime["market_regime"],
            "adx14": regime["indicators"]["adx14_wilder"],
            "atr14": regime["indicators"]["atr14_wilder"],
            "atr_vs_recent_median": regime["indicators"]["atr_vs_recent_median"],
            "rsi14": round(rsi, 2),
            "ema20": round(ema20_value, 5),
            "ema50": round(float(indicators.get("ema50", last_close)), 5),
            "price_extension_from_ema20_atr": round(extension_atr, 3),
            "extension_state": "OVEREXTENDED" if extension_atr > 1.5 else "EXTENDED" if extension_atr > 1.0 else "NORMAL",
            "macd_histogram": round(histogram, 6),
            "market_structure": structure["market_structure"],
            "confirmed_events": structure["events"],
            "liquidity_pools": structure["liquidity_pools"],
            "recent_fair_value_gaps": structure["fair_value_gaps"],
            "order_block_candidate": structure["order_block_candidate"],
            "microstructure": microstructure,
            "evidence": evidence,
        }

    # Maximum per-frame score includes the two optional microstructure confirmations.
    max_score = 5.25 * total_weight
    confluence = round(min(100.0, abs(weighted_score) / max_score * 100), 1)
    expert_direction = "BUY" if weighted_score > 0 else "SELL" if weighted_score < 0 else "WAIT"
    decision = str(base.get("decision", "WAIT"))
    if decision not in ("BUY", "SELL"):
        expert_direction = "WAIT"
    elif expert_direction != decision or confluence < 45:
        blockers.append("EXPERT_CONFLUENCE_NOT_CONFIRMED")
        reasons.append("طبقة التوافق المتقدم لا تؤكد اتجاه القرار الأساسي؛ تم تحويل القرار إلى WAIT.")
        decision = "WAIT"

    # Spread is evaluated against M5 ATR, not against a fixed dollar threshold.
    m5_atr = float(frame_reports["M5"]["atr14"])
    spread_price = float(quote["spread_points"]) * float(quote["point"])
    spread_atr_ratio = spread_price / m5_atr if m5_atr > 0 else float("inf")
    if spread_atr_ratio > 0.15:
        blockers.append("SPREAD_TOO_HIGH_FOR_M5_VOLATILITY")
        reasons.append("السبريد مرتفع مقارنة بتذبذب M5؛ تم منع إعداد الدخول.")
        decision = "WAIT"

    # Entry-quality controls: avoid chasing an extended M5 move or entering
    # when the trigger candle is already statistically stretched from EMA20.
    m5_report = frame_reports["M5"]
    m5_extension = float(m5_report["price_extension_from_ema20_atr"])
    if m5_extension > 1.5:
        blockers.append("M5_PRICE_OVEREXTENDED")
        reasons.append("السعر مبتعد أكثر من 1.5 ATR عن EMA20 على M5؛ لا نطارد الحركة وننتظر إعادة اختبار.")
        decision = "WAIT"
    elif m5_extension > 1.0:
        reasons.append("تحذير: السعر ممتد عن EMA20 على M5؛ جودة الدخول أقل ويُفضّل انتظار إعادة الاختبار.")

    if m5_report["market_regime"] == "HIGH_VOLATILITY":
        blockers.append("M5_EXTREME_VOLATILITY")
        reasons.append("تقلب M5 مرتفع مقارنة بمتوسطه؛ تم إيقاف الدخول حتى تهدأ الحركة.")
        decision = "WAIT"

    m5_rsi = float(m5_report["rsi14"])
    if decision == "BUY" and m5_rsi >= 72:
        blockers.append("M5_BUY_CHASING_OVERBOUGHT")
        reasons.append("RSI على M5 وصل لمنطقة تشبع شرائي؛ لا نطارد الشراء.")
        decision = "WAIT"
    elif decision == "SELL" and m5_rsi <= 28:
        blockers.append("M5_SELL_CHASING_OVERSOLD")
        reasons.append("RSI على M5 وصل لمنطقة تشبع بيعي؛ لا نطارد البيع.")
        decision = "WAIT"

    # Quantify directional agreement independently from the confluence score.
    directional_frames = [frame_reports[name]["direction"] for name in ("M5", "M15", "H1", "H4")]
    agreement_count = sum(value == expert_direction for value in directional_frames) if expert_direction != "WAIT" else 0
    agreement_percent = round(agreement_count / len(directional_frames) * 100, 1)

    setup_plan = None
    if decision in ("BUY", "SELL"):
        entry = float(quote["ask"] if decision == "BUY" else quote["bid"])
        stop_distance = max(1.5 * m5_atr, 3.0 * spread_price)
        m5_structure = _price_action_analysis(request.m5, 3, 3, 0.12)
        if decision == "BUY":
            swing = m5_structure.get("last_confirmed_swing_low")
            stop = entry - stop_distance
            if swing and float(swing["price"]) < entry:
                stop = min(stop, float(swing["price"]) - 0.1 * m5_atr)
            risk_distance = entry - stop
            targets = {f"target_{rr}R": round(entry + risk_distance * rr, quote["digits"]) for rr in (1, 2, 3)}
        else:
            swing = m5_structure.get("last_confirmed_swing_high")
            stop = entry + stop_distance
            if swing and float(swing["price"]) > entry:
                stop = max(stop, float(swing["price"]) + 0.1 * m5_atr)
            risk_distance = stop - entry
            targets = {f"target_{rr}R": round(entry - risk_distance * rr, quote["digits"]) for rr in (1, 2, 3)}
        setup_plan = {
            "direction": decision,
            "entry_reference": round(entry, quote["digits"]),
            "stop_loss_reference": round(stop, quote["digits"]),
            "risk_distance_price": round(risk_distance, quote["digits"]),
            "targets": targets,
            "stop_method": "max(1.5 x M5 ATR, 3 x spread), widened beyond latest confirmed opposing swing when applicable",
            "risk_reward_targets": [1, 2, 3],
            "status": "PAPER_RESEARCH_ONLY",
            "warning": "مستويات حسابية مرجعية وليست أوامر أو ضماناً للتنفيذ أو الربح. يجب إعادة حساب الحجم وفق مواصفات عقد XM.",
        }

    if decision == "WAIT" and "NO_TRADE" not in blockers:
        blockers.append("NO_FULL_CONFLUENCE")
    strategy_scan = _classify_entry_setups(decision, frames, frame_reports, spread_atr_ratio)
    grade = (
        "HIGH_CONFLUENCE" if confluence >= 70 else
        "MODERATE_CONFLUENCE" if confluence >= 45 else
        "LOW_CONFLUENCE"
    )
    return {
        **base,
        "decision": decision,
        "status": "PAPER_CANDIDATE" if decision in ("BUY", "SELL") else "NO_TRADE",
        "blockers": sorted(set(blockers)),
        "analysis_level": "ADVANCED_MULTI_LAYER_TECHNICAL_DOSSIER",
        "technical_dossier": {
            "confluence_score": confluence,
            "score_type": "HEURISTIC_CONFLUENCE_NOT_PROBABILITY",
            "weighted_directional_points": round(weighted_score, 2),
            "maximum_absolute_points": round(max_score, 2),
            "confluence_grade": grade,
            "dominant_technical_direction": expert_direction,
            "timeframes": frame_reports,
            "spread_to_m5_atr_ratio": round(spread_atr_ratio, 4),
            "directional_timeframe_agreement_percent": agreement_percent,
            "strategy_scan": strategy_scan,
            "entry_quality": {
                "m5_price_extension_from_ema20_atr": round(m5_extension, 3),
                "m5_extension_limit_atr": 1.5,
                "m5_rsi14": round(m5_rsi, 2),
                "m5_volatility_regime": m5_report["market_regime"],
                "spread_to_atr_ratio": round(spread_atr_ratio, 4),
                "anti_chasing_passed": m5_extension <= 1.5 and not (
                    (decision == "BUY" and m5_rsi >= 72) or
                    (decision == "SELL" and m5_rsi <= 28)
                ),
            },
            "methodology": [
                "EMA/RSI/MACD technical direction",
                "Wilder ADX and ATR market regime",
                "confirmed swing structure, BOS and liquidity sweep candidates",
                "equal highs/lows, FVG and heuristic order-block candidates",
                "closed-candle impulse, EMA slope/separation and optional tick-volume confirmation",
                "weighted M5/M15/H1/H4 confluence",
                "broker spread normalized by M5 ATR",
                "anti-chasing entry filter using M5 EMA20 extension, ATR and RSI extremes",
                "separate timeframe agreement and confluence diagnostics",
                "macro context and news advisory/risk gates",
            ],
            "limitations": [
                "Order blocks and liquidity events are rule-based chart candidates, not verified institutional order flow.",
                "Confluence score is not a win probability and has not been calibrated against out-of-sample XM GOLD returns.",
                "Only the supplied closed broker candles are analyzed; this endpoint does not create an independent candle feed.",
                "The anti-chasing thresholds are deterministic heuristics and require walk-forward validation; they are not calibrated probabilities.",
            ],
        },
        "setup_plan": setup_plan,
        "policy": {
            "mode": "PAPER_ONLY",
            "live_trading_enabled": False,
            "execution_enabled": False,
            "scores_are_probabilities": False,
            "automatic_order_placement": False,
        },
        "generated_at": datetime.now(timezone.utc),
    }


@app.get("/api/v1/analysis/chart")
async def live_analysis_chart(timeframe: str = "ALL") -> dict:
    """Return real, closed MT5 OHLC candles and their matching expert reports for charting."""
    requested = timeframe.upper().strip()
    allowed = {"M5", "M15", "H1", "H4", "ALL"}
    if requested not in allowed:
        raise HTTPException(status_code=400, detail="timeframe must be M5, M15, H1, H4 or ALL.")
    # Reuse the same freshness and integrity checks as the live expert endpoint.
    analysis = await live_expert_technical_analysis()
    if _latest_mt5_candles is None:
        raise HTTPException(status_code=503, detail="Waiting for authenticated MT5 candles.")
    series = _latest_mt5_candles["timeframes"]
    dossier = analysis.get("technical_dossier") or {}
    reports = dossier.get("timeframes") or {}
    frames = list(series.keys()) if requested == "ALL" else [requested]
    candles_by_timeframe = {
        frame: [bar.model_dump(mode="json") for bar in series[frame][-100:]]
        for frame in frames
    }
    reports_by_timeframe = {frame: reports.get(frame, {}) for frame in frames}
    m5_atr = float((reports.get("M5") or {}).get("atr14") or 0.0)
    quote = _fresh_mt5_quote()
    spread_price = float(quote.get("spread_points") or 0.0) * float(quote.get("point") or 0.0)
    spread_atr_ratio = spread_price / m5_atr if m5_atr > 0 else 1.0
    strategy_scan = _classify_entry_setups(
        str(analysis.get("decision", "WAIT")), series, reports, spread_atr_ratio
    )
    return {
        "symbol": _latest_mt5_candles["symbol"],
        "source": _latest_mt5_candles["source"],
        "received_at": _latest_mt5_candles["received_at"],
        "closed_candles_only": True,
        "decision": analysis.get("decision", "WAIT"),
        "blockers": analysis.get("blockers", []),
        "confluence_score": dossier.get("confluence_score"),
        "confluence_grade": dossier.get("confluence_grade"),
        "dominant_technical_direction": dossier.get("dominant_technical_direction"),
        "setup_plan": analysis.get("setup_plan"),
        "strategy_scan": strategy_scan,
        "candles_by_timeframe": candles_by_timeframe,
        "reports_by_timeframe": reports_by_timeframe,
        "limitations": dossier.get("limitations", []),
        "execution_enabled": False,
    }


# Authenticated MT5 candle cache. It is intentionally in-memory: after a server
# restart the API waits for a fresh complete batch instead of reusing stale bars.
_latest_mt5_candles: dict | None = None


class MT5CandleBatch(MultiTimeframeAnalysisRequest):
    """Closed broker candles streamed by the MT5 EA; same GOLD-only timeframe contract."""


def _validate_mt5_candle_batch(batch: MT5CandleBatch) -> tuple[dict, dict]:
    """Validate closed broker bars and return non-blocking session-gap diagnostics."""
    now = datetime.now(timezone.utc)
    series = {"M5": batch.m5, "M15": batch.m15, "H1": batch.h1, "H4": batch.h4}
    normalized: dict[str, list[Candle]] = {}
    quality: dict[str, dict] = {}
    for timeframe, candles in series.items():
        interval = _TIMEFRAME_SECONDS[timeframe]
        stamps: list[datetime] = []
        copied: list[Candle] = []
        for candle in candles:
            if candle.high < max(candle.open, candle.close, candle.low) or candle.low > min(candle.open, candle.close, candle.high):
                raise HTTPException(status_code=422, detail=f"Invalid OHLC values in {timeframe}.")
            if candle.high < candle.low:
                raise HTTPException(status_code=422, detail=f"High is below Low in {timeframe}.")
            stamp = candle.time
            if stamp.tzinfo is None:
                raise HTTPException(status_code=422, detail=f"{timeframe} candle timestamps must include a timezone.")
            stamp = stamp.astimezone(timezone.utc)
            if stamp.second != 0 or stamp.microsecond != 0:
                raise HTTPException(status_code=422, detail=f"{timeframe} candle timestamps must be aligned to whole minutes.")
            if stamp.minute % (interval // 60) != 0:
                raise HTTPException(status_code=422, detail=f"{timeframe} candle timestamp is not aligned to its timeframe.")
            stamps.append(stamp)
            copied.append(Candle(
                time=stamp, open=candle.open, high=candle.high, low=candle.low,
                close=candle.close, tick_volume=candle.tick_volume,
            ))
        if any(current <= previous for previous, current in zip(stamps, stamps[1:])):
            raise HTTPException(status_code=422, detail=f"{timeframe} candle timestamps must be strictly increasing.")
        deltas = [(current - previous).total_seconds() for previous, current in zip(stamps, stamps[1:])]
        if any(delta < interval for delta in deltas):
            raise HTTPException(status_code=422, detail=f"{timeframe} contains overlapping or too-close candle timestamps.")
        age = (now - stamps[-1]).total_seconds()
        if age < interval - 5:
            raise HTTPException(status_code=422, detail=f"Latest {timeframe} candle does not appear to be fully closed.")
        # At market reopen, the latest *closed* H4 bar can legitimately be
        # Friday's final bar while fresh M5/M15/H1 bars are already arriving.
        # Keep strict freshness for all frames, but allow this H4 session gap
        # only after the three lower timeframes have independently passed their
        # freshness checks. Never relax freshness for M5/M15/H1.
        h4_session_reopen_gap = (
            timeframe == "H4"
            and all(frame in quality for frame in ("M5", "M15", "H1"))
            and age <= 7 * 24 * 60 * 60
        )
        if age > interval * 2 + 60 and not h4_session_reopen_gap:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Latest {timeframe} candle is stale: "
                    f"bar_time_utc={stamps[-1].isoformat()}, "
                    f"age_seconds={int(age)}, "
                    f"limit_seconds={interval * 2 + 60}, "
                    f"server_now_utc={now.isoformat()}."
                ),
            )
        # Longer gaps can be legitimate weekends, holidays, or broker session breaks.
        # Report them for visibility instead of rejecting valid market closures.
        gaps = [int(delta) for delta in deltas if delta > interval + 1]
        normalized[timeframe] = copied
        quality[timeframe] = {
            "bars_received": len(copied),
            "latest_closed_bar_time": stamps[-1],
            "latest_bar_age_seconds": max(0, int(age)),
            "session_gaps_observed": len(gaps),
            "largest_gap_seconds": max(gaps, default=0),
            "h4_session_reopen_gap": h4_session_reopen_gap,
            "strictly_increasing": True,
            "timeframe_spacing_valid": True,
        }
    return normalized, quality


@app.post("/api/v1/mt5/candles")
def receive_mt5_candles(
    batch: MT5CandleBatch,
    authorization: str | None = Header(default=None),
) -> dict:
    """Receive authenticated closed GOLD candles for M5/M15/H1/H4 from the MT5 EA."""
    global _latest_mt5_candles
    expected = settings.mt5_bridge_token
    if not expected or not authorization or not secrets.compare_digest(
        authorization.removeprefix("Bearer ").strip(), expected
    ):
        raise HTTPException(status_code=401, detail="Invalid or unconfigured MT5 bridge token.")
    if batch.symbol.upper() != settings.broker_gold_symbol.upper():
        raise HTTPException(status_code=400, detail=f"Only {settings.broker_gold_symbol} candles are accepted.")
    normalized, quality = _validate_mt5_candle_batch(batch)
    # Never let a delayed/replayed request roll the live cache back to older bars.
    if _latest_mt5_candles is not None:
        previous = _latest_mt5_candles["timeframes"]
        for timeframe, rows in normalized.items():
            if rows[-1].time < previous[timeframe][-1].time:
                raise HTTPException(
                    status_code=409,
                    detail=f"Out-of-order {timeframe} candle batch rejected; cached data is newer.",
                )
    _latest_mt5_candles = {
        "symbol": settings.broker_gold_symbol.upper(),
        "timeframes": normalized,
        "data_quality": quality,
        "received_at": datetime.now(timezone.utc),
        "source": "Authenticated MetaTrader 5 broker candle feed",
    }
    return {
        "accepted": True,
        "symbol": settings.broker_gold_symbol.upper(),
        "source": _latest_mt5_candles["source"],
        "candle_counts": {frame: len(rows) for frame, rows in normalized.items()},
        "latest_candle_times": {frame: rows[-1].time for frame, rows in normalized.items()},
        "data_quality": quality,
        "execution_enabled": False,
    }


_DEMO_MIN_CONFLUENCE_SCORE = 85.0
_DEMO_ALLOWED_ENTRY_STRATEGIES = {"PULLBACK", "BREAKOUT", "LIQUIDITY_SWEEP"}


def _demo_candidate_quality_gate(analysis: dict) -> tuple[bool, str, dict]:
    """Require strong confluence and a confirmed non-reversal setup before DEMO execution."""
    dossier = analysis.get("technical_dossier")
    if not isinstance(dossier, dict):
        return False, "Expert technical dossier is unavailable.", {}
    try:
        confluence = float(dossier.get("confluence_score"))
    except (TypeError, ValueError):
        confluence = float("nan")
    if not math.isfinite(confluence):
        return False, "Confluence score is missing or invalid.", {}
    if confluence < _DEMO_MIN_CONFLUENCE_SCORE:
        return False, "Confluence is below the 85/100 DEMO entry threshold.", {
            "confluence_score": confluence,
            "minimum_confluence_score": _DEMO_MIN_CONFLUENCE_SCORE,
        }

    scan = dossier.get("strategy_scan")
    if not isinstance(scan, dict):
        return False, "Strategy scan is unavailable.", {"confluence_score": confluence}
    blockers = scan.get("blockers") or []
    if blockers:
        return False, "Strategy scan has active safety blockers.", {
            "confluence_score": confluence, "strategy_blockers": blockers,
        }
    selected = scan.get("selected")
    if not isinstance(selected, dict):
        return False, "No confirmed entry strategy is selected.", {
            "confluence_score": confluence,
        }
    strategy = str(selected.get("strategy", "")).upper()
    if strategy not in _DEMO_ALLOWED_ENTRY_STRATEGIES:
        return False, "Selected setup is informational only and cannot authorize entry.", {
            "confluence_score": confluence, "selected_strategy": strategy,
        }
    try:
        setup_score = float(selected.get("score"))
        setup_max = float(selected.get("max_score"))
    except (TypeError, ValueError):
        setup_score, setup_max = 0.0, 0.0
    if not math.isfinite(setup_score) or not math.isfinite(setup_max) or setup_max <= 0:
        return False, "Selected setup score is invalid.", {
            "confluence_score": confluence, "selected_strategy": strategy,
        }
    if setup_score < setup_max * 0.8:
        return False, "Selected setup quality is below 80% of its strategy score.", {
            "confluence_score": confluence, "selected_strategy": strategy,
            "setup_score": setup_score, "setup_max_score": setup_max,
        }
    return True, "DEMO entry quality gates passed.", {
        "confluence_score": confluence,
        "minimum_confluence_score": _DEMO_MIN_CONFLUENCE_SCORE,
        "selected_strategy": strategy,
        "setup_score": setup_score,
        "setup_max_score": setup_max,
    }


@app.post("/api/v1/manual/demo/request")
def request_manual_demo_trade(
    request: ManualDemoTradeRequest,
    x_manual_trade_pin: str | None = Header(default=None),
) -> dict:
    """Queue a user-confirmed DEMO order; the MT5 EA remains the only executor."""
    _require_manual_trade_pin(x_manual_trade_pin)
    if settings.live_trading_enabled or settings.trading_mode.lower() != "paper":
        raise HTTPException(status_code=503, detail="Manual trading is locked unless the global account mode is DEMO/PAPER.")
    if not settings.demo_execution_enabled:
        raise HTTPException(status_code=503, detail="DEMO execution is disabled on the server.")
    if request.direction == Direction.WAIT:
        raise HTTPException(status_code=422, detail="Choose BUY or SELL.")
    quote = _fresh_mt5_quote()
    required = ("tick_size", "tick_value_loss", "volume_min", "volume_max", "volume_step", "account_equity")
    if any(quote.get(key) is None for key in required):
        raise HTTPException(status_code=503, detail="Waiting for complete MT5 contract/account metadata.")
    step = Decimal(str(quote["volume_step"]))
    normalized_steps = (Decimal(str(request.lots)) / step).to_integral_value(rounding=ROUND_DOWN)
    lots = float(normalized_steps * step)
    if lots < quote["volume_min"] or lots > quote["volume_max"] or abs(lots - request.lots) > 1e-8:
        raise HTTPException(status_code=422, detail="Lot size must match XM minimum, maximum and volume step.")
    stop_price_distance = request.stop_loss_points * quote["point"]
    loss_per_lot = (stop_price_distance / quote["tick_size"]) * quote["tick_value_loss"]
    risk_amount = lots * loss_per_lot
    equity = float(quote["account_equity"])
    risk_percent = risk_amount / equity * 100 if equity > 0 else float("inf")
    if not math.isfinite(risk_percent) or risk_percent > settings.demo_risk_per_trade_percent + 1e-9:
        raise HTTPException(
            status_code=422,
            detail=f"Requested lot/stop risks {risk_percent:.3f}% which exceeds the {settings.demo_risk_per_trade_percent:.2f}% DEMO limit. Reduce lots or widen the stop.",
        )
    if _manual_trade_queue:
        raise HTTPException(status_code=429, detail="A manual order is already waiting for MT5; avoid duplicate requests.")
    command_id = secrets.token_urlsafe(12)
    command = {
        "id": command_id,
        "symbol": settings.broker_gold_symbol.upper(),
        "direction": request.direction.value,
        "lots": lots,
        "stop_loss_points": request.stop_loss_points,
        "take_profit_points": request.take_profit_points,
        "risk_percent": round(risk_percent, 4),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    _manual_trade_queue.append(command)
    return {
        "accepted": True, "queued": True, "command_id": command_id,
        "direction": command["direction"], "lots": lots,
        "estimated_risk_percent": round(risk_percent, 4),
        "message": "Manual DEMO request queued. The MT5 EA must collect and accept it before an order exists.",
    }


@app.get("/api/v1/mt5/manual/next")
def get_next_manual_demo_trade(authorization: str | None = Header(default=None)) -> dict:
    """Deliver one queued manual command to the authenticated MT5 bridge."""
    expected = settings.mt5_bridge_token
    if not expected or not authorization or not secrets.compare_digest(
        authorization.removeprefix("Bearer ").strip(), expected
    ):
        raise HTTPException(status_code=401, detail="Invalid or unconfigured MT5 bridge token.")
    if settings.live_trading_enabled or settings.trading_mode.lower() != "paper" or not settings.demo_execution_enabled:
        return {"available": False, "reason": "DEMO execution is locked."}
    if not _manual_trade_queue:
        return {"available": False}
    command = _manual_trade_queue.pop(0)
    try:
        created_at = datetime.fromisoformat(command["created_at"])
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        age_seconds = (datetime.now(timezone.utc) - created_at.astimezone(timezone.utc)).total_seconds()
    except (KeyError, TypeError, ValueError):
        return {"available": False, "expired": True, "reason": "Manual order timestamp is invalid."}
    if age_seconds < -5 or age_seconds > 30:
        return {"available": False, "expired": True, "reason": "Manual order expired; send a fresh request."}
    return {"available": True, **command}


@app.get("/api/v1/mt5/demo/signal")
async def mt5_demo_signal(
    daily_loss_percent: float = 0.0,
    authorization: str | None = Header(default=None),
) -> dict:
    """Return a candidate for a separately demo-gated MT5 executor; never place orders."""
    expected = settings.mt5_bridge_token
    if not expected or not authorization or not secrets.compare_digest(
        authorization.removeprefix("Bearer ").strip(), expected
    ):
        raise HTTPException(status_code=401, detail="Invalid or unconfigured MT5 bridge token.")
    if not math.isfinite(daily_loss_percent) or daily_loss_percent < 0 or daily_loss_percent > 100:
        raise HTTPException(status_code=422, detail="daily_loss_percent must be between 0 and 100.")
    if settings.live_trading_enabled or settings.trading_mode.lower() != "paper":
        raise HTTPException(status_code=503, detail="Demo signal service is locked by the global safety policy.")
    if not settings.demo_execution_enabled:
        return {
            "enabled": False, "decision": "WAIT",
            "reason": "Demo execution has not been explicitly enabled on the server.",
            "execution_enabled": False,
        }
    if daily_loss_percent >= settings.max_daily_loss_percent:
        return {
            "enabled": True, "decision": "WAIT",
            "reason": "Daily loss limit reached; new entries are locked.",
            "daily_loss_percent": daily_loss_percent,
            "max_daily_loss_percent": settings.max_daily_loss_percent,
            "execution_enabled": False,
        }

    analysis = await live_expert_technical_analysis()
    quality_ok, quality_reason, quality_details = _demo_candidate_quality_gate(analysis)
    if not quality_ok:
        return {
            "enabled": True, "decision": "WAIT",
            "reason": quality_reason,
            "quality_gate": quality_details,
            "execution_enabled": False,
        }
    plan = analysis.get("setup_plan")
    if analysis.get("decision") not in ("BUY", "SELL") or not isinstance(plan, dict):
        return {
            "enabled": True, "decision": "WAIT",
            "reason": "No fully confirmed expert setup is available.",
            "blockers": analysis.get("blockers", []),
            "execution_enabled": False,
        }

    quote = _fresh_mt5_quote()
    point = float(quote["point"])
    stop = float(plan["stop_loss_reference"])
    entry = float(plan["entry_reference"])
    stop_points = abs(entry - stop) / point
    sizing = calculate_position_size(PositionSizeRequest(
        stop_distance_points=stop_points,
        risk_percent=settings.demo_risk_per_trade_percent,
        account_equity=float(quote["account_equity"]),
        daily_loss_percent=daily_loss_percent,
    ))
    if not sizing.get("approved") or sizing.get("recommended_lots", 0) <= 0:
        return {
            "enabled": True, "decision": "WAIT",
            "reason": sizing.get("reason", "Position sizing was not approved."),
            "position_sizing": sizing,
            "execution_enabled": False,
        }

    return {
        "enabled": True,
        "decision": analysis["decision"],
        "symbol": settings.broker_gold_symbol.upper(),
        "generated_at": analysis.get("generated_at"),
        "entry_reference": entry,
        "stop_loss": stop,
        "take_profit": plan["targets"]["target_2R"],
        "risk_reward": "1:2",
        "recommended_lots": sizing["recommended_lots"],
        "risk_percent": settings.demo_risk_per_trade_percent,
        "daily_loss_percent": daily_loss_percent,
        "max_daily_loss_percent": settings.max_daily_loss_percent,
        "blockers": [],
        "candidate_ready": True,
        "execution_enabled": False,
        "note": "Signal candidate only. No server-side order execution. MT5 must independently verify DEMO account mode and all local risk guards.",
    }


@app.get("/api/v1/analysis/expert/live")
async def live_expert_technical_analysis() -> dict:
    """Analyze the latest complete, fresh, authenticated MT5 candle batch."""
    if _latest_mt5_candles is None:
        raise HTTPException(status_code=503, detail="Waiting for the first complete authenticated MT5 candle batch.")
    series = _latest_mt5_candles["timeframes"]
    now = datetime.now(timezone.utc)
    quality = _latest_mt5_candles.get("data_quality", {})
    # Only the authenticated batch validator can mark a legitimate H4
    # session-reopen gap. Lower timeframes remain strictly fresh.
    allow_h4_session_gap = bool(
        quality.get("H4", {}).get("h4_session_reopen_gap")
    )
    for timeframe, candles in series.items():
        age = (now - candles[-1].time.astimezone(timezone.utc)).total_seconds()
        freshness_limit = _TIMEFRAME_SECONDS[timeframe] * 2 + 60
        if timeframe == "H4" and allow_h4_session_gap:
            freshness_limit = 7 * 24 * 60 * 60
        if age < -30 or age > freshness_limit:
            raise HTTPException(status_code=503, detail=f"Cached {timeframe} candles are stale; expert analysis is paused.")
    request = MultiTimeframeAnalysisRequest(
        symbol=_latest_mt5_candles["symbol"],
        m5=series["M5"], m15=series["M15"], h1=series["H1"], h4=series["H4"],
    )
    context_token = _ALLOW_H4_SESSION_GAP.set(allow_h4_session_gap)
    try:
        result = await expert_technical_analysis(request)
    finally:
        _ALLOW_H4_SESSION_GAP.reset(context_token)
    result["market_data"] = {
        "source": _latest_mt5_candles["source"],
        "received_at": _latest_mt5_candles["received_at"],
        "candle_counts": {frame: len(rows) for frame, rows in series.items()},
        "data_quality": _latest_mt5_candles.get("data_quality", {}),
        "feed_age_seconds": max(0, int((now - _latest_mt5_candles["received_at"]).total_seconds())),
        "closed_candles_only": True,
    }
    return result
