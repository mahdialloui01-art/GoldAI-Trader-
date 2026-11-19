# GoldAI Order Flow Intelligence — 90-day data plan

## Approved configuration
- Sources: XM MT5 + COMEX Gold Futures (GC).
- Research window: rolling 90 calendar days.
- Initial state: audit/research only; no effect on production entries or execution.
- Live trading remains disabled.

## Source separation

### XM MT5 (execution reference)
Capture broker-specific GOLD ticks with UTC millisecond timestamps, Bid, Ask, Last, flags, symbol, digits and point. Capture broker DOM only if the broker terminal exposes it for this symbol. Tick volume is quote/tick activity, not executed buyer/seller contract volume.

### COMEX GC (centralized futures reference)
Use licensed trade prints and, if available in the acquired entitlement, depth updates. Preserve contract month, exchange timestamp, sequence number, trade price/size, aggressor side when supplied, and each book level's price/size/action. Do not derive true aggressor delta from candle direction or quote ticks. Do not claim hidden orders are directly observable; iceberg/absorption signals are hypotheses inferred from replenishment and executions.

GC and XM GOLD are different instruments/venues. Align UTC timestamps and compare returns/basis after contract-roll handling; never substitute GC prices into XM execution simulation.

## Data acceptance contract
- UTC timestamps with timezone and millisecond precision where supplied.
- Keep original raw exports immutable; store normalized derivatives separately.
- Deduplicate using source + instrument/contract + exchange timestamp + sequence/trade ID.
- Report missing intervals, out-of-order rows, invalid/crossed quotes, duplicate rates and source coverage.
- Every field carries provenance: observed, vendor-classified, or derived.
- A missing field remains null/unavailable; never synthesize aggressor side, traded volume, or DOM.

## Implemented in the repository

- The authenticated XM quote endpoint now stores one quote sample at most every 5 seconds in PostgreSQL.
- Samples include source timestamp, Bid, Ask, Last, spread points, point size and digits.
- The table is indexed by symbol/time and startup cleanup retains the latest 90 days.
- `GET /api/v1/learning/orderflow/xm-status` reports sample count, first/last timestamp and crossed quotes.
- Persistence is best-effort: a database outage does not stop the market quote endpoint. Sampling attempts are throttled during outages.
- This is sampled quote history, not every MT5 tick, not executed trade volume, and not Market Depth.

## Current repository tooling
Run the auditor from the repository root:

```bash
python tools/orderflow_data_quality.py --xm path/to/xm_ticks.csv --comex path/to/gc_trades_depth.csv --days 90 --output orderflow-data-quality.json
```

Each input is optional, but both are required before the combined-source research gate can pass. Accepted common headers are documented in the tool docstring. The report intentionally returns `UNAVAILABLE_NOT_INFERRED` when aggressor-side trade data is absent.

## Validation sequence
1. Verify source entitlements, symbol/contract mapping, timezone and 90-day coverage.
2. Audit raw data and reject corrupt, duplicated or out-of-window records.
3. Calculate XM spread/tick-activity features separately from GC trade delta and book features.
4. Align sources and measure GC-to-XM basis/return correlation without assuming identical prices.
5. Compare the existing strategy against the same strategy plus order-flow features on identical timestamps.
6. Use chronological walk-forward splits and a final untouched holdout; include spread, commission, slippage, roll and realistic fill assumptions.
7. Promote a feature only if it improves out-of-sample expectancy and drawdown across regimes and has adequate trade count.

## Not yet implemented / required before live collection
- MT5 EA tick/depth batch transport and durable server-side storage.
- A licensed COMEX vendor/API credential and contract-month downloader.
- Durable raw-data retention and ingestion monitoring.
- End-to-end reconciliation against XM terminal exports.

No COMEX historical depth purchase or paid subscription is authorized by this plan. The auditor only validates files already obtained. No profitability or 60% win-rate claim is made from the existence of these data sources.
