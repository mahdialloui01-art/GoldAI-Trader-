#!/usr/bin/env python3
"""Normalize and audit GoldAI XM/COMEX order-flow exports without inventing volume.

Accepted CSV columns (case-insensitive):
  timestamp (ISO-8601), bid, ask, last, bid_size, ask_size,
  trade_price, trade_size, aggressor_side (BUY/SELL/UNKNOWN), source, symbol.
XM quote ticks do not contain executed buyer/seller contract volume. Delta is
computed only when an explicit aggressor_side and positive trade_size exist.
"""
from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median


def parse_time(value: str) -> datetime:
    text = value.strip().replace("Z", "+00:00")
    stamp = datetime.fromisoformat(text)
    if stamp.tzinfo is None:
        raise ValueError("timestamp must include a UTC offset or Z")
    return stamp.astimezone(timezone.utc)


def number(row: dict, key: str) -> float | None:
    raw = (row.get(key) or "").strip()
    if not raw:
        return None
    value = float(raw)
    if value < 0:
        raise ValueError(f"{key} cannot be negative")
    return value


def inspect_csv(path: Path, source: str, days: int = 90, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)
    seen: set[tuple] = set()
    stamps: list[datetime] = []
    spreads: list[float] = []
    deltas = Counter()
    quote_count = trade_count = depth_count = invalid_count = duplicate_count = outside_window = 0
    symbols = Counter()
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise ValueError(f"{path}: missing CSV header")
        for raw in reader:
            row = {str(k).strip().lower(): (v or "").strip() for k, v in raw.items() if k}
            try:
                ts = parse_time(row["timestamp"])
                if ts < cutoff or ts > now + timedelta(minutes=5):
                    outside_window += 1
                    continue
                bid, ask = number(row, "bid"), number(row, "ask")
                last = number(row, "last")
                trade_price = number(row, "trade_price")
                trade_size = number(row, "trade_size")
                bid_size, ask_size = number(row, "bid_size"), number(row, "ask_size")
                if bid is not None and ask is not None:
                    if ask < bid:
                        raise ValueError("ask below bid")
                    quote_count += 1
                    spreads.append(ask - bid)
                if trade_price is not None and trade_size is not None and trade_size > 0:
                    trade_count += 1
                    side = (row.get("aggressor_side") or "UNKNOWN").upper()
                    if side in {"BUY", "SELL"}:
                        deltas[side] += trade_size
                if bid_size is not None or ask_size is not None:
                    depth_count += 1
                key = (ts.isoformat(), bid, ask, last, trade_price, trade_size,
                       (row.get("aggressor_side") or "").upper(), bid_size, ask_size)
                if key in seen:
                    duplicate_count += 1
                    continue
                seen.add(key)
                stamps.append(ts)
                if row.get("symbol"):
                    symbols[row["symbol"]] += 1
            except (KeyError, ValueError, TypeError):
                invalid_count += 1
    stamps.sort()
    gaps = [(b - a).total_seconds() for a, b in zip(stamps, stamps[1:])]
    explicit_delta = deltas["BUY"] - deltas["SELL"]
    return {
        "file": path.name, "source": source, "window_days": days,
        "window_start_utc": cutoff.isoformat(), "window_end_utc": now.isoformat(),
        "rows_unique": len(stamps), "quote_rows": quote_count, "trade_rows": trade_count,
        "depth_rows": depth_count, "duplicate_rows": duplicate_count,
        "invalid_rows": invalid_count, "outside_window_rows": outside_window,
        "first_timestamp_utc": stamps[0].isoformat() if stamps else None,
        "last_timestamp_utc": stamps[-1].isoformat() if stamps else None,
        "largest_gap_seconds": max(gaps, default=None),
        "median_spread_price_units": median(spreads) if spreads else None,
        "explicit_buy_volume": deltas["BUY"] if trade_count else None,
        "explicit_sell_volume": deltas["SELL"] if trade_count else None,
        "explicit_delta": explicit_delta if trade_count and (deltas["BUY"] or deltas["SELL"]) else None,
        "delta_status": "AGGRESSOR_TAGGED_ONLY" if deltas["BUY"] or deltas["SELL"] else "UNAVAILABLE_NOT_INFERRED",
        "depth_status": "DEPTH_FIELDS_PRESENT" if depth_count else "NO_DEPTH_FIELDS",
        "symbols": dict(symbols),
        "limitations": [
            "XM bid/ask ticks and tick volume are not executed contract-side volume.",
            "Delta is omitted unless each trade has an explicit BUY/SELL aggressor tag.",
            "Displayed depth is not proof of hidden or iceberg liquidity.",
            "COMEX GC and XM GOLD are separate markets; compare synchronized returns/basis, not raw prices.",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--xm", type=Path, help="XM MT5 tick CSV export")
    parser.add_argument("--comex", type=Path, help="COMEX GC trades/depth CSV export")
    parser.add_argument("--days", type=int, default=90)
    parser.add_argument("--output", type=Path, default=Path("orderflow-data-quality.json"))
    args = parser.parse_args()
    if args.days != 90:
        parser.error("GoldAI research window is fixed at 90 days for this run")
    if not args.xm and not args.comex:
        parser.error("provide --xm and/or --comex")
    reports = []
    for source, path in (("XM_MT5", args.xm), ("COMEX_GC", args.comex)):
        if path:
            reports.append(inspect_csv(path, source, args.days))
    result = {
        "project": "GoldAI Order Flow Intelligence", "selected_sources": ["XM_MT5", "COMEX_GC"],
        "requested_history_days": 90, "reports": reports,
        "readiness": "AUDIT_ONLY",
        "live_trading_enabled": False,
        "note": "This tool audits supplied exports; it does not download paid COMEX data or fabricate missing fields.",
    }
    args.output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
