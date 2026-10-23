# Trader mode and Investor mode: design

Status: DRAFT for owner review (2026-10-23). Nothing here is built yet except the shared foundation. Visual companion: `docs/design/trader_investor_modes.html` (interactive; open it first).

## 1. Why two modes

A trader and an investor ask different questions, hold for different periods, measure success differently and are hurt by different mistakes. One screen cannot serve both honestly. So the product has a **mode**
(a user setting, switchable in the header, remembered per user). The data foundation, the evidence labels and the honesty rules are shared; the objective, horizon, risk metric, sizing, outputs, alerts and language are not.

| | Trader | Investor |
|---|---|---|
| Question | "Is there a setup worth the risk now, and exactly where am I wrong?" | "Is my money arranged to reach my goals, and what (if anything) should change?" |
| Horizon | hours to about 20 sessions (F&O to expiry) | 3 to 30 years, reviewed quarterly |
| Objective | positive expectancy after all costs with a survivable loss limit | reach the goal with a tolerable worst fall, low cost, little tinkering |
| Core number | risk per trade (R), open risk, expectancy in R, drawdown | allocation vs target, chance of reaching the goal (a range), worst fall, years to recover |
| Sizing | shares = risk budget / stop distance, capped by liquidity, correlation, open risk | weights from the risk profile, SIP needed, rebalance on band breach |
| Output ("what / where / how much / till when") | instrument, entry rule, stop, target, size, time stop | asset mix, gaps vs target, amount to add or trim, vehicle options, next review date |
| Evidence shown | Live compare (hit rate, expectancy, calibration, drift); paper first | historical behaviour of the plan (labelled), peer context, data-quality badges |
| Never | place orders; promise direction; size beyond caps; encourage averaging down | time the market; predict returns; give tax advice; push products |

## 2. Shared foundation (exists)

Raw-first data (NSE, BSE raw, AMFI, F&O archive, Kite instruments), corporate-action governance, immutable prediction ledger and barrier outcomes, risk screen (defensive signal) and decision-card sizing, portfolio import,
funds snapshot with data-quality badges, alerts, evidence labels (RESEARCH / PAPER / APPROVED), the auth and audit layer.
New shared pieces: `user_profile` (mode, capital partition, risk profile), mode-aware navigation, mode-aware alert rules and language, and a visual reporting standard (section 8).

**Capital partition.** The owner's money is split into two labelled pots: *investment capital* (Investor mode) and *trading capital* (Trader mode). A holdings import is tagged with one of them. A position never silently
moves between them. This is the single most important behavioural guardrail: it stops a trade that went wrong from becoming an "investment".

## 3. Trader mode

**User stories:** see today's candidate setups with a complete plan; know exactly how many shares and the rupee loss if stopped; know the daily and weekly loss limits and how much of them is used; see whether the system's
own ideas are actually working (hit rate, expectancy, slippage); get alerts at the stop, the target and the time stop; review my trades in a journal.

**Engines (honest status).** Rule setups (momentum / breakout / mean-reversion baselines, turtle-style breakouts, F&O defined-risk structures later) filtered by the defensive risk screen and the regime. The ML directional ranker
was REJECTED (no net edge after costs), so **no engine is currently approved**: every idea is labelled RESEARCH or PAPER and is logged to the ledger and resolved by the same barrier rules and cost model. An engine moves to
APPROVED only on the written criteria (C4): deflated Sharpe at least 0.95 with the true trial count, PBO below 0.3, lower confidence bound of net expectancy above zero from at least 60 resolved paper ideas, beats the baselines
net of costs, drift not flagged, calibration in tolerance, and the owner's written decision.

**Risk rules (defaults, editable by the owner, enforced by code):**

| Rule | Default |
|---|---|
| Risk per trade | 0.5% of trading capital (allowed 0.25% to 1%) |
| Stop | 2.5 typical daily moves (the horizon's registered barrier), gap-aware |
| Open risk (sum of risk on all open trades) | at most 4% |
| Daily loss limit | 1.5%: no new ideas once reached |
| Weekly loss limit | 4%: sizes halved for the next week |
| Losing streak | after 3 stops in a row, half size for the next 5 trades |
| Positions | at most 8 open; pairwise return correlation of 0.55 or less; at most 1% of a day's traded value |
| Time stop | the horizon's session count (5 or 20) |
| Averaging down | not offered |
| F&O (later phase) | defined-risk structures only; maximum loss of a structure within the risk budget; no naked short options |

**Idea card fields:** instrument and venue, setup name, direction, entry rule and trigger, stop (price, % and in daily moves), target(s) with R multiples, time stop and "till when", shares, rupee position, rupee risk and
% of trading capital, expected cost, regime, risk bucket, why now (3 reasons), what invalidates it, evidence label and sample size, data as-of.

**Pages:** Trader desk (today's plan, risk meters: open risk, daily and weekly loss used), Idea book (all ideas with status), Live compare (exists), Journal (entry reason, plan, result, slippage vs plan, notes), Performance (expectancy
in R, hit rate, MAE / MFE, equity in R, drawdown, calibration), F&O desk (chain, OI, derived IV, structures) once the Kite session and archive are enabled.

**Alerts:** stop or target within 20% of distance, time stop tomorrow, daily or weekly limit near / hit, open risk above cap, data stale in market hours, regime flip, kill-switch, session expiry.

## 4. Investor mode

**User stories:** state goals and risk comfort once; see the target mix and how far I am from it; see the range of outcomes for each goal and what the worst fall would have looked like; know what to add, trim or leave
alone, and when to look again; compare funds fairly; be told when nothing needs doing.

**Onboarding:** goals (name, amount, date), starting amount, monthly SIP, income stability, dependents, emergency fund in months, and a drawdown question ("after a 30% fall in a year I would: add / hold / sell some / sell all").
Risk profile = the lower of *capacity* (horizon, stability, dependents) and *tolerance* (questionnaire).

**Target mixes (provisional; the owner confirms):**

| Profile | Equity | Gold | Debt |
|---|---|---|---|
| Conservative | 15 to 30 (target 25) | 5 to 10 | 60 to 80 |
| Moderately conservative | 30 to 45 (40) | 5 to 10 | 45 to 65 |
| Balanced | 45 to 60 (55) | 5 to 15 | 25 to 45 |
| Growth | 60 to 75 (70) | 5 to 15 | 10 to 30 |
| Aggressive | 75 to 90 (82) | 0 to 10 | 5 to 15 |

International equity can be carved out of the equity band. **Tolerance check:** the mix's worst historical fall (monthly data since 2016: equity about -36% in 2020, a 60/10/30 mix about -16%) must not exceed the fall the
person said they could hold through; equity is lowered until it does. **Glide path:** equity falls 2 points a year over the last 5 years before a goal date (sequence risk).

**Rebalancing:** act only when a band is breached (5 points absolute or 20% relative) or a goal is off track; prefer directing new SIP money to the under-weight asset before selling; show trade size and the cost.

**Goal range:** block bootstrap (6-month blocks, 1,500 paths) of real monthly returns of the asset-class proxies, with the monthly SIP and the chosen mix; report the 10th / 50th / 90th percentile and the share of paths that
reach the goal, always as a range with the sample length and the statement that it is a scenario, not a forecast. Stress panel: the worst historical 12 months and drawdown of the mix, years to recover.

**Funds:** the funds page already labels the return basis, shows risk, peer context (like-for-like, 8+ peers) and the data-quality badge; Investor mode excludes `poor` data-quality schemes from suggestions and says when a
choice rests on thin history. Expense ratio, holdings overlap and tax lots are NOT loaded: the plan flags these as unknowns instead of guessing.

**Stocks held by an investor:** shown as a sleeve with risk screen, concentration and drawdown; never framed as return bets.

**Pages:** Plan (goals, target vs current, fan chart), Allocation and rebalance, Funds (exists), Holdings health (portfolio cockpit, exists), Stress, Reviews (quarterly summary and next review date).

**Alerts:** drift past a band, concentration breach, goal off track, review due, fund data quality turned poor, large fall (informational: a pre-agreed message, not a trade prompt).

## 5. How "the model works and recommends accordingly"

| Need | Trader | Investor |
|---|---|---|
| Signal | rule setups plus risk screen; ML candidates only in the lab | none about returns; risk screen informs the equity sleeve |
| Sizing | fixed-fractional risk, capped | weights from profile, bands |
| Evidence of working | resolved ideas: hit rate, expectancy, calibration, drift (Live compare) | plan behaviour in history; drift vs target; goal tracking |
| Failure handling | kill switch, loss limits, stale-data stop | data-quality badges, "no action needed" is a valid answer |

Models from outside (the archived GitHub collection of LSTM / GRU / seq2seq / attention forecasters, RL trading agents, Monte Carlo and portfolio-optimisation notebooks, mostly single-stock demos without costs or out-of-sample
discipline): used as **candidates benchmarked in the model lab** under CPCV, costs and the shuffled-label null control, never as recommenders on their own. Their turtle and moving-average agents become baseline rules; their Monte Carlo
becomes the bootstrap above; mean-variance optimisation is not used (unstable): minimum-variance or risk-parity with shrinkage and bands is the allocation tool.

## 6. Data model and API (to build)

`user_profiles(user_id, mode, risk_profile, capacity_score, tolerance_score, trading_capital, investment_capital, limits jsonb)`; `goals(id, user_id, name, target_amount, target_date, monthly_sip, priority)`;
`portfolio_imports.pot` (investment | trading). API: `GET/PUT /me/profile`, `GET /trader/desk`, `GET /trader/risk`, `POST /trader/journal`, `GET /investor/plan`, `GET /investor/projection`, `GET /investor/rebalance`.
Mode is also a lens on existing pages (the alerts and the portfolio cockpit filter by pot).

## 7. Build plan and acceptance tests

1. Mode switch, profile and capital partition (tests: mode persists per user; pots never mix; viewers read-only).
2. Investor engine: profile to target mix; tolerance check; rebalance bands; bootstrap projection (tests: reproduces the bands; drift reduced without breaching turnover limit; projection reproducible from a seed and states sample length).
3. Trader risk engine: sizing never exceeds the risk budget (property test), open-risk cap and loss limits halt new ideas, streak rule, idea card completeness, ledger logging and barrier resolution.
4. Pages and alerts per mode, each with the visual explainer standard.
5. F&O desk after the Kite session is used and the archive validated.
6. Lab: benchmark external model families under the harness; report with visuals.

## 8. Visual reporting standard

Every report has a standalone HTML version (inline SVG, no external dependency, works offline) with the key chart first, a plain-language caption under each chart, animation where it explains a process (price before and
after an adjustment, a fan of outcomes, a drawdown), and honest labels (sample size, evidence label, "scenario not forecast"). Markdown versions stay for the repository. First examples: the Bucket A decision tool and the
mode explainer.

## 9. Decisions for the owner

1. Capital partition: how much is investment capital and how much is trading capital? (Sets the defaults for both modes.)
2. Investor target mixes above and the 5-point / 20% rebalance band: accept, or change.
3. Trader limits above (0.5% per trade, 1.5% daily, 4% weekly, 4% open risk, 8 positions): accept, or change.
4. F&O in Trader mode: defined-risk structures only (no naked short options) until an engine passes the gate: accept?
5. Goals: the first goal(s) to model (amount, date, monthly SIP) so Investor mode can be tested on real numbers.
6. Whether the same person uses both modes (assumed yes, separated by the capital partition) or the product should also support separate accounts later.
