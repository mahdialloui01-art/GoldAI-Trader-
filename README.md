# GoldAI Trader

A demo-first GOLD research and trading assistant. The project focuses on market data, explainable signals, risk controls, backtesting, and a MetaTrader 5 integration that remains disabled until explicitly configured.

## Safety defaults
- No live orders are sent by default.
- The API starts in paper/demo mode.
- A signal is not a promise of profit or a guaranteed successful trade.
- The public spot quote is indicative only and is not a broker bid/ask execution quote.
- Never put broker passwords, API keys, or MT5 credentials in this repository.

## Architecture
- `android/`: native Android app built with Kotlin and Jetpack Compose.
- `backend/`: FastAPI service, indicative gold spot adapter, signal/risk endpoints.
- `backend/tests/`: API safety and behavior tests.
- `mt5/`: MetaTrader 5 Expert Advisor bridge for broker Bid/Ask quotes.

## Android app
Open the `android/` directory in Android Studio. The Arabic dashboard can test backend health and request the latest authenticated XM MT5 GOLD Bid/Ask snapshot. See [android/README.md](android/README.md) for setup and limitations.

## Market data setup
The primary `/api/v1/market/snapshot` serves only a fresh authenticated MT5 broker Bid/Ask quote. The Gold API is retained at `/api/v1/market/spot-reference` for comparison only and is never used as a silent fallback. Configure a long random `MT5_BRIDGE_TOKEN` in the backend environment and the same token in the MT5 EA inputs. Never commit the real token.

## Run the API
Python 3.11+ is recommended.

```bash
cd backend
python -m venv .venv
# Windows: .venv\\Scripts\\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Open http://127.0.0.1:8000/docs.

## Run backend tests

```bash
cd backend
pytest -q
```

GitHub Actions is configured to run the backend tests and an Android debug build on pushes and pull requests.

## Current limitations
The Android-to-backend display path is implemented, but live broker connectivity still requires MT5 desktop/VPS, a deployed HTTPS backend, a private bridge token, and end-to-end testing. The economic calendar/news filter requires a valid Trading Economics API key configured only on the server. No order execution is connected. Keep the app in paper/demo mode until broker connectivity, risk controls, historical backtests, and forward demo tests have been completed.


## MetaTrader 5 broker quote bridge

1. Deploy the backend behind HTTPS and set `MT5_BRIDGE_TOKEN` to a long random secret in the server environment (never commit it).
2. In MetaTrader 5 desktop, compile `mt5/GoldAI_QuoteBridge.mq5` in MetaEditor and attach it to an GOLD chart.
3. Set the EA inputs `BackendUrl` and `BridgeToken`. Add the backend host to MT5 **Tools → Options → Expert Advisors → Allow WebRequest**.
4. The EA reads the configured broker symbol (default `GOLD`) and streams Bid/Ask, spread, precision and UTC timestamp. Android reads the latest broker snapshot from the backend.
5. If MT5 disconnects or the quote becomes stale, the snapshot returns 503; the public spot reference is not substituted.

The bridge transmits quotes only. It does not place orders. Trading remains paper/demo and live execution remains disabled.


## Price action and multi-timeframe analysis

- `POST /api/v1/analysis/price-action` returns confirmed swing points, market-structure classification, BOS candidates, liquidity sweeps, equal highs/lows, fair-value gaps and a heuristic order-block candidate.
- Swing points are confirmed only after the configured right-side candles close. These are rule-based chart interpretations, not verified institutional order flow.
- `POST /api/v1/analysis/multi-timeframe` requires M5, M15, H1 and H4 candles to agree before it can return a directional analysis. Any conflict or unsafe spread returns WAIT.
- Both endpoints are analysis-only and explicitly keep execution disabled.

## Economic calendar and news protection

Configure `ECONOMIC_CALENDAR_API_KEY` on the backend using a private Trading Economics API key. Never commit the real key or send it to the Android app.

- `GET /api/v1/calendar/us-high-impact?days_ahead=7` retrieves high-impact US releases in UTC.
- `GET /api/v1/analysis/news-filter` returns WAIT when a high-impact event falls inside the configured pre/post-event blackout window.
- The default blackout is 30 minutes before and after each event. Set `NEWS_BLACKOUT_BEFORE_MINUTES` and `NEWS_BLACKOUT_AFTER_MINUTES` to adjust it.
- Missing credentials, provider errors, or malformed calendar data fail closed with HTTP 503; no news-free assumption is made.
- Estimated-time events are treated conservatively for their full UTC date.


## Gold macro context

Configure a private FRED API key as `FRED_API_KEY` on the backend. The macro endpoint reads three daily US series from FRED:

- `DGS10`: 10-year nominal Treasury yield.
- `DFII10`: 10-year inflation-indexed (real) Treasury yield.
- `DTWEXBGS`: nominal broad US dollar index.

`GET /api/v1/analysis/gold-macro` compares the latest two valid observations and returns a transparent, rule-based macro context (gold headwind, supportive, or mixed/neutral). Data older than seven calendar days, missing credentials, and provider errors fail closed. This is daily context, not an intraday trading signal or a forecast of gold returns.
