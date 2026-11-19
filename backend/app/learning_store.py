from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime, timedelta, timezone
import re
from typing import Any

import psycopg
from psycopg.rows import dict_row
from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import BaseModel, Field, model_validator

from .orderflow_engine import build_orderflow_snapshot

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/learning", tags=["learning"])


def _database_url() -> str:
    value = os.getenv("DATABASE_URL", "").strip()
    if value.startswith("postgres://"):
        value = "postgresql://" + value[len("postgres://"):]
    return value


def _connect():
    url = _database_url()
    if not url:
        raise RuntimeError("DATABASE_URL is not configured")
    return psycopg.connect(url, connect_timeout=4, row_factory=dict_row)


def ensure_learning_schema() -> bool:
    """Create durable learning tables; safe to call repeatedly."""
    try:
        with _connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS trade_journal (
                    deal_ticket BIGINT PRIMARY KEY,
                    position_id BIGINT NOT NULL,
                    deal_time TIMESTAMPTZ NOT NULL,
                    symbol TEXT NOT NULL,
                    direction TEXT NOT NULL CHECK (direction IN ('BUY','SELL','OTHER')),
                    volume NUMERIC(18,8) NOT NULL,
                    price NUMERIC(20,8) NOT NULL,
                    profit NUMERIC(20,8) NOT NULL,
                    commission NUMERIC(20,8) NOT NULL DEFAULT 0,
                    swap NUMERIC(20,8) NOT NULL DEFAULT 0,
                    reason INTEGER NOT NULL DEFAULT 0,
                    inserted_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS opportunity_log (
                    id BIGSERIAL PRIMARY KEY,
                    fingerprint TEXT NOT NULL UNIQUE,
                    created_at TIMESTAMPTZ NOT NULL,
                    symbol TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    status TEXT NOT NULL,
                    direction TEXT,
                    confluence_score DOUBLE PRECISION,
                    strategy TEXT,
                    blockers JSONB NOT NULL DEFAULT '[]'::jsonb,
                    market_context JSONB NOT NULL DEFAULT '{}'::jsonb
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_trade_journal_time ON trade_journal (deal_time DESC)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_opportunity_log_time ON opportunity_log (created_at DESC)")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS xm_quote_samples (
                    sample_time TIMESTAMPTZ PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    bid NUMERIC(20,8) NOT NULL,
                    ask NUMERIC(20,8) NOT NULL,
                    last NUMERIC(20,8) NOT NULL DEFAULT 0,
                    spread_points NUMERIC(20,8) NOT NULL,
                    point NUMERIC(20,10) NOT NULL,
                    digits SMALLINT NOT NULL,
                    received_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_xm_quote_samples_symbol_time ON xm_quote_samples (symbol, sample_time DESC)")
            conn.execute("DELETE FROM xm_quote_samples WHERE sample_time < NOW() - INTERVAL '90 days'")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS mt5_closed_candles (
                    symbol TEXT NOT NULL,
                    timeframe TEXT NOT NULL,
                    candle_time TIMESTAMPTZ NOT NULL,
                    open NUMERIC(20,8) NOT NULL,
                    high NUMERIC(20,8) NOT NULL,
                    low NUMERIC(20,8) NOT NULL,
                    close NUMERIC(20,8) NOT NULL,
                    tick_volume BIGINT NOT NULL DEFAULT 0,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    PRIMARY KEY (symbol, timeframe, candle_time)
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_mt5_closed_candles_symbol_frame_time ON mt5_closed_candles (symbol, timeframe, candle_time DESC)")
            conn.execute("DELETE FROM mt5_closed_candles WHERE candle_time < NOW() - INTERVAL '90 days'")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS comex_market_events (
                    source TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    contract TEXT NOT NULL,
                    event_time TIMESTAMPTZ NOT NULL,
                    event_type TEXT NOT NULL CHECK (event_type IN ('TRADE','DEPTH')),
                    payload JSONB NOT NULL,
                    received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    PRIMARY KEY (source, event_id)
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_comex_events_contract_time ON comex_market_events (contract, event_time DESC)")
            conn.execute("DELETE FROM comex_market_events WHERE event_time < NOW() - INTERVAL '90 days'")
        return True
    except Exception:
        logger.exception("GoldAI learning database initialization failed")
        return False


class ClosedDeal(BaseModel):
    deal_ticket: int = Field(gt=0)
    position_id: int = Field(gt=0)
    deal_time: datetime
    symbol: str = Field(min_length=1, max_length=32)
    direction: str
    volume: float = Field(gt=0, le=1000)
    price: float = Field(gt=0)
    profit: float
    commission: float = 0
    swap: float = 0
    reason: int = 0


class ClosedDealBatch(BaseModel):
    deals: list[ClosedDeal] = Field(min_length=1, max_length=100)


def save_opportunity(analysis: dict[str, Any]) -> bool:
    """Persist a deduplicated analysis observation, including WAIT blockers."""
    try:
        market = analysis.get("market_data") or {}
        created = analysis.get("generated_at") or datetime.now(timezone.utc)
        if isinstance(created, str):
            created = datetime.fromisoformat(created.replace("Z", "+00:00"))
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        dossier = analysis.get("technical_dossier") or {}
        entry_engine = analysis.get("entry_engine") or {}
        plan = analysis.get("setup_plan") or {}
        symbol = str(analysis.get("symbol") or "GOLD").upper()
        decision = str(analysis.get("decision") or "WAIT").upper()
        blockers = analysis.get("blockers") or []
        # Preserve the exact decision-time evidence so closed trades can later be
        # evaluated against what the engine actually knew, not a reconstructed chart.
        context = {
            "market_data": market,
            "technical_dossier": dossier,
            "entry_engine": entry_engine,
            "setup_plan": plan,
            "decision": decision,
            "blockers": blockers,
            "reasons": analysis.get("reasons") or [],
        }
        fingerprint_source = {
            "symbol": symbol,
            "decision": decision,
            "blockers": blockers,
            "latest_candle_times": market.get("latest_candle_times", {}),
            "confluence_score": dossier.get("confluence_score"),
            "entry_score": entry_engine.get("effective_entry_quality_score_0_to_100",
                                            entry_engine.get("entry_quality_score_0_to_100")),
            "entry_direction": entry_engine.get("direction"),
            "entry_strategy": entry_engine.get("selected_strategy"),
            "strategy": plan.get("strategy") or dossier.get("setup_type"),
        }
        fingerprint = hashlib.sha256(
            json.dumps(fingerprint_source, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
        with _connect() as conn:
            conn.execute("""
                INSERT INTO opportunity_log
                    (fingerprint, created_at, symbol, decision, status, direction,
                     confluence_score, strategy, blockers, market_context)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb)
                ON CONFLICT (fingerprint) DO NOTHING
            """, (
                fingerprint, created, symbol, decision,
                "CANDIDATE" if decision in ("BUY", "SELL") else "REJECTED_OR_WAIT",
                decision if decision in ("BUY", "SELL") else None,
                float(dossier["confluence_score"]) if dossier.get("confluence_score") is not None else None,
                str(plan.get("strategy") or dossier.get("setup_type") or "") or None,
                json.dumps(blockers, default=str),
                json.dumps(context, default=str),
            ))
        return True
    except Exception:
        logger.exception("GoldAI opportunity observation could not be persisted")
        return False


def _require_bridge(authorization: str | None) -> None:
    import hmac
    expected = os.getenv("MT5_BRIDGE_TOKEN", "").strip()
    supplied = (authorization or "").removeprefix("Bearer ").strip()
    if not expected or not supplied or not hmac.compare_digest(expected, supplied):
        raise HTTPException(status_code=401, detail="Invalid or unconfigured MT5 bridge token.")


@router.post("/trades/journal")
def ingest_closed_deals(batch: ClosedDealBatch, authorization: str | None = Header(default=None)) -> dict:
    _require_bridge(authorization)
    normalized = []
    for deal in batch.deals:
        if deal.symbol.upper() != "GOLD":
            raise HTTPException(status_code=400, detail="Only GOLD journal deals are accepted.")
        direction = deal.direction.upper()
        if direction not in {"BUY", "SELL", "OTHER"}:
            raise HTTPException(status_code=422, detail="Invalid deal direction.")
        stamp = deal.deal_time
        if stamp.tzinfo is None:
            raise HTTPException(status_code=422, detail="deal_time must include a timezone.")
        normalized.append((
            deal.deal_ticket, deal.position_id, stamp.astimezone(timezone.utc),
            "GOLD", direction, deal.volume, deal.price, deal.profit,
            deal.commission, deal.swap, deal.reason,
        ))
    try:
        with _connect() as conn:
            inserted = 0
            for row in normalized:
                result = conn.execute("""
                    INSERT INTO trade_journal
                        (deal_ticket, position_id, deal_time, symbol, direction, volume,
                         price, profit, commission, swap, reason)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (deal_ticket) DO NOTHING
                """, row)
                inserted += result.rowcount
        return {"accepted": True, "received": len(normalized), "inserted": inserted, "duplicates": len(normalized) - inserted}
    except Exception as exc:
        logger.exception("GoldAI closed-deal journal write failed")
        raise HTTPException(status_code=503, detail="Trade journal database is unavailable; retry the upload.") from exc


@router.get("/summary")
def learning_summary() -> dict:
    try:
        with _connect() as conn:
            trades = conn.execute("""
                WITH positions AS (
                    SELECT position_id, SUM(profit + commission + swap) AS net_result
                    FROM trade_journal
                    WHERE symbol = 'GOLD'
                    GROUP BY position_id
                )
                SELECT COUNT(*) AS total,
                       COUNT(*) FILTER (WHERE net_result > 0) AS wins,
                       COUNT(*) FILTER (WHERE net_result < 0) AS losses,
                       COALESCE(SUM(net_result), 0) AS net_profit,
                       COALESCE(AVG(net_result), 0) AS expectancy
                FROM positions
            """).fetchone()
            opportunities = conn.execute("""
                SELECT COUNT(*) AS total,
                       COUNT(*) FILTER (WHERE decision IN ('BUY','SELL')) AS candidates,
                       COUNT(*) FILTER (WHERE decision NOT IN ('BUY','SELL')) AS waits
                FROM opportunity_log WHERE symbol = 'GOLD'
            """).fetchone()
        total = int(trades["total"])
        wins = int(trades["wins"])
        return {
            "database_ready": True,
            "closed_trades": total,
            "wins": wins,
            "losses": int(trades["losses"]),
            "win_rate_percent": round(wins / total * 100, 2) if total else None,
            "net_profit_account_currency": float(trades["net_profit"]),
            "average_net_result_per_deal": float(trades["expectancy"]),
            "opportunities_recorded": int(opportunities["total"]),
            "trade_candidates": int(opportunities["candidates"]),
            "wait_or_rejected_opportunities": int(opportunities["waits"]),
            "learning_mode": "OBSERVE_AND_REPORT_ONLY",
            "automatic_strategy_changes_enabled": False,
            "note": "Statistics are descriptive; they are not a forecast or a measured probability of future profit.",
        }
    except Exception as exc:
        logger.exception("GoldAI learning summary unavailable")
        raise HTTPException(status_code=503, detail="Learning database is not ready or unavailable.") from exc


@router.get("/trades")
def recent_trades(limit: int = Query(default=50, ge=1, le=200)) -> dict:
    try:
        with _connect() as conn:
            rows = conn.execute("""
                SELECT deal_ticket, position_id, deal_time, symbol, direction, volume,
                       price, profit, commission, swap, reason
                FROM trade_journal ORDER BY deal_time DESC LIMIT %s
            """, (limit,)).fetchall()
        return {"items": [dict(row) for row in rows], "count": len(rows)}
    except Exception as exc:
        logger.exception("GoldAI learning trades unavailable")
        raise HTTPException(status_code=503, detail="Learning database is not ready or unavailable.") from exc


@router.get("/opportunities")
def recent_opportunities(limit: int = Query(default=50, ge=1, le=200)) -> dict:
    try:
        with _connect() as conn:
            rows = conn.execute("""
                SELECT created_at, symbol, decision, status, direction,
                       confluence_score, strategy, blockers, market_context
                FROM opportunity_log ORDER BY created_at DESC LIMIT %s
            """, (limit,)).fetchall()
        return {"items": [dict(row) for row in rows], "count": len(rows)}
    except Exception as exc:
        logger.exception("GoldAI learning opportunities unavailable")
        raise HTTPException(status_code=503, detail="Learning database is not ready or unavailable.") from exc



def save_mt5_candle_history(symbol: str, candles_by_timeframe: dict[str, list[Any]]) -> int:
    """Persist closed broker OHLC bars so Render restarts cannot erase pre-session context."""
    rows = []
    for timeframe, candles in candles_by_timeframe.items():
        for candle in candles:
            stamp = candle.time
            if stamp.tzinfo is None:
                continue
            rows.append((
                symbol.upper(), timeframe.upper(), stamp.astimezone(timezone.utc),
                float(candle.open), float(candle.high), float(candle.low),
                float(candle.close), int(getattr(candle, "tick_volume", 0) or 0),
            ))
    if not rows:
        return 0
    try:
        with _connect() as conn:
            for row in rows:
                conn.execute("""
                    INSERT INTO mt5_closed_candles
                        (symbol, timeframe, candle_time, open, high, low, close, tick_volume)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (symbol, timeframe, candle_time) DO UPDATE SET
                        open = EXCLUDED.open, high = EXCLUDED.high, low = EXCLUDED.low,
                        close = EXCLUDED.close, tick_volume = EXCLUDED.tick_volume,
                        updated_at = NOW()
                """, row)
            conn.execute("DELETE FROM mt5_closed_candles WHERE candle_time < NOW() - INTERVAL '90 days'")
        return len(rows)
    except Exception:
        logger.exception("GoldAI MT5 closed-candle history persistence failed")
        return 0


def load_mt5_candle_history(symbol: str, timeframes: list[str], limit: int = 500) -> dict[str, list[dict]]:
    """Restore the newest closed bars per timeframe after an API process restart."""
    restored: dict[str, list[dict]] = {}
    try:
        with _connect() as conn:
            for timeframe in timeframes:
                rows = conn.execute("""
                    SELECT candle_time AS time, open, high, low, close, tick_volume
                    FROM mt5_closed_candles
                    WHERE symbol = %s AND timeframe = %s
                      AND candle_time >= NOW() - INTERVAL '90 days'
                    ORDER BY candle_time DESC
                    LIMIT %s
                """, (symbol.upper(), timeframe.upper(), limit)).fetchall()
                restored[timeframe.upper()] = [
                    {**dict(row), "open": float(row["open"]), "high": float(row["high"]),
                     "low": float(row["low"]), "close": float(row["close"]),
                     "tick_volume": int(row["tick_volume"] or 0)}
                    for row in reversed(rows)
                ]
        return restored
    except Exception:
        logger.exception("GoldAI MT5 closed-candle history restore failed")
        return {}


def save_xm_quote_sample(*, symbol: str, timestamp: datetime, bid: float, ask: float,
                         last: float, spread_points: float, point: float, digits: int) -> bool:
    """Persist one sampled XM quote; never label tick activity as traded volume or delta."""
    try:
        if timestamp.tzinfo is None:
            return False
        with _connect() as conn:
            conn.execute("""
                INSERT INTO xm_quote_samples
                    (sample_time, symbol, bid, ask, last, spread_points, point, digits)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (sample_time) DO NOTHING
            """, (timestamp.astimezone(timezone.utc), symbol.upper(), bid, ask,
                  last, spread_points, point, digits))
        return True
    except Exception:
        logger.exception("GoldAI XM quote sample persistence failed")
        return False


@router.get("/orderflow/xm-status")
def xm_orderflow_status() -> dict:
    """Report durable XM quote sample coverage; no synthetic order-flow metrics."""
    try:
        with _connect() as conn:
            row = conn.execute("""
                SELECT COUNT(*) AS samples, MIN(sample_time) AS first_sample,
                       MAX(sample_time) AS last_sample,
                       COUNT(*) FILTER (WHERE ask < bid) AS crossed_quotes
                FROM xm_quote_samples
                WHERE sample_time >= NOW() - INTERVAL '90 days'
            """).fetchone()
        return {
            "source": "XM_MT5",
            "history_window_days": 90,
            "samples": int(row["samples"]),
            "first_sample_utc": row["first_sample"],
            "last_sample_utc": row["last_sample"],
            "crossed_quotes": int(row["crossed_quotes"]),
            "data_type": "sampled_bid_ask_quotes",
            "actual_aggressor_volume": "UNAVAILABLE",
            "market_depth": "NOT_CAPTURED",
            "delta": "NOT_CALCULATED",
            "retention_days": 90,
            "execution_enabled": False,
        }
    except Exception as exc:
        logger.exception("GoldAI XM order-flow status unavailable")
        raise HTTPException(status_code=503, detail="Order-flow database is unavailable.") from exc


class ComexMarketEvent(BaseModel):
    event_id: str = Field(min_length=1, max_length=160)
    contract: str = Field(min_length=2, max_length=40)
    timestamp: datetime
    event_type: str
    price: float | None = Field(default=None, gt=0)
    size: float | None = Field(default=None, gt=0)
    aggressor_side: str | None = None
    book_side: str | None = None
    level: int | None = Field(default=None, ge=1, le=50)
    action: str | None = None

    @model_validator(mode="after")
    def validate_event(self):
        self.event_type = self.event_type.upper()
        self.contract = self.contract.strip().upper()
        if not re.fullmatch(r"GC(?:[FGHJKMNQUVXZ]\d{1,2})?", self.contract):
            raise ValueError("contract must be GC or a valid COMEX GC delivery code")
        stamp = self.timestamp.astimezone(timezone.utc) if self.timestamp.tzinfo else None
        if stamp is None:
            raise ValueError("timestamp must include timezone")
        now = datetime.now(timezone.utc)
        if stamp < now - timedelta(days=90):
            raise ValueError("timestamp is outside the retained 90-day window")
        if stamp > now + timedelta(minutes=5):
            raise ValueError("timestamp is too far in the future")
        if self.event_type == "TRADE":
            if self.price is None or self.size is None:
                raise ValueError("TRADE events require price and size")
            if self.aggressor_side is not None:
                self.aggressor_side = self.aggressor_side.upper()
                if self.aggressor_side not in {"BUY", "SELL", "UNKNOWN"}:
                    raise ValueError("aggressor_side must be BUY, SELL, or UNKNOWN")
        elif self.event_type == "DEPTH":
            if self.book_side is None or self.level is None or self.price is None or self.size is None:
                raise ValueError("DEPTH events require book_side, level, price, and size")
            self.book_side = self.book_side.upper()
            if self.book_side not in {"BID", "ASK"}:
                raise ValueError("book_side must be BID or ASK")
            if self.action is not None:
                self.action = self.action.upper()
                if self.action not in {"UPSERT", "DELETE"}:
                    raise ValueError("depth action must be UPSERT or DELETE")
        else:
            raise ValueError("event_type must be TRADE or DEPTH")
        return self


class ComexMarketEventBatch(BaseModel):
    source: str = Field(min_length=2, max_length=80)
    events: list[ComexMarketEvent] = Field(min_length=1, max_length=500)


@router.post("/orderflow/comex/batch")
def ingest_comex_events(batch: ComexMarketEventBatch, authorization: str | None = Header(default=None)) -> dict:
    """Ingest vendor-licensed GC trades/depth. Requires a separate COMEX_DATA_TOKEN."""
    import hmac
    expected = os.getenv("COMEX_DATA_TOKEN", "").strip()
    supplied = (authorization or "").removeprefix("Bearer ").strip()
    if not expected or not supplied or not hmac.compare_digest(expected, supplied):
        raise HTTPException(status_code=401, detail="COMEX data ingestion is not configured or token is invalid.")
    rows = []
    for event in batch.events:
        rows.append((
            batch.source, event.event_id, event.contract.upper(),
            event.timestamp.astimezone(timezone.utc), event.event_type,
            json.dumps(event.model_dump(mode="json"), default=str),
        ))
    try:
        with _connect() as conn:
            inserted = 0
            for row in rows:
                result = conn.execute("""
                    INSERT INTO comex_market_events
                        (source, event_id, contract, event_time, event_type, payload)
                    VALUES (%s,%s,%s,%s,%s,%s::jsonb)
                    ON CONFLICT (source, event_id) DO NOTHING
                """, row)
                inserted += result.rowcount
        return {
            "accepted": True, "received": len(rows), "inserted": inserted,
            "duplicates": len(rows) - inserted,
            "source": batch.source,
            "delta_available_only_for_explicit_aggressor_tags": True,
            "execution_enabled": False,
        }
    except Exception as exc:
        logger.exception("GoldAI COMEX event ingestion failed")
        raise HTTPException(status_code=503, detail="COMEX event storage is unavailable; retry the batch.") from exc


@router.get("/orderflow/comex-status")
def comex_orderflow_status() -> dict:
    try:
        with _connect() as conn:
            row = conn.execute("""
                SELECT COUNT(*) AS events,
                       COUNT(*) FILTER (WHERE event_type = 'TRADE') AS trades,
                       COUNT(*) FILTER (WHERE event_type = 'DEPTH') AS depth_updates,
                       MIN(event_time) AS first_event,
                       MAX(event_time) AS last_event
                FROM comex_market_events
                WHERE event_time >= NOW() - INTERVAL '90 days'
            """).fetchone()
        return {
            "source": "COMEX_GC",
            "history_window_days": 90,
            "events": int(row["events"]),
            "trade_events": int(row["trades"]),
            "depth_updates": int(row["depth_updates"]),
            "first_event_utc": row["first_event"],
            "last_event_utc": row["last_event"],
            "vendor_connection": "NOT_CONFIGURED",
            "execution_enabled": False,
        }
    except Exception as exc:
        logger.exception("GoldAI COMEX status unavailable")
        raise HTTPException(status_code=503, detail="COMEX order-flow database is unavailable.") from exc



def load_orderflow_snapshot(contract: str = "GC", window_seconds: int = 300) -> dict:
    """Load and normalize recent licensed COMEX events for analysis fusion."""
    normalized_contract = contract.strip().upper()
    if not re.fullmatch(r"GC(?:[FGHJKMNQUVXZ][0-9]{1,2})?", normalized_contract):
        raise ValueError("Only COMEX Gold GC contracts are supported")
    with _connect() as conn:
        rows = conn.execute("""
            SELECT event_time, event_type, payload
            FROM comex_market_events
            WHERE (
                contract = %s
                OR (%s = 'GC' AND contract ~ '^GC[FGHJKMNQUVXZ][0-9]{1,2}$')
            )
              AND event_time >= NOW() - (%s * INTERVAL '1 second')
            ORDER BY event_time DESC
            LIMIT 10000
        """, (normalized_contract, normalized_contract, window_seconds)).fetchall()
    result = build_orderflow_snapshot(
        rows, now=datetime.now(timezone.utc), window_seconds=window_seconds
    )
    result["contract"] = normalized_contract
    result["history_days_retained"] = 90
    result["note"] = (
        "Explicit aggressor-tagged COMEX prints only. Depth/iceberg outputs are "
        "hypotheses, not observed hidden orders. COMEX is contextual data, not XM execution data."
    )
    return result


@router.get("/orderflow/snapshot")
def orderflow_snapshot(
    contract: str = Query(default="GC", min_length=2, max_length=40),
    window_seconds: int = Query(default=300, ge=60, le=900),
) -> dict:
    try:
        return load_orderflow_snapshot(contract, window_seconds)
    except Exception as exc:
        logger.exception("GoldAI order-flow snapshot unavailable")
        raise HTTPException(status_code=503, detail="Order-flow data is unavailable or the database is not ready.") from exc
