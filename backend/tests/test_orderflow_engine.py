from datetime import datetime, timezone, timedelta

from app.orderflow_engine import build_orderflow_snapshot


NOW = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)


def event(seconds, kind, **payload):
    stamp = NOW - timedelta(seconds=seconds)
    return {"event_time": stamp, "event_type": kind,
            "payload": {"timestamp": stamp.isoformat(), "event_type": kind, **payload}}


def test_delta_footprint_and_unknown_volume_are_separated():
    rows = [
        event(20, "TRADE", price=2650.1, size=7, aggressor_side="BUY"),
        event(10, "TRADE", price=2650.1, size=3, aggressor_side="SELL"),
        event(5, "TRADE", price=2650.2, size=4, aggressor_side="UNKNOWN"),
    ]
    result = build_orderflow_snapshot(rows, now=NOW)
    assert result["tagged_buy_volume"] == 7
    assert result["tagged_sell_volume"] == 3
    assert result["tagged_trade_count"] == 2
    assert result["unknown_aggressor_volume"] == 4
    assert result["delta"] == 4
    assert result["delta_status"] == "VALID_EXPLICIT_AGGRESSOR"
    assert result["execution_enabled"] is False


def test_quote_only_or_unknown_trades_never_create_delta():
    rows = [event(1, "TRADE", price=2650.1, size=12, aggressor_side="UNKNOWN")]
    result = build_orderflow_snapshot(rows, now=NOW)
    assert result["delta"] is None
    assert result["delta_status"] == "UNAVAILABLE_NO_TAGGED_TRADES"


def test_depth_replenishment_is_only_a_low_confidence_candidate():
    rows = [
        event(30, "DEPTH", book_side="ASK", level=1, price=2650.1, size=8, action="UPSERT"),
        event(25, "TRADE", price=2650.1, size=6, aggressor_side="BUY"),
        event(20, "DEPTH", book_side="ASK", level=1, price=2650.1, size=7, action="UPSERT"),
        event(15, "TRADE", price=2650.1, size=6, aggressor_side="BUY"),
        event(10, "DEPTH", book_side="ASK", level=1, price=2650.1, size=9, action="UPSERT"),
    ]
    result = build_orderflow_snapshot(rows, now=NOW)
    assert result["depth_status"] == "AVAILABLE"
    assert result["iceberg_candidates"]
    assert result["iceberg_candidates"][0]["confidence"] == "LOW"


def test_stale_and_future_events_are_ignored():
    rows = [
        event(600, "TRADE", price=2650.1, size=8, aggressor_side="BUY"),
        {"event_time": NOW + timedelta(seconds=30), "event_type": "TRADE",
         "payload": {"price": 2650.2, "size": 5, "aggressor_side": "BUY"}},
    ]
    result = build_orderflow_snapshot(rows, now=NOW)
    assert result["event_count"] == 0
    assert result["delta"] is None


def test_unknown_aggressor_does_not_increase_tagged_trade_count():
    rows = [
        event(20, "TRADE", price=2650.1, size=7, aggressor_side="BUY"),
        event(10, "TRADE", price=2650.1, size=3, aggressor_side="UNKNOWN"),
    ]
    result = build_orderflow_snapshot(rows, now=NOW)
    assert result["trade_count"] == 2
    assert result["tagged_trade_count"] == 1
    assert result["data_quality"]["explicit_aggressor_coverage_percent"] == 70.0
