# Stoxsy platform roadmap: data first, then pipelines, then models, then product, then clients

Status: DRAFT for owner approval, 2026-10-23; revised 2026-10-31 (see the revision section below). Principle for every phase: **raw first, point-in-time (`known_at`), validated before use, evidence-labelled, nothing preset, nothing promoted without the gate.**
"ML and DL for all" means every asset class gets candidate models measured under the same honest harness. It does not mean every model is trusted: each earns RESEARCH, PAPER or APPROVED on evidence, and no engine is approved today.

## Revision 2026-10-31: the goal restated, and what we do for it (DRAFT for owner approval)

**Goal, as confirmed by the owner on 2026-10-31.** Stoxsy is a private research and decision-support assistant that acts like a personal financial advisor.
- **For whom:** the owner first. It is designed so other people could use it later, but the Phase 5 hard gate is unchanged: nobody else is onboarded and no personalised recommendation is given to anyone else until counsel confirms the regulatory route.
- **How far the advisor goes:** a full goal-based plan: goals (amount and date), ranges (never a point forecast), a monthly check-in, plain-language explanations, flags, and options with reasons. The owner decides. Every screen says it is not advice. No orders are placed.
- **Data:** automatic wherever the source allows; whatever stays manual is made as small as possible.
- **Language model:** Gemini (a paid key created for this project, kept in `.env`, never in the repository) explains numbers the system has already computed. At first it receives public data only (fund figures, prices, alerts). It never produces signals, sizes or promotions, and no number it writes may differ from the numbers it was given.
- **Models:** the ML layer keeps running as research under the same honest harness; the advisor uses only what has earned trust. Today no model has, so the advisor leans on the risk ranking, fund costs and the plan engine, and says so.
- **Project hygiene:** one clear structure, current docs, legacy kept in labelled archives, nothing deleted without an approved list (see `CLEANUP_PROPOSAL.md`).

**Decisions kept as recorded:** official and free sources only; no paid news vendor; legal route stays personal use until counsel; Debian always-on host for the pipeline; CLI plus local web UI, never public; Choice for holdings and Kite for live data only; the LLM never produces signals, sizes or promotions.

**One recorded decision this revision touches (owner to confirm).** On 2026-10-25: "local Gemma first; a paid frontier API only as an optional fallback". On 2026-10-31 the owner has a paid Gemini key and wants it used. Proposed reading: Gemini for explanations built from public data; local Gemma reserved for anything that would involve the owner's own holdings or broker files, if that boundary is ever widened.

**Done since 2026-10-23.** 1.1 expense ratios: Part A (ingestion, revisions, `fetch-ter`), Part B (API and Funds page); nightly routine, CLI, doctor and alerts working; broker-file import and lots built; UI reviewed.

### Work, in order

| # | Workstream | What we do | Needs from the owner |
|---|---|---|---|
| A | **Clean-up and structure** | Reorganise on its own branch: archive legacy and one-off code, fix the docs and CLAUDE.md, add indexes. Nothing deleted without an approved list. | Approve `CLEANUP_PROPOSAL.md` |
| B | **Fresh data without you** | (1) feature build and model scoring join the nightly routine, with a freshness check in `stoxsy doctor`; (2) the pipeline host: the always-on mini-PC decision stands, and until it exists the laptop must not sleep on battery; (3) schedule `fetch-ter` once AMFI's written permission is in hand; (4) an upload screen for broker files (same dry-run check); (5) failures reach you, not just the log. | AMFI email sent; host purchase |
| C | **Cost-aware fund ranking** (Part C, migration 066) | Rank funds against like-for-like peers on the base expense ratio, with risk and data quality; flagged figures excluded. Feeds the advisor's fund shortlist. | Review the ranking rules |
| D | **Advisor layer** | (1) `qsde/advisor/`: a thin model adapter (Gemini now, local Gemma possible), guardrails (every number in the text must appear in the input; no buy or sell; labelled AI-written; every prompt and answer logged); (2) first use: the Alerts page as one-line summaries with details folded; (3) a "Today" home screen on top of the cards, alerts and plan; (4) the **goal-based plan engine** extending the Investor plan: goals, contributions, risk capacity, ranges, a monthly check-in; (5) a UX pass: phone width, keyboard use, plain wording. | Gemini key in `.env`; data boundary (public only to start) |
| E | **Models (Phase 3)** | Continue under the same harness; the advisor reads only models that have earned PAPER or APPROVED. | none |
| F | **Others later (gated)** | Per-user data separation audit, data-licence checks (AMFI is personal, non-commercial; NSE and BSE terms for any redistribution), privacy and security review, counsel on the SEBI route. Building does not cross the gate; opening the door does. | counsel |

Dependencies: A first (everything builds on a clear structure); B1 before D3 and D4 (an advisor on stale data misleads); C before the fund shortlist in D4; D1 before D2 to D4; E runs alongside.

## 0. Where we stand (measured 2026-10-23)

| Domain | Held today | Gap that blocks models or product |
|---|---|---|
| **Equity, cash** | NSE end-of-day prices 2016-01-01 to 2026-10-16: 5.1M rows, 4,005 symbols. BSE end-of-day: 9.7M rows. NSE corporate actions 24,356 and BSE 22,060 records. Live minute bars from Kite (verified 2026-10-23), reconciled after the close to Kite history. | BSE adjusted series (only after validation); historical index membership (survivorship); delivery %, bulk/block deals, institutional flows, India VIX, macro (tables exist, **0 rows**). |
| **Futures and options** | NSE F&O archive 2016-2026: 35.2M contract rows, 2,667 sessions. Kite instrument master (41,026 NSE F&O contracts). Live ticks work. | Implied volatility is not in the archive (derive and label it); lot-size history; historical option-chain snapshots beyond EOD; one missing archive session (2021-03-30). |
| **Mutual funds** | 23,858 schemes (4,612 direct-growth), 24.2M NAV rows 2012-2026, classification, returns, risk, peer context. | **Expense ratios (TER) history, portfolio holdings, AUM, exit loads, riskometer, benchmark total-return indices, fund-manager history.** Without these, "which fund" is returns-only. |
| **Fundamentals** | `fundamentals` table exists, **0 rows**. | Quarterly and annual financials, shareholding pattern, balance sheet items, point-in-time (filing date, not period end). |
| **News and events** | `news_sentiment` table exists, **0 rows**. Exchange corporate-action files only. | Exchange announcements feed, results calendar, a licensed headline source, then sentiment as a derived, labelled layer. |
| **Models** | Defensive-card shadow model (risk label, no return forecast); harness with purged CV, DSR, PBO, null control; ledger, monitor, kill switch. ML directional ranker **rejected**. | Everything in phase 3. |

## Owner decisions (recorded 2026-10-25)

| Question | Decision |
|---|---|
| Phase 1 sources | **Approved: official and free sources only** (items 1.1 to 1.6, 1.8 to 1.11). Every source must pass terms review, raw-first landing with SHA-256 and `known_at`, idempotent parsing and validation, a coverage/gap report, and **no silent fallback to an unapproved source** |
| Paid news or fundamentals vendor | **No.** Revisit only if exchange filings prove unreliable, a model shows news adds out-of-sample value, historical timestamps are needed, and licensing is handled |
| 1.7 paid headlines, 1.12 MCX | **Deferred** (MCX after equities, funds, F&O research and the product core are stable) |
| Legal route | **Personal use.** No outside users, no personalised recommendations to others, no public "wealth management" claims; KYC, Terms and Privacy stay drafts until counsel and a clear SEBI route exist |
| Pipeline host | **Always-on Debian mini-PC with UPS** (headless, wired, auto-restart after power loss, reboots outside market hours, remote monitoring, nightly backup to a second disk and cloud). Not this laptop, not a VM on it. A cloud VM is a monitored fallback only |
| Operator interface | **CLI-first operator console (`stoxsy`) plus the local web UI** for charts and analysis; the web UI is reachable on the LAN or a private VPN only, never public |
| Build order | (1) finish and validate the 1.8 adapters, (2) AMFI expense ratios, (3) fund holdings for the 20 largest fund houses, (4) exchange fundamentals and announcements after terms review, (5) unified daily pipeline with scheduler, alerting and backup/restore drills, (6) Phase 3 model candidates |
| Accounts and holdings (2026-10-26) | Equities are held at **Choice Equity**; **Zerodha Kite is for live market data only**. Holdings come from files via `stoxsy import` (dry run, per-layout approval, per-security pot triage); tax lots and holding-period classes follow. All 12 Choice holdings were assigned to the investment pot by the owner; the trading pot is empty |
| Assistant model (2026-10-25) | **Local Gemma first** for "Ask Stoxsy"; a paid frontier API (Gemini 4 Argon) only as an optional, opt-in deep-research fallback with minimal retrieved context; the LLM never produces signals, sizes or promotions. **Revision pending 2026-10-31: see the revision section at the top** |
| Bucket A rebuild (2026-10-25) | **Approved on the dry-run diff and executed** (Option B); see ADR nineteenth review and `ohlcv_change_report` |
| Next priorities (2026-10-25) | stream heartbeat before Monday, then the trader risk engine, live-compare resolution of the 978 predictions, then fund expense ratios and holdings, Debian migration; defer DL, paid vendors, MCX |
| Incident rule | The stream silently stopping when a PC slept is a **production incident**: heartbeat alerts, scheduler health checks and automatic recovery are required |

Product statement (owner): a Trader mode and an Investor mode with different mindsets; research on any Indian stock; daily candidate ideas; streaming data for equities, futures and mutual funds; every output answers what, where, how much, until when, under what conditions, the exit, and alerts. Positioning agreed: **an evidence-first Indian market research and wealth-decision platform** (transparent signals, risk-sized plans, portfolio oversight, models that must prove themselves), not "AI that predicts stocks".

## Phase 1: acquire the data (nothing downstream starts before its inputs pass a gate)

Each source below is verified before building: official or licensed status, terms of use, format, cadence, point-in-time behaviour. A source that fails its gate is dropped, not worked around. **New external sources need your approval; this document is that request.** Items marked (verify) are not yet confirmed against the live site.

| # | Dataset | Preferred source (official first) | How | Cadence | Unlocks | Acceptance gate |
|---|---|---|---|---|---|---|
| 1.1 | Mutual-fund **expense ratio** (base TER, additional expenses, GST, by plan) | SEBI requires every AMC to publish scheme-wise, date-wise TER daily on its site and on AMFI's site as a downloadable spreadsheet. Start with the AMFI listing, fall back to each AMC's TER page (verify current URLs; AMFI's new site is script-driven and has no stable direct link) | adapter per source, manual-download inbox as fallback (the pattern used for BSE actions) | daily, change-driven | fund cost screen, net-of-cost fund ranking, direct vs regular | every scheme in the master has a TER row for the latest date or an explained gap; TER within SEBI slab limits; history starts at first capture (we cannot back-fill what was not archived: say so) |
| 1.2 | Mutual-fund **portfolio holdings** (security, ISIN, quantity, value, % of NAV) | Monthly (and half-yearly) disclosure files that each AMC must publish on its site and AMFI's (deadlines in SEBI's mutual-fund master circular, verify current). Kite's MF API lists funds, not underlying holdings | adapter per AMC family (about 45), spreadsheets parsed to ISIN, manual inbox fallback | monthly | overlap and concentration across funds, look-through equity/debt/gold split for the Investor plan, style drift | holdings sum to about 100% of NAV (±2%); ISINs resolve to our security master; every scheme with AUM above a floor has a recent file |
| 1.3 | Fund **AUM, exit load, riskometer, benchmark, manager** | AMFI monthly AUM; scheme documents and AMC factsheets (verify) | file parse | monthly | size and flow filters, risk labels | cross-checked against AMFI category totals |
| 1.4 | **Benchmark total-return indices** (Nifty 50 TRI etc.) | NSE index data (verify licence for use beyond personal research) | download | daily | fair fund-vs-benchmark comparison | tracks the price index plus dividends within a stated tolerance |
| 1.5 | **Fundamentals** (income statement, balance sheet, cash flow, shareholding) | Results filed by listed companies with NSE and BSE in XBRL (NSE moved to an "Integrated Filing - Financials" page for periods from March 2025, verify access method). Not scraped third-party sites (their terms normally forbid it) | adapter reading the exchange filing, stored with **filing date as `known_at`** | quarterly, event-driven | value, quality, growth features, equity screens, fundamental risk flags | restated vs original kept separately; totals reconcile (assets = liabilities + equity); coverage report by market cap |
| 1.6 | **Corporate announcements and results calendar** | NSE and BSE announcement feeds (official) | adapter, raw landing | intraday | event alerts, earnings-date features, news layer base | de-duplicated; every row has exchange timestamp and `known_at` |
| 1.7 | **News headlines** | Start from the exchange announcements (1.6). A licensed vendor or RSS source is a separate, paid, owner-approved decision (terms of use decide what we may store and show) | adapter per approved source | continuous | sentiment features, event study | licence reviewed; duplicates and syndication collapsed; timestamps are publication time |
| 1.8 | **Institutional flows, delivery %, bulk/block deals, India VIX, FII derivatives stats** | NSE and SEBI/NSDL public data (code and tables already exist; tables are empty) | existing adapters, run and validate | daily | flow and regime features | gap report; spot checks against the source |
| 1.9 | **Macro** (policy rate, yields, inflation, USD/INR, crude) | RBI and government releases; a free series service (the earlier FRED key was exposed and must stay rotated) | adapter | daily to monthly | regime model, real-return view for the Investor plan | release-date handling (what was known when) |
| 1.10 | **Index membership history** | NSE index reconstitution circulars (verify) | parse circulars | event | removes survivorship bias in equity research | rebuilt membership matches known constituents on spot dates |
| 1.11 | **Futures extras**: lot-size history, derived implied volatility | lot sizes from circulars and instrument snapshots; IV derived from archive prices with a stated model | derive | daily | F&O models, defined-risk strategy checks | put-call parity and no-arbitrage checks; model labelled "derived" |
| 1.12 | **MCX commodities** | Kite (segment already activated) | later, per your ordering | daily | commodity research | last, after the rest |

Data you will notice is absent from this list: anything that requires scraping a site against its terms, or buying a feed. Those are listed as decisions for you, not assumed.

**Phase 1 exit:** a data-inventory report with, for each dataset, coverage, gaps, `known_at` rule, validation result and the terms-of-use note; every gate in the table passed or the dataset marked "not available" honestly.

## Phase 2: the data pipeline

1. **Raw-first landing** for every new source (`data/landing/<source>/yyyy/mm/dd/…`, hash and `.meta.json`), then parse; re-parse never needs re-download.
2. **Stream adapters** on the existing framework (adapter, sink, checkpoint), idempotent upserts, one migration per dataset, point-in-time columns (`known_at`, `valid_from`).
3. **Security master** extended: ISIN-first identity across NSE, BSE, AMFI, fund holdings and fundamentals; renames and mergers handled once.
4. **Validation layer** per dataset (the reports we already produce for NAV, F&O, BSE actions), a **data-quality page**, and alerts on staleness and failed gates (the alert engine exists).
5. **Scheduler**: the daily after-close routine grows steps in dependency order; intraday streaming plus the reconcile; Windows Task Scheduler registration; a machine that does not sleep during the session (the 2026-10-23 stream silently stopped when the computer slept).
6. **Backups and restore drill**, retention policy for raw files, disk budget.

**Phase 2 exit:** one command runs the whole daily pipeline; a day's data can be rebuilt from raw; failures page you before they reach a model.

## Phase 3: the ML and DL pipeline (all three asset classes, one harness)

Shared: point-in-time feature store (no look-ahead, tests that fail on leakage), label factory (barrier labels, forward returns, rank targets), purged and combinatorial CV with embargo in sessions, true trial counting for deflated Sharpe, probability of backtest overfitting, cost stress, baselines on the identical paths, calibration, drift monitors, a registry with dataset and feature hashes, and the written go-live criteria (DSR at least 0.95 with the real trial count, PBO below 0.3, lower confidence bound above zero from at least 60 resolved paper ideas, beats baselines net of costs).

| Asset class | Targets (what a model predicts or ranks) | Classical ML candidates | Deep-learning candidates | Honest limits |
|---|---|---|---|---|
| **Equity, cash** | 5/10/20-session sector-neutral rank; probability a barrier trade hits target before stop; regime state; realised-volatility forecast | gradient-boosted rankers, meta-labeller on rule signals, HMM regime, HAR/GARCH volatility | TCN/GRU sequence ranker, temporal fusion transformer with quantiles, tabular transformer | ranker already rejected once; about 4,000 symbols but few independent periods; DL must beat the tree baseline net of costs |
| **Futures and options** | volatility risk premium (derived IV minus forecast realised), term-structure and basis signals, open-interest flow states, defined-risk structure payoff distribution | volatility forecasters, gradient-boosted classifiers on OI and basis features | LSTM-HAR volatility, sequence models on the chain surface | IV is derived, not published; **research and paper only; defined-risk structures only; no order placement** |
| **Mutual funds** | forward risk-adjusted net-of-cost return rank within like-for-like peers; probability a fund lags its benchmark; style and overlap embeddings; drawdown risk | regularised linear and boosted models on TER, size, age, holdings concentration, factor loadings from NAV regressions | holdings-based embeddings, sequence models on NAV (candidate only) | fund performance persistence is weak in the literature: the baseline "lowest cost in the peer group" must be beaten; peers with under 8 funds are not scored |
| **Cross-asset (Investor plan)** | regime and risk-capacity inputs, not return forecasts | volatility and correlation estimators, shrinkage covariance | none until evidence | Investor mode does not depend on return forecasts by design |

**Phase 3 exit:** every candidate has a model card, CPCV/PBO report, cost table, baseline comparison and a verdict (RESEARCH, PAPER, REJECTED). Outputs are probabilities or ranks with intervals, never a bare direction.

## Phase 4: product surfaces

Investor: fund selection with cost, look-through holdings and overlap; plan unchanged in principle (range, never a point forecast). Trader: risk engine first (sizing, open-risk cap, daily and weekly limits, losing-streak rule, research-only idea cards), then the futures desk (defined risk only), then live compare for trader ideas. Alerts per mode. Every report carries charts and animations (standing rule).

## Phase 5: clients, KYC, terms and privacy

`docs/design/KYC_ONBOARDING.md`, `docs/legal/TERMS_OF_USE_DRAFT.md`, `docs/legal/PRIVACY_POLICY_DRAFT.md` (drafts). **Hard gate:** no outside person is onboarded, and no personalised recommendation is given to anyone but you, until counsel confirms the regulatory route (register as an investment adviser or research analyst, partner with a registered entity, or stay a personal-use tool). Building the screens does not cross the gate; opening the door does.

## Phase 6: hardening and launch gates

Security review and penetration test, secrets handling and rotation, audit-log completeness, backup restore, load and chaos tests on the streaming and alert paths, accessibility pass, incident runbook, data-protection impact assessment, SEBI cyber-resilience checklist if applicable.

## Order and dependencies

```
Phase 1 data (gated per dataset) -> Phase 2 pipeline -> Phase 3 models -> Phase 4 surfaces -> Phase 5 clients -> Phase 6 launch gates
        Trader risk engine (Phase 4) can start in parallel: it needs no new data
        Phase 5 drafts exist now; Phase 5 build waits for the legal route decision
```

## Starting now (Phase 1, in this order)

1. 1.8 run the existing adapters for flows, delivery, bulk deals, VIX (no new source): fastest value.
2. 1.1 expense ratios: verify the AMFI and AMC listings, land raw files, parse, validate.
3. 1.2 fund holdings for the 20 largest fund houses first, then the rest.
4. 1.5 and 1.6 exchange results and announcements, after reading each exchange's terms.
5. 1.3, 1.4, 1.9, 1.10, 1.11, then 1.7 (news vendor decision), then 1.12.

## Decisions needed from you

1. Approve the sources in Phase 1 (or strike any).
2. A paid news or fundamentals vendor: yes or no (default: no, official filings only).
3. The legal route for Phase 5 (and who the counsel is).
4. Whether the daily pipeline runs on this PC (must not sleep) or on a small always-on machine or cloud VM.
