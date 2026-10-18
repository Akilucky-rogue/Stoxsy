# ADR 0001: Parquet feature store, horizon/label/cost registries, evaluation harness

Status: accepted (owner decision 2026-10-18). Supersedes the long-form `factor_pit` for `ffd_v2`.

## Decision

1. **Feature store.** Factors for `ffd_v2` live in partitioned Parquet under `D:\Stoxsy\data\features\`
   (git-ignored), one immutable *snapshot* per build, described by a `MANIFEST.json` inside the snapshot
   directory and registered in the `feature_snapshots` table (governance + `is_current`). TimescaleDB keeps raw
   prices, adjusted prices, corporate-action events and the point-in-time universe.
2. **Bitemporality without a bitemporal table.** Each row carries `known_at` (session close + 18h IST, the
   same knowability rule as the old `valid_from`). A training or serving read may pass `known_before=` so only rows
   knowable at that instant are returned. Corrections create a new snapshot/`factor_version`, never an overwrite.
3. **Reproducibility.** A run pins `factor_version`, `snapshot_id`, `universe_version` and `dataset_hash`.
4. **Horizons, labels, costs are registries**, not constants scattered across modules.
5. **One evaluation harness** (purged/CPCV, true trial count, cost stress, PBO) that grades what paper trading
   resolves, not a look-alike.

## Build order (each step shippable)

1. Store + manifest + reader; parity against the 46-symbol `factor_pit` pilot; measure read speed.
2. Switch training (`dataset.py`), serving (`signal_generator`), the engine write path and the other
   `factor_pit` readers to the reader **in the same change**; no two live stores.
3. Horizon / label / cost registries, then the harness (self-tests first, calibration against the old OOF files).
4. Corporate-actions source evaluation (offline inventory first). No full-universe generation before it.

## Traps designed out

| # | Trap | Design response |
|---|---|---|
| 1 | The pilot is survivor-selected (40 liquid-today + 20 from 2018; ~4 names per decile) | Every harness output on it is stamped `PLUMBING-ONLY / NOT EVIDENCE`; no model card gets a verdict from it |
| 2 | Labels measured from the close of day *t*, not the spec's next-open entry; label code differs from the Scorecard resolver | Label function and resolver share one implementation, plus a parity test on identical synthetic paths |
| 3 | Corporate-action look-ahead (split confirmed by the next 3 closes) and demergers left `UNEXPLAINED` creating fake stop hits | Exclude label windows and feature dates touching an unexplained event; serving flags a symbol with an unconfirmed large gap in the last 3 sessions; rule recorded in the manifest |
| 4 | `fillna(0)` hides 282-session warm-up NaNs (11.4% zero cells) | Reader returns NaN; trees use native NaN, DL gets explicit imputation + missing-indicator; rank transforms live in the versioned *view*, not the snapshot |
| 5 | A hash is meaningless if the writer is nondeterministic | Sorted rows, fixed column order, fixed zstd settings, no pandas metadata, hash over file bytes + input fingerprint (git SHA, params, checksums of `eod_raw`, `corp_action_events`, `universe_pit`); test that two builds hash identically; `is_current` only in the table |
| 6 | Daily partitions = ~2,300 tiny files per version | Measure first; monthly files + incremental snapshots unless a full-pilot read is slow |

## Harness correctness rules

* Purge by each sample's `label_end_date` in **sessions** (AUDIT M6); registry `purge_days` is a floor.
* Overlapping-sleeve daily returns (1/h of the book per entry day), not subsampled rebalances.
* Append-only trial ledger per (horizon, label, universe version, feature version); failed trials count.
* Self-tests before any model: planted alpha passes; noise rejected (DSR low, PBO ~ 0.5); future-return feature
  caught; shuffled labels give IC ~ 0.
* Calibration: re-score the old `oos_*.parquet` and reproduce recorded net DSR (0.000 / 0.075 / 0.42).
* No index/VIX history: baselines use an equal-weight `universe_pit` basket and a top-50-ADV proxy; regimes are
  sliced by universe trend/vol. The proxies are documented, not hidden.

## Status after implementation (2026-10-18)

Done and tested: Parquet store + manifest + registry; training/serving/ATR/factors-API readers switched in one change
(a test fails if any live module reads `factor_pit`); horizon/label/cost registries with derived legacy constants;
one shared barrier walker (labels, paper journal, Scorecard resolver); `tb_nextopen_v1` labels; CPCV with per-sample
purge and session embargo; overlapping-sleeve evaluation, cost stress, ledger-based DSR, PBO, bootstrap, baselines,
regime slices, leakage guard; model-card generator; self-tests on known-answer worlds; calibration against the old OOF
files; corporate-actions source evaluation (`docs/CORPORATE_ACTIONS_EVALUATION.md`).

### Behaviour changes the owner should know about
* Promotion cost: the gate now charges the Scorecard's paper cost (**30 bps** delivery, 8 intraday), not 10.
* Embargo: `long` (20-session label) embargo **5 -> 20** sessions.
* Gap-aware exits: paper trades and the Scorecard fill a stop/target at the **open** when a bar opens through it
  (more pessimistic than filling at the stop price).
* `fillna(0)` removed from training and serving: NaN is preserved (trees handle it; DL must impute explicitly).
* **Legacy ML promotion is paused by default** (`QSDE_ML_PROMOTION_ENABLED` now defaults to false and is set false in
  `.env`): see "Known divergences" below.

### Owner decisions of 2026-10-18 (implemented)
1. **`factor_pit` is deprecated.** Migration 026 comments the table; a test fails if any live module reads it. It is kept
   only as the parity reference for the 46-symbol pilot (8,195,321 ffd_v2 cells matched exactly). It is deleted only after
   the first full-universe snapshot passes validation, and the deletion is recorded here (row count, hash, factor
   version, date) before the drop. *Known live writer:* `api/routes/analyze.py` (the "pin symbol" flow) still writes
   raw factors to it through `pit_writer`; it reads nothing back and is slated for removal with the table.
2. **Official vs research corporate-action series.** `ohlcv` (source tag `nse_adj_v2_src`) is adjusted only for
   NSE-source-confirmed split/bonus/consolidation events. Price-inferred events are stored `confidence='low'`,
   `applied=false`, `applied_research=true`: they never touch the official series. `qsde.ml.data.load_prices(policy=)`
   serves `official` (default; label windows touching price-inferred events are excluded, as are UNEXPLAINED and
   SOURCE_UNCONFIRMED) or `research` (extra events applied on the fly; the run is forced PLUMBING_ONLY, gets its own
   ledger key, and the card says adjustment is incomplete). Symbols with a fresh unconfirmed >=18% gap are still
   excluded at serving time.
3. **SOURCE_UNCONFIRMED listings** (85) are reconciled read-only by `scripts/reconcile_source_unconfirmed.py` into
   `confirm_and_apply` / `needs_manual_review` / `reject_listing_or_data_mismatch`. Nothing is applied by that script.
4. **Full-universe snapshots** are built source-first with inferred events excluded from the official series, staged in
   a temp directory, validated (file hashes, schema, ordering, `known_at`, inf, all-NaN features, universe coverage),
   hashed, then promoted atomically, with free-disk checks before and after. The manifest records factor version,
   universe version, date range, row and cell counts, NaN cells, corporate-action source (file hash, method, event
   counts), excluded-event count and dataset hash. `scripts/snapshot_dq_report.py` runs straight afterwards.
   **No full-universe training until that report and the reconciliation are reviewed:** `scripts/retrain.py` refuses a
   non-plumbing snapshot unless `--reviewed-snapshot <id>` matches.

### Training / paper / Scorecard now share one contract
* `qsde.ml.dataset.build_labelled_dataset` (snapshot features + `tb_nextopen_v1` labels under a price policy) and
  `qsde.ml.experiment.run_experiment` (CPCV -> harness -> ledger trial -> PBO -> model card) are the only route to a
  verdict. `models.dataset.build_training_dataset` is a shim over them; the close-of-day labeler is no longer used.
* The legacy `lgbm_model.train_lightgbm_model` evaluator is disabled unless `QSDE_ALLOW_LEGACY_TRAINER=true` (kept only to
  reproduce historical numbers). `scripts/retrain.py` writes ledger trials and cards and, with `--save-candidate`, a
  candidate artifact under `data/models/candidates`; it never touches active weights or `model_runs`.
* **Entry convention:** the decision is at the close of t, the fill at the open of t+1, and stop/target are re-anchored to
  the fill with the signal's fractional offsets (`qsde.ml.barrier.levels_at_fill`). The Scorecard resolver
  (`entry_fill_price`/`entry_fill_date` on `trade_ideas`) and the paper journal (`paper_trades.ref_price`, levels rescaled in
  `fill_pending_entries`) both do this; a test grades a harness label and the resolver on the same path and requires the
  same outcome. Migration 027.

### Known divergences (not hidden)
1. **Barrier widths:** the engines' trade levels (`trade_levels`, `_atr_levels`) and the registry barrier (`sl_mult*sigma`,
   EWM span 100) are separate formulas. After re-anchoring, a paper trade or idea and a harness label for the same signal
   agree on fill and walk but not necessarily on barrier distance. Making `trade_levels` read the registry barrier is open.
2. **Corporate actions:** rights, cash dividends, symbol changes and complex demergers are not adjusted; `adj_close == close`
   (no `total_return` series yet). 131 price-inferred events (some demergers) are research-only.
3. **Pre-existing paper trades** logged before migration 027 have no `ref_price`; their levels stay absolute and they are
   resolved on the old basis. Ideas (`trade_ideas`) are re-anchored at resolution regardless of age, but ideas resolved
   before 027 were scored from the signal close and have no `entry_fill_price`. Do not mix either into harness comparisons.
4. **Promotion** remains paused (`QSDE_ML_PROMOTION_ENABLED=false`) until a harness-based gate exists and a human signs off.

### First full-universe snapshot (2026-10-18) and review findings
* Snapshot `20261018T131903Z-aa6d315a45`, universe `adv60_top500_v1+adv10+full`, 832,773 rows, 1,100 symbols, 95 features
  (93 usable: `smc_demand_zone` / `smc_supply_zone` are constant and excluded from model inputs), 2017-01-09 .. 2026-04-30,
  official prices, 105 label-exclusion events. Validation passed; universe coverage 100%.
* **Feature-contamination window raised 60 -> 282 sessions.** Measured on three price-inferred events: after a 0.2x or 0.5x
  cliff, 37-39 of 95 features differ by > 0.1 sd for 270-330 sessions (EMA-200-based smc_* up to ~740); a 0.6x cliff clears in
  ~22. Cost: 8,869 of 830,192 swing_5d labels dropped (1.1%).
* **Symbol renames:** the NSE feed keys actions by the CURRENT symbol, `eod_raw` by the historical one. 24 of the 85
  SOURCE_UNCONFIRMED listings are likely renames (same-date matching gap under another symbol, e.g. UNOMINDA<-MINDAIND,
  PCBL<-PHILIPCARB, UNITDSPR<-MCDOWELL-N, TARIL<-TRIL). A symbol-change map is needed before these can be applied, and renamed
  stocks are currently two separate series. `eod_raw.isin` exists only for 2016 - mid 2020, so ISIN continuity is untestable later.
* Production reconciler requires the immediately preceding market session; illiquid names that skip sessions read as unconfirmed
  (DOLPHIN 2024-01-29). Widening it is a proposal (changes the official series), not done.
* DQ: 1,946 feature-row gaps >5 sessions are all universe exit/re-entry (no missing bars). The 14 "event applied but cliff" rows are
  20% circuit days, not failed adjustments. Several `smc_*_dist` features reach 500-770 sd (absolute price distances): fine for trees,
  need a per-date transform for linear/deep models.
* **`factor_pit` retention record (owner decision 2026-10-18: keep until migrated).** ~8.2M rows (8,195,321 ffd_v2 cells matched
  exactly against the Parquet store), ~2.1 GB, factor_version ffd_v2, 46-symbol pilot snapshot `20261018T121111Z-38f2c45749`
  (dataset hash recorded in its MANIFEST). Kept as the parity reference. The `analyze.py` pin flow, its last live writer, now
  writes through the feature store (`compute_factors_batch`), and a test fails if any live module imports `pit_writer`.
  **Deletion conditions:** zero production references (test-enforced), parity test retargeted or retired, and one full backup
  with manifest verification; then archive the schema, record row count + hash here, drop. Two writable factor paths must
  never coexist.

### Owner review of the snapshot packet (2026-10-18) and what was done
* **The five `confirm_and_apply` proposals are HELD.** Evidence packs (`data/reports/corp_actions/evidence/`, script
  `scripts/evidence_packs.py`): DOLPHIN, DRCSYSTEMS, NDRAUTO and UEL pass all four checks (gap matches listing, 3-session
  persistence, adjusted-return continuity, same symbol trades both sides); VERTOZ fails persistence (price fell ~12.8% off the
  factor over the first three sessions after a 12-session halt). The feed has no record date or ISIN for these. Nothing applied;
  approval is the owner's, per pack.
* **Symbol renames:** `symbol_changes` table (migration 028) holds 16 handoff-consistent CANDIDATES (old series ends the session
  before the new one starts, no overlap; e.g. MOTHERSUMI->MOTHERSON, MINDAIND->UNOMINDA, MCDOWELL-N->UNITDSPR,
  ESSELPACK->EPL) and rejected 50 same-ratio chance twins. All `verified = FALSE`, price-archive inference, used for nothing yet.
  The next step is an NSE symbol-change source (or Kite instruments for the current side), then stitching under a stable
  internal security id.
* **Reconciler widening:** approved only as a separate reviewed change (bounded skipped sessions, NSE confirmation, resolved
  symbol mapping, `reconciler_version` + evidence + confidence logged, prior snapshot retained). Not implemented.
* **Smoke retrain** (`scripts/retrain.py --smoke --reviewed-snapshot <id>`): 60 explicitly listed liquid names, PLUMBING_ONLY,
  NOT_ASSESSED, separate ledger (`data/ledger_smoke`), no candidates. First run: 114,906 labels, 300 regraded through the
  Scorecard resolver and the paper-journal path with 0 disagreements on fill, status and exit price. Its Sharpe (-0.54 net) is
  not evidence and says nothing about any model. The subset is today's liquid names, i.e. survivor-selected.

### Owner decisions of 2026-10-18 (second review) and what was done
* **Approved: DOLPHIN, DRCSYSTEMS, NDRAUTO, UEL. Rejected (kept unapplied): VERTOZ.** Recorded in the versioned file
  `infra/data/corp_action_approvals.json`, applied by `qsde.ingestion.corp_actions_approvals` (method `owner_approved_v1`) at
  the OBSERVED gap date, with a guard that re-measures the gap and refuses on mismatch. Two facts that were in the packs but not
  in my summary: NDRAUTO's gap bar is 25 market sessions after its previous traded bar (a month after the listed date; applied
  anyway on the owner's approval, flagged in the approval notes) and DRCSYSTEMS has 3 untraded sessions. Undo = edit the file
  and rebuild.
* New snapshot `20261018T144014Z-1ca69ab5be` (manifest records reconciler version `source_confirmed_v2 + owner_approved_v1` and the
  approvals file hash); the prior snapshot `20261018T131903Z-aa6d315a45` is retained. Before/after diff: **zero changed feature
  cells** - none of the four symbols is in the >= 10 crore universe, so the approvals change the official `ohlcv` for those
  symbols (e.g. DOLPHIN 2024-01-24 close 1830.60 -> 183.06) but not this snapshot's features.
* **NSE symbol-change list fetched** (one GET of the public file; stored at `data/nse/symbolchange.csv`, sha256 69c0c5e18cca3350,
  1,058 rows) and loaded to `symbol_changes` with `verification_status = official` (migration 029). 15 of the 16 price-inferred
  candidates match it exactly, each effective the session after the old symbol's last bar; PHILIPCARB->PCBL is not in the list
  (the list is not exhaustive: absence is not rejection) and stays `inferred`. 239 official changes touch universe symbols, so
  stitching renamed stocks under a stable security id is material.
* **Reconciliation with the official map:** 21 of the SOURCE_UNCONFIRMED listings are actions that happened under the OLD symbol
  of an official rename (e.g. UNOMINDA<-MINDAIND, UNITDSPR<-MCDOWELL-N, MOTHERSON<-MOTHERSUMI). They are proposals to apply to
  the old symbol under a rename-aware reconciler; none applied, each still needs its evidence pack.
* **Heavy-tailed features:** `qsde.ml.transforms` ranks them per date for linear/deep models (`LabelledDataset.model_frame`);
  trees keep raw values.
* **Continuous data:** design and build order in `docs/STREAMING_DATA.md`; nothing beyond the one NSE file has been fetched.

### Owner decisions of 2026-10-18 (third review): NDRAUTO reversed, hard bounds, rename-aware reconciler, streaming foundation
* **NDRAUTO approval REVERSED** (`rejected_pending_evidence`). Record: listed ex-date 2023-07-24, observed gap bar 2023-08-21 -
  **19 market sessions later** (25 untraded sessions; previous bar 2023-07-13); observed factor **0.461 vs 0.5 (7.8% off)**. Reason:
  timing and magnitude do not agree closely and promptly. It reverts to SOURCE_UNCONFIRMED. DOLPHIN and UEL stay applied; DRCSYSTEMS
  stays applied with `confidence = 'medium'` (3 untraded sessions, gap bar 3 sessions after the listing; migration 030).
  Snapshot `20261018T145857Z-f782666885` (reconciler `source_confirmed_v2 + owner_approved_v1`, approvals file hash in the
  manifest, 643 official events); the earlier snapshots `...131903Z-aa6d315a45` and `...144014Z-1ca69ab5be` are retained. Feature
  cells are identical across all three (none of the affected symbols is in the >= 10 crore universe).
* **Hard validation rule, in code** (`corp_actions_approvals.check_bounds`): an approval is valid only if the gap bar is within
  **5 market sessions** of the listed ex-date, the factor is within **5%** (3% for factors 0.8-1.25) and at most **5 untraded
  sessions** precede the gap bar. It is checked when the evidence pack is produced (decision time) and again at apply time on the
  data as it then is. Re-evaluated under it: DOLPHIN, DRCSYSTEMS, UEL pass (UEL by a hair: 4.96% off); NDRAUTO and VERTOZ fail.
  The earlier 10% / +-2-session guard was too loose and is superseded.
* **Rename-aware reconciler `source_confirmed_v3`** (evaluation only; the official series is untouched): `security_master`
  (migration 032) maps symbols to a stable `security_id` ('S:' + earliest symbol in the chain) using ONLY official NSE renames that
  also pass an archive hand-off check (old series ends, new begins, no overlap): 217 securities / 451 symbol intervals; 574
  official rows could not be used, 571 of them because one side has no bars in the archive (delisted / never in the price files).
  The v3 evaluator resolves each of the 82 still-unconfirmed listings to the symbol that traded, measures the gap against the last
  traded close with <= 5 untraded sessions, applies the hard bounds and 3-session persistence: **22 candidates**, 57 rejected,
  3 unresolved. All 22 are actions that happened under an old symbol (gap on the listed date, 0 untraded sessions, errors 0-4.9%);
  they would touch 19 symbols, 13 in the universe. Before/after diff: events changed 0, removed 0, added 22 (if all approved).
  They are written to `corp_action_candidates` and to `infra/data/corp_action_approvals_PROPOSED_v3.json` (NOT loaded); evidence
  packs are in `data/reports/corp_actions/evidence/`. `apply_approvals` accepts `apply_to_symbol`, which attaches the event to the
  old symbol and supersedes any price-inferred research event for the same gap.
* **Not yet done for renames:** see "Fourth review" below - stitched features now exist as a separate factor version.
* **Streaming foundation (S0) built, nothing enabled** (`qsde/stream/`, migration 031 `source_health`): adapter contract
  (fetch/parse/validate), raw-first landing zone (immutable, atomic, hash-verified), `known_at` stamping (live and backfill modes;
  never earlier than the fetch; corrections are new rows), runner with checkpoints advanced only after commit, health stores
  (memory, JSON, Postgres), alert sink with dedupe, due-time scheduler with backoff and market-hours-aware staleness, replay from
  landed bytes. 20 tests cover raw-first on parse failure, idempotence, corrections, replay equals live, checkpoint safety,
  failure escalation, staleness only while the market is open, and one failing source not stopping the others. No external
  polling and no Kite streaming is enabled.

### Fourth review (2026-10-18): tiered rename approvals, UEL, stitched features, NSE EOD sources
* **Tiered approval of the 22 renamed-stock proposals** (`scripts/promote_v3_proposals.py`): official NSE rename + gap on the listed
  ex-date + 0 untraded sessions + factor error <= 3% -> **approve (14)**; 3-5% -> **manual_review (8, not applied)**: AURIGROW/GODHA,
  CCAVENUE/INFIBEAM 2022, COSMOFIRST/COSMOFILMS, DHARAN/KBCGLOBAL, GANESHHOU/GANESHHOUC, LANDSMILL/EXCEL, MOTHERSON/MOTHERSUMI 2018,
  SECURKLOUD/8KMILES. **UEL moved to manual_review** (gap 0.3168 is 4.96% off 1/3: inside the 5% hard bound, too close for an
  automatic approval; no independent confirmation; the 3-session level is 0.332, i.e. 0.3% off, which favours it). Applied official
  events now 656 (16 owner-approved). The 14 approved events adjust the OLD symbol's series; they change 6,745 feature rows across 10
  universe symbols (MCDOWELL-N, INFIBEAM, MOTHERSUMI, SUVENPHAR, WELSPUNIND, MINDAIND, AMIORG, TRIL, NBVENTURES, TIPSINDLTD).
  Note: DRCSYSTEMS (3.5% off) sits in the 3-5% band by the new tiering; it stays applied at `confidence = medium` per the earlier decision.
* **Stitched features are a separate factor version, `ffd_v2s`**; `ffd_v2` is untouched. Per-symbol snapshot with the approvals:
  `20261018T153203Z-8615b43353` (ffd_v2); stitched: `20261018T155033Z-ea05b50c97` (ffd_v2s). Rules: only OFFICIAL NSE renames that pass
  the archive hand-off check AND a **price-continuity gate** (overnight jump at the join <= 20%) are stitched: 194 securities / 403
  intervals. The gate was added after the first stitched build showed 25 of 234 hand-offs with a -46% to -95% jump (a split / merger /
  swap coinciding with the rename: KSBPUMPS->KSB -79.9%, NIITTECH->COFORGE -80.2%, SRTRANSFIN->SHRIRAMFIN -80.0%, ...); stitching
  them would have put a fake cliff in every feature. Those 25 stay separate series and are listed in
  `data/reports/corp_actions/handoff_with_price_event.csv`; the old feed lists no action near any of them, so they need the NSE
  corporate-actions history (below) before they can be adjusted and stitched. Older segments are rescaled by the official events of the
  LATER symbols so the join is continuous; `symbol` in each row is the symbol that traded that day, fracdiff runs across the rename.
* **Diff, ffd_v2 vs ffd_v2s** (`scripts/stitched_feature_diff.py`, report `data/reports/stitching/stitched_feature_diff.md`): identical
  row keys; 29,463 rows (3.54%) in 63 symbols changed, **0 rows outside stitched securities**; 156,415 warm-up NaN cells removed,
  **0 NaN introduced**; 0 of 209 hand-offs with a >20% jump. Biggest effect is on long-lookback families (distance/EMA, 12-month
  momentum, 52-week range, volatility), as expected. The two feature versions never mix; training can opt in with `--factor-version ffd_v2s`.
  Smoke retrain on both versions: PLUMBING_ONLY, NOT_ASSESSED, contract check 0 disagreements out of 300.
* **NSE EOD sources on the stream foundation** (`qsde/stream/adapters/nse.py`, `sinks.py`, `scripts/stream_run.py`, migration 033):
  security-wise bhavcopy (`sec_bhavdata_full_DDMMYYYY.csv`, 404 = non-trading day), the corporate-actions API
  (`api/corporates-corporateActions`; returns **record date, face value, ISIN** and dividends / rights / demergers / buy-backs, the data
  the old feed lacked) and the symbol-change CSV. All network access is one injectable `HttpFetcher` (spacing, retries, 404 handled);
  tests run on real response samples, including failure injection and replay. **Nothing is scheduled**: `stream_run.py` fetches only
  with `--enable-network` and loops only with `--enable-scheduled-downloads`. One manual run each of corporate actions and symbol changes
  proved land -> parse -> DB: 492 corporate-action rows (467 dividends, 11 split/bonus, 5 rights, 5 demergers; ex-dates 2026-09-03 to
  2026-10-23, all with ISIN and record date); symbol changes added 0 rows (idempotent). The new table is NOT yet a source for price
  adjustment.
* **Data gap (closed in the fifth review below).**

### Fifth review (2026-10-18): bhavcopy backfill, corporate-action history, held items, rename correction
* **Bhavcopy backfill done** (owner-approved): 116 new sessions 2026-05-04 .. 2026-10-16 (323,019 rows). Per-file validation: the date
  printed INSIDE each file must be the requested session, row-count floor (1,000), schema, OHLC consistency; the SHA-256 of every file
  is in its landing sidecar (NSE publishes no checksum). **NSE serves the previous trading day's file on a holiday instead of a 404**
  (1 May, 28 May, 26 Jun, 14 Sep, 2 Oct): the adapter keeps the raw file, loads nothing, and does not treat it as an error. Resumable
  by landed session; reruns add nothing. Result: 0 OHLC-inconsistent rows, 2,676-2,962 rows/session. `universe_pit` rebuilt to
  2026-10-16: **0 of 19,500 pre-cutoff rows changed**; 45 symbols entered for the first time; 396 of 500 April members remain.
  Official `ohlcv` rebuilt (5,104,705 rows, 657 applied events). Successor snapshots (predecessors kept):
  `ffd_v2` `20261018T171714Z-30c99bfa19` and `ffd_v2s` `20261018T172506Z-21c4623e57`, both 890,773 rows / 1,147 symbols to 2026-10-16;
  the part up to 2026-04-30 is cell-for-cell identical to its predecessor (0 changed cells); 58,000 new symbol-days.
* **NSE corporate-action history loaded** (quarterly windows 2016-01 .. 2026-10, raw payloads landed, 43 windows): 24,356 actions,
  all with ISIN, 11,539 with record date: 14,435 dividends, 792 split/bonus, 279 rights, 148 demergers, 8,701 other (AGMs, buy-backs, ...).
  Exact duplicate rows in NSE's history are collapsed; a landing-zone sidecar/payload name collision on JSON files was found and fixed.
  **Candidates only: nothing is applied to prices.** `corp_action_reconciliation` (migration 034, `ca_reconcile_v1`) classifies each
  action: split/bonus 504 confirmed / 262 needs_review / 26 rejected; dividends 14,046 / 184 / 205; rights 257 / 17 / 5; demergers 0 / 147 / 1.
* **Unapplied NSE actions are now label-exclusion events** (`qsde.ml.data.label_exclusion_events`): every NSE split / bonus /
  consolidation / demerger with no applied event within 5 days is excluded from labels and from the 282-session feature window, so a raw
  -50% / -90% cliff is never trained on as a return. In the new period 601 one-day moves of >= 18% appear, 36 with an NSE record; exclusion
  events rose from 106 to 169 (full `ffd_v2s` swing_5d: 874,836 of 890,773 rows labelled).
* **Correction of the "25 renames that coincide with a split" finding.** They were an artifact of my continuity gate: the new symbol's
  bars are adjusted for ITS later splits (KSB 1:5 in 2024, COFORGE 2025, SHRIRAMFIN 2025, SCHAEFFLER 2022, PAISALO 2022/2024 ...) while the
  old symbol's are not, which looks like an -80% cliff at the join. `stitched_ohlcv` already rescales older segments; the gate now
  evaluates the join on the same rescaled basis. With that, **0 renames coincide with a price event** and the map stitches **228
  securities / 473 intervals**. A genuinely coincident split is still refused (test).
* **ffd_v2s pre-use checks** (`scripts/stitched_integrity_check.py`): (1) all 16 owner-approved events are ordinary overnight moves in
  both the official and the stitched series (16/16, 16/16). (2) NOT fully clean: inside the 228 stitched securities there are 144
  moves of >= 25%: 39 with an NSE record (held renames, demergers and schemes such as CROMPGREAV 2016, BOROSIL, FRL) that the official
  series does not adjust, and 105 with no NSE record (mostly genuine crashes / 25% circuit runs, e.g. GAMMNINFRA 2020). These cliffs exist
  in the unstitched series too (stitching did not create them) and are covered by the label exclusions above; they are NOT a licence to
  treat ffd_v2s as clean. Stitched vs per-symbol: 44,374 rows (4.98%) changed, all inside stitched securities, 201,880 warm-up NaN cells
  removed, 0 introduced. Data-quality reports exist for both versions. A model card must name the factor version it used.
* **Held items vs the official NSE record** (`scripts/held_items_review.py`; RECOMMENDATIONS, nothing applied): for UEL, NDRAUTO, VERTOZ,
  DRCSYSTEMS and the eight 3-5% renames, the NSE record independently confirms the action type, ratio and ex-date (symbol/ISIN, record
  date, face value), and in every case the official ex-date lies after the stock's last pre-gap bar and on/before the gap bar (UEL: ex-date
  2025-10-10, no bar that day, first post-ex bar 2025-10-13, which explains the earlier "+1 session" shift). The price gaps differ from
  the theoretical factor by 3-8%, consistent with ordinary moves in illiquid names. A proposed second tier ("official-record-backed"):
  official factor equals the approved factor, official ex-date inside the untraded window or on the gap bar, gap within the existing
  hard bounds. Under it the 8 renames + UEL (all inside 5%) would be approvable; NDRAUTO (7.9%, 19 sessions after the ex-date) and VERTOZ
  (5.0%, 12 untraded sessions, persistence 12.8% off) would still violate the existing hard bounds and stay held unless the owner
  decides that the official record may override them. DRCSYSTEMS: the record (FV 10 -> 1, ex 2022-03-16, record 2022-03-17) confirms its
  factor; the gap bar is 3 sessions later because the stock did not trade; it stays `medium`, as instructed, pending the owner's review.

### Sixth review (2026-10-18): `official_confirmed` tier, reviewed adjustment pipeline

* **Tier `official_confirmed` (confidence value of the same name).** An event is applied under it only when ALL hold: the NSE record
  matches the action type, ratio and ex-date; the official ex-date is on the gap bar or inside the symbol's untraded window
  (`(last pre-gap bar, gap bar]`); the observed factor is within the existing tolerance (5%, 3% for mild factors 0.8-1.25) and the other hard
  bounds (gap <= 5 sessions from the ex-date, <= 5 untraded sessions) hold; symbol/security continuity is explained by the official
  rename map; and the event row records source URL, retrieval time, ISIN and an evidence-pack reference. An official record can NEVER
  override a hard timing/factor failure.
* **Decisions.** Approved under the tier: the eight 3-5% renames (AURIGROW/GODHA, CCAVENUE/INFIBEAM, COSMOFIRST/COSMOFILMS,
  DHARAN/KBCGLOBAL, GANESHHOU/GANESHHOUC, LANDSMILL/EXCEL, MOTHERSON/MOTHERSUMI, SECURKLOUD/8KMILES) and UEL. **DRCSYSTEMS:** it was
  initially applied at `medium` confidence under the earlier policy and was reclassified to `official_confirmed` after reconciliation
  against the official NSE record (FV 10 -> 1, ex 2022-03-16, gap bar three sessions later because the stock did not trade). The
  original approval and its audit trail are preserved in the entry's `history` list in `infra/data/corp_action_approvals.json`.
  **NDRAUTO (7.9% off, 19 sessions after the ex-date) and VERTOZ (12 untraded sessions, persistence 12.8% off) stay held**; the file records
  that the official record does not override the bounds. Applied after rebuild: 25 approvals, 0 refused; 10 `official_confirmed` events.
* **Snapshots after the tier** (corporate-action source unchanged otherwise): per-symbol `ffd_v2` `20261018T175041Z-528561aa2c`, stitched
  `ffd_v2s` `20261018T180008Z-aae491a930` (890,773 rows, 1,147 symbols, 95 features each; DQ reports in `data/reports/data_quality/`). Stitched vs
  per-symbol: 44,374 rows (4.98%) differ, all inside stitched securities (0 outside), 201,880 warm-up NaN cells removed, 0 introduced.
* **Split/bonus-adjusted series (`ohlcv_split_bonus_adjusted`, `sbadj_v1-34759406b1`, VALIDATED).** Separate Parquet materialization
  under `data/adjusted/split_bonus/`, never written into `ohlcv`. Inputs: only `confirmed` (ca_reconcile_v1) and `official_confirmed`/owner-approved
  events (508 events: 483 confirmed, 15 owner_approved, 10 official_confirmed). The NSE-LISTED factor is applied to bars strictly before the
  gap bar and volume is divided. Every applied event is recorded in `adjustment_events_applied` with source record, tier, factor, ex-date, gap
  date, ISIN and evidence reference; the run (input fingerprints, output hash, validation) is in `adjustment_runs`. Rebuilt from scratch from the
  same inputs: **identical output hash**. Event continuity: 508/508 ok (adjusted overnight move < 20%), 0 hard failures, 0 missing gap bars.
  Reports: `data/reports/adjustment/split_bonus_*.md` (+ per-event CSV).
* **Difference from the current official `ohlcv` - not hidden.** 187 events adjusted in `ohlcv` (price-confirmed `source_confirmed_v2`, high)
  are NOT in the new series because ca_reconcile_v1 classifies their NSE record `needs_review` (price did not persist / factor error > 5%).
  29 events are in the new series but not in `ohlcv` (mostly 2026, after the last official rebuild). No shared event has a different factor. The two
  series therefore disagree at those 216 dates; the 187 are a review queue, not a defect of either series.
* **Total-return series (`tr_v1-f15f02f736`, VALIDATED).** Built on the split-adjusted series; dividends are not in `ohlcv` or the adjusted
  prices. Separate labelled columns `price_return_1d`, `dividend_return_1d`, `total_return_1d` (= sum) and the two indices; dividends are GROSS cash
  per share as announced (no tax modelled), attached to the ex-date bar (next bar if the ex-date had none), amount over the previous
  adjusted close; 14,023 confirmed (symbol, ex-date) groups -> 14,020 used, 3 skipped as non-positive. Rebuild: identical hash.
* **Rights and demergers** are event-level cases with no universal ratio: `scripts/ca_review_worksheet.py` writes evidence worksheets
  (`data/reports/corp_actions/review_rights.csv` 274 events, `review_demergers.csv` 147) with a blank owner `decision` column. Nothing is adjusted.
* **Not yet allowed:** the adjusted series (price or total-return) must not be used for training until the full data-quality and event-level
  validation passes (still pending: review of the 187 and of the rights/demerger worksheets). Smoke retraining on it is deferred; promotion stays
  disabled (`QSDE_ML_PROMOTION_ENABLED=false`); scheduled NSE downloads remain off.
* **Model-card requirement.** Every model card must name: `factor_version`, snapshot ID, universe version, label exclusions (count and rule),
  and the corporate-action source hash. The 144 large moves in stitched securities are acceptable for research only while the 39 with an
  unapplied NSE record stay out of the labels and the 105 without a record stay out where they make a label ambiguous.

### Seventh review (2026-10-19): first smoke results, a harness defect found, triage, prediction ledger

* **Smoke retrain on `ffd_v2s` `20261018T180008Z-aae491a930`** (60 liquid names, `PLUMBING_ONLY`, verdict `NOT_ASSESSED`, nothing saved or promoted;
  the symbol subset is today's most liquid names, so it is survivor-biased by construction). Cards: `data/reports/model_cards/smoke_*_ffd_v2s_20261018T180008Z-aae491a930.md`.
* **Defect found and fixed.** The first run regressed the pooled raw `ret_net`. Through the same harness a no-fit rule (-ATR%) scored OOF rank IC
  +0.139 while LightGBM scored about -0.01 (and the within-date shuffled-label control was -0.03, i.e. biased): the pooled target lets trees spend
  capacity on the market component and tails. Training on a per-date rank-gauss target (`cs_rank_gauss`, now the default) gives OOF rank IC +0.124 (swing_5d)
  and +0.109 (position_20d) with a shuffled-label null of ~0.000. The null control is now part of every run and card; a biased null downgrades the run to
  `PLUMBING_ONLY`. (A first null design scored the shuffled model against the real returns and was wrong for low-capacity models with one strong
  feature; it now scores against the shuffled labels and reports the real-return IC as information only.)
* **What the smoke result does and does not say.** IC is positive and significant, but it is mostly one low-volatility effect (-ATR% alone has IC 0.14),
  and a top-quintile long-only book is NOT profitable net of costs at swing_5d (net Sharpe -0.72 at 30 bps; 0.47 at 10 bps) and is not better than
  baselines at position_20d (0.72 vs equal-weight 0.63, 12-1 momentum 0.95, random 0.54). PBO 0.66/0.74 with 2-4 trials. DSR 0.953 at position_20d rests
  on N=2 and the single-estimate fallback and must not be read as a pass. Rank IC is not an edge until it survives costs and baselines.
* **Triage of the 187 price-only split events** (`scripts/triage_price_only_splits.py`; ordering aid, nothing approved): A 69 (exact NSE record, gap on
  the ex-date, 0 untraded, factor error 3-10%: they fail only the 5%/3% tolerance), B 113 (price did not persist or large factor error), C 5. Bucket A is
  the owner's decision: approving it is a policy exception to the factor tolerance, not an application of an existing tier.
* **Prediction ledger** (migration 037, `qsde/live_loop/`): `prediction_log` is append-only (UPDATE/DELETE rejected by trigger); re-logging a key with
  different content raises `ImmutableViolation`; a prediction stamped before its session's close is a `PITViolation`; the label definition comes from the
  registry. The outcome joiner resolves with `make_labels` itself (same labels as training), cannot see bars after `data_through`, and omits predictions
  whose window is incomplete. The monitor computes calibration (Brier, log loss, slope/intercept), ranking (rank IC, precision@q, lift), economics (top-q net
  mean, excess, profit factor, drawdown) and PSI drift; `retrain_decision` recommends only a CANDIDATE retrain (scheduled / drift / data triggers, drift
  triggers suppressed on thin live evidence) and `kill_switch` lists reasons to halt a model. Not yet built: candidate registry and the promotion gate that
  requires the offline gate + live-paper evidence + owner approval.
* **Housekeeping.** Full `pg_dump` (`data/backup/qsde_full_2026-10-19.dump`, 434 MB, sha256 recorded, table of contents lists 6,950 entries) and key table row
  counts (`counts_2026-10-19.csv`) taken; no restore drill has been done, so `factor_pit` must still not be dropped. A regex scan of all 38 commits found no
  secret-shaped additions (a heuristic, not gitleaks); keys named in HANDOVER still need your rotation, and nothing has been pushed.

### Eighth review (2026-10-19): bucket A held, full-universe research evaluation, restore drill

* **Bucket A is NOT approved and the tolerance is NOT widened.** The 69 events carry the status `official_match_price_mismatch`; each is decided on its own
  evidence (`scripts/ca_bucket_a_review.py` -> `data/reports/corp_actions/review_bucket_a_official_match_price_mismatch.{md,csv}`: NSE record, price series around the gap
  bar, halt/thin/rename/demerger/neighbouring-action flags, proposed factor and its source, residual move after the LISTED factor in daily sigmas, a blank `decision`).
  Facts to weigh: 26 events involve a symbol in the point-in-time universe within 60 days (review first); 56 of 69 have no flag; the residual after applying the
  listed factor is positive in 90% of events (median +6.0%, median |2.2| sigma). Errors in a wrong factor would be symmetric, so this looks like a systematic market
  reaction at the ex-date rather than 69 bad factors - but that is an inference for the owner to accept or reject per event, not a finding that the factors are right.
* **Full-universe research evaluation** (`scripts/research_eval.py`; `ffd_v2s` `20261018T180008Z-aae491a930`, official prices, 1,147 symbols, ~870k labelled rows per
  horizon, 164 label-exclusion events, rank-gauss target, CPCV 10x2 = 9 paths, shuffled-label null -0.0007 / -0.0001 (OK)). RESEARCH_ONLY: nothing saved or promoted.
  Each horizon is trial 1 on its ledger key (so DSR uses the single-estimate fallback and is optimistic; PBO unavailable).
  * OOF rank IC +0.171 (swing_5d) and +0.200 (position_20d), stable across liquidity tiers (rank 1-100 / 101-250 / 251-500: 0.15-0.20), NW t 15 and 8.7.
  * **The model score is 0.91 rank-correlated with low volatility (-ATR%).** What was found is the low-volatility effect.
  * swing_5d: nothing is profitable net of 25-30 bps - equal-weight is -1.59 Sharpe at 30 bps, model long-only -1.73, 12-1 momentum -1.19, low-vol -2.03;
    the ATR-residualised variant is better than the baselines (-0.87 vs -1.57) but still negative; long-short +0.84 at 10 bps, -0.78 at 30 bps. REJECT.
  * position_20d (net Sharpe at 10/25/30 bps): model long-only 0.73/0.41/0.30; ATR-residualised 0.65/0.43/0.35; long-short 0.56/0.23/0.12; baselines: random -0.08,
    equal-weight -0.04, 12-1 momentum 0.14, low-vol 0.18, **MOP time-series momentum (Tier-1) 0.62/0.41/0.34 - equal to or better than the model**. The model is not
    shown to beat the simple Tier-1 rule or low-vol. Positive in uptrends and low-vol regimes, negative in downtrends (-0.65). KEEP_RESEARCHING at best (DSR 0.82 on N=1).
  * Reading, per the pre-agreed decision table: the IC persists but is almost entirely low-vol exposure and does not beat momentum net of costs, so the next build is
    not a directional alpha model. The long-short leg is optimistic: it uses the delivery cost model and shorting cash equities overnight is not available.
* **Backup restore drill passed.** `qsde_full_2026-10-19.dump` (sha256 verified) was restored into a scratch database (TimescaleDB pre/post-restore): all 13 key table
  counts identical (eod_raw 5,104,705; ohlcv 5,104,705; factor_pit 8,195,321; corp_action_events 974; ...) and three content hashes matched (price rows for four symbols,
  all corporate-action events with confidence/tier, adjustment runs). Scratch database dropped. `factor_pit` may be dropped once the owner decides to; it has not been.
* **Still open:** secret scan with gitleaks (only a regex scan has been run), key rotation (owner), candidate registry + promotion gate.

## Out of scope here

Kite backfill/live (needs a session and the owner's go-ahead for any network call), a verified corporate-actions
source for fundamentals/news, review of the 187 price-only split events and the rights/demerger worksheets, dropping `factor_pit` (after the
first validated full snapshot).
