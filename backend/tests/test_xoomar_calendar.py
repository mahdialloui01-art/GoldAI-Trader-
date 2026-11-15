from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app import xoomar_calendar


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class FakeClient:
    payload = None
    request = None

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get(self, url, params=None, headers=None):
        FakeClient.request = (url, params, headers)
        return FakeResponse(FakeClient.payload)


def envelope(rows, updated=None):
    return {
        "updatedAt": (updated or datetime.now(timezone.utc)).isoformat(),
        "data": rows,
    }


def event(**overrides):
    row = {
        "source": "bls",
        "eventName": "Consumer Price Index",
        "importance": "high",
        "scheduledAt": (datetime.now(timezone.utc) + timedelta(days=2)).isoformat(),
        "periodLabel": "October 2026",
        "previous": "0.3",
        "forecast": None,
        "actual": None,
        "unit": "% m/m",
    }
    row.update(overrides)
    return row


@pytest.mark.asyncio
async def test_loads_high_impact_events_without_key_and_never_invents_forecast(monkeypatch):
    FakeClient.payload = envelope([event(), event(importance="med")])
    monkeypatch.setattr(xoomar_calendar.httpx, "AsyncClient", FakeClient)

    result = await xoomar_calendar.load_xoomar_high_impact_events()

    assert len(result) == 1
    assert result[0]["event"] == "Consumer Price Index"
    assert result[0]["forecast"] == ""
    assert result[0]["forecast_available"] is False
    assert result[0]["data_provider"] == "XOOMAR"
    assert FakeClient.request[2] == {}


@pytest.mark.asyncio
async def test_optional_api_key_is_sent_only_as_header(monkeypatch):
    FakeClient.payload = envelope([event()])
    monkeypatch.setattr(xoomar_calendar.httpx, "AsyncClient", FakeClient)

    await xoomar_calendar.load_xoomar_high_impact_events(api_key="secret-test-key")

    assert FakeClient.request[2] == {"X-API-Key": "secret-test-key"}
    assert "secret-test-key" not in str(FakeClient.request[1])


@pytest.mark.asyncio
async def test_rejects_stale_calendar(monkeypatch):
    FakeClient.payload = envelope([], datetime.now(timezone.utc) - timedelta(days=4))
    monkeypatch.setattr(xoomar_calendar.httpx, "AsyncClient", FakeClient)

    with pytest.raises(ValueError, match="freshness"):
        await xoomar_calendar.load_xoomar_high_impact_events()


@pytest.mark.asyncio
async def test_rejects_timezone_naive_release_time(monkeypatch):
    FakeClient.payload = envelope([event(scheduledAt="2026-11-20T13:30:00")])
    monkeypatch.setattr(xoomar_calendar.httpx, "AsyncClient", FakeClient)

    with pytest.raises(ValueError, match="timezone"):
        await xoomar_calendar.load_xoomar_high_impact_events()


@pytest.mark.asyncio
async def test_rejects_missing_data_array(monkeypatch):
    FakeClient.payload = {"updatedAt": datetime.now(timezone.utc).isoformat()}
    monkeypatch.setattr(xoomar_calendar.httpx, "AsyncClient", FakeClient)

    with pytest.raises(ValueError, match="array"):
        await xoomar_calendar.load_xoomar_high_impact_events()
