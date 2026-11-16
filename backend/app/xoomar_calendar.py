from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx


XOOMAR_CALENDAR_URL = "https://xoomar.com/api/markets/calendar"
XOOMAR_DOCS_URL = "https://xoomar.com/markets/api/calendar"
MAX_UPDATED_AGE = timedelta(hours=72)


async def load_xoomar_high_impact_events(
    days_ahead: int = 7,
    api_key: str = "",
) -> list[dict]:
    """Load and validate high-impact US releases from XOOMAR.

    XOOMAR aggregates agency schedules; its forecast field is intentionally null.
    Missing, malformed, timezone-naive, or stale data raises rather than clearing
    the news filter.
    """
    if not 1 <= days_ahead <= 14:
        raise ValueError("days_ahead must be between 1 and 14.")

    now = datetime.now(timezone.utc)
    params = {
        "from": (now.date() - timedelta(days=1)).isoformat(),
        "to": (now.date() + timedelta(days=days_ahead)).isoformat(),
        "importance": "high",
    }
    headers = {"X-API-Key": api_key} if api_key else {}

    async with httpx.AsyncClient(timeout=12.0) as client:
        response = await client.get(
            XOOMAR_CALENDAR_URL,
            params=params,
            headers=headers,
        )
        response.raise_for_status()
        payload = response.json()

    if not isinstance(payload, dict):
        raise ValueError("XOOMAR response envelope must be an object.")
    rows = payload.get("data")
    if not isinstance(rows, list):
        raise ValueError("XOOMAR data must be an array.")
    meta = payload.get("meta")
    source_attribution = meta.get("sourceAttribution") if isinstance(meta, dict) else None
    if not isinstance(source_attribution, str) or not source_attribution.strip():
        raise ValueError("XOOMAR source attribution is missing.")

    updated_raw = payload.get("updatedAt")
    if not isinstance(updated_raw, str) or not updated_raw.strip():
        raise ValueError("XOOMAR updatedAt is missing.")
    updated_at = datetime.fromisoformat(updated_raw.replace("Z", "+00:00"))
    if updated_at.tzinfo is None:
        raise ValueError("XOOMAR updatedAt must include a timezone.")
    age = now - updated_at.astimezone(timezone.utc)
    if age < timedelta(minutes=-10) or age > MAX_UPDATED_AGE:
        raise ValueError(
            "XOOMAR calendar update is outside the accepted freshness window "
            f"(updatedAt={updated_at.isoformat()}, age_hours={age.total_seconds() / 3600:.2f}, "
            f"max_age_hours={MAX_UPDATED_AGE.total_seconds() / 3600:.0f})."
        )

    events: list[dict] = []
    seen: set[tuple[str, datetime]] = set()
    for item in rows:
        if not isinstance(item, dict):
            raise ValueError("XOOMAR event row must be an object.")
        importance = str(item.get("importance", "")).lower()
        if importance != "high":
            continue
        name = item.get("eventName")
        raw_time = item.get("scheduledAt")
        source = item.get("source")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("High-impact event is missing eventName.")
        if not isinstance(raw_time, str) or not raw_time.strip():
            raise ValueError("High-impact event is missing scheduledAt.")
        event_time = datetime.fromisoformat(raw_time.replace("Z", "+00:00"))
        if event_time.tzinfo is None:
            raise ValueError("High-impact event scheduledAt must include a timezone.")
        event_time = event_time.astimezone(timezone.utc)
        if not isinstance(source, str) or not source.strip():
            raise ValueError("High-impact event is missing its issuing agency source.")
        key = (name.strip(), event_time)
        if key in seen:
            continue
        seen.add(key)
        events.append({
            "event_id": f"{source}:{event_time.isoformat()}:{name.strip()}",
            "event": name.strip(),
            "category": str(item.get("periodLabel") or ""),
            "date": event_time,
            "importance": 3,
            "date_span": "0",
            "actual": str(item.get("actual") or ""),
            "forecast": "",
            "previous": str(item.get("previous") or ""),
            "source": source,
            "source_url": str(item.get("canonicalUrl") or XOOMAR_DOCS_URL),
            "unit": str(item.get("unit") or ""),
            "forecast_available": False,
            "data_provider": "XOOMAR",
            "source_attribution": source_attribution,
            "data_updated_at": updated_at,
        })

    return sorted(events, key=lambda event: event["date"])
