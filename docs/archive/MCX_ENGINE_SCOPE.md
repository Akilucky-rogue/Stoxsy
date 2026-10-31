> **Archived 2026-10-31. Deferred, not obsolete:** MCX commodities are roadmap item 1.12, to be done after equities, funds, F&O research and the product core are stable. The rest of the platform has moved on; see `docs/design/PLATFORM_ROADMAP.md`.

# MCX Commodity-Futures Engine — Scope

> A design scope, not a build. MCX is a **different asset class** from the equity
> pipeline (NSE/BSE). This documents the right approach, the data/infra it needs,
> honest edge expectations, and a phased plan — reusing the existing
> `Engine` / `TradeIdea` / Scorecard contract so it inherits the honesty machinery.

## 1. Why the equity pipeline does NOT apply

The whole NSE/BSE stack is **cross-sectional equity ranking**: score N stocks by
factors, long the top decile / short the bottom. MCX breaks every assumption:

| Assumption (equities) | MCX reality |
|---|---|
| Hundreds of names to rank | ~15 liquid underlyings — no cross-section to rank |
| Cash instruments, hold indefinitely | **Futures** — monthly contracts that **expire + must be rolled** |
| Company factors (RSI, ROE, momentum) | Drivers are **global macro**: USD, real rates, supply/demand, inventories, OPEC/weather |
| One price series per name | **Term structure** — a curve of contracts (near/far); contango vs backwardation matters |
| NSE hours 09:15–15:30 IST | MCX trades to **~23:30 IST** (evening session); different holiday calendar |

So the LightGBM cross-sectional ranker and the pairs engine are the **wrong tools**.
The right tool for ~15 commodity futures is the **time-series trend + carry** approach
(classic CTA / managed-futures) — the "Trend Following" family, whose natural home is
exactly commodities/futures.

## 2. The liquid MCX universe (~15 underlyings)

`GOLD`, `GOLDM` (mini), `SILVER`, `SILVERM`, `CRUDEOIL`, `NATURALGAS`, `COPPER`,
`ZINC`, `ALUMINIUM`, `LEAD`, `NICKEL`, `COTTON`, `MENTHAOIL`, `CPO` (crude palm oil).
Bullion + energy carry most of the liquidity; base metals next; agri thin.

## 3. Engine design — `McxTrendEngine` (on the existing contract)

Emits the SAME `TradeIdea` (uses `Structure.FUTURE_LONG / FUTURE_SHORT`, already in
the enum; `validate()` already requires entry+stop → defined-risk), so it plugs into
the idea-log + Scorecard with zero contract changes.

**Signal = trend, with a carry tilt (both time-series, per underlying):**
- **Trend** (primary): time-series momentum — sign of the 12-month return, and/or a
  Donchian breakout (N-day high/low), and/or MA crossover. Long uptrends, short
  downtrends. Low win-rate (~35–45%), high reward:risk (2–3:1) — a few big moves pay
  for many small stops. This is the documented commodity edge.
- **Carry** (overlay): term-structure slope = (near − far) / far. **Backwardation**
  (near > far) → positive roll yield → long bias; **contango** → short bias. Reinforce
  or veto the trend signal.

**Defined-risk sizing** (reuses the cash pattern): stop = entry − k·ATR (k from
vol/regime); `max_loss_rupees = |entry − stop| × lot_size × lots`; qty sized so the
stop-loss = the per-trade risk budget, capped by margin. Futures are leveraged, so
`margin_required` (SPAN+exposure from Kite) is tracked separately from max-loss.

**Resolution:** barrier-walk the continuous series forward vs stop/target/time — reuse
`resolver._resolve_barrier_daily` (same walker the cash + F&O resolvers use).

## 4. The hard part: continuous back-adjusted series (the roll)

A single "GOLD" price history doesn't exist — each contract expires. You must build a
**roll-adjusted continuous series** per underlying:
- Pick a roll rule (roll N days before expiry, or on volume/OI crossover to the next month).
- **Back-adjust** (panama-canal / ratio) so the splice at each roll doesn't create a
  fake gap that a trend model would misread as a signal.
- Track the realized **roll yield** (it IS part of the return, and the carry signal).

This is the one genuinely new piece of infra — everything else reuses existing patterns.

## 5. Data + infra needed

1. `client.refresh_instruments("MCX")` → MCX-FUT contracts into `kite_instruments`
   (currently NOT loaded — the universe audit flagged this). Needs the daily Kite token.
2. Kite historical for each contract month → assemble the **continuous back-adjusted
   series** + term-structure snapshots (near/far). New module `qsde/mcx/continuous.py`.
3. **MCX trading calendar** (hours to 23:30 IST, its own holidays) — the NSE
   session arithmetic does not apply.
4. Lot sizes + margin per contract (from the Kite instrument row).

## 6. Honest edge expectations

Commodity **trend-following is a real, documented edge** — CTAs have decades of
positive-but-volatile returns, Sharpe ~0.5–0.8, and (the real prize) **low correlation
to equities** → genuine diversification for a book that's otherwise NSE-equity +
index-vol. Caveats, stated plainly:
- Low hit-rate strategy — psychologically hard; many small losses, occasional big wins.
- The **roll/back-adjustment must be correct** or the backtest is garbage (a fake gap
  at a splice looks like alpha).
- Costs: MCX has no STT on non-agri, but brokerage + impact on the thinner metals/agri
  is real — the same net-of-cost DSR gate applies; don't expect intraday to survive.
- Capacity is fine at retail size; the constraint is discipline, not liquidity.
- Verdict up front: this is a **diversifier**, not a magic edge. The DSR gate + Scorecard
  will measure whether the trend edge is real on MCX after costs — same honesty as everywhere.

## 7. Phased plan (each phase = one shippable increment, contract-reused)

- **Phase 0 — Data.** `refresh_instruments("MCX")`; `qsde/mcx/continuous.py`
  (roll-adjusted series + term-structure); MCX calendar. Tests on synthetic contracts.
- **Phase 1 — Trend math (pure).** `qsde/mcx/trend.py` — ts-momentum + Donchian + ATR.
  Pure, unit-tested. Backtest one underlying (GOLD) end-to-end.
- **Phase 2 — Carry overlay.** term-structure slope → bias; combine with trend.
- **Phase 3 — Engine + resolution.** `McxTrendEngine(Engine)` → defined-risk
  FUTURE_LONG/SHORT `TradeIdea`s → idea-log → barrier resolver → Scorecard.
- **Phase 4 — Paper-validate** across the ~15 underlyings; read the measured net edge
  before any capital.

## 8. Effort + fit

Comparable in size to the F&O flagship (~4–5 increments). It does **not** touch the
equity pipeline and reuses `TradeIdea`/`Engine`/`Scorecard`/barrier-resolver, so the
honest-measurement guarantees come for free. The only new infra is the continuous-series
roll logic (Phase 0) — that's the make-or-break piece; everything downstream is
pattern-reuse. Blocked today only on: a Kite token to load MCX instruments + history.
