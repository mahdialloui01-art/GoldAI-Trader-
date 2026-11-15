from datetime import datetime, timezone

import pytest

from app.calendar_import import parse_forex_factory_weekly


def test_parses_only_high_impact_usd_events_and_normalizes_timezone():
    payload = [
        {"title": "Core CPI m/m", "country": "USD", "date": "2026-11-10T08:30:00-05:00",
         "impact": "High", "forecast": "0.2%", "previous": "0.3%"},
        {"title": "GDP q/q", "country": "USD", "date": "2026-11-10T09:00:00-05:00",
         "impact": "Medium"},
        {"title": "CPI y/y", "country": "CNY", "date": "2026-11-10T20:30:00-05:00",
         "impact": "High"},
    ]
    events = parse_forex_factory_weekly(payload)
    assert len(events) == 1
    event = events[0]
    assert event["event"] == "Core CPI m/m"
    assert event["date"] == datetime(2026, 11, 10, 13, 30, tzinfo=timezone.utc)
    assert event["gold_relevance"] == "INFLATION"
    assert event["actual_available"] is False
    assert event["data_provenance"] == "CLIENT_SUPPLIED_NOT_INDEPENDENTLY_VERIFIED"


def test_rejects_timestamp_without_timezone():
    payload = [{"title": "CPI", "country": "USD", "date": "2026-11-10T08:30:00",
                "impact": "High"}]
    with pytest.raises(ValueError, match="timezone offset"):
        parse_forex_factory_weekly(payload)


def test_rejects_non_array_payload():
    with pytest.raises(ValueError, match="JSON array"):
        parse_forex_factory_weekly({"title": "CPI"})


def test_deduplicates_identical_event_rows():
    row = {"title": "PPI m/m", "country": "USD", "date": "2026-11-13T08:30:00-05:00",
           "impact": "High", "forecast": "0.4%"}
    assert len(parse_forex_factory_weekly([row, row])) == 1
