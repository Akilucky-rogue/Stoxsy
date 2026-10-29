# Stoxsy — Run Guide

Real-time Indian-market (NSE/BSE) quant signal + research platform on the Zerodha Kite API.

## Quick start (launcher)

From the project root (`C:\Users\NEW\Documents\stockscreener\stoxsy`):

```powershell
.\run.ps1            # core app:  DB + backend(:8700) + frontend(:5273)
.\run.ps1 all        # core app + live market windows (Kite stream + live feed)
.\run.ps1 live       # live market windows only (Kite must be logged in)
.\run.ps1 stop       # stop the database containers
.\run.ps1 status     # docker + backend health
.\run.ps1 test       # backend test suite  (expect: 160 passed)
.\run.ps1 eod        # end-of-day data refresh
.\run.ps1 retrain    # retrain ML models (net-DSR gated)
```

Live feed on specific names / size:
```powershell
.\run.ps1 live -Symbols RELIANCE,TCS,KEI
.\run.ps1 live -Top 15
```

> If PowerShell blocks the script: `Set-ExecutionPolicy -Scope Process Bypass -Force` then re-run.

## Daily, during market hours (≥ 09:15 IST)

Kite's access token expires ~06:00 IST, so log in once each morning:

1. Browser → **http://localhost:8700/api/kite/login_url** → Zerodha login (redirects back, stores token).
2. `curl -X POST http://localhost:8700/api/kite/refresh_instruments`
3. Start the live windows: `.\run.ps1 live`

## Manual commands (if you prefer separate terminals)

| Window | Commands |
|---|---|
| **DB** | `cd stoxsy` → `docker compose up -d` |
| **Backend** | `cd stoxsy\backend` → `..\.venv\Scripts\Activate.ps1` → `uvicorn api.main:app --reload --port 8700` |
| **Frontend** | `cd stoxsy\frontend` → `npm run dev` |
| **Kite stream** | `cd stoxsy\backend` → `..\.venv\Scripts\Activate.ps1` → `python scripts\kite_stream.py` |
| **Live feed** | `cd stoxsy\backend` → `..\.venv\Scripts\Activate.ps1` → `python scripts\live_watch.py` |

- API: http://localhost:8700/docs · Health: http://localhost:8700/api/health
- UI: http://localhost:5273

## Notes / current honest state

- **Live windows only do work during market hours** with Kite logged in. Off-hours the feed shows last-session snapshots / WAIT.
- **`live_watch.py` requires `kite_stream.py` running** (it reads the minute bars the stream writes).
- **Intraday ML model is currently deactivated** — under the net-of-cost DSR gate, no horizon clears the bar (only long-horizon has a real, sub-gate net edge). The microstructure live feed (`live_watch.py`) is rule-based and unaffected.
- **First-time-only** on a fresh machine: `cd frontend; npm install`, create `.venv`, and apply DB migrations. None are needed on this machine (already set up; DB populated).

## Prerequisites

Docker Desktop · Python venv at `.venv` · Node/npm · `.env` (gitignored) with DB URL + Kite keys.
