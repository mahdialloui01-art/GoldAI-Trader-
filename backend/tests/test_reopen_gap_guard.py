from datetime import datetime, timedelta, timezone

from app.main import Candle, _reopen_gap_guard


def _candles_with_gap(price_gap: float, bars_after: int = 0) -> list[Candle]:
    start = datetime(2026, 11, 20, 8, 0, tzinfo=timezone.utc)
    rows = []
    price = 4000.0
    for index in range(20):
        stamp = start + timedelta(minutes=5 * index)
        rows.append(Candle(
            time=stamp,
            open=price,
            high=price + 1.0,
            low=price - 1.0,
            close=price + 0.2,
        ))
        price += 0.2

    reopen_time = rows[-1].time + timedelta(hours=48)
    reopen_price = rows[-1].close + price_gap
    rows.append(Candle(
        time=reopen_time,
        open=reopen_price,
        high=reopen_price + 1.0,
        low=reopen_price - 1.0,
        close=reopen_price + 0.2,
    ))
    for index in range(bars_after):
        stamp = reopen_time + timedelta(minutes=5 * (index + 1))
        open_price = reopen_price + 0.2 * (index + 1)
        rows.append(Candle(
            time=stamp,
            open=open_price,
            high=open_price + 1.0,
            low=open_price - 1.0,
            close=open_price + 0.2,
        ))
    return rows


def test_significant_reopen_gap_blocks_until_three_closed_bars():
    result = _reopen_gap_guard(_candles_with_gap(price_gap=4.0, bars_after=1))
    assert result["detected"] is True
    assert result["gap_type"] == "WEEKEND_OR_LONG_CLOSURE"
    assert result["guard_active"] is True
    assert result["bars_after_reopen"] == 1
    assert result["execution_enabled"] is False


def test_significant_reopen_gap_guard_releases_after_three_bars():
    result = _reopen_gap_guard(_candles_with_gap(price_gap=4.0, bars_after=3))
    assert result["detected"] is True
    assert result["guard_active"] is False
    assert result["status"] == "REOPEN_GAP_CONFIRMATION_WINDOW_COMPLETE"


def test_small_reopen_price_gap_does_not_block():
    result = _reopen_gap_guard(_candles_with_gap(price_gap=0.1, bars_after=0))
    assert result["detected"] is True
    assert result["guard_active"] is False
    assert result["status"] == "SMALL_PRICE_GAP_NO_BLOCK"


def test_no_time_gap_does_not_activate_guard():
    rows = _candles_with_gap(price_gap=4.0, bars_after=3)
    for index in range(1, len(rows)):
        rows[index].time = rows[index - 1].time + timedelta(minutes=5)
    result = _reopen_gap_guard(rows)
    assert result["detected"] is False
    assert result["guard_active"] is False
    assert result["status"] == "NO_TIME_GAP"
