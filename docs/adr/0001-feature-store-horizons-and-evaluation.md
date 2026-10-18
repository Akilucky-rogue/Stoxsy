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

## Out of scope here

Full-universe factor generation (after corporate-actions evaluation), Kite backfill/live (needs a session and
the owner's go-ahead for any network call), dropping the 2.1 GB pilot rows from `factor_pit` (ask first).
