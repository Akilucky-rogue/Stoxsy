# Continuous data: market, fundamentals, news, updates, technicals

Owner requirement (2026-10-18): the platform should keep itself fed with market data, fundamentals, news, corporate updates
and technical features without manual loads. This document fixes the design and the order of work. It builds on the plan's
live-compare / alert phases (`C:\Users\ADMIN\.claude\plans\...`, sections D1, D5, D6) and on ADR 0001 (feature store).

## What "streaming" can honestly mean here

| Stream | Source | Real cadence | Needs |
|---|---|---|---|
| Live prices, ticks | Kite WebSocket (`full` mode: LTP, volume, OI, 5-level depth) | sub-second to ~1 s | daily Kite login (token expires ~06:00 IST), admin only |
| Intraday bars | built from ticks; Kite historical to repair gaps | 1 minute | same |
| End-of-day prices, delivery, indices, VIX, FII/DII | NSE archives (public files) | once, ~18:00-20:00 IST | none (HTTP, rate-limited) |
| Corporate actions, symbol changes, announcements | NSE public files / feeds | EOD for actions; announcements polled every 1-5 min | none |
| Results / fundamentals | exchange filings (NSE/BSE results), stamped with FILING date | event-driven; quarterly | parser per format; no yfinance fundamentals |
| News | RSS (publisher feeds, per-symbol queries) + exchange announcements | poll 1-5 min | optional paid API key |
| Macro | RBI, MOSPI, FRED releases | calendar-driven | FRED key (rotated, not in repo) |
| Technical / quant features | DERIVED from the above by the factor engine | after each EOD; intraday subset on bar close | none |

Not claimed: Level-3 order books, co-location or microsecond latency (retail Kite offers 5-level depth over the internet);
HFT/arbitrage execution; reinforcement-learning execution agents; any live order placement (out of scope, unchanged).
Satellite/credit-card "alternative data" is unavailable; the realistic alternative data here is news/announcement text,
delivery percentage, FII/DII flows and option-chain OI.

## Architecture (one pattern for every source)

```
scheduler (APScheduler, Asia/Kolkata)  ──► SourceAdapter.fetch(since)
                                              │ raw bytes + fetch metadata
                                              ▼
                         landing zone  D:\Stoxsy\data\landing\<source>\<yyyy>\<mm>\<dd>\<hhmmss>-<sha8>.<ext>   (immutable)
                                              │ parse + validate (schema, ranges, duplicates)
                                              ▼
                         normalized tables (TimescaleDB)  — idempotent upserts, every row carries
                         event_time  (when it happened)  and  known_at  (when we could first have known it)
                                              │
              ┌───────────────────────────────┼───────────────────────────────┐
              ▼                               ▼                               ▼
      factor engine (incremental)     Redis pub/sub + SSE             alert rules (stale feed, filings, price
      → Parquet increment snapshot    (live compare, UI)               cross, news spike, token expiry)
```

Rules that keep it honest:
1. **Raw first.** Every fetch is stored byte-for-byte before parsing, so a parser fix can be replayed without re-fetching.
2. **Point-in-time.** `known_at` is set from the fetch time (or the publisher timestamp if later), never back-dated; features
   read with `known_before=`. Corrections are new rows, not rewrites.
3. **Idempotent and resumable.** Natural keys + `ON CONFLICT`; each adapter keeps a high-water mark.
4. **Health is data.** A `source_health` table records last success, last data time, lag, error count per source; staleness
   beyond an SLO raises an alert (existing alert design) and shows in the Data & System page.
5. **Politeness and limits.** Public NSE endpoints are rate-limited and need browser-like headers; adapters back off and never
   hammer. Anything needing credentials reads them from `.env`; nothing secret is committed.
6. **Corporate-action truth is a dependency of price streams.** New EOD prices flow through the same reconcile → approvals →
   official-series path; new unconfirmed gaps are quarantined by the serving guard rather than scored.

## Feature families fed by the streams (mapping of the four analysis frameworks)

* **Fundamental** (filing-dated, PIT): revenue/profit growth, margins, debt/equity, cash conversion, valuation ratios (P/E, P/B)
  computed with price at `known_at`. Replaces the nulled `fund_*` columns only once a filing source exists.
* **Technical:** already in the engine (44 tech + SMC + pattern features); intraday variants on bar close.
* **Quantitative:** volatility estimators, regime state, cross-sectional ranks, residual/beta features, pairs spreads.
* **Macro / top-down:** India VIX, rates, FX, crude, FII/DII flows, sector-relative strength.
* **Text:** headline/announcement sentiment (FinBERT-class model, CPU inference) aggregated per symbol per day with `known_at`.

Models stay behind the same harness: CPCV, trial ledger, costs, baselines, verdict rule. Candidate families in the roadmap
(TFT, GBDT ranker, HMM regime, LSTM-HAR volatility, FinBERT text features, HRP/LASSO for portfolio construction) all enter
through `qsde.ml.experiment`; none is promoted without the gate.

## Build order

| Step | Content | Needs your go-ahead |
|---|---|---|
| S0 | `SourceAdapter` interface, landing zone, `source_health` table, scheduler skeleton, replay-from-landing test | no |
| S1 | NSE EOD adapters: bhavcopy, delivery, indices/VIX, FII/DII, corporate actions, symbol changes (the symbol-change file is already fetched once) | each new endpoint: yes (public HTTP) |
| S2 | Kite ticker → Redis → 1-minute bars → live compare; Kite instruments to validate current symbols | Kite login (you run it) |
| S3 | Exchange announcements + RSS news landing and storage, dedupe, symbol tagging | yes (HTTP); paid APIs only if you choose |
| S4 | Results/fundamentals parser with filing-date stamping | yes (HTTP) |
| S5 | Text sentiment features (local model) feeding the factor engine | model download |
| S6 | Incremental factor refresh after each S1/S2 landing; alerts for stale sources | no |

S0 and S6 can be built now with no network. Nothing in S1-S5 runs until you approve that source.
