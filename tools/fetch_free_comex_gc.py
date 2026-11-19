#!/usr/bin/env python3
"""Fetch free-tier COMEX Gold (GC) daily bars from FinzData for research.

Requires a free FinzData API key in FINZDATA_API_KEY and the optional
`finzdata` Python package. This endpoint supplies OHLCV/open-interest bars,
not trade-by-trade aggressor flow or order-book depth.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=90, choices=(90,),
                        help="GoldAI research window (fixed at 90 days)")
    parser.add_argument("--output", type=Path,
                        default=Path("data/orderflow/comex_gc_daily_free.csv"))
    args = parser.parse_args()

    if not os.getenv("FINZDATA_API_KEY", "").strip():
        print("Missing FINZDATA_API_KEY. Create a free FinzData key and set it as an environment variable.",
              file=sys.stderr)
        return 2
    try:
        import finzdata as fd
    except ImportError:
        print("Missing optional dependency. Install it with: pip install finzdata",
              file=sys.stderr)
        return 2

    try:
        client = fd.Client()
        # FinzData documents this call for the continuous COMEX Gold series.
        frame = client.futures("GC", continuous=True)
        if frame is None or frame.empty:
            print("FinzData returned no GC futures bars.", file=sys.stderr)
            return 3

        frame = frame.copy()
        frame.columns = [str(column).strip().lower() for column in frame.columns]
        time_column = next((name for name in ("timestamp", "date", "datetime", "time")
                            if name in frame.columns), None)
        if time_column is None:
            frame.index.name = frame.index.name or "timestamp"
            frame = frame.reset_index()
            frame.columns = [str(column).strip().lower() for column in frame.columns]
            time_column = next((name for name in ("timestamp", "date", "datetime", "time")
                                if name in frame.columns), None)
        if time_column is None:
            print("Unexpected FinzData response: no timestamp/date column. No file written.",
                  file=sys.stderr)
            return 4

        import pandas as pd
        frame[time_column] = pd.to_datetime(frame[time_column], utc=True, errors="coerce")
        frame = frame.dropna(subset=[time_column]).sort_values(time_column)
        cutoff = pd.Timestamp(datetime.now(timezone.utc) - timedelta(days=args.days))
        frame = frame.loc[frame[time_column] >= cutoff]
        if frame.empty:
            print("No GC bars were returned inside the requested 90-day window.",
                  file=sys.stderr)
            return 3

        args.output.parent.mkdir(parents=True, exist_ok=True)
        frame.to_csv(args.output, index=False)
        coverage_start = frame[time_column].min().isoformat()
        coverage_end = frame[time_column].max().isoformat()
        coverage_complete = (datetime.now(timezone.utc) - pd.Timestamp(coverage_start).to_pydatetime()).days >= args.days - 1
        metadata = {
            "provider": "FinzData",
            "source": "COMEX_GC_CONTINUOUS",
            "interval": "daily",
            "rows": int(len(frame)),
            "requested_window_days": args.days,
            "coverage_complete_90d": coverage_complete,
            "actual_coverage_start_utc": coverage_start,
            "actual_coverage_end_utc": coverage_end,
            "fields": list(frame.columns),
            "delta_status": "UNAVAILABLE_NOT_INFERRED",
            "depth_status": "NOT_PROVIDED",
            "live_trading_enabled": False,
            "note": "Daily OHLCV/open-interest bars are not trade prints, aggressor delta, or order-book depth. Actual coverage depends on the free account entitlement.",
        }
        metadata_path = args.output.with_suffix(".meta.json")
        metadata_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
        print(json.dumps(metadata, indent=2))
        if not coverage_complete:
            print("WARNING: provider returned less than 90 days; do not treat this as a complete 90-day dataset.",
                  file=sys.stderr)
        return 0
    except Exception as exc:
        print(f"FinzData fetch failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 5


if __name__ == "__main__":
    raise SystemExit(main())
