# Spec v2.0 ("design stoxy.md", mock-stoxy.md and the AI Studio notes) reconciled with what is built

Date: 2026-10-25. Sources read: `design stoxy.md` (Master Design Specification v2.0), `mock-stoxy.md` (screen mocks), and nine AI Studio answers (architecture suites, timeline, onboarding mocks, market assessment). They are design input, not facts about this repository. This note records what was adopted, what was corrected, and what is still open.

## Adopted and built in this step
- **Law 1, existing book first.** Imported trading-pot positions now take part in every trader limit (open risk, position count, correlation, averaging down, daily and weekly P&L). A position with no stop is UNPROTECTED: it is counted at an emergency stop (the lower of the 5-session swing low and last close minus 2 x ATR14) and new ideas are refused until each such position has a recorded stop (`legacy_triage`, migration 058; `GET /api/trader/triage`, `PUT /api/trader/triage/{symbol}`).
- **Section A / Section B on the Trader desk.** Section A lists every open position (imported and paper ideas) with one command: `SET_STOP_NOW`, `EXIT_STOP_BREACHED`, `EXIT_TIME_STOP`, `RAISE_STOP_TO_COST`, `HOLD_AS_PLANNED`. Section B (new ideas) is sized only from the risk left after Section A.
- Alerts for an unprotected imported position (critical), stop and target proximity, time stops and limits (already built).

## Corrected before use
| Spec says | What is recorded here | Action |
|---|---|---|
| Migrations 054 to 060 | 054 to 058 are already applied (deals_raw, ops_heartbeat, review_status/ohlcv_versions, trader_ideas, legacy_triage) | Renumber the spec's tables to 059 onward when built |
| Risk per trade 0.5% default | Owner decision: 0.25% default, 0.5% ceiling with an admin note, 1% never | Keep the recorded policy |
| "Run the Bucket A rebuild" as step 1 | Done and validated on 2026-10-25 | None |
| Mock numbers: PAPER_VALIDATED N=84, M3 win probability 64.8%, DSR 0.96, +0.34R expectancy, "past memory +6.4%" | No engine is approved and no model has resolved live predictions yet (978 logged, none resolved until after Monday). The mocks are illustrations | These values must never be shown as real. The product shows RESEARCH or PAPER and a resolved count of 0 until the ledger says otherwise |
| Models M1 to M4 with specific names and verdicts | Only candidates exist; verdicts come from the harness, not from a plan | Build under the existing harness; no numbers in advance |
| Live order placement ("Execute Plan", consent-gated basket orders) in one AI Studio answer | Guardrail: no order path exists in the product | Not adopted |
| "You are a SEBI RIA and broker" (one AI Studio answer) | Confirmed by the owner in chat on 2026-10-25 (no registration numbers given, none recorded). The build scope does not change: personal-use v1 first; client modules stay frozen until the personal proof gates pass and the owner confirms the route with their compliance officer or counsel | Legal drafts keep their placeholders until the owner supplies registration details. Open for the compliance officer: advisory vs broking segregation, any personal-trading disclosure rules, exchange data redistribution licences before outside users see NSE/BSE-derived data, SEBI rules on AI/ML tools used by intermediaries |
| Endpoint URLs (NSE API paths, participant OI file names, AMFI TER) and hardware/software versions | Unverified; exchange sites change and have terms | Verify each against the live site and its terms when its adapter is built (raw-first, with a manual-download fallback) |

## Not built yet, in the order the owner's priorities and the spec agree
1. Official NSE/BSE holiday and F&O expiry calendar (`exchange_holidays`, `fno_expiry_calendar`) so every time stop and expiry countdown is session-exact (today the desk skips weekends only).
2. Buy dates for FIFO tax lots: broker tradebook or CAS import (`portfolio_tax_lots`), then the STCG/LTCG hold-lock and Regular-to-Direct switch planner. Needs a source of purchase dates; the current import has only quantity and average cost.
3. Universal 360-degree stock X-Ray page (price chart with levels, corporate-action markers, delivery panel, position-context banner).
4. Fund expense ratios and top-AMC holdings (look-through overlap).
5. Point-in-time fundamentals, participant open interest, announcements and event memory (pgvector), each after a terms review.
6. Debian mini-PC migration; local Gemma assistant; F&O defined-risk structures (research only).
7. Models: only after the above inputs exist and under the existing harness.

## Update (same day): exchange calendar built; regulatory notes filed for the compliance officer
- **Built:** `exchange_holidays` (migration 059) filled from NSE's holiday master, raw-first (`nse_holidays` adapter, daily step, sha256 and `known_at` kept) and a derived `fno_expiry_calendar` view (the expiries that really exist in the archive, so holiday-shifted weekly and monthly expiries are exact). `qsde/exchange_calendar.py` counts regular sessions, treats the starred special-timing day (Muhurat, 8 Nov 2026) as not a regular session, and knows its own horizon: the list covers one calendar year, so past 31 December it answers with weekdays AND says `estimated`. Decision cards and trader time stops now use it; `stoxsy doctor` and a global alert warn 45 days before the horizon and fail after it. Cross-check: the 2026 list agrees with the price bars exactly (no holiday has a bar; no weekday without a bar is missing from the list).
- **Not wired on purpose:** the prediction ledger's resolution window. It resolves from actual bars, and changing what gets logged would alter hashes of an append-only record.
- **Restricted list (migration 060, `/api/trader/restricted`):** an empty, owner-controlled list. A listed symbol is not proposed or logged by the trader desk until released or its end date passes; every change is audited. Nothing is preset: which securities and until when is for the owner and the compliance officer.
- **Regulatory notes from an AI Studio answer (NOT verified here):** it asserts rules on advisory/broking segregation, trading contrary to client advice and a research-report blackout window, exchange data redistribution, and mandatory reporting of AI/ML tools by intermediaries, with citations. None of those citations or dates was checked against SEBI or exchange text, and some dates in it are in the future relative to what could be known. Treat all of it as questions for the compliance officer, not as findings. The four questions stay open.
