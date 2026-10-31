# Stoxsy: how the whole system works (data in, processing, storage, output)

Written 2026-10-31 from the code (an end-to-end read, with the scheduled task, the AMFI tables and the running pages checked directly). Where an older document disagrees with the code, the code is described and the document is listed in section 9. Research and decision support only: nothing here places an order.

## 1. The picture

```
 PUBLIC ARCHIVES (no login)                OWNER'S INBOX (you save files)             KITE (daily manual login)
 NSE cash / F&O / deals / corp actions     AMFI TER workbooks  (or `stoxsy fetch-ter`)   live minute stream (market data only)
 NSE holidays / symbol changes             BSE corporate-action exports                  token lapses 06:00 IST
 BSE cash  .  AMFI NAV + scheme list       Choice broker files (holdings, trades, lots)
        |                                          |                                          |
        v  adapters, each: fetch -> land raw bytes (immutable, sha256) -> parse -> stamp known_at -> upsert
 data/landing/<source>/yyyy/mm/dd/        loaders with a reviewed-layout gate               kite_stream.py
        |                                          |                                          |
        v                                          v                                          v
 eod_raw . bse_eod_raw . fno_eod_raw        mf_ter* (+revisions) . bse_ca_* .          ohlcv_intraday
 deals_raw . nse_corporate_actions          portfolio_* . broker_* . lot_*             (after close: reconcile -> *_official)
 mf_nav . mf_scheme_master . holidays
        |
        v  after close, Mon-Fri 19:15 (Windows task "Stoxsy daily after close" -> scheduled_daily.py -> daily_after_close.py)
 corporate actions -> adjusted ohlcv -> universe_pit (top 500 by 60-day traded value) -> defensive signal -> decision cards
        -> prediction_log (shadow) -> outcomes                      mf_nav + mf_ter -> mf_fund_snapshot (returns, risk, quality, peers)
        -> alerts (stored only, nothing sent)                       heartbeats -> ops_heartbeat
        |
        v
 FastAPI :8700 (every route behind login)  ->  web UI (Vite + React, Investor / Trader modes)   .   `stoxsy` CLI (doctor, status, backup, fetch-ter, import ...)
 data/reports/*  .  data/signals/defensive/latest.parquet
```

## 2. Data in

| Source | How | Lands | Writes |
|---|---|---|---|
| NSE cash bhavcopy | daily step `nse_cash` (required) | landing/bhavcopy | `eod_raw` |
| NSE corporate actions, symbol changes, holidays | steps `nse_corp_actions`, `nse_holidays` | landing | `nse_corporate_actions`, `symbol_changes`, `exchange_holidays` |
| NSE F&O bhavcopy, bulk/block deals | steps `nse_fo` (looks back 10 days), `nse_deals` | landing | `fno_eod_raw`, `deals_raw` |
| BSE bhavcopy | step `bse_cash` | landing | `bse_eod_raw` |
| AMFI NAV (all schemes, history) | step `amfi_nav` | landing | `mf_nav`, `mf_scheme_master` |
| AMFI expense ratios (TER) | owner-run `stoxsy fetch-ter` or a hand-saved workbook in `data/inbox/amfi_ter`; nightly step `amfi_ter_inbox` loads what is waiting | landing/amfi_ter | `mf_ter`, `mf_ter_files`, `mf_ter_scheme`, `mf_ter_map`, `mf_ter_unmatched`, `mf_ter_revisions` |
| BSE corporate-action exports | owner saves file, `load_bse_corporate_actions.py` | inbox | `bse_ca_*` |
| Broker files (Choice) | `stoxsy import ...` (dry run first) | inbox/choice | `portfolio_*`, `broker_*`, `lot_*`, `portfolio_tax_lots` |
| Kite minute data | `stoxsy kite login` (daily, by hand) then `stoxsy stream start` | | `ohlcv_intraday` |

Every public-archive source goes through the same adapter shape, so adding a source means adding one adapter. Raw bytes are kept unchanged before any parsing, so any day can be re-read.

## 3. The nightly routine (`stoxsy ingest daily`), in order

R = required (a failure stops the run). 1 `nse_cash` R . 2 `nse_corp_actions` . 3 `rebuild_ohlcv` R (derives corporate-action events and the adjusted `ohlcv`) . 4 `universe` R (`universe_pit`) . 5 `intraday_reconcile` . 6 `nse_deals` . 7 `nse_holidays` . 8 `nse_fo` . 9 `bse_cash` . 10 `amfi_nav` . 11 `signal` R (defensive signal -> `data/signals/defensive/latest.parquet`) . 12 `log_cards` (shadow `prediction_log`) . 13 `resolve` (`prediction_outcomes`) . 14 `amfi_ter_inbox` . 15 `fund_snapshot` (`mf_fund_snapshot`, replaced wholesale) . 16 `features_refresh` (adds the newest sessions to the Parquet feature store as an increment snapshot; skips when current) . 17 `alerts` (`alert_events`). It ends with a heartbeat and a log in `data/reports/daily/`. Verified: the task last ran 30 Oct 19:15 with result 0; next run Mon 2 Nov 19:15 (the scheduler skips weekends and exchange holidays).

## 4. Where data is stored

TimescaleDB (Docker, port 5433, database `qsde`) for everything queryable: prices (`eod_raw`, `ohlcv`, `bse_eod_raw`, `fno_eod_raw`, `deals_raw`), minute bars, universe and corporate actions, funds (`mf_*`), portfolio and lots, users and modes, alerts, predictions, heartbeats. Files under `C:\Stoxsy\data`: `landing` (immutable raw), `inbox` (files you drop), `reports`, `signals`, `features` (the live factor store is Parquet snapshots, not the `factor_pit` table), `backup`, `logs`, `secrets`. Migrations 001-065 are applied by `backend/scripts/apply_migrations.py` (idempotent).

## 5. How it is processed

- **Prices:** raw exchange files -> corporate-action events -> adjusted `ohlcv`; the point-in-time universe is the top 500 by 60-day traded value.
- **Defensive signal (the production path):** five equal-weight percent ranks (ATR, 60-day volatility, downside volatility, 120-day drawdown, liquidity). It ranks risk; it makes no return forecast. Decision cards add sizing and are labelled research-only. Logged in shadow mode and resolved later against outcomes.
- **Funds:** returns with an explicit basis label and status, risk from about 3 years of NAV, a data-quality badge, peer percentiles (8 or more like-for-like peers). Expense ratios (Part A/B) are shown as AMFI published them, broken into parts, with revisions kept.
- **Portfolio:** broker files -> holdings, FIFO tax lots with reconciliation (no tax amount is computed; the tax policy is marked unverified), concentration and defensive buckets.
- **Alerts:** rules in `infra/data/alert_rules.json`; stored, not sent.
- **Research only, run by hand:** factor computation, ML (purged CV, trial ledger, model cards). The registry lists only `defensive_cards_v1` in state "research"; promotion is switched off and no model has cleared the net deflated-Sharpe gate.
- **Not wired into the nightly run:** the older engines (momentum, F&O income, pairs), paper trading, order placement (dry-run by default, several gates).

## 6. Outputs

API: about 119 routes, all behind login. Current routers: alerts, auth/admin, cockpit (defensive, system status, reports, decision cards), funds, health, investor, live compare, profile/mode, portfolio (import, lots, triage), trader. Several legacy routers remain. CLI `stoxsy`: status, doctor, heartbeat, coverage, ingest, fetch-ter, alerts, kite login, stream, backup, restore, import, triage, restrict, lots. Files: `data/reports/*` (daily log, coverage, fund and AMFI reports, model cards), `data/signals/defensive/`.

## 7. The UI (web/, port 5273, proxies /api to 8700)

Investor / Trader switch (stored in your profile; the two pots stay separate and the server enforces it). Pages: Dashboard, Investor plan, Trader desk, Profile and capital, My portfolio, Tax lots, Decision cards, Defensive signal, Alerts, Live compare, Mutual funds and ETFs, Reports and models, System, Admin. Login is a JWT with a refresh cookie.

## 8. Operations

`stoxsy doctor` checks: runtime, data directory, disk, database and migration count, freshness of NSE, BSE, F&O and AMFI data, heartbeats (stream, daily pipeline), fund expense-ratio coverage, Kite session, exchange calendar, open alerts. `stoxsy backup` / `restore`. CI: secrets scan, web typecheck/tests/build, backend tests on an empty TimescaleDB (`real_data` tests skipped).

## 9. Known mismatches and loose ends (as of 2026-10-31)

- HANDOVER.md (June) says migrations 001-016 and 69 routes and no migration runner; the repo has 065 and about 119 routes and `apply_migrations.py`. START.md, HANDOVER and `run.ps1` describe the old Next.js `frontend/`; the real UI is `web/`. CLAUDE.md still lists Celery, Telegram, a Nifty 200 universe and a mandatory `factor_pit`; `DATA_FOUNDATION.md` says `factor_pit` is no longer read or written. `docs/MIGRATIONS.md` stops at 001 and shows an old password.
- Unused code: `qsde/models/regime_engine.py` (the 5-state HMM) is imported only by its own test; `pit_writer.py`, `finnhub_client.py`, `nse_flows.py`; a leftover `triple_barrier.py.pre_2c1_backup`.
- Hazard if ever re-registered: the legacy `daily_eod.py` / `QSDE_Daily_EOD` task writes `ohlcv`, which `rebuild_ohlcv` deletes and rebuilds. That task is not registered today.
- The Parquet feature store is now refreshed nightly (step 16, from 2026-10-31); `stoxsy doctor` warns when it falls more than 2 sessions behind prices. Model training and scoring are still by hand: no model is trained for production, so there is nothing to score nightly yet.
- The repo is public: `live_test.json` (a June paper-trade example, no credentials) is tracked and can be deleted. Plaintext `data/secrets/owner_bootstrap.txt` and broker files in `data/inbox/choice` live outside the repo (not tracked); HANDOVER lists API keys that were never rotated.
