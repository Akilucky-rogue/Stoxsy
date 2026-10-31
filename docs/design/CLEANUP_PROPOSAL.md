# Clean-up and structure (2026-10-31): approved and executed on branch `chore/restructure`

**Outcome.** Groups 1 to 4 were executed as approved, with these differences found by checking for live references before moving: 26 of the 41 candidate scripts moved (10 stay because live code imports them or a documented usage note names them, and the 3 hand-run tools stay until the owner says which he still runs); `MCX_ENGINE_SCOPE.md` is archived with a banner saying it is deferred, not obsolete. Verified after the moves: CI-equivalent suite 975 passed, 20 skipped (identical to before), web 50 tests, types and build clean, nightly routine still 16 steps with every script present. Group 5 (secrets, Gemini key) is the owner's. The original proposal follows.

---

# Original proposal: nothing had been moved yet

Owner's choice: **reorganise and archive; delete nothing without an approved list**, done on its own branch. This file is the list for approval. Every move uses `git mv` so history is kept, and every move can be reversed.

## Rules for the work
1. One branch (`chore/restructure`), one pull request, small commits (one per group below).
2. No deletion. "Archive" means moved into a folder whose name says it is archived, with a short README saying why and when.
3. No change to the data folder (`C:\Stoxsy\data`), the database, or anything the nightly routine runs.
4. After each group: the CI-equivalent suite (empty database) and the web tests must pass, and the nightly routine's step list must be unchanged. A group that breaks either is reverted, not patched around.
5. Docs are corrected in the same pull request, so the structure and the descriptions agree.

## How the candidates were found
A read-only scan counted, for each script, how many tracked files mention it (nightly routine, command line, docs, tests, other code). It is a heuristic: a script run only by hand from a terminal looks unreferenced. **So these are candidates to review, not verdicts.** Each line below needs your yes or "keep".

## Group 1: scripts (`backend/scripts`, 93 files)
Keep in place: the 11 the routine or the command line runs, and the 35 that docs or tests refer to. Nothing in the routine, `stoxsy`, the runbooks or the tests changes path.

Move to `backend/scripts/_archive/` (a folder that already exists for this purpose) if you agree:

**28 referenced by nothing:**
amfi_validation_report, audit_db, audit_universe, backfill_report, build_adjustment, build_modes_explainer, build_symbol_map, ca_bucket_a_decision_packet, ca_bucket_a_rebuild_preview, ca_bucket_a_visual_packet, clean_universe, expand_universe_yf, fix_misaligned_metas, fno_validation_report, kite_login, load_nse_equity_master, precision_audit, print_research_json, promote_official_confirmed, reclassify_funds, reconcile_ca_history, run_harness_pilot, test_db, upgrade_universe, verify_kei_e2e, verify_ml_train, verify_non_equity, xref_evidence_packs.

Likely keep (look unreferenced but are run by hand): `kite_login`, `load_nse_equity_master`, `reclassify_funds`, the `*_validation_report` scripts. Say which you still run.

**13 referenced by other code only (older scripts calling each other):**
ca_bucket_a_packet, compute_rule_ic, compute_rule_signals, expand_universe_nifty500, kite_daily_refresh, kite_seed_ohlcv, load_nse_symbol_changes, ohlcv_snapshot, reconcile_v3, refresh_india_data, run_signal_loop, seed_demo_session, test_research_endpoints.
These belong to the older pipeline (`kite_daily_refresh` and `kite_seed_ohlcv` write `ohlcv`, which the nightly rebuild also writes). Proposal: archive them with a note; they are not part of the current routine.

## Group 2: the old web app and old guidance (repo root)
| Item | Today | Proposal |
|---|---|---|
| `frontend/` (36 files, older Next.js app) | CI builds `web/` only; only an allowlist line and old docs mention `frontend/` | `git mv` to `archive/frontend-next/` with a README; the real app is `web/` |
| `HANDOVER.md`, `START.md`, `run.ps1`, `MCX_ENGINE_SCOPE.md` | describe the earlier build (migrations 001-016, the old frontend, a different launch path) | move to `docs/archive/` with a "superseded" banner; replace START with a short, correct `docs/START_HERE.md` |
| `CLAUDE.md`, `README.md`, `ARCHITECTURE.md` | still describe Celery, Telegram, a Nifty 200 universe, a mandatory `factor_pit`, and the old frontend | rewrite to match reality (this is the file AI sessions read first, so it matters most) |
| `live_test.json` (June paper-trade example, no credentials) | tracked in the public repo | archive, or delete if you agree |
| `backend/qsde/models/triple_barrier.py.pre_2c1_backup` | leftover backup | archive |
| 3 modules nothing imports: `qsde/ingestion/nse_bulk_deals.py`, `qsde/ingestion/nse_flows.py`, `qsde/models/feature_view.py` | unused | move to `backend/qsde/_archive/` with a note |

## Group 3: docs (`docs/`)
Keep `design/`, `runbooks/`, `adr/`, `legal/`. Add `docs/README.md` as an index. Move `AUDIT_v1.md`, `AUDIT_v2.md` and `docs/legacy/` under `docs/archive/` (they are history, not guidance). Fix `docs/MIGRATIONS.md`, which stops at migration 001 and shows an old password.

## Group 4: loose files outside the repo (`C:\Stoxsy`)
`pip_install.err`, `pip_install.log`, `robocopy_data.log`, `robocopy_reference.log`, `missing_pkgs.txt` are leftovers from moving the project here. Proposal: move into `C:\Stoxsy\tools\old-logs\`. `reference\` (330 MB) and `tools\` (30 MB): I have not looked inside; I will list their contents for you before suggesting anything.

## Group 5: things only you can do
- `data\secrets\owner_bootstrap.txt` holds plaintext secrets. Move them into a password manager and delete the file. HANDOVER lists API keys that were never rotated (`docs/SECURITY_ROTATION.md`).
- Create the Gemini key for this project and put it in `.env` as `GEMINI_API_KEY` (git ignores `.env`). Do not paste it anywhere else.

## Target structure after the clean-up
```
stoxsy/
  backend/  api/  qsde/ (+ _archive/)  scripts/ (+ _archive/)  tests/
  web/                  the app
  infra/                migrations, compose, config
  docs/  design/ runbooks/ adr/ legal/ archive/  README.md
  archive/frontend-next/
  README.md  CLAUDE.md  pyproject.toml  docker-compose.yml
```
A new `qsde/advisor/` (the language-model layer) is added later, in its own pull request, on top of this structure.

## What I need from you
Approve the groups (or strike items): especially Group 1's "likely keep" list and the 13 older scripts, and whether `live_test.json` may be deleted rather than archived.
