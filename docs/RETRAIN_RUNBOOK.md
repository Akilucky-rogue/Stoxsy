# Retrain Runbook (Session 2 Correctness Series)

Follow this after Session 2's Stage 2A–2C.3 fixes land, to restake every
downstream artifact (factor_pit rows, ML weights, edge_stats) on the
corrected pipeline. Do this ONCE before the next paper-vs-live promotion
attempt.

## Preconditions
- Migrations 017–021 all applied against your qsde Postgres.
- All Stage 2A–2C.3 tests green.
- Kite historical OHLCV backfilled for the tradeable universe.

## Sequence

### 1. Apply migrations
```powershell
# From repo root
python backend/scripts/apply_migrations.py
```
Verify each of 017, 018, 019, 020, 021 lands with no errors.

### 2. Recompute all factors with fracdiff at ingest (C1)
```powershell
# Full backfill: compute_factors.py writes the whole history for every symbol,
# tagged factor_version='ffd_v2' (see backend/qsde/factors/versions.py).
cd backend
python scripts/compute_factors.py --start 2018-01-01        # --symbols A,B to limit
```
(`compute_factors.py` has no `--all` / `--write-tail-days`. The daily job
`daily_eod.py --write-tail-days N` persists only the last N days, but it
differences the FULL history first, so tail rows equal backfill rows.)

Success signal in the log:
```
Persisted N individual factor records to factor_pit (version=ffd_v2).
```
If you see `version=raw_v0`, fracdiff at ingest failed (it runs in `strict`
mode, so the error is logged at ERROR level just above) and training/serving
will ignore those rows. Rows tagged `ffd_v1` are retired and never read: they
came from a tail-only differencing step with a different kernel.

The fixed kernel is 282 sessions wide (d=0.4, thres=1e-4), so the first 281
sessions of every symbol have no differenced level-form features, and symbols
with fewer sessions have none at all. That is intentional
(`frac_diff_ffd(auto_widen=False)`); it is why Phase 1 backfills 5-7+ years.

### 3. Retrain all horizons on the corrected pipeline
```powershell
# QSDE_PROMOTION_COST_BPS gates on dsr_net (C3). 10 bps is the intraday MIS
# default; use 25-30 bps if you're retraining swing/long delivery models.
$env:QSDE_PROMOTION_COST_BPS = "25"
python backend/scripts/retrain.py --horizons intraday swing long
```
Expected behavior:
- `_evaluate_oos_with_costs` runs and prints `dsr_gross` and `dsr_net`.
- Only models where `dsr_net >= should_promote.threshold` get promoted.
- Anything else stays in candidate — that is the correct, honest state.

### 4. Verify the gate
```powershell
python -c "from qsde.models.lgbm_model import _evaluate_oos_with_costs; help(_evaluate_oos_with_costs)"
# Confirms the gate uses net-of-cost DSR.

python -m pytest backend/tests/test_stage_2c3_c3_costs.py -v
# All C3 regression tests must pass.
```

### 5. Drop legacy raw_v0 rows (optional, once ffd_v1 verified in live)
```powershell
# ONLY after you're confident the ffd_v1 rows are complete and live signals
# are stable. This reclaims disk space.
psql qsde -c "DELETE FROM factor_pit WHERE factor_version = 'raw_v0';"
```

## Rollback

If the retrained models produce worse live paper P&L than the pre-retrain
gross-trained ones over a 30-session window:
1. Restore the previous weights from git history (`weights/lgbm_*.txt`).
2. Investigate: is the DSR bar too aggressive? Are new features NaN too often?
3. Bump `QSDE_PROMOTION_COST_BPS` down and retrain — some models may need
   a lower net threshold to clear.

## What each bugfix delivered

| Bug | Effect on live signals |
|---|---|
| **C1** | ML predictions now use fracdiff'd features at both train and serve → tree splits route correctly. Live signal ranking matches CV ranking. |
| **C2** | Fundamentals no longer stamp current PE/ROE into historical rows → training doesn't see 2026 ratios in 2018 data. |
| **C3** | Promotion gate uses `dsr_net`. No promotion on gross Sharpe that misrepresents the paper track record. |
| **C4** | PIT queries return honest as-of-date data. `pit_query()` becomes real, not decorative. |
