# Stoxsy

A private research and decision-support assistant for Indian markets (NSE, BSE) and mutual funds. It keeps its own data fresh, ranks risk, explains what it finds, and is being built toward a personal-advisor experience: goals, ranges, a monthly check-in, plain-language answers. **It never places an order, and nothing it shows is investment advice.** (Internal codename: `qsde`; the Python package, database and containers keep that name.)

Status and plan: `docs/design/PLATFORM_ROADMAP.md`. How the whole system works: `docs/design/SYSTEM_FLOW.md` (there is also a diagram page: ask for the "Stoxsy flow map"). Rules for anyone changing the code: `CLAUDE.md`.

## What is in it
- **Data, automatic after the close** (weekdays 19:15): NSE prices, corporate actions, F&O, deals, holidays; BSE prices; AMFI fund NAVs. Raw files are kept untouched, then parsed.
- **Data you provide:** AMFI expense-ratio workbook (`stoxsy fetch-ter`, about monthly), broker files (`stoxsy import ...`), a daily Kite login for live minute prices.
- **Analysis:** price adjustment for splits and bonuses, a point-in-time universe, a defensive risk ranking and decision cards (risk-sized, no return forecast), fund returns / risk / quality / peers / expense ratios, alerts, tax-lot tracking, an investor plan (ranges, never a point forecast).
- **Research layer:** features, models and an honest test harness. No model has passed the promotion gate, so none is trusted yet.
- **Screens** (`web/`): Dashboard, Investor plan, Trader desk, Profile and capital, My portfolio, Tax lots, Decision cards, Defensive signal, Alerts, Live compare, Mutual funds and ETFs, Reports and models, Data and system.

## Run it (Windows)
Prerequisites: Python 3.11+ (the project venv is `C:\Stoxsy\.venv`), Node.js, Docker Desktop.

```powershell
docker compose up -d                                   # TimescaleDB on port 5433 (and Redis)
cd backend
..\..\.venv\Scripts\python.exe scripts\apply_migrations.py   # idempotent; applies every migration in infra\migrations
..\..\.venv\Scripts\python.exe -m uvicorn api.main:app --port 8700
cd ..\web; npm install; npm run dev                    # http://127.0.0.1:5273, proxies /api to 8700
```
Copy `.env.example` to `.env` and fill it in (`.env` is git-ignored; never commit a key).

Daily use, from any terminal (`C:\Stoxsy\stoxsy.cmd` launches the CLI):

| Command | What it does |
|---|---|
| `stoxsy doctor` | all health checks (data freshness, heartbeats, Kite session, expense-ratio coverage, alerts) |
| `stoxsy status` | one-screen summary |
| `stoxsy ingest daily` | the after-close routine (the scheduled task "Stoxsy daily after close" runs it for you) |
| `stoxsy fetch-ter [--check]` | fetch AMFI's monthly expense-ratio workbook into the inbox and load it |
| `stoxsy import holdings` / `tradebook` | bring in broker files (dry run first) |
| `stoxsy kite login` | the daily Kite login, then `stoxsy stream start` |
| `stoxsy backup` | compressed database dump |

## Tests
```powershell
cd backend
..\..\.venv\Scripts\python.exe -m pytest -q -m "not real_data"      # what CI runs (empty database)
..\..\.venv\Scripts\python.exe -m pytest -q                          # also the tests that read your real data (local only)
cd ..\web; npm test; npm run build
```

## Where things are
```
backend/api/       FastAPI routes (login required)         backend/qsde/     the Python package (see ARCHITECTURE.md)
backend/scripts/   operational and one-off scripts         backend/tests/    tests (real-data tests are marked real_data)
web/               the web app (React, Vite)               infra/            migrations, config data, init.sql
docs/              design, runbooks, ADRs, legal drafts    archive/          retired code, kept for history
```
Your data lives outside the repository in `C:\Stoxsy\data` (landing, inbox, reports, signals, backups). The repository is public: **no data, key or personal file belongs in it.**
