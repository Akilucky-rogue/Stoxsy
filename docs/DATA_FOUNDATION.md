# Data foundation (rebuilt 2026-10-18)

The original TimescaleDB volume could not be recovered (no account can start the Docker engine that held it;
the 50 GB `docker_data.vhdx` is untouched in `D:\Users\NEW\AppData\Local\Docker\wsl\disk\` for an optional later
recovery of paper trades / idea-log). Prices were rebuilt from the local NSE bhavcopy archive.

## Pipeline

```
data/stocktrack_data/cache/historical/nse/{old_legacy,legacy}/parsed/YYYY-MM-DD.parquet   (2016-01-01 .. 2026-04-30)
   -> qsde/ingestion/nse_eod.py      normalise + validate + idempotent upsert   -> eod_raw      (RAW, unadjusted)
   -> qsde/ingestion/corp_actions.py detect split/bonus/consolidation          -> corp_action_events
                                      adjust prices                             -> ohlcv  (source nse_bhavcopy_adj_v1)
   -> qsde/universe/pit.py           trailing-60d median traded value top-500  -> universe_pit  (point in time)
   -> qsde/factors/engine.py         factors + fixed-kernel fracdiff (ffd_v2)  -> factor_pit  (only universe symbol-days)
```

Commands (from `backend/`, venv at `D:\Stoxsy\.venv`):

```
python scripts/load_bhavcopy.py --start 2016-01-01 --end 2026-04-30 --rebuild-ohlcv
python -c "from qsde.universe import pit; print(pit.build_from_db())"
python scripts/compute_factors_pit.py --pilot            # 46 symbols; or --min-adv 10 for everything
```

## What is in the database now

| Table | Rows | Notes |
|---|---|---|
| `eod_raw` | 4,781,686 | 3,696 symbols, 2,551 sessions, EQ/BE/BZ series, raw prices + delivery % |
| `ohlcv` | 4,781,686 | split/bonus-adjusted; `adj_close == close` (no dividend adjustment) |
| `corp_action_events` | 1,327 | 1,195 applied (SPLIT_BONUS), 132 UNEXPLAINED (not applied, mostly demergers) |
| `universe_pit` | 1,150,000 | 500 symbols/day, 2017-01-09 .. 2026-04-30, 1,360 symbols ever |
| `factor_pit` | 8.2M (pilot) | `ffd_v2`, 46 symbols, 95 factors (tech 44, smc 45, pattern 6) |

## Findings that shaped the design

1. **The date inside the data is authoritative, not the file name.** On market holidays the downloader saved
   the previous session's file under the holiday's name (e.g. `2026-04-03` holds 2026-04-02). Nine such files were
   found in 2026 alone; the loader reports them as `stale_files`.
2. **PREV_CLOSE is NOT adjusted on ex-dates in this archive** (464 of 465 large drops). Corporate actions are
   therefore inferred from the overnight price gap (open vs previous close) matched to a short list of common
   ratios, with persistence and day-range checks (`price_gap_fingerprint_v1`).
3. First version of the detector over-fitted (243 false "consolidations" on Rs0.10 stocks; a 1:5 split snapped to a
   "7:2 bonus"). Fixed with a price floor, a rights/warrant exclusion and a short common-ratio list; verified on
   174 liquid events (open lands within 4% of a common ratio 83% of the time, within 6% 93%).

## Known limitations (not hidden)

* **Mild adjustments are invisible**: bonuses/splits with ratio 0.80-0.97 (e.g. 1:5, 1:10 bonus), rights issues and
  cash dividends are not adjusted. A real corporate-actions source is required to close this.
* **Demergers / mergers / symbol changes** (VEDL 2026-04-30, PEL 2022, ABFRL 2025, TATAMOTORS->TMPV) stay as visible
  cliffs (`UNEXPLAINED`). A renamed symbol leaves the universe under its old name.
* Rank-500 liquidity is only ~Rs1-5 cr/day in 2017-2023 (Rs14+ cr from 2024); tradeability still needs the ADV filter.
* **Storage**: factor_pit long-form costs ~256 bytes/row. The pilot (8.2M rows) is 2.1 GB; the full Rs10cr+ universe
  (833k symbol-days x 95 factors = ~79M rows) would be ~20 GB and ~110 min of writing. A wide/parquet feature store
  is the proposed alternative (see HANDOVER).
* `fund_*`, `flow_*`, `macro_*`, `sent_*` factor families are empty (no sources loaded yet).
* `tech_obv_slope` is in share units after differencing (max ~5e7); harmless for trees, must be scaled for DL.
