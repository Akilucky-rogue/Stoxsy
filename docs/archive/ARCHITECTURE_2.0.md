> **Archived 2026-10-31, kept for history.** This describes an earlier build. The current description of the system is `docs/design/SYSTEM_FLOW.md`; the current plan is `docs/design/PLATFORM_ROADMAP.md`. Do not follow the instructions below without checking them against those.

# Stoxsy 2.0 — Architecture & Rebuild Plan

> Living document. Source of truth for the refactor. Built from the audit (this is the
> system the audit said we *should* have, vs the 4-codebases-in-one we *do* have).

## 0. Locked decisions

| Decision | Choice |
|---|---|
| Goal | Earn from Indian markets across horizons, hands-off, with honest measured edge |
| Capital | ₹15 lakh+ |
| Instruments | Equity + F&O (defined-risk only; cautious) |
| Execution | Semi-auto — engine builds order, **human one-click confirm → Kite** |
| Operation | **Mostly hands-off** → autonomous always-on engine + phone push alerts |
| Validation | **Paper-first** — no engine gets real capital until its live-paper Scorecard clears a net-of-cost bar |
| Alpha clock | Real-time = timing/monitoring/risk; **not** the alpha source (Kite is not HFT) |
| Engines (priority) | F&O income (flagship) → momentum rotation → factor/quality → intraday assist |
| LLM | **Gemma 3 12B (4-bit) via Ollama**, local/private, batch (8–12GB VRAM) |
| Alerts | **ntfy** (action buttons for one-click confirm) over Tailscale; Pushover/FCM later |
| News | NSE/BSE filings (primary) + Indian RSS + GDELT → local Gemma sentiment/thesis |
| UI | **TUI console now** (extend `live_watch`) → **Tauri desktop research app later** (reuses existing React charts) |

## 1. What this is

An **autonomous, always-on, multi-engine quant book + deep-research brain** for Indian markets.
It runs strategies in paper, measures itself honestly, alerts you when a real edge fires, and
places **defined-risk** orders on one-click confirm. Real-time is the nervous system (live charts,
timing, risk, P&L) — the alpha lives on slower, paper-validated clocks.

## 2. The honesty doctrine — accuracy is MEASURED, never promised

"Accuracy" is three different things; only one can be ~90%:

| Measure | Honest target | Note |
|---|---|---|
| Chart/indicator fidelity vs market | **99.9%** | Correctness, not prediction. Reconciled vs Kite's official values. |
| Directional prediction | **52–58%** | 55% is elite. >60% sustained = suspect leak. 90% = the lie we removed today. |
| Options-income win-rate | 70–85% but a **trap** | High win-rate, asymmetric payoff. We measure **expectancy + tail**, not win-rate. |

**Profit comes from expectancy, not accuracy.** At 2:1 R:R, breakeven hit-rate is ~33%.
We never chase 90% directional — that path guarantees self-deception.

### The Scorecard (Reality Engine) — a first-class pillar
- **Fidelity:** computed OHLC/VWAP/indicators reconciled vs Kite official → must be ~100%.
- **Predicted → actual:** every TradeIdea logged with prediction, matched to realized outcome (target/stop/time). Honest hit-rate + expectancy per engine.
- **Calibration:** stated confidence vs realized frequency.
- **Backtest-vs-live drift:** divergence = overfitting alarm.
- **Promotion rule:** real capital only after the live-paper Scorecard clears a realistic **net-of-cost** bar. Same gate we just enforced on the ML model, applied to every engine.

## 3. Keep / Rebuild / Drop / Build

| Verdict | What |
|---|---|
| ✅ KEEP | Kite ingestion (ticks + REST hist, IST-correct), TimescaleDB + PIT factor store (leak-free read), factor/microstructure math, purged-CV + **net-cost DSR gate**, paper journal, liquidity/cost models |
| ♻️ REBUILD | Serving layer → **one engine, one `TradeIdea` schema, one level/risk engine**. Collapse `signal_generator` + `analyze.py` (893 lines) + `rule_signal_writer` + `live/engine` + `live_watch` into modular engines. |
| 🗑️ DROP | Website-as-primary, the gross-DSR intraday mirage, `QSDE_FORCE_PROMOTE` bypass, the 4–7 duplicated trade-level copies |
| 🆕 BUILD | **F&O module** (chain/Greeks/defined-risk structures/vol-regime/margin), **Portfolio Risk Engine**, **Research Brain** (Gemma), **Scorecard**, **Alerter** (ntfy confirm flow), **News pipeline** (filings/RSS/GDELT) |

## 4. Target architecture

```
        ┌────────────  STOXSY ENGINE  (headless, always-on, 09:00–15:30 IST)  ────────────┐
DATA (keep)               STRATEGY ENGINES (modular, paper-first)
Kite ticks ─┐             ├─ Factor/Quality     (months)   ─┐
Kite hist  ─┤─► Timescale ├─ Momentum Rotation  (1wk–1mo)   ├─►  ONE TradeIdea  ─► RISK ENGINE
yf/Bhavcopy─┤   + PIT     ├─ F&O Income         (def-risk)  ─┤    (normalized)      (caps, margin-at-
filings/RSS─┤  factor_pit └─ Intraday assist    (timing)    ─┘                       risk, DEFINED-RISK
GDELT ──────┘   + options                                                            gate, kill-switch,
                  chain                                                               vol-regime)
                │                                                                        │
   RESEARCH BRAIN (Gemma 12B): filings+news+fundamentals → thesis per name               ▼
                │                                                                  SEMI-AUTO TICKETS
                ▼                                                                  build → alert → YOU
   THE SCORECARD ◄── logs every idea + outcome ── reconciles vs Kite ──► drift/calibration/expectancy
                │                                                                        │
        ┌───────┴──────────┬─────────────────────┬───────────────────────────┬──────────┘
        ▼                  ▼                     ▼                           ▼
   PAPER JOURNAL      ALERTER (ntfy)        TUI CONSOLE              TAURI RESEARCH APP (later)
   per-engine         push + confirm        book pulse / risk        live charts + indicators +
   live validation    buttons (Tailscale)   (extend live_watch)      F&O payoff/Greeks + thesis
```

**Pillars:** ① one signal schema + one level engine · ② Risk Engine (defined-risk, margin-at-risk, net kill-switch, vol-regime) · ③ F&O module · ④ Research Brain (Gemma) · ⑤ **Scorecard** · ⑥ Alerter-as-interface · ⑦ thin swappable UI.

## 5. The `TradeIdea` contract (one schema every engine emits)

```
TradeIdea:
  id, engine, symbol/underlying, horizon            # who/what/when
  structure: CASH_LONG | CASH_SHORT | FUT | OPT_SPREAD | IRON_CONDOR | ...
  legs: [ {instrument, side, qty, strike?, expiry?, opt_type?} ]
  entry, stop, target            (cash)   OR   net_credit, max_loss, max_profit, breakevens (F&O)
  max_loss_rupees                # REQUIRED — Risk Engine rejects if null/uncapped (defined-risk)
  horizon_clock, valid_until     # when this idea expires
  confidence, expected_value     # calibrated, net-of-cost
  thesis_ref                     # link to Research Brain note
  scorecard_id                   # so the outcome can be matched back
```

## 6. Module layout (new)

```
stoxsy/backend/qsde/
  ingestion/     (keep)  kite ticks/hist, intraday_storage, options_chain (new), news (new)
  factors/       (keep)  technical, smc, intraday_microstructure, fundamental, flow, macro
  engines/       (NEW — replaces scattered signal code)
     base.py            # Engine ABC -> emits TradeIdea
     factor_quality.py
     momentum_rotation.py
     fno_income.py       # defined-risk options structures
     intraday_assist.py
  levels/        (NEW)   one engine: cash levels + option-structure builder (no duplication)
  risk/          (rebuild) portfolio.py (margin-at-risk, caps), defined_risk.py, kill_switch.py, vol_regime.py
  research/       (rebuild) brain.py (Gemma/Ollama), thesis.py ; keep dcf/comps/sector
  scorecard/     (NEW)   tracker.py (idea->outcome), fidelity.py (vs Kite), calibration.py, drift.py
  execution/     (keep+)  order_tickets (defined-risk aware), kite place
  alerting/      (NEW)   ntfy.py (push + confirm actions), formatting
  paper/         (keep)   journal per-engine
api/             thin REST/WS over the engine (authenticated)
tui/             (NEW)    console (extend live_watch)
desktop/         (later)  Tauri app reusing frontend/ React charts
```

## 7. Phased roadmap (dependency-honest)

- **Phase 0 — Consolidation.** One `TradeIdea` schema, one level engine, the Engine ABC, the Scorecard skeleton. Rip out the 4–7 duplicates + `analyze.py`. *Foundation; nothing safe until this exists.* (spec below)
- **Phase 1 — Cheap real edges live in paper.** Momentum rotation + factor/quality engines on the now-honest factor pipeline. Alerter (ntfy) + Scorecard online. A real paper book running in days.
- **Phase 2 — Risk Engine + F&O income (flagship).** Options chain/Greeks ingest, **defined-risk structure builder**, vol-regime filter, margin-at-risk, adjustment/exit rules. Paper-first; guardrails before a rupee.
- **Phase 3 — Research Brain.** Gemma thesis from filings+RSS+GDELT. News pipeline. Deep research per name.
- **Phase 4 — Intraday assist + Tauri research app.** Live charting (point 4), indicators overlay, F&O payoff/Greeks, thesis — reusing the React charts.

## 8. Guardrails (non-negotiable)

- **F&O = defined-risk only.** Risk Engine rejects any structure without a capped `max_loss_rupees`. No naked shorts while hands-off.
- **Paper-first per engine.** No real capital until the live-paper Scorecard clears a net-of-cost bar.
- **Hands-off ⇒ bulletproof risk.** Net-aware daily-loss kill-switch, per-engine capital budget, vol-regime circuit breaker, confirm-or-it-doesn't-fire (SEBI-safe).
- **Security.** Confirm endpoint authenticated, reachable only over Tailscale/WireGuard — never raw on the internet. (Closes the "API has no auth" audit finding.)
- **No 90% theater.** Targets are honest (§2). The Scorecard reports the true edge, whatever it is.

## 9. Data — what we have, and the one real gap

- **Have (enough):** Kite live ticks + REST historical (equity/index, daily + intraday window), yfinance/Bhavcopy daily depth, NSE/BSE filings, RSS, GDELT.
- **Gap to flag honestly:** **historical options chain / IV data is thin** (Kite's historical F&O depth is limited). For F&O-income *backtesting* we either (a) accumulate the chain forward from now, (b) source a historical options dataset, or (c) validate F&O income primarily via forward paper-trading. → decision needed in Phase 2.

---

## Phase 0 spec — Consolidation (the first build)

**Goal:** kill duplication, establish the one contract everything speaks, stand up the Scorecard skeleton. No new alpha — pure foundation.

1. **`TradeIdea` schema** (§5) as a dataclass + DB table; every signal path must produce it.
2. **`engines/base.py`** — `Engine` ABC: `scan() -> list[TradeIdea]`, `name`, `horizon`, `clock`.
3. **`levels/`** — ONE level/structure engine. Migrate the centralized (percent-unambiguous, CORR-1-fixed) `compute_trade_levels`; add the option-structure builder stub. Delete the 4–7 copies (`paper_journal._atr_levels`, `analyze.py` levels, etc.) → all call this.
4. **Collapse serving paths:** route `signal_generator`, `rule_signal_writer`, `live/engine`, `live_watch` through the Engine ABC + `TradeIdea`. Retire `analyze.py` (893 lines) — its useful bits (research fetch, charts data) move to `research/` + the API.
5. **`scorecard/` skeleton:** `tracker` (idea→outcome), `fidelity` (computed vs Kite reconcile). Wire the existing paper journal into it.
6. **Tests + green suite** at each step; nothing merges red.

**Definition of done:** one `TradeIdea` flows from one engine, through the one level engine, into the paper journal + Scorecard, with the duplicated paths deleted and the suite green.

---

## Audit adjustments (v1.1 — from the pre-Phase-0 audit)

Verdict: **GO with staged fixes.** No fatal flaw. Adjustments now baked in:

- **Depth before breadth.** The equity ML engines net ~0 (may never clear paper). The earner thesis rests on **F&O income** — prove it end-to-end on **forward paper** before building momentum/factor/intraday. Don't fund what doesn't clear.
- **F&O = INDEX options only** (NIFTY/BANKNIFTY: European, cash-settled, liquid) → eliminates assignment/early-exercise risk. No single-stock option income while hands-off.
- **Risk Engine caps AGGREGATE, not just per-trade.** Bound net portfolio short-vega / worst-case correlated loss across all positions — defined-risk per leg does NOT bound a vol-spike hitting 10 positions at once. (P0 before F&O capital.)
- **Hands-off token/crash safety (P0 before F&O capital):** Kite token expires ~06:00 daily; an open position with a lapsed token is unmanageable → token-health monitor + alert + "can't-manage ⇒ refuse-overnight / flatten" rule. On restart, **reconstruct position state from Kite (broker = source of truth)**, never internal memory. Durable (DB) kill-switch, default-safe on restart.
- **`FORCE_PROMOTE` ⇒ candidate-only.** It must never auto-activate the live book; at most write a candidate + loud alert. (Phase 0, since we touch promotion.)
- **`TradeIdea` extended:** add `greeks` (δ/γ/θ/ν), `margin_required`, `expiry`, `adjustment_plan`. EOD-Bhavcopy F&O backtest is a **filter, not proof** (overstates edge: no intraday path / idealized fills) — paper is the proof.
- **Phase 0 = strangler pattern, NOT big-bang.** `analyze.py` (893 lines) serves the live `/analysis` flow; build the `TradeIdea`/Engine path alongside, migrate one engine at a time, delete old paths only after the new one serves green.
- **Triple-barrier honesty:** labels are `sign(return-to-first-touch)`, not pure {-1,0,+1} (the {0} class is ~empty) — keep, but name it accurately.
- **Compliance:** keep the human confirm REAL (never auto-confirm) to stay clear of algo-registration; log every suggested+confirmed order.

### Sizing ownership (decided during Phase 0 self-review)

There is exactly **ONE sizer**: `qsde/engines/sizing.py::size_cash` (cash/future) and the
`levels/options.py` builders (defined-risk option structures), both wrapping the canonical,
unit-tested `qsde/risk/budget_screener.size_position`. **No engine may reimplement sizing**
(a self-review caught `RuleMomentumEngine` doing exactly that — fixed).

- **Engines size-to-budget.** Each engine is configured with `(budget, risk_per_trade, lot_size)`
  and emits a `TradeIdea` already carrying `qty` + `capital_required` + `max_loss_rupees`, so
  every emitted idea is defined-risk by construction. Affordability AND per-trade risk both bind.
- **The Risk Engine gates at the PORTFOLIO level** — aggregate short-vega / correlated-loss caps,
  vol-regime breaker, dedup, daily-loss kill-switch. It does NOT re-size individual ideas; it
  accepts, scales-down, or rejects them.
- This keeps `TradeIdea.validate()` (which requires a finite `max_loss_rupees`) satisfiable at
  emit time and keeps the money-math in one place.

### Deletion order correction (from the increment-3 safety check)

A grep before deleting `rule_signal_writer` caught a wrong assumption in this roadmap:
`rule_signal_writer` is **UPSTREAM** of the engines — it fills the `signals` table that
`RuleMomentumEngine` *reads* — **not redundant with them.** Deleting it would make the engine
emit nothing. The legacy `signals` table is the shared backbone that the writer fills and *every*
legacy consumer (`/api/signals`, `analyze.py`, `paper_journal`) reads.

Corrected strangler order:
1. ✅ New path runs **beside** the legacy flow (engines → idea-log → Scorecard), now wired into
   `daily_eod` so the Scorecard accumulates each engine's real net-of-cost edge.
2. Let it run until the Scorecard **proves (or kills) the edge** on paper.
3. Only then migrate the `signals`-table **consumers** (`/api/signals`, `analyze.py`) onto the
   contract — and fix the `/api/signals` score-as-return bug while doing it.
4. **Last**, once nothing reads the legacy `signals` rows, retire the writers
   (`rule_signal_writer`, the legacy bits of `signal_generator`). Deletions come at the END, not
   the start — the new path must be the proven source of truth first.
