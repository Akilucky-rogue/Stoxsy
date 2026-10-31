# Archived scripts

Scripts moved here on 2026-10-31 (clean-up approved by the owner, see `docs/design/CLEANUP_PROPOSAL.md`). A read-only scan found nothing in the nightly routine, the `stoxsy` command, the runbooks, the tests or any other code that mentions them.

- They are **kept, not deleted**. To bring one back: `git mv backend/scripts/_archive/<name>.py backend/scripts/<name>.py`.
- They are **not maintained and not guaranteed to run from here**: most were one-off research, review or repair tools, and some import their neighbours by bare name (which only works from `backend/scripts/`).
- Older files in this folder (`audit_query.py`, `check_indexes.py`, `create_active_index.py`, `test_research_endpoints.py`) were archived earlier.

Scripts that looked unused but are **still in place on purpose**:
- Run by hand: `kite_login`, `load_nse_equity_master`, `reclassify_funds`, the `*_validation_report` scripts.
- Imported by live code: `load_nse_symbol_changes` (the NSE adapter), and `compute_rule_ic`, `compute_rule_signals`, `kite_daily_refresh`, `refresh_india_data` (the legacy `daily_eod.py`).
- Named in documented usage notes: `expand_universe_nifty500`, `ohlcv_snapshot`, `reconcile_v3`, `run_signal_loop`.
- `test_research_endpoints` would collide with the archived file of the same name.

The legacy `daily_eod.py` chain (it writes `ohlcv`, which the nightly rebuild also writes) is a candidate for a later archive, but needs a separate decision because live code imports it.
