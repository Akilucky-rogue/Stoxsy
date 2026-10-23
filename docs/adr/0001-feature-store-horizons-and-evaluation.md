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

### Ninth review (2026-10-19): non-equity instruments in the universe, defensive signal, accounts, first UI

* **Defect found: the "equity" universe contained ETFs and liquid-fund units.** `adv60_top500_v1` is ranked on traded value alone, so 21 instruments
  (LIQUID, LIQUID1, LIQUIDADD, LIQUIDCASE, LIQUIDPLUS; gold ETFs; silver ETFs; KOTAKNIFTY, ICICINIFTY, MON100) entered it. Liquid funds (daily vol 0.00-0.02%)
  ranked as the MOST defensive names in the defensive signal and sat at the low-volatility end of every cross-sectional ranking, which is exactly the effect the
  eighth review found to be driving the model. The list is `infra/data/non_equity_instruments.json` (`non_equity_v1`, name + behaviour heuristic, status
  *pending owner review*; operating companies such as SKYGOLD, SHANTIGOLD, GOLDIAM, PNBGILTS are deliberately NOT on it; to be verified against NSE's official ETF
  list if the owner approves fetching it). The dataset builder now drops them (`exclude_non_equity=True`), the universe version carries `|ex_non_equity_v1:<hash>`
  (so trial keys and model cards differ from earlier runs), and the defensive evaluation excludes them. **The eighth review's full-universe results ran on the
  contaminated universe: they are marked SUPERSEDED and are being re-run; do not rely on them.** The defensive equal-weight / tilt results barely moved (Sharpe 0.76 / 0.81;
  top defensive quintile 0.94, max drawdown -35.7% vs -60.8%).
* **Defensive allocation signal** (`qsde/defensive`, `scripts/defensive_eval.py`): five equally-weighted percent-ranked risk components (ATR%, 60d vol, 60d downside
  deviation, drawdown from the 120d high, traded value), point-in-time (a test proves future bars cannot change a past score), long-only weight schemes (ew, tilt 0.5x-1.5x,
  tilt gated in a risk-off regime, top quintile), weights from the close of t executed at the next close, 12.5 bps one way. Portfolio construction, not prediction: the tilt cuts
  volatility ~5% with a Sharpe edge of +0.06 (95% paired-bootstrap interval +0.02 to +0.10); the top quintile cuts volatility ~27% and max drawdown by ~25 points but its Sharpe
  edge (+0.18, -0.10 to +0.53) is not established, turnover is 5.7x/yr, and 2026 YTD it lagged (-6.3% vs +3.9%).
* **Bucket A** (status `official_match_price_mismatch`): worksheet for all 69 and a top-10 decision packet (`packet_bucket_a_top10.md`) with a deterministic recommendation per event
  (confirm / reject / needs_more_evidence) and the price table around each gap bar. Recommendations only; the owner decides; no tolerance widened.
* **Bounded ATR-neutral experiment** (`scripts/research_resid.py`): criteria pre-registered in the script before any run (residual IC >= 0.02 with NW t >= 3; net Sharpe @25 bps above the
  best of 12-1 / MOP / low-vol; DSR >= 0.95 on the real trial count; null control OK), three trials, position_20d only, otherwise REJECT. First launch was discarded (contaminated universe); re-running.
* **Accounts** (migration 038, `qsde/auth`, `api/auth_deps.py`): argon2id, 15-minute JWT + rotating 14-day refresh cookie (httpOnly, SameSite=Strict, CSRF header; reuse revokes the family),
  throttling (5 failures / 15 min per email), generic failure message, forced password change for temporary passwords, roles admin | trader | viewer, audit log, self-demotion blocked.
  `require_auth` is app-wide; a test sends an anonymous request to every route in the OpenAPI document and requires 401 when `AUTH_ENFORCED=true`. Default `AUTH_ENFORCED=false` keeps the
  existing local UI working; `scripts/create_owner.py` creates the first admin (password from env or hidden prompt). Not yet: MFA, per-user portfolios/alerts, Redis rate limiting.
* **First UI** (`web/`, Vite + React 18 + TypeScript, react-router, TanStack Query, self-hosted Inter / JetBrains Mono, light/dark/system theme, fintech tokens): sign-in, forced password
  change, dashboard, defensive signal, reports & models (markdown, sanitised), data & system, admin. Read-only API for it: `/api/defensive/latest`, `/api/system/status`,
  `/api/research/reports`. Verified in real browsers: sign-in -> forced change -> dashboard with live data, light and dark at desktop width, 390 px width with the drawer and no horizontal
  page overflow; 13 unit tests, 739 backend tests. The old Next.js app is untouched (retire after route parity). Not yet built: ideas/paper/F&O/pairs views, live compare, alerts, PWA, Tauri.

### Tenth review (2026-10-19): NSE official ETF list, owner account, corrected research, decision cards

* **Scope clarified by the owner:** NSE, BSE and the whole tradeable universe; the product must say what, where, how much and until when. Today the database holds NSE only
  (`eod_raw`: 5.10M rows, 4,005 symbols, 2016-2026-10-16); BSE ingestion and non-equity instrument classes each need the owner's go-ahead for the network source.
* **ETF verification (owner approved the fetch):** `eq_etfseclist.csv` from NSE's archive was landed raw (`data/landing/nse_etf_list/2026/10/19/005642-9d90ef6e.csv`, sha256 `9d90ef6e...`, 355 ETFs, all ISIN INF*).
  Of the 21 provisional exclusions, 16 are on the official list; 5 (AXISGOLD, AXISILVER, ICICINIFTY, KOTAKGOLD, KOTAKNIFTY) are ETFs by name that are not on today's file (delisted or merged) and stay
  excluded, labelled `not_on_current_official_list`. **The official list also found 4 ETFs that had entered the universe and that I had missed** (HDFCSML250, ICICIB22, MODEFENCE, TATSILV); TATSILV had
  appeared in a draft decision card. Exclusion is now the union (360 symbols) and the universe version records both lists: `ex_non_equity_v1:<hash>+non_equity_official_v1:<hash>`.
  SKYGOLD, SHANTIGOLD, GOLDIAM and PNBGILTS are operating companies and remain in. ISIN/scheme verification beyond the ETF file (e.g. an equity master) would need a further NSE file.
* **Corrected full-universe research (universe with the provisional exclusions; the 4 extra ETFs are not removed in these two runs, the ATR-neutral experiment runs with the official list):**
  swing_5d - OOF rank IC +0.172, score 0.913 rank-correlated with low volatility, null +0.0001, long-only net Sharpe at 30 bps -1.73 vs equal-weight -1.59: REJECT. position_20d - IC +0.200,
  corr with low vol 0.905, null -0.0005; net Sharpe at 10/25/30 bps: model 0.71/0.39/0.28, ATR-residualised 0.63/0.40/0.33, MOP momentum 0.61/0.40/0.33, low-vol 0.61/0.28/0.17, 12-1 momentum 0.38/0.19/0.13,
  equal-weight 0.24/0.03/-0.04. The model does not beat the simple Tier-1 MOP rule; the harness verdict is KEEP_RESEARCHING at most (DSR 0.81 on N=1). The conclusions of the eighth review stand: removing the
  ETFs changed no number materially.
* **Owner account and authentication:** account created for the owner with a generated temporary password written only to `data/secrets/owner_bootstrap.txt` (never printed; must be changed at first sign-in, then the
  file deleted). `JWT_SECRET` (48 random bytes) and `AUTH_ENFORCED=true` were added to the gitignored `.env`; on the live API every protected route (health excepted) returned 401 anonymously. The test suite
  now forces `AUTH_ENFORCED=false` unless a test turns it on (`tests/conftest.py`). Not yet: MFA, a documented break-glass procedure, rate limiting beyond the audit-log throttle.
* **Decision cards** (`qsde/decision/cards.py`, `/api/decision/cards`, web page "Decision cards"): WHAT = the lowest-risk liquid names (defensive bucket >= 4, traded value >= Rs 5 Cr/day, not > 35% below the 120-session high,
  no name with > 0.55 return correlation to one already chosen); WHERE = venue and segment (NSE cash equity today); HOW MUCH = quantity from a risk budget (stop distance = the registry stop multiplier x EWM volatility;
  default 0.5% of capital lost if stopped; caps: 8% per name, 1% of daily traded value, regime exposure cap 95/80/50%); TILL WHEN = review date and an exit date after the horizon's sessions, plus stop / regime / bucket
  triggers. Every card is labelled RESEARCH_ONLY: it is risk allocation, not a forecast; dates are estimates; stops can gap. With the default settings on the 2026-10-16 signal: 13 positions, 80% invested, loss if
  every stop is hit 2.8% of capital, average pairwise correlation 0.26. 9 unit tests + endpoint test; page verified in a real browser.
* **Pending:** the bounded ATR-neutral experiment (pre-registered criteria in `scripts/research_resid.py`) is running; BSE; gitleaks and key rotation; Bucket A decisions.

### Eleventh review (2026-10-19): BSE ingestion foundation, gitleaks, asset-class order

* **Owner decisions:** approve BSE Bhavcopy ingestion; approve the gitleaks download; asset-class order BSE equities -> mutual funds / ETFs -> F&O research data -> commodities / MCX last (no MCX execution, no live orders).
  Decision cards stay labelled `RESEARCH_ONLY`; Bucket A is decided event by event by the owner.
* **BSE data (official BSE archive, three formats):** `BhavCopy_BSE_CM_0_0_0_<yyyymmdd>_F_0000.CSV` (UDiFF, from 2024-06; ISIN + ticker), `EQ_ISINCODE_<ddmmyy>.zip` (about 2017-2024; ISIN),
  `EQ<ddmmyy>_CSV.ZIP` (2016; no ISIN). A non-session day returns an HTML page with status 200, not a 404; the adapter treats that as "no session". First window (2026-10-05..16): 10 sessions, ~5,100 scrips/day,
  ISIN and ticker on 100% of rows. Raw files are landed first (`data/landing/bse_bhavcopy`), then parsed; `known_at` is publication time for backfilled history.
* **Design:** BSE is stored in its OWN table `bse_eod_raw` (migration 039, key = BSE scrip code + date), NOT in `eod_raw`: six existing NSE readers (corporate-action rebuild, reconcilers, evidence packs, snapshot
  provenance counters) select from `eod_raw` and assume NSE, so mixing exchanges there would have silently corrupted them. `stream_run --source bse_bhavcopy` runs it; `--source all` remains the NSE bundle.
  Cross-listing: `listing_xref` (migration 040) links an NSE symbol and a BSE scrip code by ISIN first, with a labelled weaker `ticker_price` tier (same ticker and closes within 2% on >= 90% of >= 30 shared sessions);
  `bse_listing_master` classifies each scrip (equity / fund_etf / debt) with its evidence. Separate exchange listings and liquidity are preserved. `scripts/bse_validation_report.py` produces the first milestone report
  (coverage, ISIN match rate, NSE/BSE price divergence, duplicates, BSE-only symbols).
* **Known limit:** NSE ISINs are known only for 2016-2020 bhavcopy rows and for symbols with NSE corporate-action records (2,728), so some dual-listed names look "BSE-only" until an NSE equity master (EQUITY_L.csv) is
  available. That file is another NSE fetch and needs the owner's approval; the report states the BSE-only count with this caveat.
* **gitleaks 8.30.1** (downloaded from the official release, SHA-256 verified against the release's checksum file; installed at `D:\Stoxsy\tools\gitleaks`): git history (45 commits) - **no leaks**; working tree - only the
  gitignored local `.env` (the generated JWT secret) and Next.js build caches under `frontend/.next` (generated, gitignored, allow-listed in `.gitleaks.toml` with a reason). A pre-commit hook runs `gitleaks git --staged`.
  Rotation of the keys named in HANDOVER is still the owner's task; they were never committed to this repository's history.

### Twelfth review (2026-10-19): ATR-neutral experiment closed; BSE history loaded and validated

* **Bounded ATR-neutral experiment: REJECT (pre-registered).** Universe with the official ETF exclusion, position_20d, three trials on the real ledger key. Residual IC (OOF score vs the
  ATR-neutral realised target) +0.086 / +0.082 / +0.087 (t 8.6 / 10.0 / 8.6) with corr(score, -ATR) ~ -0.01..-0.04, so something is left once the low-volatility exposure cannot be learned. But net Sharpe at
  25 bps was 0.34 / 0.40 / 0.33 against MOP momentum 0.40, low-vol 0.28, 12-1 0.19 (the model never clearly beats the simple Tier-1 rule), and DSR on the real trial count was 0.80 / 0.69 / 0.75 (< 0.95).
  The pre-registered rule closes the directional-ranker track. It reopens only with new information (new features/data classes such as fundamentals, flows or news with `known_at`), not by re-tuning.
* **BSE history loaded** (`bse_eod_raw`, official BSE bhavcopy, raw landed first): **2,666 sessions, 2016-01-01 .. 2026-10-16, 9.72M rows, 14,364 scrip codes.** ISIN present on 100% of rows from 2017
  (6.4% in 2016). Missing BSE sessions against NSE's archive: 1 (2023-10-19). Weekend special sessions (Muhurat 2020-11-14, budget/special Saturdays) needed explicit dates (`--extra-dates`). Two ingestion faults were
  found and fixed: parallel workers shared one checkpoint file (Windows file lock) and BSE had published a few corrupted/spliced records (2022-02-07, 2022-07-15, 2022-07-22); malformed lines are now skipped, counted and
  tolerated only up to a stray handful. A transient fetch failure used to look like a holiday; the 11 December-2017 sessions were recovered by an explicit re-fetch, so completeness is checked against NSE's calendar.
* **Validation milestone** (`data/reports/bse/bse_validation.md`): scrips by rule-based type: equity 6,025, debt 7,547, fund/ETF 780. Cross-listing: ISIN-linked pairs 2,423, weaker ticker+price tier 516; 2,869 of 4,005
  NSE symbols have a BSE link. Active BSE equities on 2026-10-16: 4,330, of which 2,301 (53%) are linked to NSE and 2,029 are BSE-only (12.3% of the last 60 days' traded value of active BSE equities) - an upper
  bound until the NSE equity master (ISINs for all NSE symbols) is fetched. NSE-vs-BSE closing prices on ISIN-linked pairs agree to a median 0.12%; 101 pairs have a median gap > 1% and 33 a gap > 10% on some day
  (listed in `price_divergence.csv`; e.g. DTIL vs BSE 523736 differs by ~80%: an ISIN link that needs review, not a data point to trust). 73 ISINs sit on more than one BSE scrip; 893 scrips changed ISIN over time;
  171 BSE scrips map to more than one NSE symbol (renames / re-listings): the map is evidence for a security master, not yet one.
* **Not yet done for BSE:** corporate-action adjustment of BSE prices (BSE-only names have no NSE action source), BSE in the universe / defensive signal / decision cards (cards still say NSE only), the NSE equity
  master, and review of the divergent pairs. Mutual funds / ETFs (AMFI NAV and scheme data) are next in the owner's order and need the owner's approval of each source.

### Thirteenth review (2026-10-19): NSE equity master, cross-listing statuses, evidence packs, `factor_pit` decision

* **NSE equity master** (`EQUITY_L.csv`, official NSE archive; landed raw, then `nse_equity_master`, 2,619 symbols with ISIN and security code). ISIN coverage of NSE symbols ever loaded: 3,231 of 4,005 (master + 2016-2020
  bhavcopy + corporate-action rows). Only NSE equity-share ISINs (security codes 01-06/10/20-23) and BSE `equity` instruments are candidates for a link: 114 cases where a bond ISIN on an NSE corporate-action row
  had linked a share to a BSE bond (e.g. IRFC) are recorded in `xref_conflicts`, not overwritten.
* **Link statuses** (`listing_xref.link_status`, migrations 041/044): `linked_verified` 2,558 (price check passed on up to 250 shared sessions), `linked_unchecked` 173 (< 30 shared sessions),
  `unlinked_secondary_scrip` 9 (a legacy/secondary BSE scrip of an NSE name that is already `linked_verified` through its primary scrip), `unlinked_pending_review` 332 (median close gap > 1% or any day > 10%).
  Unlinked means neither side is stitched or combined. Active BSE equities on 2026-10-16: 4,330; linked 91.3% of 60-day traded value; BSE-only 8.6% (1,876 scrips); unlinked pending review 0.06% (159 scrips).
* **Evidence packs for the five NSE top-500 names that were unlinked** (TRENT 600251, SOUTHBANK 632218, EMAMILTD 543894, IGL 632514, TATACHEM 600770; `data/reports/bse/evidence_packs/`): in every case the NSE
  symbol is already `linked_verified` through its primary BSE scrip (500251, 532218, 531162, 532514, 500770; median close gap 0.03-0.05% over 250 sessions) and the flagged scrip is a legacy/secondary line that
  ended between 2017 and 2023 (TRENTSL, South Ind. Bank old line, EMAMILTD7, INDRA GAS under the old ISIN INE203G01019, Tata Chemicals old line) with thin trading and a systematic ~+1% premium. Recommended
  status for all five flagged scrips: **`unlinked`** (kept unlinked; the names themselves are not at risk). The recommendation is advisory: the owner decides and nothing is applied automatically. The remaining
  lower-liquidity pairs stay in the review queue (largest-impact first) and do not block product work.
* **BSE corporate actions** still have no source. Until one is identified, approved and reconciled (action-by-action, same governance tiers as NSE), BSE-only names stay out of the universe, the defensive signal
  and the decision cards, and BSE prices stay `raw_prices_only`.
* **`factor_pit` retention decision: RETAINED, not dropped.** The table holds 8.2M rows and is still the only provenance for the model-lab snapshots recorded in the trial ledger and model cards. The full-database dump
  `qsde_full_2026-10-19.dump` (sha256 and per-table counts alongside) passed a restore drill into a scratch database, so a later drop is recoverable; but a drop needs a validated full snapshot replacement
  first, which does not exist. Revisit only after the next validated snapshot and a second restore drill.

* **AMFI fund layer (separate asset class; nothing in the equity universe, defensive signal or decision cards).** Scheme master and NAV history from AMFI's own files, raw landed first:
  **24.2M NAV rows, 23,858 schemes, 64 fund houses, 2016-01-01 .. 2026-10-18** (plus 148 single pre-2016 points from the current-NAV file). Load faults found and fixed: the portal answers HTTP 200 with an HTML page
  (or hangs) for some quarter-sized windows, which the loader had stored as "landed" and then skipped on every retry - the adapter now rejects non-data pages, splits a failing window, and the backfill uses monthly
  windows from 2023-04; one landed quarter (2020 Q1) had never been loaded, which showed up as 56 weekdays with no NAV and 9,974 schemes with false gaps (now 0 missing weekdays; 207 schemes with a gap > 30 days,
  198 of them dormant IDCW lines).
* **Validation gate (`data/reports/funds/amfi_validation.md`): PASS WITH FINDINGS.** No duplicate (scheme, date) rows (unique key); 0 non-positive NAVs; classification rule-consistency checks all 0; 1,895 growth/IDCW
  families paired - the IDCW line's 3-year NAV return is no higher than the growth line's in 99.8% (median 12.2% shortfall), so the labels behave as stated; independent re-computation of 1,248 return values: 0 mismatches;
  no annualised value below 1Y. The former 4,157-scheme `other` bucket is now 188 `other` (vehicle known, underlying not determinable) and 9 `unclassified`, each with a stored reason; the rest sit in explicit groups
  (fixed-term 8.4k, standard 7.5k, index 1.4k, closed-end 1.3k, liquid/overnight 1.1k, ETF 632, fund-of-funds 506, arbitrage 386, solution-oriented 169, interval 90). Open findings: 2,305 schemes (9.7%) have no
  growth/IDCW flag (mostly ETFs, which have no option, and inactive fixed-term plans; ETFs get an explicit "price return" basis and are not ranked with growth lines); 3 active growth schemes (IL&FS infrastructure
  debt series) have a 92-day NAV gap.
* **Return rules added from the validation:** 409 single-day NAV moves above 50% exist (unit consolidations, face-value changes, bad prints; e.g. an overnight fund's NAV rose ~9x in a day in 2022). A return horizon
  whose window contains such a move is dropped rather than reported (`MAX_DAILY_MOVE`); annualised figures only from 1Y; IDCW and ETF lines are labelled price return.
* **BSE replay idempotency confirmed:** a second replay of 2,672 landed files left `bse_eod_raw` unchanged (9,720,416 rows; identical sums of close and volume).

### Fourteenth review (2026-10-20): funds page, live compare, alerts

* **Owner order followed:** funds page completion, then live compare, then alerts. Fund analytics stay a separate layer (nothing feeds the equity universe, defensive signal or decision cards); the three IL&FS series with a
  92-day NAV gap and the 2,305 schemes without a growth/IDCW flag are flagged in the product, not treated as clean.
* **Funds page.** Derived table `mf_fund_snapshot` (migration 046, rebuilt by `scripts/fund_snapshot.py` after each NAV load; 8,685 active / recent schemes: 7,640 data ok, 944 caution, 101 poor; 3,339 with peer
  context). Basis labels are explicit and never mixed: `total_return_after_expenses` (growth), `price_return_only` (IDCW / payout), `price_return` (ETF), `option_unknown`. Every horizon carries a status
  (`ok`, `insufficient_history`, `stale_start`, `nav_discontinuity`) with the reason: no extrapolation. The data-quality badge (ok / caution / poor) comes from unit-change moves, NAV gaps, short history and
  classification uncertainty. Peer context is like-for-like only (same asset class, category and plan; growth lines, or ETFs of one asset class), needs 8+ peers and excludes `poor` schemes; the benchmark
  line is the NIFTYBEES ETF price return and is labelled a proxy. Filters: fund house, category, vehicle, plan, growth / IDCW / ETF, status, data quality, sort. Not added (no reliable source): expense ratio,
  holdings overlap, AUM.
* **Live compare.** Read-only API and page over the existing append-only ledger: lineage, forecast (only if the model logged one), entry rule, estimated exit window, outcome (shown only after the resolver's
  data cut-off reached the exit date), realised-vs-expected (only against a logged expectation), rank IC, economics net of the registered cost model, calibration, score drift, kill switch, retrain candidate.
  New ledger guard: a prediction stamped after the next session's open is rejected (the entry price would already be known). A `model_registry.json` carries the state (`research` / `paper` / `approved`; unknown
  is `research`). Forward source: `scripts/log_cards.py` logs the whole defensive signal universe per session at 5 and 20 sessions, with the decision-card size, as model `defensive_cards_v1` in shadow mode and
  with no forecast fields (the model makes no return forecast). Today's signal (2026-10-16) could not be logged: its entry open had passed, so the ledger is empty until the first end-of-day load after this change.
* **Alerts.** In-app only (migration 047; nothing is sent outside the application, nothing can place an order). Rules in `qsde/alerts/rules.py` with thresholds in `infra/data/alert_rules.json`: data staleness
  (NSE, BSE, AMFI), source failures, corporate-action state (events applied in research only, unresolved candidates, ex-dates around signal-universe names), model health (kill switch, degradation, score drift,
  missed / due logging), Kite session, portfolio risk (single-name and top-5 concentration, average correlation, pro-forma drawdown, high-risk share in a risk-off regime), and a visible alert when a check itself
  cannot run. Alerts are derived: a persisting condition is one alert (refreshed), a cleared condition resolves, an acknowledgement survives refreshes unless the severity worsens. Current open alerts on the real
  data: 12 unresolved corporate-action candidates, 151 research-only events, logging not started, no Kite session.
* **Daily routine after the end-of-day load:** `scripts/defensive_eval.py --latest`, then `scripts/log_cards.py` (before the next open), `scripts/live_loop.py resolve`, `scripts/fund_snapshot.py`, `scripts/run_alerts.py`.
* **Still open for the owner:** Bucket A decisions, key rotation, the push, the F&O source (Kite after login, plus a historical-chain archive source) and a BSE corporate-action source.

### Fifteenth review (2026-10-20): Bucket A decisions, price-series truth, BSE actions, NSE F&O archive

* **Finding: two corporate-action truths.** The official `ohlcv` series (what the cards, defensive signal, portfolio page, outcome resolver and fund benchmark read) applies 666 NSE-source-confirmed events, including all 69 Bucket A
  events; the governed registry `sbadj_v1` (508 events) excludes Bucket A and nothing reads it. By exact (symbol, ex-date) key, 210 events are applied in `ohlcv` but not in the registry (69 Bucket A, 137 `needs_review`, 2 rejected,
  the rest unmatched) and 52 registry events are not applied in `ohlcv`. The decision-card transparency text had wrongly described `sbadj_v1` as the series in use: corrected.
* **Owner decision:** keep `ohlcv` unchanged until all 69 Bucket A events are individually adjudicated (excluding them now would create false cliffs, e.g. a 90% drop in TATAINVEST inside today's lookback);
  then reconcile the registry with the approved set, rebuild `ohlcv` ONCE, version the result, retain the prior series, and produce before/after cliff and return-continuity reports. Until then `ohlcv` is labelled
  `official_prices_with_pending_corp_action_review` and `sbadj_v1` `governed_research_series_not_production` (System page, transparency panel, alerts). Decisions are recorded in
  `infra/data/corp_action_bucket_a_decisions.json` (the hard bounds of `owner_approved` are not weakened): **6 confirm** (ENGINERSIN, BEL, ASTRAL, INOXWIND, CUB, TATAINVEST), **4 hold** (BLS x2, RITES, JINDWORLD), 59 pending
  (packet generated: 26 confirm / 29 needs_more_evidence / 4 reject recommended). The prediction-ledger lineage for the shadow model now names the `ohlcv` state, not the registry (changed before the first prediction was logged).
* **BSE corporate actions.** BSE's JSON API rejects automated clients (HTTP 403, bot protection): not circumvented. Evidence-first pipeline for BSE's own export, downloaded by the owner: raw landing (SHA-256, `known_at`), parse with
  every bad row reported, content-hashed storage, reconciliation with the NSE v3 hard bounds into confirmed / needs_review / rejected, nothing adjusted (migration 048). The first real file is refused until its column mapping has
  been reviewed in a dry run (the mapping was written without seeing BSE's actual file). BSE adjusted prices come only after validation and owner approval; BSE-only names stay out of universe, signal and cards.
* **NSE F&O archive** (migration 049): the official derivatives bhavcopy carries every listed contract and strike per session, so it is a full historical chain (not only expiry level). Two formats (legacy through 2024-07-05, UDiFF from
  2024-06-28; on their six overlap sessions settlement and open interest agree on 100% of 15-16k contracts). Loaded 2016-01-01 .. 2026-10-19: **2,667 sessions, 35.1M contract rows** (idle contracts with no OI and no volume are
  counted per session, not stored). Gate: PASS WITH FINDINGS (`data/reports/fno/fno_validation.md`): no integrity violations; NIFTY / BANKNIFTY futures carry open interest every session; expiry weekdays follow the known
  Thursday-to-Tuesday move; near-month stock-futures basis median +0.20% (52 observations beyond 5%). Gaps: 2021-03-30 has no file in NSE's archive (404); four earlier gaps (three Saturday special sessions and one lost to a network
  drop) were recovered. Raw prices are not adjusted: NSE re-bases a contract on a corporate action. Implied volatility is not published and will be derived in a labelled research step. Kite (instrument master, expiry calendar, live
  quotes) is not enabled: no session and no key in `.env`.
* **Operations:** `scripts/daily_after_close.py` runs the daily routine in order (not yet run end to end on a live day); CI workflow added (`.github/workflows/ci.yml`, not yet run on GitHub); `docs/OWNER_CHECKLIST.md` lists what only the
  owner can do. gitleaks re-run after the changes: history (58 commits), reports, signals, features and raw landing clean; the only working-tree hit is the local gitignored `.env` JWT secret. Key rotation is the owner's and is pending, so nothing is pushed.

### Sixteenth review (2026-10-23): push, CI, BSE corporate-action evidence

* **Rotation reported done by the owner** (provider side, not verifiable from here; the workspace `.env` still held placeholders until the owner entered the Kite key and secret). gitleaks re-run: history (58 commits), working tree,
  reports, signals, features and 3.9 GB of raw landing files clean; the only hit is the local gitignored `.env` JWT secret. Nothing sensitive is tracked. `feat/stoxsy-v2` pushed to the PUBLIC repository (45 commits ahead of `main`).
  `main` is protected: pull request required, no force-push or deletion, and the three CI jobs (gitleaks on full history, frontend types / tests / build, backend tests on an empty TimescaleDB seeded from `infra/init.sql` then the
  migrations) are required checks. Both CI faults found on the first real runs were mine and are fixed (base-schema bootstrap; checksum file name). Tests that need the loaded datasets are marked `real_data` and run locally only.
* **BSE corporate actions loaded** from the owner's download of BSE's Corporate Actions page (22,065 rows, 22,060 distinct actions, 0 unreadable; raw file landed with SHA-256). The page's ten columns matched the parser exactly (the
  mapping review gate worked as designed). First reconciliation (v1) rejected 666 actions, including 425 of 861 bonuses: investigation showed the reconciler was wrong, not the data. Two defects fixed (v2): (1) several actions on one
  ex-date (a bonus AND a split, e.g. BAJFINANCE 2025-06-16, BAJAJFINSV 2022-09-13) move the price once by the PRODUCT of their factors and are now tested together (91 same-day groups); (2) `rejected` is now reserved for prices that
  contradict the listing (observed gap more than 25% from the listed factor); a gap that matches but fails persistence / untraded-session bounds, or sits 5-25% off, is `needs_review`. Also typed: consolidations and sub-divisions
  listed without terms, InvIT / REIT distributions, suspensions, meetings and book closures (not price-affecting).
* **Result (v2)**, price-factor actions (bonus / split / consolidation, 1,608): **1,001 confirmed** (all hard bounds pass), 371 needs_review with the gap 5-25% off the listed factor (the BSE analogue of Bucket A), 158 gap matches but
  persistence or untraded-session check failed, 55 terms not readable (factor must come from the company announcement), 41 no BSE bars near the ex-date, **23 rejected** (prices contradict the listing). Dividends: 18,006 confirmed
  (amount readable, yield <= 25%, scrip traded after the ex-date: a sanity check, not a price test), 118 needs_review. Rights (568), demergers / schemes (242) and buy-backs (376) always need review. Nothing is adjusted.
  Still required before any BSE adjusted series: event-level review of the 371 near-miss events (same packet process as Bucket A), a validated BSE adjusted-price materialisation, and owner approval.

## Out of scope here

Kite backfill/live (needs a session and the owner's go-ahead for any network call), a verified corporate-actions
source for fundamentals/news, review of the 187 price-only split events and the rights/demerger worksheets, dropping `factor_pit` (after the
first validated full snapshot).
