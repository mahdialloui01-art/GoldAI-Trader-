from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

import psycopg
from psycopg.rows import dict_row
from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import BaseModel, Field

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
