> **Archived 2026-10-31, kept for history.** This describes an earlier build. The current description of the system is `docs/design/SYSTEM_FLOW.md`; the current plan is `docs/design/PLATFORM_ROADMAP.md`. Do not follow the instructions below without checking them against those.

# Stoxsy / QSDE — Deep Audit Report v2

**Date:** 2026-06-11
**Auditor:** Claude (Cowork)
**Scope:** Full static read of the entire repo — infra (init.sql + 13 migrations, docker-compose), all ~25k LOC of backend Python (ingestion, factors, models, risk, execution, live, research, API), frontend, tests, ops scripts. Cross-checked against `docs/AUDIT_v1.md` (2026-05-10) and the 5 Critical Rules in `CLAUDE.md`. No live DB access from sandbox; all findings are from code, schema, and config analysis. All 160 Python files byte-compile cleanly; 153 tests exist across 18 files.

---

## TL;DR

The project has matured enormously since AUDIT_v1: the PIT writer exists, purged k-fold CV with per-horizon embargo is real and correct, the DSR math bug is fixed, the candidate/active model promotion gate works, triple-barrier labels and meta-labeling are implemented faithfully to AFML, and the paper-trading validation loop with three baselines + drift-driven risk de-escalation is a genuinely well-designed piece of engineering discipline. The execution layer (dry-run default, kill switch, HMAC confirm tokens) is defensively built.

However, the audit found **4 critical correctness issues** that undermine the system's central claims:

1. **Train/serve skew:** models are trained on fractionally-differenced features but served raw factor values. Production predictions from any promoted model are systematically distorted.
2. **Fundamental factor lookahead:** the yfinance fundamentals ingester stamps *today's* trailing ratios (PE, ROE, margins, market cap…) onto every historical filing row. Every `fund_*` factor in the training history leaks current data.
3. **The "cost-aware model" claim is false in the default path:** the cost subtraction is applied to fixed-horizon returns that triple-barrier labeling then discards. Labels, `target_ret`, and the promotion-gate DSR are all **gross of cost** — Critical Rule 5 is not enforced where it matters most.
4. **The PIT machinery does not actually enforce point-in-time correctness:** `valid_from` is the compute wall-clock (so the bitemporal predicate returns nothing for backfilled history), every production read uses `valid_to = 'infinity'` (latest snapshot), and the one sanctioned function `pit_query()` has **zero callers**. Rule 1 survives only through compute-time shift discipline — which is mostly good, but is not the stated mechanism.

None of these affect the *paper-trading* track record's internal model-vs-baseline fairness (all strategies share the same conventions), but they mean the backtested DSR/Sharpe numbers and the live signals' quality cannot be trusted until fixed.

---

## 1. Critical findings

### C1 — Fracdiff train/serve skew (production signals are distorted)

- **Train:** `qsde/models/dataset.py` defaults `apply_fracdiff=True`; `fracdiff.apply_fracdiff_to_features()` transforms every non-stationary feature **in place** (same column name) with `frac_diff_ffd(d=0.4)` (~150-bar kernel).
- **Serve:** `qsde/models/signal_generator.py:generate_signals()` reads the **raw latest values** from `factor_pit` (`DISTINCT ON … valid_to='infinity'`) and calls `model.predict()` directly. **No fracdiff is applied anywhere in the serve path** — nor could it be, since serving fetches a single latest value per factor, not the ~150-day history the kernel needs.
- **Effect:** every level-form feature (OBV, rolling-VWAP levels, volume-profile POC/VAH/VAL, SMA/EMA levels, Donchian levels, macro levels, …) is in completely different units at serve time than at train time. Tree splits learned on fracdiff'd magnitudes route raw levels into degenerate leaves. The model's cross-sectional ranking in production is not the ranking the CV validated.
- **Also:** `edge_stats.json` seed claims "fracdiff features" were used in the stress-tested numbers — those numbers came from OOF predictions (fracdiff'd, consistent), so the *backtest* is internally consistent; it's *production serving* that diverges.
- **Fix options:**
  (a) Move fracdiff upstream into the factor engine so `factor_pit` stores the transformed values (then train and serve read the same thing). Persist `d` and kernel width with the factor version.
  (b) Or train with `apply_fracdiff=False` and accept level features (LightGBM is split-based; the original motivation was SHAP saturation, not accuracy).
  (c) Or have the serve path rebuild the trailing window per symbol and apply the identical transform (slowest, most fragile).
  (a) is the right answer architecturally. Until then, treat all live `ml` signals as suspect.

### C2 — yfinance fundamentals contaminate history with current data (lookahead)

`qsde/ingestion/yfinance_client.py:fetch_fundamentals_batch()` builds a `trailing` dict from `tk.info` — **current** trailingPE, priceToBook, ROE, margins, debt/equity, market cap, enterprise value, dividend yield — then **copies it into every historical fiscal-period row**:

```python
row = trailing.copy()
row["fiscal_date"] = fiscal_date          # e.g. 2022-03-31
row["filing_date"] = fiscal_date + 45d    # e.g. 2022-05-15
```

So the `fundamentals` row "filed 2022-05-15" carries June-2026 ratios. The downstream `merge_asof(filing_date <= t)` in `factors/fundamental.py` is PIT-correct *mechanically*, but it forward-fills poisoned values: **every `fund_*` factor over the entire training history equals (approximately) its current value.** This is textbook lookahead — e.g. the model can "learn" that names whose *2026* PE is low outperformed in 2021. This path runs in production seeding (`run_ingestion.py`, `scripts/kite_ingest.py`).

Secondary issues in the same function: `filing_date = fiscal_date + 45 days` is a guess (fine if labeled as such; annual results have 60-day deadlines); newer yfinance returns `dividendYield` already in percent, so `* 100` may double-scale; only `revenue` / `net_income` / `fcf` are genuinely per-period.

**Fix:** populate per-period fundamentals only (compute ratios from the period statements + price *as of filing date*), or drop `fund_*` factors from training until a true PIT fundamentals source exists. Add a regression test: for any symbol, `fund_pe` must differ across years.

### C3 — Costs are not in the labels or the promotion gate (Rule 5 violated at the decision point)

- `dataset.py` subtracts `cost_frac(horizon)` from `fwd_returns_pivot` — then, in the **default** `label_method="triple_barrier"` path, `apply_triple_barrier_labels()` **replaces `target` entirely** with labels computed from raw close-price paths, and produces `target_ret` as the **gross** barrier-exit return. The cost subtraction is dead code in the default path. The docstring's claim ("a trade that would have hit gross-PT now needs to hit gross-PT + cost to register as a WIN") is not implemented anywhere in `triple_barrier.py`.
- `lgbm_model.train_lightgbm_model()` computes the promotion DSR via `_evaluate_oos()` on **gross** `target_ret`. The cost-aware evaluator `_evaluate_oos_with_costs()` exists but is only called by the offline `scripts/reevaluate_oos.py`.
- Consequence: **the only promotion metric (DSR, Rule 2) is computed on cost-free returns** — exactly what Rule 5 forbids. `retrain.py`'s docstring ("models that predict NET-of-cost returns natively") and the README's "Cost-aware target" bullet are both false under default settings.
- Honest mitigations that *do* exist: `edge_stats.json` (stress-tested net numbers shown in the UI), paper trades net of `PAPER_DEFAULT_BPS`, and the drift report comparing net Sharpe. Good — but the gate itself must be net.
- **Fix:** make the promotion gate use `_evaluate_oos_with_costs(...)["dsr_net"]`, and/or implement cost-aware barriers in `triple_barrier.py` (shift PT up / SL down by `cost_frac`), then correct the docs.

### C4 — The PIT system is decorative; Rule 1 is enforced only by convention

- `pit_writer.write_factors_to_pit()` sets `valid_from = datetime.now()` (compute wall-clock). For any historical backfill, a 2015 row gets `valid_from = 2026-06-11`, so the canonical predicate `valid_from <= as_of_date AND valid_to > as_of_date` returns **zero rows for all of history**. The bitemporal scheme can never answer "what was known on date D" for backfilled data.
- Consequently every production read bypasses the predicate: `dataset.py` and `signal_generator.py` filter `valid_to = 'infinity'` (latest version), i.e. **current-snapshot semantics**. If a factor is ever recomputed with revised inputs, backtests silently use the revision (the precise failure PIT was built to prevent).
- `db/connection.pit_query()` — documented as "the ONLY sanctioned way to retrieve factors" — has **no callers** anywhere in the codebase.
- What actually protects against lookahead today is compute-time discipline, which I verified factor-by-factor: technical/SMC/pattern factors use only backward windows (no negative shifts anywhere in `qsde/factors/`); SMC swing pivots correctly defer knowledge by `.shift(right)`; flow/sentiment/macro all `.shift(1)`; fundamentals use `merge_asof` on filing_date. That discipline is good — but it's invisible to the schema and untested.
- **Fix:** either (a) make `valid_from` the *knowability* timestamp (as_of_date's session close for derived factors; filing/publication time for fundamentals/news) so the bitemporal predicate becomes meaningful, or (b) simplify honestly to a versioned snapshot table and delete the pretense. Either way, add a PIT regression test (still missing, as in AUDIT_v1).

---

## 2. High-severity findings

### H1 — ATR percent/fraction auto-detect mis-fires for low-volatility names
`risk/trade_levels.py` treats `atr_pct > 1.0` as percent-form and divides by 100, else assumes fraction. `factors/technical.py` stores `tech_atr_pct` as **percent**. A calm mega-cap with ATR = 0.85% arrives as `0.85` → treated as an 85% *fraction* → intraday stop placed 0.75 × 85% ≈ **64% below entry**, targets equally absurd, R:R math garbage. Affects signals, paper trades, order tickets for every name whose ATR% < 1 (common among Nifty-50 large caps in calm regimes). **Fix:** pick one unit, enforce it at the boundary, delete the heuristic.

### H2 — Three inconsistent definitions of every horizon
- `triple_barrier._HORIZON_BARRIERS` uses `t1_days` as **calendar** days (`t + pd.Timedelta(days=5)`) → a "swing" label window is ~3 trading sessions.
- `paper_journal._HORIZON_SESSIONS` uses **5 NSE sessions** for swing.
- `settings.horizons` says intraday/swing/long = 5/20/60 forward days (stale, unused); `purged_cv.HORIZON_DAYS` says 1/5/20.
So training labels, live reconciliation, and documentation measure three different windows. Same for `long`: 20 calendar days ≈ 14 sessions in labels vs 20 sessions in paper. **Fix:** define all windows in trading sessions, derive t1 by stepping the price index (`searchsorted` on position, not Timedelta).

### H3 — "Intraday" is actually an overnight trade
Default TB labels for intraday are close[t] → close[t+1] (overnight gap included), and paper reconciliation evaluates intraday trades on next-day daily bars; but `intraday_target="open_to_close"` (the documented intent, "no overnight gap") is silently discarded when `label_method="triple_barrier"`, and order tickets default to **MIS** (must square off same day — cannot hold the overnight move the model was trained on). The model, the paper scorecard, and the execution product disagree about what an intraday trade is. **Fix:** either rename the horizon to `btst` and use CNC, or build intraday labels/reconciliation on minute bars open→close.

### H4 — Label barriers use closes; paper trades use highs/lows
`triple_barrier._apply_pt_sl_on_t1` detects PT/SL hits on **daily closes only**; `paper_journal._resolve_barrier_daily` uses **high/low** (with pessimistic same-bar tie). Live outcomes will systematically hit barriers more often than training labels did — the live hit-rate is not measuring the labeled quantity. **Fix:** use high/low (long: high≥PT, low≤SL) in labeling too, with the same pessimistic tie rule.

### H5 — `init.sql` contains an index that fails on every fresh install
`CREATE INDEX … ON factor_pit(…) WHERE valid_to > NOW()` — PostgreSQL requires IMMUTABLE functions in index predicates; `NOW()` is STABLE, so this statement errors (the rest of init.sql still applies, statement-by-statement). The working replacement (`WHERE valid_to = 'infinity'`) lives only in `scripts/_archive/create_active_index.py` — i.e., a fresh machine gets **no** active-factor index and the `DISTINCT ON` serve query degrades badly at 87M+ rows. **Fix:** replace the predicate in init.sql with `valid_to = 'infinity'::timestamptz` and add the index as migration 014.

### H6 — Survivorship bias in the training universe
The universe is today's constituents (NSE API / Kite instrument dump); OHLCV history is backfilled only for them. Delisted/derated names never enter training. Every backtest metric (IC, Sharpe, DSR) is inflated by index-survivor selection — particularly for the `long` horizon. **Fix:** at minimum document it next to edge stats; properly, ingest historical index membership and gate training rows on membership-as-of-date.

### H7 — Finnhub sentiment writer will crash if re-enabled
Migration 012 changed `news_sentiment` PK to `(symbol, date, source)`, and the MoneyControl/ET writers were updated — but `finnhub_client.sync_sentiment_to_db()` still does `ON CONFLICT (symbol, date)`, which Postgres rejects outright when no matching unique constraint exists. Currently dormant only because free-tier Finnhub 403s and returns empty. **Fix:** add `source` to the conflict columns.

### H8 — Paper-trade entries are filled at a price you can't get
`take_trade()` records entry at the signal's `entry_price` = the close of the session that just ended; the EOD job runs ~15:45+. You cannot buy today's close after the close. Baselines share the convention, so the **relative** model-vs-baseline comparison is fair, but the absolute net Sharpe the governor escalates on is optimistic by roughly the overnight drift + open slippage. **Fix:** fill paper entries at next session's open (the data is there the next evening; reconciliation already starts at `date > entry_date`).

---

## 3. Medium-severity findings

| # | Finding | Where | Notes |
|---|---|---|---|
| M1 | Tick timestamps: Kite's `exchange_timestamp` is naive **IST**, but `intraday_storage._floor_to_minute` labels naive input as **UTC** (`replace(tzinfo=utc)`); the fallback path uses real UTC `now()`. Bars from the two paths sit 5.5h apart in the same hypertable; contradicts the CLAUDE.md "tz-naive IST" convention. | `ingestion/kite_ticker.py`, `intraday_storage.py` | Localize naive ticks as IST, convert once, document. |
| M2 | Serve-time feature staleness: `DISTINCT ON … last 20 days` mixes factor vintages within one feature vector (one factor from yesterday, another from 3 weeks ago). | `signal_generator.py` | Require a common `as_of_date` (the latest session) and log coverage. |
| M3 | `fillna(0)` at train and serve injects in-range values (RSI=0 = max oversold, ADX=0, etc.). LightGBM handles NaN natively — zero-filling actively misleads splits. | `dataset.py`, `signal_generator.py` | Keep NaN; LightGBM's default split direction handles it. |
| M4 | Four conflicting cost tables: CLAUDE.md (5–8/12–20 bps), `settings.transaction_costs` (7/16/35 — unused), `costs.py` (6/12), paper defaults (8/15). The 12 bps delivery figure contradicts its own comment math (STT alone = 20 bps both sides) — delivery round-trip is realistically 25–30 bps. | `risk/costs.py`, `config/settings.py`, CLAUDE.md | Reconcile to one table; delete the dead settings property; re-run reevaluate_oos with the honest number. |
| M5 | DSR `n_trials = 1` while the team has iterated labels, features, params, embargoes across many retrains. Selection breadth across the research path is unaccounted; DSR degenerates to PSR. | `lgbm_model.py` | Track a trial counter in `model_runs` and feed it to the gate. |
| M6 | Embargo and purge windows measured in **calendar** Timedeltas (5d embargo ≈ 3 sessions; `label_end = horizon×1.5` calendar). Purge factor 1.5 mostly covers it, but the "5-day embargo" of Rule 3 is effectively ~3 trading days. | `purged_cv.py`, `lgbm_model.py` | Convert to session counts. |
| M7 | `smc._last_value(high, sh)` records the **confirmation bar's** high (t) as the "last swing high," not the pivot bar's high (t−right). Structure levels (BOS breaks, etc.) are computed against slightly wrong levels. | `factors/smc.py` | Shift the *value* series too: `high.shift(right).where(sh==1).ffill()`. |
| M8 | Regime engine (5-state, in the architecture diagram and CLAUDE.md) has **zero consumers** — `generate_signals`, the live loop, and risk never call it. Dead code presented as a pipeline stage. | `models/regime_engine.py` | Wire it in (condition sizing/thresholds) or move to research/. |
| M9 | No auth on the API: anyone who can reach :8000 can toggle the kill switch, confirm order tickets (token is returned by the same API), escalate risk tier, or read everything. CORS only restrains browsers. Acceptable strictly on localhost; unsafe the day it binds 0.0.0.0 or gets reverse-proxied. Also reflected-XSS via `status` and `{e}` interpolation in `kite_callback` HTML. | `api/*` | Add a bearer token middleware before any non-local deployment; escape interpolated HTML. |
| M10 | ML weights (`lgbm_*.txt`, `meta_*.txt`) are tracked in git, violating the repo's own "never commit ML artifacts" rule; also creates "weights in repo vs model_runs in DB" provenance drift. | git, `.gitignore` | `git rm --cached`, add `*.txt` under weights/ to .gitignore (OOS parquet already ignored). |
| M11 | `_HORIZON_BARRIERS` PT/SL multipliers (e.g. swing 2.5σ/1.5σ on EWM-σ of returns) vs `trade_levels` ATR multipliers (2.5×ATR floor/1.5×ATR) are *similar shapes on different vol measures* — labels and live plans diverge whenever ATR ≠ EWM σ (always). The "labels match execution" claim is approximate, not exact. | `triple_barrier.py`, `trade_levels.py` | Acceptable if documented; ideal is one vol measure. |
| M12 | Signals dated `date.today()` regardless of underlying session — a manual weekend run writes Saturday-dated signals that `auto_taker` then picks as "latest date" with stale prices. Weekday-scheduled runs are fine. | `signal_generator.py` | Date signals by the latest OHLCV session (and the rule engine already computes `latest_session` — reuse it). |
| M13 | `_evaluate_oos` ranks per `as_of_date` within OOS folds: on thin dates (early history, sparse coverage) "top decile" can be 1–2 names; per-date mean spread return is noisy. Minor metric noise, not bias. | `lgbm_model.py` | Require a minimum cross-section per date. |
| M14 | `universe` keyed by `symbol` alone — NSE/BSE dual listings collide (acknowledged in a comment); `sync_universe_from_kite(replace=True)` deactivates then upserts, so symbols dropped by Kite stay inactive — good — but NSE-website path can silently fall back to a hardcoded 50. | `ingestion/universe.py` | Composite key (symbol, exchange) when BSE work resumes. |

---

## 4. Low-severity / hygiene

- **Dead dependencies:** xgboost, catboost, optuna, shap, mlflow, alembic, celery are in `pyproject.toml` but unused (Celery also still appears in the CLAUDE.md architecture diagram; the real scheduler is Windows Task Scheduler). Trim or implement.
- **Stale config:** `settings.lgbm_dart_params` (num_leaves 63, 500 estimators, MAE) is not what training uses (`LGBM_PARAMS` in lgbm_model.py: 31 leaves, 100 rounds, RMSE); `settings.horizons` contradicts `purged_cv.HORIZON_DAYS`. Single-source these.
- **`upsert_dataframe` interpolates table/column names into SQL via f-strings** — all call sites are internal constants today, but it's an injection footgun; validate identifiers.
- **Logs and weekly reports** are committed/modified in the working tree (`backend/logs/*.log` tracked? `.gitignore` has `logs/` but `backend/weekly_reports/*.json` are tracked and churn every run). Decide whether reports are data or artifacts.
- **`frontend`:** API base URL `http://localhost:8000` is hardcoded in every page (no `NEXT_PUBLIC_API_URL`); `tsconfig.tsbuildinfo` is committed; otherwise the App-Router structure is clean and matches the backend routes (the AUDIT_v1 "six 404 nav links" gap is fully closed).
- **`fetch_fii_dii_activity`** ingests only the current-day snapshot — the `flow_fii_20d`-style market factors need months of accumulation before they mean anything; no backfill source wired.
- **`compute_net_accumulation`** uses `lb * INTERVAL '1 day'` (calendar) for a "20-day" window — consistent with other calendar/session confusion (H2).
- **Test gaps:** zero tests for `purged_cv`, `deflated_sharpe`, `dataset`, `pit_writer`, `signal_generator`, `trade_levels` unit auto-detect, or any PIT regression — i.e., the highest-risk math is the least tested. The 153 existing tests concentrate on rules, india_data, AFML triplet helpers, paper/live plumbing.
- **Python version:** project requires ≥3.11; fine, but note tooling here compiled under 3.10 — no 3.11-only syntax is used, so CI on 3.11 is the only guarantee that matters.
- **`docker-compose` / `.env.example`** ship a fixed DB password and an unauthenticated Redis on host ports — fine for localhost-only dev; revisit before any VPS deployment (the README's Hetzner ambition).

---

## 5. CLAUDE.md Critical Rules — compliance verdict

| Rule | Verdict | Evidence |
|---|---|---|
| 1. No lookahead; all factor reads via `factor_pit` PIT predicate | **Partially met.** Reads go through factor_pit, but with snapshot semantics, not the PIT predicate; `pit_query` unused; bitemporal `valid_from` meaningless for backfills (C4). Compute-time shift discipline is good — except fundamentals ingestion poisons history (C2). |
| 2. DSR is the only promotion metric | **Met mechanically** (`should_promote` gates on DSR; audit-logged; force/pause paths explicit) — but the DSR fed to it is gross-of-cost (C3) and n_trials=1 (M5). |
| 3. Purged CV, embargo = 5 days | **Met in spirit.** Correct LdP purge + embargo implementation; embargo is per-horizon (1d intraday — a deliberate, documented deviation from the blanket 5d) and measured in calendar days (M6). No plain KFold anywhere. |
| 4. Small modules, docstrings | **Met.** Genuinely excellent module hygiene and docstrings throughout. |
| 5. Transaction costs non-optional | **Not met at the gate.** Costs live in paper trades, edge stats, and an offline re-eval script; absent from default labels and from the promotion DSR (C3). Cost constants themselves are inconsistent (M4). |

## 6. AUDIT_v1 bug status

| v1 bug | Status |
|---|---|
| 1. DSR units bug | **Fixed** (rescaled by SE in daily units; verified math). |
| 2. Research engines vs schema | **Fixed** (migration 001 added columns; engines query existing columns). |
| 3. bulk_deals no backfill / dupes | **Half-fixed** (UNIQUE constraint + CSV backfill helper; no automated historical fetch). |
| 4. fundamentals PIT PK | **Fixed structurally** (PK includes filing_date) — but data written into it is contaminated (new C2). |
| 5. FMP client missing columns | **Superseded** by yfinance fundamentals — which introduced C2. |
| 6. No PIT writer | **Fixed** (pit_writer.py) — with the C4 semantic caveat. |
| 7. validate_strategy gate | **Fixed** (DSR-as-probability, 0.95, consistent). |
| 8. Telegram sync race | Unchanged (minor). |
| Tests: zero files | **Fixed** (153 tests) — critical-path gaps remain. |
| Frontend: 6 dead links | **Fixed** (all pages exist). |

## 7. What is genuinely good (keep doing this)

- **The live-validation loop is the best part of the system:** auto-taken model trades vs three baselines, net-of-cost scorecard, weekly drift verdict with exit codes, one-way automatic risk de-escalation (T0 1% cap until 30+ sessions of proven edge), append-only governor audit log. This is the discipline most retail quant projects never build.
- Honest self-knowledge encoded in code: `edge_stats.py` ships the *stress-tested* numbers with caveats ("88% of return from best 5% of days"), the ADV ≥ ₹10cr liquidity gate exists because a stress test proved fills below it are fiction, and signal rows carry `is_liquid`/`adv_20d`.
- Execution safety: dry-run default, env-gated live orders, kill switch, HMAC `confirm_token` with `compare_digest`, risk gates re-checked at confirm time, SEBI-style audit columns everywhere.
- Idempotent, guarded SQL migrations with real comments; thoughtful TimescaleDB usage (hypertables, chunk sizing, tick retention policy).
- AFML implementations (purged CV, triple barrier, meta-labeling, FFD fracdiff) are faithful to the book, with the deviations documented in-line.
- Tick pipeline design: single fanout, bounded subscriber queues with drop counters, cumulative-volume delta handling, stale-bar flusher, clean shutdown.

## 8. Prioritized fix plan

**P0 — before trusting any ML signal or escalating beyond T0:**
1. Resolve C1 (fracdiff at serve): move fracdiff into the factor engine (persist transformed factors) or retrain without fracdiff. Then retrain + re-gate.
2. Quarantine `fund_*` factors (C2): truncate the fundamentals table, retrain without them until a per-period PIT source exists.
3. Make the promotion gate net-of-cost (C3): gate on `dsr_net` from `_evaluate_oos_with_costs`, with the corrected delivery cost (M4).
4. Fix H1 (ATR units) — it silently mis-sizes live trade plans today.

**P1 — correctness of the validation loop:**
5. Unify horizon windows in sessions (H2) and barrier hit rules (H4); decide what "intraday" means (H3).
6. Paper entries at next-open (H8).
7. Fix init.sql index (H5) + finnhub conflict columns (H7); add migration 014.

**P2 — structural honesty:**
8. Decide the PIT story (C4): knowability-based `valid_from` or an explicit snapshot design; add a PIT regression test either way.
9. Survivorship documentation/membership history (H6).
10. Tests for purged_cv, DSR, dataset labeling, trade_levels; wire or retire the regime engine (M8); single-source costs and horizon configs.

---
*End of report. Line references are approximate against the working tree at commit 3771d42 (+ uncommitted changes).*
