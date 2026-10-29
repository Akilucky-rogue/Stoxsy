# AMFI TER: progress tracker

Spec: `docs/design/AMFI_TER_DESIGN.md`. Rules: real data only, no synthetic data; latest month first; nothing is built against a layout that has not been seen; a milestone is done only when its acceptance check has been run and the result recorded here. Contains no AMFI data (public repo): counts and header names only.

Status key: TODO, ACTIVE, BLOCKED (waiting on someone), DONE (acceptance met, evidence recorded).

| # | Goal | Acceptance check (must be run, result recorded) | Who | Status |
|---|---|---|---|---|
| M0 | A real AMFI TER file in hand | At least one real `.xlsx` or `.csv` is in `C:\Stoxsy\data\inbox\amfi_ter\` (latest complete month, all fund houses) | Owner | **ACTIVE** (waiting for the file) |
| M1 | Real layout known | Read-only inspection recorded below: sheets, header row, every column name and type, row count, date format, whether a scheme code / ISIN exists, how plans are represented, banner rows | Claude | TODO |
| M2 | Plan approved | Implementation plan written from the real layout (not from assumptions) and approved by the owner | Claude, Owner | TODO |
| M3 | Reader works on the real file | Dry run: every TER column mapped; parsed + rejected = source rows (no silent drops); no data written | Claude | TODO |
| M4 | Storage and loader | Migration 062 applied to the live database (owner OK asked first); layout approved with `--mapping-ok` after the owner reviews the dry run; real file loaded; re-running skips it; row counts reconcile | Claude, Owner | TODO |
| M5 | Matching and coverage (roadmap gate 1.1) | Matched / unmatched counts against `mf_scheme_master`, every unmatched row has a reason, coverage of in-scope schemes reproducible by hand, report in `data/reports/amfi/` | Claude | TODO |
| M6 | Operator surface | `stoxsy doctor` shows the TER line; daily routine has the inbox step (shown by `--dry-run`, then one real run); runbook written and followed once | Claude, Owner | TODO |
| M7 | More months | Each further month loads; overlap and restatement counts recorded; history range stated | Owner (downloads), Claude | TODO |
| M8 | Part B: API and Funds page | `ter` in the funds API and page; for at least 10 real funds the displayed values match the downloaded file by eye (owner checks 3) | Claude, Owner | TODO |
| M9 | Part C: cost ranking | Migration 063 applied; one real peer group's percentiles reproduced by hand; cases with no real instance listed as "not covered" | Claude, Owner | TODO |

## Layout facts (filled at M1; header names and types only)
Not yet known.

## Decision log
| Date | Decision | By |
|---|---|---|
| 2026-10-30 | Source: AMFI TER page, manual download only (AMFI terms: personal non-commercial use, no bulk storage, nothing not agreed in writing) | Owner |
| 2026-10-30 | Read both `.xlsx` and `.csv` | Owner |
| 2026-10-30 | Scope: ingestion (A), API and Funds page (B), cost-aware peer ranking (C), in that order | Owner |
| 2026-10-30 | Real data only, no synthetic data; tests are `real_data` and run locally | Owner |
| 2026-10-30 | Latest month first, then decide how far back to go | Owner |
| 2026-10-30 | Progress tracked in this file plus a summary after each step | Owner |

## Open questions
- Does AMFI's download carry a scheme code or ISIN? (M1)
- How far back does the month selector go, and is "All fund houses" in one file allowed or must it be per fund house? (M0/M1)
- In-scope definition for the coverage gate: schemes with a NAV in the last 90 days, growth and IDCW lines. Confirm once the unmatched list is seen. (M5)

## Log
- 2026-10-30: spec written and committed locally; inbox folder created at `C:\Stoxsy\data\inbox\amfi_ter\`; `openpyxl` confirmed installed in the project environment.
