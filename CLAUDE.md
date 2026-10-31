# CLAUDE.md: context for AI-assisted work on Stoxsy

Rewritten 2026-10-31 to match the code. The earlier version is `docs/archive/CLAUDE_before_2026-10-31.md`.

## What this is
**Stoxsy** (internal codename `qsde`): a private research and decision-support assistant for Indian markets (NSE, BSE) and mutual funds. It keeps its own data fresh, ranks risk, and is being built toward a personal-advisor experience for its owner (goals, ranges, a monthly check-in, plain-language answers). **It never places an order and nothing it shows is investment advice.** Read first: `docs/design/PLATFORM_ROADMAP.md` (plan and the owner's recorded decisions), `docs/design/SYSTEM_FLOW.md` (how it works today), `ARCHITECTURE.md` (principles and code map), `README.md` (how to run it).

## Rules that always apply
1. **Real data only.** No synthetic data in the pipeline. A test that needs data reads the real database and is marked `real_data` (local only; CI has an empty database and skips them). A test that must exercise a path no real file has produced (for example a revision) changes a stored value inside a rolled-back transaction; it never creates fake source data.
2. **Raw first, point in time.** Save external bytes unchanged (`data/landing`), then parse; stamp `known_at`; never read data from the future of its own date. Features are read through the Parquet feature store (`qsde.features.store`, ADR 0001); the `factor_pit` table is legacy and kept only as a parity reference.
3. **Never correct a published value.** Store it as published; problems become flags with plain-language reasons. If a source later changes a value, the first value stays and the change is kept as a revision (`mf_ter_revisions` for expense ratios).
4. **Honest evidence.** Deflated Sharpe net of costs is the only promotion metric (raw Sharpe is diagnostic). Purged cross-validation with an embargo for any model trained on returns; plain k-fold on return series is banned. Transaction costs are non-optional (about 5-8 bps round trip for large caps, 12-20 bps for mid caps). No model is promoted today; promotion is switched off and changes only by the owner's recorded decision.
5. **Research and decision support only.** No order placement. Any language model (planned: Gemini, public data only at first) may only explain numbers the system computed: every number in its text must appear in its input, no buy or sell wording, outputs labelled AI-written and logged. It never produces signals, sizes or promotions.
6. **The repository is public.** Never commit `.env`, keys, tokens, broker files, personal data, market data files or AMFI rows. Secrets live in `.env` (git-ignored) or a password manager; never paste a key into chat or a document.

## Ask the owner before
Pushing or merging; applying a migration to the live database; sending any data to an external service; adding a network data source; deleting anything (archive instead, with a README); changing a recorded decision in the roadmap. Local commits and branches are fine.

## How to work here
- **Test first.** Write the failing test, watch it fail for the right reason, then implement; run the suite before claiming done. Keep progress in the relevant `docs/design/*_PROGRESS.md` and summarise each step to the owner.
- **Small modules, clear docstrings, type hints.** Python 3.11+ (venv `C:\Stoxsy\.venv`), PEP 8, factor columns `snake_case` with a category prefix. NSE tickers have no `.NS` suffix. Times are tz-naive IST; money is INR.
- **Migrations** are `infra/migrations/NNN_*.sql`, additive and idempotent, applied by `backend/scripts/apply_migrations.py`. Next free number: 066.
- **Retired code** goes to an `_archive/` folder with a README (`backend/scripts/_archive`, `backend/qsde/_archive`, `archive/`), never straight to deletion.
- **Where things are:** nightly routine `backend/scripts/daily_after_close.py` (scheduled task "Stoxsy daily after close", Mon-Fri 19:15); CLI `backend/qsde/cli/main.py`; API `backend/api/routes/`; web app `web/src/`; runbooks `docs/runbooks/`.

## Running things (Windows)
```
cd backend
..\..\.venv\Scripts\python.exe -m pytest -q -m "not real_data"       # CI equivalent needs an EMPTY database (see below)
..\..\.venv\Scripts\python.exe -m pytest tests\test_funds_ter.py -q --import-mode=importlib -p no:cacheprovider
cd ..\web; npm test; npx tsc --noEmit; npm run build
```
- A CI-equivalent run uses a scratch database (`qsde_ci`) built from `infra/init.sql` plus every migration, and drops it afterwards. Run the long suites **one at a time**: a low-memory stop kills background runs.
- Git Bash rewrites `/tmp/...` paths (set `MSYS_NO_PATHCONV=1`); PowerShell quoting differs from bash: for anything long, write a script file and run it.
- The API needs the venv (`C:\Stoxsy\.venv`), not the system Python. `AUTH_ENFORCED` may be true in `.env`; the test conftest turns it off per test.
- Never start or stop the owner's running API on port 8700; use another port for checks.
