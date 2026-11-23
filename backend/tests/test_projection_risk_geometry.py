from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.main import _derive_structural_invalidation, _atr_wilder


def _bars(center: float, width: float, count: int = 100):
    rows = []
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for i in range(count):
        close = center + width * (0.5 if i % 2 else -0.5)
        open_price = center - width * (0.5 if i % 2 else -0.5)
        rows.append(SimpleNamespace(
            time=start + timedelta(minutes=5 * i),
            open=open_price,
            high=max(open_price, close) + width * 0.25,
            low=min(open_price, close) - width * 0.25,
            close=close,
        ))
    return rows


def test_remote_h1_swing_cannot_create_remote_m5_invalidation():
    m5 = _bars(3640.0, 0.20)
    m15 = _bars(3640.0, 0.35)
    h1 = _bars(3690.0, 1.0)
    frames = {"M5": m5, "M15": m15, "H1": h1}
    reference = 3640.09
    atr_m5 = _atr_wilder(m5)

    invalidation, basis, reported_atr = _derive_structural_invalidation(
        "SELL", reference, frames, spread_price=0.03
    )

    distance = invalidation - reference
    assert distance > 0
    assert distance <= 2.5 * atr_m5
    assert reported_atr == atr_m5
    assert invalidation < 3690.0
    assert "M5_ATR" in basis or "bounded_M5_ATR" in basis


def test_projection_refuses_to_build_without_m5_volatility_reference():
    m15 = _bars(3640.0, 0.35)
    h1 = _bars(3640.0, 1.0)
    try:
        _derive_structural_invalidation("SELL", 3640.0, {"M15": m15, "H1": h1}, 0.02)
    except ValueError as exc:
        assert "M5 ATR unavailable" in str(exc)
    else:
        raise AssertionError("Expected M5 ATR fail-closed validation")
