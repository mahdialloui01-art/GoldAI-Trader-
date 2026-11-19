import pytest
from pydantic import ValidationError

from app.learning_store import ComexMarketEvent


def test_comex_trade_requires_explicit_price_and_size():
    with pytest.raises(ValidationError):
        ComexMarketEvent(
            event_id="trade-1", contract="GCZ6",
            timestamp="2026-11-19T10:00:00Z", event_type="TRADE",
        )


def test_comex_trade_keeps_unknown_aggressor_unknown():
    event = ComexMarketEvent(
        event_id="trade-2", contract="GCZ6",
        timestamp="2026-11-19T10:00:00Z", event_type="TRADE",
        price=2650.2, size=4, aggressor_side="UNKNOWN",
    )
    assert event.event_type == "TRADE"
    assert event.aggressor_side == "UNKNOWN"


def test_comex_depth_requires_book_side_and_level():
    with pytest.raises(ValidationError):
        ComexMarketEvent(
            event_id="depth-1", contract="GCZ6",
            timestamp="2026-11-19T10:00:00Z", event_type="DEPTH",
            price=2650.0, size=20,
        )



def test_comex_contract_codes_are_normalized_and_validated():
    event = ComexMarketEvent(
        event_id="trade-3", contract="gcz6",
        timestamp="2026-11-19T10:00:00Z", event_type="TRADE",
        price=2650.2, size=4, aggressor_side="BUY",
    )
    assert event.contract == "GCZ6"


def test_comex_rejects_non_gold_contracts():
    with pytest.raises(ValidationError):
        ComexMarketEvent(
            event_id="trade-4", contract="SIH7",
            timestamp="2026-11-19T10:00:00Z", event_type="TRADE",
            price=31.2, size=1, aggressor_side="BUY",
        )


def test_comex_rejects_events_outside_retention_window():
    from datetime import datetime, timedelta, timezone

    old_timestamp = (datetime.now(timezone.utc) - timedelta(days=91)).isoformat()
    with pytest.raises(ValidationError):
        ComexMarketEvent(
            event_id="trade-5", contract="GCZ6",
            timestamp=old_timestamp, event_type="TRADE",
            price=2650.2, size=4, aggressor_side="BUY",
        )

def test_generic_gc_snapshot_only_matches_valid_monthly_contracts(monkeypatch):
    from app import learning_store

    class FakeResult:
        def fetchall(self):
            return []

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, query, params):
            assert "contract ~ '^GC[FGHJKMNQUVXZ][0-9]{1,2}$'" in query
            assert params[:2] == ("GC", "GC")
            return FakeResult()

    monkeypatch.setattr(learning_store, "_connect", lambda: FakeConnection())
    monkeypatch.setattr(
        learning_store,
        "build_orderflow_snapshot",
        lambda rows, now, window_seconds: {"status": "NO_DATA"},
    )

    result = learning_store.load_orderflow_snapshot("GC", 300)
    assert result["contract"] == "GC"
    assert result["history_days_retained"] == 90

