> **Archived 2026-10-31, kept for history.** This describes an earlier build. The current description of the system is `docs/design/SYSTEM_FLOW.md`; the current plan is `docs/design/PLATFORM_ROADMAP.md`. Do not follow the instructions below without checking them against those.

# Stoxsy — Project Handover

> Single source of truth for picking this project up cold. Last updated 2026-06-26.
> Supersedes nothing in code; where this doc and the code disagree, **the code wins** —
> re-verify before trusting any number here.

---

## 0. TL;DR (read this first)

- **Stoxsy** (formerly `qsde`) is a real-time **Indian-market (NSE/BSE) quant trading
  platform** on the **Zerodha Kite paid API**. Capital ₹15 lakh+. The Python package
  is still named `qsde` (only the repo folder was renamed to `stoxsy`).
- Path: `C:\Users\NEW\Documents\stockscreener\stoxsy`. Windows / PowerShell.
  venv: `stoxsy\.venv\Scripts\python.exe`. Run pytest from `stoxsy\backend`.
- **Current state: GREEN.** Full suite **260 passed** + frontend `tsc --noEmit` clean
  (2026-06-28). DB migrations **001–016 applied** to the local TimescaleDB
  (`qsde_timescaledb`, port **5433**; user `qsde`, db `qsde`). API boots with 69 routes.
  Apply migrations idempotently with `python scripts/apply_migrations.py` (no
  migration-tracking table exists, so run this after pulling new ones — that's how
  013/`liquidity_tier` got skipped and broke the Tier-1 step).
- **Three engines now on the contract**, one per viable strategy family for this stack:
  `rule_momentum` (trend, weak/measured), `fno_income` (vol-premium condors, flagship),
  `pairs_statarb` (market-neutral cointegration — new). All emit defined-risk
  `TradeIdea`s → idea-log → mechanically resolved → Scorecard.
- We are mid-way through a **strangler-pattern refactor**: a new, honest `TradeIdea` /
  `Engine` / `Scorecard` spine is being built *alongside* the legacy signal path.
  Nothing legacy has been deleted yet — deletions come **last**, after the new path
  is paper-proven.
- The **flagship** being built is the **F&O income engine** (defined-risk index-option
  iron condors). Math + engine + **chain source + IV-history persistence are done and
  tested** (increment 4). It now produces & logs real condors from the NSE F&O bhavcopy
  on a 20-session vol-history warm-up. Remaining for live: the Kite **NFO** live-chain
  wiring + accruing the warm-up window, then paper-validate via the Scorecard.

### The doctrine that governs everything
1. **Honesty over hype.** Accuracy is **measured, not promised.** 99.9% *chart fidelity*
   (correctness) is achievable; **52–58% directional is elite**; >60% sustained = a
   leak to hunt down. Profit comes from **expectancy**, not hit-rate. "90% accuracy" is
   a mirage — do not design toward it.
2. **DSR is the only ML promotion gate**, and it is now **net-of-cost**. A model is
   promoted to "active" *only* if its **net Deflated Sharpe ≥ 0.95**. As of the last
   run this gate **promotes nothing** — which is the honest outcome (see §4).
3. **Defined-risk only.** `TradeIdea.validate()` hard-rejects any idea with
   null/inf/≤0 max-loss, bans naked short options, requires entry+stop for cash and
   legs for options. F&O = **European, cash-settled INDEX options only** (NIFTY/
   BANKNIFTY) → no assignment / early-exercise risk.
4. **Hard human-in-the-loop.** Semi-auto execution must never silently auto-fire
   (SEBI compliance). Every suggested + confirmed order is logged with timestamps.

---

## 1. ⚠️ OUTSTANDING — do these

### 1a. Rotate the leaked API keys (STILL NOT DONE — user action)
These were found committed in spec `.md`s / legacy config during the audit and have
been scrubbed from the working tree, but **must be rotated at the providers** and
**purged from git history**:
- FMP, **Finnhub ×2**, **FRED ×2**, **Alpha Vantage `EPZDH2PL62LNGHCQ`**,
  Telegram bot token, **Choice UCC + IP** (treat as exposed), **Kite API secret**.
- `.env` is gitignored (confirmed). Never commit it.

### 1b. Immediate engineering next step
Wire a **real options-chain source** into `FnoIncomeEngine` (see §5, "F&O remaining").
Until then the flagship runs only on synthetic/test chains.

---

## 2. How to run

```powershell
# from repo root: C:\Users\NEW\Documents\stockscreener\stoxsy
$py = ".\.venv\Scripts\python.exe"

# infra (already up as of handover: qsde_timescaledb:5433, qsde_redis:6379)
docker compose up -d --wait

# tests  (run from backend; pytest.ini scopes to tests/)
cd backend
& $py -m pytest tests -q          # expect: 210 passed

# backend API
& $py -m uvicorn api.main:app --reload --port 8000

# frontend (Next 16 / React 19)
cd ..\frontend ; npm run dev
```

There is also a root **`run.ps1`** launcher with modes: `app` / `all` / `live` /
`stop` / `status` / `test` / `eod` / `retrain`. And **`START.md`** documents commands.

**Migrations** are applied directly to Postgres (no runner script). The container is
`qsde_timescaledb` on port **5433**. To apply a new `infra/migrations/NNN_*.sql`, pipe
it to psql in the container or run it via the project's DB connection.

---

## 3. Architecture — the new spine (strangler target)

```
            ┌──────────────── Engines (Engine ABC) ────────────────┐
 signals →  │ RuleMomentumEngine   FnoIncomeEngine   (more later)  │
 chain   →  └───────────────┬──────────────────────────────────────┘
                            │ emits
                            ▼
                       TradeIdea  ── validate() enforces DEFINED-RISK
                            │
              record at EMIT time (idempotent)
                            ▼
                   trade_ideas  (the idea-log, Postgres)
                            │
                 resolve_open_ideas() (mechanical idea→outcome)
                            ▼
        Scorecard (Reality Engine): per-engine NET expectancy,
        calibration, chart-fidelity — accuracy MEASURED here
```

**Key contract — `backend/qsde/engines/base.py`:**
- `Structure` / `Horizon` enums; `Leg` / `Greeks` / `TradeIdea` dataclasses; `Engine` ABC.
- `TradeIdea` carries cash fields (entry/stop/target/qty/capital_required) **and**
  option fields (legs/net_credit/max_profit/breakevens/greeks/margin_required/expiry),
  plus `max_loss_rupees`, `confidence`, `idea_id`, `scorecard_id`, `created_at`, `reasons`.
- `validate()` is the defined-risk gate (rejects uncapped / naked / inconsistent).

**One shared sizer** — `backend/qsde/engines/sizing.py` `size_cash(...)` wraps the
canonical `size_position`. Engines must **not** re-implement sizing (a self-review
caught RuleMomentumEngine doing exactly that; it was fixed to use this).

**Levels** — `backend/qsde/levels/`: `compute_trade_levels` (cash) + `options.py`
(`build_credit_vertical`, `build_iron_condor` — exact defined-risk economics).

**Scorecard** — `backend/qsde/scorecard/`:
- `metrics.py` (hit_rate, expectancy, payoff, brier, calibration, summarize)
- `fidelity.py` (reconcile_series / reconcile_ohlc — chart correctness)
- `store.py` (idea_to_row PURE; record_idea/record_ideas idempotent; fetch_resolved)
- `resolver.py` (resolve_idea_outcome pure + resolve_open_ideas; reuses
  paper_journal `_resolve_barrier_daily`)
- `tracker.py` (report)

---

## 4. The validated-edge reality (why the gate promotes nothing)

Direct read of `dataset.py` + `triple_barrier.py` confirmed the cost subtraction was
**dead** under triple-barrier labeling. After fixing the gate to **net DSR**:

| Horizon  | Net DSR @ 5bps round-trip |
|----------|---------------------------|
| Intraday | **0.000** |
| Swing    | **0.098** |
| Long     | **0.650** |

Gate threshold is **0.95**. So **only the long-horizon equity engine has any real
net edge, and even it is sub-gate.** The net-DSR gate therefore promotes **nothing** —
this is correct and honest. The validated edge is **execution-limited**.

**`lgbm_model.py` gate behaviour (locked in):**
- `promotion_cost_bps = QSDE_PROMOTION_COST_BPS` (default 10).
- `dsr_net` from `_evaluate_oos_with_costs`; `gate = should_promote(dsr_net)`.
- `if gate["passed"]:` → activates (genuine pass only).
- `elif gate.get("forced"):` → saves candidate but **does NOT activate**
  (FORCE_PROMOTE can no longer flip the live model).
- Audit row records `sharpe=ann_sharpe_net`, `deflated_sharpe=dsr_net`,
  `promoted=bool(gate["passed"])`; gross kept in `params_json`.

---

## 5. What's built vs. pending

### Built + tested (Phase 0 consolidation + F&O flagship)
- **TradeIdea contract + Engine ABC** (`engines/base.py`) — 7 tests.
- **Shared sizer** (`engines/sizing.py`).
- **Levels**: cash + `levels/options.py` (credit vertical, iron condor) — tested.
- **Scorecard** (metrics / fidelity / store / resolver / tracker).
- **RuleMomentumEngine** (`engines/rule_momentum.py`) — reads tier-1 signals,
  sizes via size_cash, emits defined-risk cash ideas. Wired into `daily_eod.py`.
- **FnoIncomeEngine** (`engines/fno_income.py`) — expected_move, ~1σ-OTM strike
  selection, **NaN-safe iv_rank gate + `min_iv_history` warm-up floor**, builds iron
  condors, per-trade max_loss cap, snapshot-overridable lot/strike/wing (one instance
  handles NIFTY+BANKNIFTY), stamps `created_at` from the session date. Pure brain, tested.
- **F&O math** (`fno/black_scholes.py`) — bs_price, bs_greeks, implied_vol
  (bisection), DEFAULT_R=0.065 — 7 tests.
- **F&O data bridge (increment 3)** — `fno/bhavcopy.py` (`parse_fo_bhavcopy`: legacy
  **and** UDiFF, now also surfacing `UndrlygPric`/`NewBrdLotQty`; `fetch_fo_bhavcopy`
  via the shared NSE session infra), `fno/vol.py` (`add_iv`, `atm_iv`, `iv_rank`,
  `build_chain_snapshot`). `tests/test_fno_data.py`.
- **F&O chain source + IV-history (increment 4, just landed):**
  - migration **016** `fno_iv_history` — the `iv_rank` denominator.
  - `fno/iv_store.py` — idempotent ATM-IV upsert + **lookahead-safe** trailing read
    (`as_of_date < asof`, strict; front-month pick).
  - `fno/indices.py` — index registry (strike_step/wing/lot **fallbacks**; data wins).
  - `fno/chain_source.py` — pure `build_snapshots()` (front-expiry pick, spot/lot from
    data, lookahead-safe iv_rank, carries `asof`/`iv_history_n`) + `BhavcopyChainSource`
    (EOD, persists ATM IV) + `KiteChainSource` (live, auth-gated; pure `kite_rows_to_options`).
  - `scripts/snapshot_fno_chain.py` runner + wired into `daily_eod.py`.
  - `tests/test_fno_chain_source.py` (pure) + **DB smokes**: iv-store idempotency,
    lookahead, and the full seeded-history → condor → idea-log round-trip (session-stamped,
    deduped, defined-risk). **`iv_rank` is honest cold**: emits nothing until the
    20-session warm-up accrues and vol is measurably rich.
  - **Adversarial review** (5-dimension workflow, per-finding verify): caught a real HIGH
    bug — `NaN < 0.5` and degenerate `iv_rank` (`0.5`) both *passed* the `< iv_rank_min`
    gate, so a cold/single-session window would sell premium on no signal. Fixed:
    `iv_rank` degenerate→NaN, gate bails on NaN, `min_iv_history=20` warm-up in production.
- **F&O condor resolution + Scorecard report (increment 5, just landed):**
  - `fno/payoff.py` — pure iron-condor **settlement P&L** (`iron_condor_settlement_pnl`,
    `condor_params_from_legs`, `estimate_condor_cost`); returns are risk-normalised
    (pnl / max_loss). Cost is a transparent **estimate** (`QSDE_FNO_COST_FRAC`, default
    10% of credit) until real fills exist. 9 tests incl. builder↔payoff consistency.
  - `scorecard/resolver.py` — `resolve_option_ideas()` settles every OPEN `IRON_CONDOR`
    past expiry via an **injectable settlement lookup** (default: `fno_iv_history.spot`
    on the expiry session → `ohlcv` close; **returns None / waits** when settlement isn't
    knowable — never guessed). Wired into `daily_eod.py` + `snapshot_fno_chain.py`.
  - `scripts/scorecard_report.py` — the **Reality Engine read-out**: resolves matured
    ideas, then prints per-engine MEASURED net expectancy / hit-rate / payoff / brier
    (+ `--calibration`). Accuracy is reported here, never promised.
  - **DB-smoked** end-to-end: a win + a loss condor → resolved → `fno_income` block
    (n=2, hit 50%, expectancy −0.138, payoff 0.75, brier 0.29).
- **Index settlement source (increment 5b)** — `fno/settlement.py`: `index_settlement`
  composes sources cheapest-first and **never guesses** — persisted `fno_iv_history.spot`
  on the expiry session → expiry-day **F&O bhavcopy `UndrlygPric`** (same free EOD file,
  no Kite token) → injectable extras (e.g. a future Kite index-close). Pure
  `settlement_from_bhavcopy` is tested; the resolver's default delegates here. 8 tests +
  **DB smoke**: a condor resolves via the *default* path from persisted spot
  (`no_settlement: 0`). The earlier settlement gap is now closed with free EOD data; the
  only remaining piece is an optional Kite index-close extra for days neither EOD source ran.
- **PairsStatArbEngine (StatArb family, market-neutral) — just landed:**
  - `stats/pairs_math.py` (pure) — OLS hedge β, Engle-Granger coint p-value, AR(1)
    half-life, rolling z; `evaluate_pair` gates on p≤0.05 + half-life∈[1,30] + β>0.
  - `engines/pairs.py` — `PairsStatArbEngine` emits a defined-risk `Structure.PAIR`
    (z-stop sizes `max_loss`; short rich leg / long cheap leg) with a **frozen
    resolution plan** (β/α/mean/σ/spread₀/z-levels) in `adjustment_plan` JSON.
  - `Structure.PAIR` added to the contract (`base.py`): actionable at direction 0,
    `validate()` requires 2 legs + finite max_loss.
  - `scorecard/resolver.py` — `resolve_pair_outcome` (pure) + `resolve_pair_ideas`:
    replays the spread forward vs the **frozen entry** mean/σ (lookahead-safe),
    exits WIN at exit_z / LOSS at stop_z / TIME at max_hold. Wired into `daily_eod`,
    `/api/scorecard/resolve`, and `scorecard_report.py`.
  - `scripts/run_pairs.py` runner. **12 tests + full DB-lifecycle smoke** (emit →
    record → seed reverting bars → resolve WIN → Scorecard block).
  - Candidate pairs restricted to the SAME real sector (anti-data-mining). Sectors now
    **populated 48/50** via `scripts/backfill_sectors.py` (GICS from yfinance;
    LTIM/TATAMOTORS fail — ticker renamed) → **~123 candidate pairs**, emits sensible
    same-sector pairs (e.g. HDFCBANK/HDFCLIFE @ p=0.029). Re-run backfill on universe changes.
  - Cointegration gate hardened by adversarial review: EG p = **max of both directions**
    (order-robust), **one 250-day window** for hedge+z+EG+half-life, and half-life needs
    the AR(1) λ to be **t-test-significant** (`t ≤ −2`).
  - ⚠️ "defined risk" is a **z-stop**, not a hard structural cap — the spread can gap
    through it; the F&O condor is the only truly hard-capped structure.
- **Idempotent idea-log**: migration 015 adds `idea_date` + `UNIQUE(engine, symbol,
  idea_date, structure)`; `store.record_idea` uses `ON CONFLICT … DO NOTHING`.
  **DB-verified idempotent** (same business key twice → 1 row).
- **CLIs**: `scripts/run_engines.py`, `scripts/resolve_ideas.py`,
  `scripts/snapshot_fno_chain.py`, `scripts/scorecard_report.py`; engines + resolver
  + F&O snapshot + condor-resolve wired into `scripts/daily_eod.py` (beside the legacy flow).

### Pending
- **F&O remaining (the immediate path to a live flagship):**
  1. **Accrue the warm-up window** — run `snapshot_fno_chain.py` (or daily_eod) each
     session so `fno_iv_history` builds ≥20 sessions; until then the engine correctly
     emits nothing. For a head start, backfill ATM IV from saved/downloaded bhavcopies.
  2. **Index settlement source** — DONE (increment 5b, `fno/settlement.py`): condors
     settle from persisted spot or the expiry-day bhavcopy. Optional follow-up: a Kite
     index-close `extra_lookup` for days neither EOD source ran.
  3. Wire the **Kite NFO live chain** (`KiteChainSource` shell exists; needs a daily
     token + `instruments("NFO")`) for intraday snapshots; the EOD bhavcopy path is done.
  4. Then `FnoIncomeEngine` runs **live in paper** via the Scorecard.
- **Later strangler increments:** migrate `/api/signals` (fix the score-as-return bug)
  and `analyze.py` onto the new contract. **Legacy writer deletions come LAST**, only
  after the new path is paper-proven. NOTE: RuleMomentumEngine **depends on**
  `rule_signal_writer`'s output (shared `signals` table) — do **not** delete that writer.
- **Paper-validate** every engine through the Scorecard before any real capital.
- **Accuracy comparison harness** (user requirement) — **delivered**: emit-time idea-log
  → mechanical resolver (cash barriers + condor expiry) → measured per-engine net edge +
  calibration + chart-fidelity (`fidelity.py`), exposed THREE ways: `scorecard_report.py`
  CLI, `GET/POST /api/scorecard{,/ideas,/resolve}` (`api/routes/scorecard.py`), and the
  **`/scorecard` dashboard page** (`frontend/src/app/scorecard/page.tsx`, in the nav).
  What remains is purely operational: let it run over real sessions so the idea-log
  accumulates a track record.

### Decided but not yet built (the "research platform" rethink)
- **Alerts:** `ntfy` push (Telegram is banned in India) with confirm-buttons over
  Tailscale/WireGuard. Confirm endpoint must be **authenticated** + reachable only on
  the private net.
- **Local LLM:** Gemma 3 12B via Ollama (8–12GB VRAM) for news/sentiment + research.
- **News/sentiment:** NSE/BSE filings + Indian RSS + GDELT → Gemma.
- **UI:** TUI console now → Tauri desktop research app later (reuse the React charts).
- **Live research everywhere** (e.g. pick PATANJALI → live charts, indicators,
  compared to live market).

---

## 6. Recent fixes already landed (don't redo)
- **PIT read** (`models/dataset.py`): training reads earliest-written version via
  `SELECT DISTINCT ON (symbol, as_of_date, factor_name) … ORDER BY … valid_from ASC`
  (the canonical `valid_to='infinity'` predicate returns nothing because pit_writer
  stamps `valid_from` = wall-clock; the auditor's `valid_from <= as_of_date` fix would
  have broken training).
- **Day-loss wiring** (`api/routes/orders.py`): `_today_live_pnl()` + `QSDE_MAX_DAY_LOSS`
  passed to `check_risk_gates`.
- **UTC→IST** (`ingestion/intraday_storage.py`): naive Kite timestamps localized to
  `Asia/Kolkata`, not UTC (two sites).
- **CORR-1 ATR unit** (`risk/trade_levels.py` + `live/intraday_signal.py` +
  `execution/paper_journal.py`): `atr_pct` is unambiguously **percent**; removed the
  `>1.0` magnitude heuristic that caused 100×-too-wide stops. 5 regression tests.
- **statsmodels** dependency + pytest **collection error** (duplicate test basenames)
  fixed via `backend/pytest.ini` (`testpaths=tests`, `--import-mode=importlib`).

---

## 7. Gotchas / environment notes
- A broken hook `check-sql-files.py` (path missing) fires on every Write/Edit —
  **harmless noise, ignore it.**
- `read_sql` (SQLAlchemy `text()`) uses **`:name`** bind params; `execute_sql`
  (psycopg2) uses **`%s` / `%(name)s`**. Don't mix them (this bit the dedup smoke test).
- Multi-agent audit workflows kept dying on "session limit" — audits were done inline.
- `tests/test_scorecard_store.py` has a linter/user-added test
  `test_idea_to_row_includes_idea_date_for_dedup` — keep it.
- Test count climbs as increments land (73 → 204 → **210**). If it drops, something
  regressed.

---

## 8. File map (new spine)
```
backend/qsde/engines/   base.py  sizing.py  rule_momentum.py  fno_income.py
backend/qsde/levels/    __init__.py  options.py
backend/qsde/scorecard/ metrics.py  fidelity.py  store.py  resolver.py  tracker.py
backend/qsde/fno/       black_scholes.py  bhavcopy.py  vol.py
backend/scripts/        run_engines.py  resolve_ideas.py  daily_eod.py (edited)
infra/migrations/       014_trade_ideas.sql  015_trade_ideas_dedup.sql
ARCHITECTURE.md         full rebuild plan + honesty doctrine + Scorecard + roadmap
run.ps1 / START.md      launcher + run commands
```

Tests live in `backend/tests/test_*.py` (trade_idea, levels_options, black_scholes,
fno_income, fno_data, scorecard_*, trade_levels, live_watch_state, …).
