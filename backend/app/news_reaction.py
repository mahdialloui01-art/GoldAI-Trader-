from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

router = APIRouter()


class ReactionCandle(BaseModel):
    # Timestamp is the candle CLOSE time, not its opening time.
    close_time: datetime
    open: float = Field(gt=0)
    high: float = Field(gt=0)
    low: float = Field(gt=0)
    close: float = Field(gt=0)


class PostReleaseNewsRequest(BaseModel):
    symbol: str = "GOLD"
    event_id: str = Field(min_length=2, max_length=100)
    event_name: str = Field(min_length=3, max_length=160)
    release_at: datetime
    actual: Optional[str] = Field(default=None, max_length=80)
    previous: Optional[str] = Field(default=None, max_length=80)
    unit: Optional[str] = Field(default=None, max_length=30)
    calendar_source: str = Field(default="XOOMAR", max_length=40)
    reference_price: float = Field(gt=0, description="XM GOLD mid price immediately before release.")
    reference_price_time: datetime
    candles: list[ReactionCandle] = Field(min_length=1, max_length=1000)
    spread_points: float = Field(ge=0)
    max_spread_points: float = Field(default=35, gt=0, le=10000)


def _numeric_value(value: Optional[str]) -> Optional[float]:
    if value is None:
        return None
    candidate = value.strip().replace(",", "")
    if not re.fullmatch(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)", candidate):
        return None
    parsed = float(candidate)
    return parsed if math.isfinite(parsed) else None


def _actual_vs_previous(actual: Optional[str], previous: Optional[str], unit: Optional[str]) -> str:
    a, p = _numeric_value(actual), _numeric_value(previous)
    if a is None or p is None or not unit or not unit.strip():
        return "NOT_COMPARABLE"
    if a > p:
        return "ABOVE_PREVIOUS"
    if a < p:
        return "BELOW_PREVIOUS"
    return "UNCHANGED_VS_PREVIOUS"


@router.post("/api/v1/news/post-release-analysis")
def post_release_news_analysis(request: PostReleaseNewsRequest) -> dict:
    """Describe observed broker-price reaction after a release; never creates or places a trade."""
    if request.symbol.upper() != "GOLD":
        raise HTTPException(status_code=400, detail="Only GOLD is supported.")

    now = datetime.now(timezone.utc)
    if request.release_at.tzinfo is None or request.reference_price_time.tzinfo is None:
        raise HTTPException(status_code=422, detail="Release and reference-price timestamps must include a timezone.")

    release_at = request.release_at.astimezone(timezone.utc)
    reference_time = request.reference_price_time.astimezone(timezone.utc)
    if reference_time > release_at:
        raise HTTPException(status_code=422, detail="Reference price must be timestamped at or before the release.")
    if (release_at - reference_time).total_seconds() > 5 * 60:
        raise HTTPException(status_code=422, detail="Reference price is too old; provide a price from within 5 minutes before release.")
    if release_at > now:
        raise HTTPException(status_code=422, detail="Post-release analysis cannot run before the scheduled release.")

    ordered = sorted(request.candles, key=lambda candle: candle.close_time)
    times = []
    for candle in ordered:
        if candle.close_time.tzinfo is None:
            raise HTTPException(status_code=422, detail="Every candle close_time must include a timezone.")
        candle_time = candle.close_time.astimezone(timezone.utc)
        if candle_time <= release_at:
            raise HTTPException(status_code=422, detail="Post-release candles must close strictly after the release.")
        if candle_time > now:
            raise HTTPException(status_code=422, detail="Future candle timestamps are not allowed.")
        if candle.high < max(candle.open, candle.close, candle.low) or candle.low > min(candle.open, candle.close, candle.high):
            raise HTTPException(status_code=422, detail="Candle OHLC values are inconsistent.")
        times.append(candle_time)

    if len(set(times)) != len(times):
        raise HTTPException(status_code=422, detail="Candle close_time values must be unique.")
    ordered_pairs = list(zip(times, ordered))
    first_time, last_time = times[0], times[-1]
    last_price = ordered[-1].close
    elapsed_minutes = (last_time - release_at).total_seconds() / 60
    total_return = (last_price / request.reference_price - 1) * 100

    horizons = {}
    for minutes, tolerance in ((1, 1.5), (5, 2), (15, 3), (60, 5)):
        target = release_at.timestamp() + minutes * 60
        candidates = [
            (stamp, candle) for stamp, candle in ordered_pairs
            if stamp.timestamp() >= target and (stamp.timestamp() - target) <= tolerance * 60
        ]
        if candidates:
            stamp, candle = candidates[0]
            horizons[f"{minutes}m"] = {
                "close_time": stamp,
                "close": candle.close,
                "return_percent_from_pre_release": round((candle.close / request.reference_price - 1) * 100, 6),
                "available": True,
            }
        else:
            horizons[f"{minutes}m"] = {"available": False, "reason": "Required post-release candle is not available yet."}

    direction = "UP" if last_price > request.reference_price else "DOWN" if last_price < request.reference_price else "FLAT"
    last_three = [candle.close for _, candle in ordered_pairs[-3:]]
    monotonic_confirmation = (
        len(last_three) == 3 and (
            (direction == "UP" and last_three[0] < last_three[1] < last_three[2]) or
            (direction == "DOWN" and last_three[0] > last_three[1] > last_three[2])
        )
    )
    spread_ok = request.spread_points <= request.max_spread_points
    confirmed_for_research = elapsed_minutes >= 5 and monotonic_confirmation and spread_ok

    return {
        "symbol": "GOLD",
        "event": {
            "event_id": request.event_id,
            "event_name": request.event_name,
            "release_at": release_at,
            "calendar_source": request.calendar_source,
            "actual": request.actual,
            "previous": request.previous,
            "unit": request.unit,
            "actual_vs_previous": _actual_vs_previous(request.actual, request.previous, request.unit),
            "comparison_note": "Actual versus previous is descriptive only; it is not consensus surprise and does not determine gold direction.",
        },
        "observed_gold_reaction": {
            "reference_price": request.reference_price,
            "reference_price_time": reference_time,
            "latest_price": last_price,
            "latest_candle_close_time": last_time,
            "elapsed_minutes_after_release": round(elapsed_minutes, 2),
            "direction_from_reference": direction,
            "return_percent_from_reference": round(total_return, 6),
            "horizons": horizons,
            "last_three_closes_monotonic_in_reaction_direction": monotonic_confirmation,
        },
        "market_conditions": {
            "spread_points": request.spread_points,
            "max_spread_points": request.max_spread_points,
            "spread_acceptable": spread_ok,
        },
        "decision": "REACTION_CONFIRMED_FOR_RESEARCH" if confirmed_for_research else "WAIT_FOR_MORE_DATA_OR_CONFIRMATION",
        "decision_reasons": [
            "At least five minutes have elapsed since release." if elapsed_minutes >= 5 else "Wait until at least five minutes after release.",
            "The last three candle closes confirm the observed direction." if monotonic_confirmation else "The last three candle closes do not yet confirm a consistent direction.",
            "Spread is within the configured limit." if spread_ok else "Spread exceeds the configured limit.",
        ],
        "data_policy": {
            "price_source": "Caller-supplied broker candles; source and timestamp integrity are not independently verified by this endpoint.",
            "candle_timestamp_semantics": "close_time",
            "actual_previous_comparison_is_not_consensus_surprise": True,
            "historical_edge_not_established": True,
            "execution_enabled": False,
            "mode": "RESEARCH_ONLY",
        },
        "generated_at": now,
        "disclaimer": "Observed reaction is descriptive, not a forecast or trade instruction. This endpoint never places orders.",
        "execution_enabled": False,
    }
