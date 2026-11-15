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
- `mt5/`: MetaTrader 5 Expert Advisor bridge (planned).

## Android app
Open the `android/` directory in Android Studio. The Arabic dashboard can test backend health and manually request the indicative XAU/USD spot price. See [android/README.md](android/README.md) for setup and limitations.

## Market data setup
The backend currently uses the no-key Gold API spot endpoint by default. Configure the provider URL and freshness threshold in `backend/.env` using `backend/.env.example`. The endpoint rejects stale or malformed responses. Provider spot prices are indicative references only; use a broker's own quote feed for any future MT5 execution logic.

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
