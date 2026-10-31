# Architecture

The current end-to-end description (sources, nightly routine, storage, outputs, UI) is `docs/design/SYSTEM_FLOW.md`. This file holds the principles and the map of the code. The earlier "Stoxsy 2.0" plan (one-click orders, F&O income engine, Tauri app) is archived at `docs/archive/ARCHITECTURE_2.0.md`; it is not the direction.

## Principles
1. **Raw first.** Every external file is saved byte for byte (SHA-256, never edited) before it is parsed, so any day can be re-read. Parsing is idempotent.
2. **Point in time.** Every row carries when we learned it (`known_at`). Nothing reads data from the future of its own date.
3. **Validated before use.** A file that fails its checks is refused with a message; a new column layout needs a person's one-time approval.
4. **Honest evidence.** Every output says what it is (RESEARCH, PAPER or APPROVED) and carries its basis. No model is promoted without passing the gate (deflated Sharpe net of costs, purged cross-validation); today none has.
5. **Values are never changed, only flagged.** A figure published by a source is stored and shown as published; problems are flags with plain-language reasons. If a source later changes a value, the first value stays and the change is kept as a revision.
6. **Research and decision support only.** Nothing places an order. The language model (planned) only explains numbers the system has computed.
7. **Real data only.** No synthetic data in the pipeline; tests that need data read the real database and run locally (`real_data` marker).

## Map of the code (`backend/qsde/`)
Generated from each package's own description on 2026-10-31; a package with no description is listed as such.

| Package | Purpose |
|---|---|
| `stream/` | continuous-data foundation: adapter contract, raw-first landing, `known_at` stamping, health, scheduling |
| `ingestion/` | data clients, loaders and the AMFI TER reader / loader / fetch |
| `adjust/`, `universe/` | reviewed price adjustments; the point-in-time universe |
| `features/`, `factors/` | Parquet feature store (ADR 0001) and factor math |
| `ml/`, `models/`, `live_loop/`, `scorecard/` | model lab and harness, immutable prediction ledger and outcome joiner, honest predicted-vs-actual measurement |
| `defensive/`, `decision/`, `risk/`, `levels/` | the risk ranking, decision cards (risk-based sizing, stops, horizons), risk rules, the one place that turns a decision into prices |
| `funds/` | mutual funds and ETFs: classification, NAV returns, risk, quality, peers, expense ratios (`ter.py`) |
| `portfolio/`, `investor/`, `trader/`, `modes/` | holdings and tax lots, the investor plan, the trader desk, the Investor / Trader modes |
| `alerts/`, `ops/`, `notifications/` | alert rules and engine, health checks and heartbeats, outbound notices |
| `auth/`, `config/`, `db/`, `cli/` | login and roles, settings and paths, database access, the `stoxsy` command |
| `fno/`, `engines/`, `execution/`, `live/`, `research/`, `stats/`, `backtest/` | older and research-stage code (F&O math, strategy engines, a semi-auto execution layer, live signal layer, research helpers). Not part of the nightly routine; orders are off by default. |

Comments in the code that cite `ARCHITECTURE.md §N` or `AUDIT_v2` refer to the archived copies, `docs/archive/ARCHITECTURE_2.0.md` and `docs/archive/AUDIT_v2.md`.

New code goes in the package whose purpose it matches. Retired code goes to an `_archive/` folder (`backend/scripts/_archive`, `backend/qsde/_archive`, `archive/`) with a README, never straight to deletion.

## Layers
```
sources -> stream/ adapters -> data/landing (raw)  -> parse -> TimescaleDB tables (eod_raw, mf_nav, mf_ter ...)
data/inbox (your files) -> loaders (layout gate)   ->                    |
                         nightly routine (scripts/daily_after_close.py): adjust -> universe -> signal -> cards -> fund snapshot -> alerts
                                                                          |
                         FastAPI (backend/api, login required)  ->  web/ app      +   stoxsy CLI   +   data/reports
```
