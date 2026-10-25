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
| "You are a SEBI RIA and broker" (one AI Studio answer) | Never stated in this project. The recorded route is personal use; legal route for outside users is undecided | Nothing built for clients. If this is true and changes the route, say so and the legal gate is revisited |
| Endpoint URLs (NSE API paths, participant OI file names, AMFI TER) and hardware/software versions | Unverified; exchange sites change and have terms | Verify each against the live site and its terms when its adapter is built (raw-first, with a manual-download fallback) |

## Not built yet, in the order the owner's priorities and the spec agree
1. Official NSE/BSE holiday and F&O expiry calendar (`exchange_holidays`, `fno_expiry_calendar`) so every time stop and expiry countdown is session-exact (today the desk skips weekends only).
2. Buy dates for FIFO tax lots: broker tradebook or CAS import (`portfolio_tax_lots`), then the STCG/LTCG hold-lock and Regular-to-Direct switch planner. Needs a source of purchase dates; the current import has only quantity and average cost.
3. Universal 360-degree stock X-Ray page (price chart with levels, corporate-action markers, delivery panel, position-context banner).
4. Fund expense ratios and top-AMC holdings (look-through overlap).
5. Point-in-time fundamentals, participant open interest, announcements and event memory (pgvector), each after a terms review.
6. Debian mini-PC migration; local Gemma assistant; F&O defined-risk structures (research only).
7. Models: only after the above inputs exist and under the existing harness.
