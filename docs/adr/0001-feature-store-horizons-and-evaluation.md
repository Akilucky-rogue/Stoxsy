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
3. **Pre-existing paper trades and ideas** logged before migration 027 have no `ref_price`; their levels stay absolute and
   they are resolved on the old basis. Do not mix them into harness comparisons.
4. **Promotion** remains paused (`QSDE_ML_PROMOTION_ENABLED=false`) until a harness-based gate exists and a human signs off.

## Out of scope here

Kite backfill/live (needs a session and the owner's go-ahead for any network call), a verified corporate-actions
source (dividends, rights, symbol changes, demergers) and `total_return` vs `price_return`, dropping `factor_pit` (after the
first validated full snapshot).
