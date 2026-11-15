# GoldAI Trader

A demo-first XAUUSD research and trading assistant. The first milestone focuses on market data, explainable signals, risk controls, backtesting, and a MetaTrader 5 integration that remains disabled until explicitly configured.

## Safety defaults
- No live orders are sent by default.
- The API starts in paper/demo mode.
- A signal is not a promise of profit or a guaranteed successful trade.
- Risk limits, stop-loss validation, and a daily loss lockout must be enabled before any execution adapter can be used.
- Never put broker passwords, API keys, or MT5 credentials in this repository.

## Architecture
- `backend/`: FastAPI service, signal/risk engine, and MT5 adapter boundary.
- `frontend/`: dashboard (next milestone).
- `mt5/`: MetaTrader 5 Expert Advisor bridge (next milestone).
- `tests/`: automated tests.

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

## Current milestone
Initial API scaffold with health, configuration, market snapshot placeholder, signal evaluation, and risk checks. No broker connection or order execution is active yet.
