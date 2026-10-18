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
* **Not yet done for renames:** features are still computed per symbol, so a renamed stock's history is two series. Computing
  features on the stitched `security_id` series (and a snapshot keyed by security) is the next step after the candidates are decided.
* **Streaming foundation (S0) built, nothing enabled** (`qsde/stream/`, migration 031 `source_health`): adapter contract
  (fetch/parse/validate), raw-first landing zone (immutable, atomic, hash-verified), `known_at` stamping (live and backfill modes;
  never earlier than the fetch; corrections are new rows), runner with checkpoints advanced only after commit, health stores
  (memory, JSON, Postgres), alert sink with dedupe, due-time scheduler with backoff and market-hours-aware staleness, replay from
  landed bytes. 20 tests cover raw-first on parse failure, idempotence, corrections, replay equals live, checkpoint safety,
  failure escalation, staleness only while the market is open, and one failing source not stopping the others. No external
  polling and no Kite streaming is enabled.

## Out of scope here

Kite backfill/live (needs a session and the owner's go-ahead for any network call), a verified corporate-actions
source (dividends, rights, symbol changes, demergers) and `total_return` vs `price_return`, dropping `factor_pit` (after the
first validated full snapshot).
