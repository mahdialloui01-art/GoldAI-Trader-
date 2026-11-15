"""Strict parser for user-supplied Forex Factory weekly JSON exports.

This module parses data for inspection/research only. It does not certify source
freshness, completeness, licensing, or authorize trading.
"""
from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
from typing import Any


def _gold_relevance(title: str) -> str:
    name = title.casefold()
    groups = {
        "MONETARY_POLICY": ("interest rate", "fomc", "fed ", "federal reserve", "rate decision", "powell", "minutes"),
        "INFLATION": ("cpi", "ppi", "pce", "inflation"),
        "LABOR": ("nonfarm", "payroll", "unemployment", "jobless", "employment", "adp"),
        "GROWTH": ("gdp", "retail sales", "ism ", "pmi", "industrial production"),
        "TREASURY": ("bond auction", "treasury", "10-y", "30-y", "20-y"),
        "CENTRAL_BANK_SPEECH": ("speaks", "testifies", "speech"),
    }
    for category, terms in groups.items():
        if any(term in name for term in terms):
            return category
    return "OTHER_USD_EVENT"


def parse_forex_factory_weekly(payload: Any) -> list[dict]:
    """Return normalized USD high-impact events from a Forex Factory JSON array."""
    if not isinstance(payload, list):
        raise ValueError("Calendar export must be a JSON array.")
    normalized = []
    seen = set()
    for index, item in enumerate(payload):
        if not isinstance(item, dict):
            raise ValueError(f"Calendar row {index} must be an object.")
        title = item.get("title")
        country = item.get("country")
        raw_date = item.get("date")
        impact = item.get("impact")
        if not isinstance(title, str) or not title.strip():
            raise ValueError(f"Calendar row {index} is missing a title.")
        if not isinstance(country, str) or not country.strip():
            raise ValueError(f"Calendar row {index} is missing a currency.")
        if not isinstance(raw_date, str) or not raw_date.strip():
            raise ValueError(f"Calendar row {index} is missing a timestamp.")
        try:
            event_time = datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"Calendar row {index} has an invalid timestamp.") from exc
        if event_time.tzinfo is None or event_time.utcoffset() is None:
            raise ValueError(f"Calendar row {index} timestamp must include a timezone offset.")
        if not isinstance(impact, str):
            raise ValueError(f"Calendar row {index} has an invalid impact value.")
        if country.upper() != "USD" or impact.casefold() != "high":
            continue
        event_utc = event_time.astimezone(timezone.utc)
        key = f"{title.strip()}|USD|{event_utc.isoformat()}"
        event_id = sha256(key.encode("utf-8")).hexdigest()[:20]
        if event_id in seen:
            continue
        seen.add(event_id)
        normalized.append({
            "event_id": event_id,
            "event": title.strip(),
            "currency": "USD",
            "date": event_utc,
            "original_date": raw_date,
            "original_timezone_offset": event_time.strftime("%z"),
            "impact": "High",
            "gold_relevance": _gold_relevance(title),
            "forecast": str(item.get("forecast") or ""),
            "previous": str(item.get("previous") or ""),
            "actual": str(item.get("actual") or ""),
            "actual_available": bool(str(item.get("actual") or "").strip()),
            "source": "Forex Factory weekly JSON export (user supplied)",
            "data_provenance": "CLIENT_SUPPLIED_NOT_INDEPENDENTLY_VERIFIED",
        })
    return sorted(normalized, key=lambda event: event["date"])
