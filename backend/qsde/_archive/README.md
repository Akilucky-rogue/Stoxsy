# Archived modules

Moved here on 2026-10-31 (clean-up approved by the owner). Nothing imports them (read-only scan of the code, tests, scripts and docs), so they are not part of the package any more.

- `nse_bulk_deals.py`, `nse_flows.py`: older ingestion code; the current bulk-deal and flow handling is in `qsde/stream/adapters`.
- `feature_view.py`: an unused model feature view.
- `triple_barrier.py.pre_2c1_backup`: a backup of `qsde/models/triple_barrier.py` from before the Stage 2C.1 fixes.

To restore one: `git mv backend/qsde/_archive/<file> <its old place>`.
