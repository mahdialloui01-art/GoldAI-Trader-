# GoldAI Trader

A demo-first XAUUSD research and trading assistant. The project focuses on market data, explainable signals, risk controls, backtesting, and a MetaTrader 5 integration that remains disabled until explicitly configured.

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
Open the `android/` directory in Android Studio. The Arabic dashboard can test backend health and manually request the indicative XAU/USD spot price. See [android/README.md](android/README.md) for setup and limitations.

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
The spot-price adapter is connected in code but has not yet been verified by a successful CI run in this development session. No economic-news provider, broker quote feed, MetaTrader bridge, or order execution is connected. The app must not be used to make real trades until those components, authentication, risk controls, and demo/forward tests have been completed.


## MetaTrader 5 broker quote bridge

1. Deploy the backend behind HTTPS and set `MT5_BRIDGE_TOKEN` to a long random secret in the server environment (never commit it).
2. In MetaTrader 5 desktop, compile `mt5/GoldAI_QuoteBridge.mq5` in MetaEditor and attach it to an XAUUSD chart.
3. Set the EA inputs `BackendUrl` and `BridgeToken`. Add the backend host to MT5 **Tools → Options → Expert Advisors → Allow WebRequest**.
4. The EA streams broker Bid/Ask, spread, precision and UTC timestamp. Android reads the latest broker snapshot from the backend.
5. If MT5 disconnects or the quote becomes stale, the snapshot returns 503; the public spot reference is not substituted.

The bridge transmits quotes only. It does not place orders. Trading remains paper/demo and live execution remains disabled.
