"""Conservative COMEX GC order-flow feature extraction.

Only explicitly aggressor-tagged prints contribute to Delta/CVD. Depth is
reported separately; replenishment/iceberg signals are hypotheses, never facts.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from math import isfinite


def _utc(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        return None
    return value.astimezone(timezone.utc)


def build_orderflow_snapshot(events, *, now=None, window_seconds=300, tick_size=0.1):
    now = _utc(now or datetime.now(timezone.utc))
    if now is None or not isfinite(float(tick_size)) or tick_size <= 0:
        raise ValueError("A timezone-aware clock and positive tick_size are required")
    cutoff = now - timedelta(seconds=window_seconds)
    recent = []
    for row in events:
        payload = row.get("payload") or {}
        stamp = _utc(row.get("event_time") or payload.get("timestamp"))
        if stamp is None or stamp < cutoff or stamp > now + timedelta(seconds=5):
            continue
        recent.append((stamp, row.get("event_type") or payload.get("event_type"), payload))
    recent.sort(key=lambda item: item[0])

    buy = sell = unknown = 0.0
    trade_count = 0
    footprint = defaultdict(lambda: {"buy_volume": 0.0, "sell_volume": 0.0, "unknown_volume": 0.0})
    trade_prices = []
    for stamp, kind, payload in recent:
        if str(kind).upper() != "TRADE":
            continue
        price, size = payload.get("price"), payload.get("size")
        try:
            price, size = float(price), float(size)
        except (TypeError, ValueError):
            continue
        if not isfinite(price) or not isfinite(size) or price <= 0 or size <= 0:
            continue
        side = str(payload.get("aggressor_side") or "UNKNOWN").upper()
        bucket = round(round(price / tick_size) * tick_size, 8)
        trade_count += 1
        trade_prices.append(price)
        if side == "BUY":
            buy += size
            footprint[bucket]["buy_volume"] += size
        elif side == "SELL":
            sell += size
            footprint[bucket]["sell_volume"] += size
        else:
            unknown += size
            footprint[bucket]["unknown_volume"] += size

    depth = {}
    replenishments = defaultdict(int)
    for stamp, kind, payload in recent:
        if str(kind).upper() != "DEPTH":
            continue
        side = str(payload.get("book_side") or "").upper()
        level = payload.get("level")
        price, size = payload.get("price"), payload.get("size")
        try:
            price, size, level = float(price), float(size), int(level)
        except (TypeError, ValueError):
            continue
        if side not in {"BID", "ASK"} or price <= 0 or size < 0 or level < 1:
            continue
        key = (side, level, round(price / tick_size))
        action = str(payload.get("action") or "UPSERT").upper()
        if action == "DELETE":
            depth.pop(key, None)
        elif action == "UPSERT":
            if key in depth:
                replenishments[key] += 1
            depth[key] = {"side": side, "level": level, "price": price,
                          "size": size, "timestamp": stamp}

    total_tagged = buy + sell
    total_volume = total_tagged + unknown
    delta = buy - sell if total_tagged > 0 else None
    cvd = delta  # rolling-window CVD anchor; cumulative history is a future durable series.
    footprint_rows = []
    for price, values in sorted(footprint.items()):
        tagged = values["buy_volume"] + values["sell_volume"]
        imbalance = (values["buy_volume"] / values["sell_volume"]
                     if values["sell_volume"] > 0 else None)
        footprint_rows.append({
            "price": price, **{k: round(v, 6) for k, v in values.items()},
            "delta": round(values["buy_volume"] - values["sell_volume"], 6)
                     if tagged > 0 else None,
            "buy_sell_imbalance_ratio": round(imbalance, 4) if imbalance is not None else None,
        })

    absorption = []
    if len(trade_prices) >= 2 and total_tagged >= 10:
        price_range = max(trade_prices) - min(trade_prices)
        if price_range <= 2 * tick_size:
            pressure = max(buy, sell) / total_tagged
            if pressure >= 0.70:
                absorption.append({
                    "type": "POSSIBLE_ABSORPTION",
                    "dominant_aggressor": "BUY" if buy > sell else "SELL",
                    "tagged_volume": round(total_tagged, 6),
                    "observed_trade_range": round(price_range, 8),
                    "confidence": "LOW_TO_MODERATE",
                    "note": "Aggressive volume with limited observed price range; requires full tick sequence and execution context.",
                })

    iceberg = []
    for key, count in replenishments.items():
        if count >= 2:
            side, level, price_tick = key
            price = round(price_tick * tick_size, 8)
            opposing_volume = (buy if side == "ASK" else sell)
            volume_at_price = sum(
                row["buy_volume"] + row["sell_volume"]
                for row in footprint_rows if abs(row["price"] - price) <= tick_size / 2
            )
            if opposing_volume >= 5 and volume_at_price >= 5:
                iceberg.append({
                    "type": "POSSIBLE_REPLENISHING_LIQUIDITY",
                    "book_side": side, "level": level, "price": price,
                    "depth_replenishments": count,
                    "executed_volume_at_price": round(volume_at_price, 6),
                    "confidence": "LOW",
                    "note": "Repeated displayed-size refresh near aggressive executions; not proof of a hidden iceberg order.",
                })

    depth_levels = [
        {"side": item["side"], "level": item["level"], "price": item["price"],
         "displayed_size": item["size"]}
        for item in sorted(depth.values(), key=lambda x: (x["side"], x["level"]))
    ]
    last_event = max((item[0] for item in recent), default=None)
    has_trades = trade_count > 0
    has_depth = bool(depth_levels)
    return {
        "source": "COMEX_GC",
        "contract": "GC",
        "window_seconds": window_seconds,
        "event_count": len(recent),
        "trade_count": trade_count,
        "tagged_buy_volume": round(buy, 6),
        "tagged_sell_volume": round(sell, 6),
        "unknown_aggressor_volume": round(unknown, 6),
        "delta": round(delta, 6) if delta is not None else None,
        "cvd_window_delta": round(cvd, 6) if cvd is not None else None,
        "delta_status": "VALID_EXPLICIT_AGGRESSOR" if delta is not None else "UNAVAILABLE_NO_TAGGED_TRADES",
        "footprint": footprint_rows,
        "depth_levels": depth_levels,
        "depth_status": "AVAILABLE" if has_depth else "UNAVAILABLE",
        "absorption_candidates": absorption,
        "iceberg_candidates": iceberg,
        "last_event_utc": last_event.isoformat() if last_event else None,
        "freshness_seconds": round((now - last_event).total_seconds(), 3) if last_event else None,
        "data_quality": {
            "trade_data_available": has_trades,
            "explicit_aggressor_coverage_percent": round(total_tagged / total_volume * 100, 2) if total_volume else None,
            "depth_data_available": has_depth,
            "hidden_liquidity_is_inferred_not_observed": True,
            "xm_gold_is_not_comex_gc": True,
        },
        "execution_enabled": False,
    }
