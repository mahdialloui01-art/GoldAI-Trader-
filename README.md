# GoldAI Trader

A demo-first XAUUSD research and trading assistant. The first milestone focuses on market data, explainable signals, risk controls, backtesting, and a MetaTrader 5 integration that remains disabled until explicitly configured.

## Safety defaults
- No live orders are sent by default.
- The API starts in paper/demo mode.
- A signal is not a promise of profit or a guaranteed successful trade.
- Risk limits, stop-loss validation, and a daily loss lockout must be enabled before any execution adapter can be used.
- Never put broker passwords, API keys, or MT5 credentials in this repository.

## Architecture
- `android/`: native Android app built with Kotlin and Jetpack Compose.
- `backend/`: FastAPI service, signal/risk engine, and MT5 adapter boundary.
- `backend/tests/`: API safety and behavior tests.
- `mt5/`: MetaTrader 5 Expert Advisor bridge (planned).

## Android app
Open the `android/` directory in Android Studio. The Arabic dashboard includes a backend health-check action. See [android/README.md](android/README.md) for setup and development URL configuration.

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
No live market-data provider, economic-news provider, MetaTrader bridge, or order execution is connected. The app must not be used to make real trades until those components, authentication, risk controls, and demo/forward tests have been completed.
