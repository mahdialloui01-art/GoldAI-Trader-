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
