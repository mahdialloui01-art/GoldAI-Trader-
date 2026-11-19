import csv
from datetime import datetime, timezone
from pathlib import Path

from tools.orderflow_data_quality import inspect_csv


def test_delta_requires_explicit_aggressor_and_depth_is_separate(tmp_path: Path):
    path = tmp_path / "gc.csv"
    rows = [
        {"timestamp": "2026-11-19T10:00:00Z", "trade_price": "2650.1", "trade_size": "3", "aggressor_side": "BUY", "bid_size": "12", "ask_size": "8"},
        {"timestamp": "2026-11-19T10:00:01Z", "trade_price": "2650.0", "trade_size": "2", "aggressor_side": "SELL"},
        {"timestamp": "2026-11-19T10:00:02Z", "bid": "2650.0", "ask": "2650.2", "last": "2650.1"},
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=sorted({k for row in rows for k in row}))
        writer.writeheader()
        writer.writerows(rows)
    now = datetime(2026, 11, 19, 10, 1, tzinfo=timezone.utc)
    report = inspect_csv(path, "COMEX_GC", days=90, now=now)
    assert report["rows_unique"] == 3
    assert report["explicit_buy_volume"] == 3
    assert report["explicit_sell_volume"] == 2
    assert report["explicit_delta"] == 1
    assert report["depth_status"] == "DEPTH_FIELDS_PRESENT"


def test_quote_ticks_do_not_create_fake_delta(tmp_path: Path):
    path = tmp_path / "xm.csv"
    path.write_text(
        "timestamp,bid,ask,last,tick_volume\n"
        "2026-11-19T10:00:00Z,2650.0,2650.2,2650.1,42\n",
        encoding="utf-8",
    )
    report = inspect_csv(path, "XM_MT5", now=datetime(2026, 11, 19, 10, 1, tzinfo=timezone.utc))
    assert report["quote_rows"] == 1
    assert report["explicit_delta"] is None
    assert report["delta_status"] == "UNAVAILABLE_NOT_INFERRED"
