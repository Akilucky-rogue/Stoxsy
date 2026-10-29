# AMFI expense ratios (TER): manual-inbox ingestion, API/Funds page, cost-aware peer ranking

Status: DRAFT for owner review, 2026-10-30. Roadmap item 1.1 (`docs/design/PLATFORM_ROADMAP.md`). Three parts, built in order A, B, C; each part ends at its own acceptance checks.

## Decisions already made (owner, 2026-10-30)
- **Source: AMFI's "TER of MF Schemes" page, manual download only.** AMFI's terms of use allow personal, non-commercial use, forbid storing "any significant portion" electronically and any use not agreed in writing, and say nothing about automated access. The page's data comes from AMFI's own internal `/api/` routes, which are not a published API. No automated access is built; if AMFI gives written permission later, that is a new decision.
- **File format: support both** `.xlsx` (read with `openpyxl` when installed, otherwise a clear "save as CSV" refusal) and `.csv`.
- **Scope: all three parts together** (A ingestion plus daily inbox scan, B API and Funds page, C cost-aware ranking), one spec, built in order.
- **Real data only, no synthetic data anywhere.** Nothing is built against a guessed layout: the first step is the owner downloading a real month, and the reader is written against that file. Tests read the owner's real files from the inbox and are tagged `real_data` (they run locally; CI skips them). No AMFI rows are committed to the repo (public repo, AMFI storage terms). Pure logic is checked against the real file's own headers and values.
- **Load order: latest month first**, verified end to end and against the scheme master; earlier months only after the pipeline is proven.
- **Progress** is tracked in `docs/design/AMFI_TER_PROGRESS.md` (milestones, acceptance checks, status), updated as each is met and summarised at the end of each step.
- `openpyxl` is already installed in the project environment; `.xlsx` and `.csv` are both supported.

## What was verified and what was not
- Verified (2026-10-30): the page has five filters (financial year, month, fund type, category, fund house) and an Excel download; rows carry `D_BaseTER`, `D_TER` (direct), `R_BaseTER`, `R_TER` (regular) and `TER_Date`; the legal basis is SEBI circular 2018/91 as amended by 2019/42 (daily, scheme-wise, downloadable spreadsheet on AMC and AMFI sites).
- **Not verified: the layout of the downloaded Excel/CSV file.** Whether it carries a scheme code or ISIN, its header text, banner rows, and how far back the month selector goes are all unknown until the first real file is dry-run. The reader therefore uses tolerant header aliases and the reviewed-mapping gate (below). Part B and C consume `mf_ter` only, so a layout surprise is confined to Part A's reader.

## Part A: ingestion (foundation)

### Flow (same shape as `scripts/load_bse_corporate_actions.py`)
1. Owner saves each month's file in `<data>/inbox/amfi_ter/` (any file name, `.xlsx` or `.csv`).
2. `python backend/scripts/load_amfi_ter.py --inbox --dry-run` prints `column_mapping`, `unmatched_headers`, a `sample`, counts of parsed / rejected / matched / unmatched rows. Nothing is written.
3. The first file of each **layout** (hash of the normalised header row) is refused until the same command is run with `--mapping-ok`. The approval is stored per layout in `mf_ter_layouts`.
4. Load: raw bytes land unchanged (`LandingZone`, SHA-256, retrieval time, `known_at` = retrieval time, a conservative stand-in because AMFI's own publication time is unknown); rows are parsed; bad rows are listed, never silently dropped; a file with an already-loaded SHA-256 is skipped.

### Units (each testable alone)
- `qsde/ingestion/amfi_ter.py`, pure, no database: `read_export(payload, filename) -> Export` (frame, mapping, unmatched headers, issues, sha256, row counts); `normalise_name()`; `match_schemes(frame, master) -> (matched, unmatched)`; `validate(frame) -> findings`.
- `scripts/load_amfi_ter.py`: CLI and database writes (`--file`, `--inbox`, `--dry-run`, `--mapping-ok`).
- `infra/migrations/062_mf_ter.sql`.
- `qsde/ops/checks.py`: `check_fund_ter`. `scripts/daily_after_close.py`: one non-required step.

### Parsing rules
- `.xlsx` needs `openpyxl`; without it the loader refuses with "install openpyxl or save as CSV". `openpyxl` is added to `pyproject.toml` optional dependencies and the lock file (reads only; no macros or formulas are evaluated).
- Title and banner rows above the header are skipped (the header row is the first row containing a TER-looking column). Headers are matched by normalised aliases (case, punctuation, "(%)", plan words).
- Each source row yields up to two plan rows (regular, direct) with: `amc`, `scheme_name`, `plan`, `base_ter`, `total_ter`, `ter_date`; any other column (additional expenses, GST, B30, brokerage) is kept in `raw_row` JSON. Percent values are stored as percent numbers (1.25 means 1.25%).
- Dates are parsed explicitly (`dd-mm-yyyy`, ISO, Excel serial); an unreadable date rejects that row.

### Scheme matching (nothing guessed)
- Match to `mf_scheme_master` on normalised exact (AMC, scheme name, plan); if the file carries a scheme code or ISIN, that is used first and the name match becomes a cross-check.
- Anything else goes to `mf_ter_unmatched` with a reason (`no_name_match`, `ambiguous_name`, `plan_missing`). Fuzzy candidates are written to the report as suggestions only and are never applied.

### Storage (migration 062)
- `mf_ter_layouts(layout_hash PK, headers jsonb, approved boolean, approved_at)` (set only by `--mapping-ok`).
- `mf_ter_files(id, sha256 UNIQUE, filename, layout_hash, n_rows, n_parsed, n_bad, n_matched, n_unmatched, issues jsonb, retrieved_at, known_at, landing_path)`.
- `mf_ter(scheme_code, ter_date, plan, base_ter, total_ter, extras jsonb, file_id, known_at, PRIMARY KEY (scheme_code, ter_date, plan))`. `ON CONFLICT DO NOTHING`: the first-seen value stays; a different value for the same key is counted as a **restatement** in the file's `issues` and the report, not applied.
- `mf_ter_unmatched(file_id, amc, scheme_name, plan, ter_date, reason)`.
- Not a hypertable: one row per scheme, plan and month, so thousands of rows a month at most (row count per file is unknown until the first real file).

### Validation gates (reported, hard ones refuse the file)
- Hard (file refused, nothing written): no recognisable TER columns after mapping; more than 2% of rows with an unreadable date or non-numeric TER; any `total_ter` outside 0 to 5 percent.
- Findings (reported): `total_ter` above 2.5; `total_ter` below `base_ter`; for a scheme with both plans, direct `total_ter` not below regular; restatements; unmatched share.
- **Roadmap gate 1.1:** for the latest `ter_date`, every in-scope scheme in `mf_scheme_master` (active in the last 90 days, growth and IDCW lines) has a TER row or a listed reason. Output: `data/reports/amfi/amfi_ter.md` (counts, unmatched list, coverage, restatements, files).
- History starts at the earliest month the owner downloads; nothing is back-filled.

### Operator surface
- `docs/runbooks/amfi_ter.md`: exact download steps (page, filters, "Download Excel"), where to save, the dry-run then `--mapping-ok` sequence.
- Daily routine step `amfi_ter_inbox` (non-required): runs `load_amfi_ter.py --inbox`. Empty inbox: exit 0, message only. A file in an unapproved layout is **refused with exit 0 and a message** (a waiting review is not a pipeline failure); `doctor` shows it.
- `stoxsy doctor` line "fund expense ratios": latest `ter_date`, coverage percent, files waiting; WARN (never FAIL) when the latest month is more than 45 days old or a file is waiting for review.

### Tests (real data only; all tagged `real_data`, so they run locally and CI skips them)
Run against the owner's real files in `<data>/inbox/amfi_ter/` and the loaded database. They skip with a clear message when no real file is present. They check: the header mapping covers every TER column of the real file, banner rows skipped, both plans per row, no row silently dropped (parsed + rejected = source rows), out-of-range and unreadable values reported, idempotent reload (same SHA-256 skipped, row counts unchanged), restatement counting on a re-downloaded overlapping file, layout gate (refused, then approved), name match / ambiguous / unmatched counts add up, coverage figure reproducible by hand from the loaded rows, dry run writes nothing, raw file landed unchanged (hash equal).

### Acceptance (A)
With the owner's first real file: dry run shows a sensible mapping and sample, load succeeds, the report shows coverage against the 1.1 gate with unmatched rows explained, and the real-data tests pass locally.

## Part B: API and Funds page
- `GET /api/funds/schemes` and `/api/funds/schemes/{code}` gain a `ter` object: `{plan, base_ter, total_ter, ter_date, source: "AMFI", stale: bool, other_plan: {plan, total_ter} | null, annual_saving_pp: number | null}` (`null` when the scheme has no TER row). Query parameters `max_ter` and `sort=ter` are added to the list.
- `web/src/pages/Funds.tsx`: a TER column; `web/src/components/FundDetail.tsx`: a cost panel showing regular and direct total TER, the difference in percentage points and "about X rupees per 1 lakh per year" (difference times 1,00,000), each labelled "AMFI, as of <month>, indicative; excludes brokerage and transaction costs".
- A TER older than two calendar months is shown as `stale` and greyed, never hidden or silently reused as current.
- No recommendation wording ("best", "buy", "switch to direct" as advice). The page states the difference, not what to do.
- Tests (`real_data`): API tests against the loaded real `mf_ter` rows (a scheme with no TER, a stale month, both plans, filter and sort); component tests alongside `FundDetail.test.tsx` use values read from the real API response, not invented ones.
- Acceptance (B): suites green; one real fund checked by eye against the downloaded AMFI file.

## Part C: cost-aware peer ranking (not a return forecast)
**Correction to the roadmap wording.** A fund's published NAV is already net of its expenses, so historical NAV returns are already "net of cost". Subtracting TER from them again would double-count. "Net-of-cost ranking" is therefore defined here as a **cost position within a like-for-like peer group**, plus the **direct versus regular saving**, shown beside the existing (already net) returns.
- New pure module `qsde/funds/cost.py`, reusing `qsde/funds/snapshot.py` `peer_key()` and `MIN_PEERS = 8`, excluding schemes whose data-quality badge is `poor`. Per peer group and plan, using each scheme's latest non-stale `total_ter`: the scheme's cost percentile (share of peers it is cheaper than), cost quartile, the peer median TER, and `cost_vs_median_pp`.
- A peer group needs at least 8 members **with a valid TER**; otherwise `status = "insufficient_peers"` and no rank is shown. A stale or missing TER gives `status = "no_current_ter"`.
- Stored in the fund snapshot (`mf_fund_snapshot.cost jsonb`, migration 063), computed by the existing `fund_snapshot` step, exposed as `cost` next to `peers` in the Funds API and as a small block on the detail panel.
- The cheapest-quartile-in-peer-group set is documented as **the baseline any later fund model must beat** (roadmap Phase 3: fund performance persistence is weak, so "lowest cost" is the bar). Nothing here forecasts returns, scores a fund as good or bad, or advises a switch.
- Tests (`real_data`): on real peer groups from the loaded data, find and check the actual cases that occur (a group just under and just over 8, ties, a poor-quality member excluded, a stale TER, direct and regular pairs, percentile edge cases); where the real data has no instance of a case, the case is reported as "not covered", not simulated.
- Acceptance (C): suites green; for a real peer group, percentiles reproduce by hand from the loaded `mf_ter` rows.

## Order, gating and risks
- Build order A, then B, then C, each started only after the previous part's acceptance is met on real data loaded by the owner. There is no synthetic stand-in: until real data is loaded, B and C are not built.
- Risk: the real file layout differs from the aliases. Contained to `amfi_ter.read_export`; the layout gate catches it before anything is written.
- Risk: names in the sheet do not match `mf_scheme_master`. Mitigated by the explicit unmatched table and coverage gate; the fix is a reviewed alias list, never fuzzy matching in the load path.
- Risk: AMFI changes the page or its download. Manual flow degrades to "no new file"; `doctor` warns after 45 days.
- AMFI terms: files are the owner's personal download; the repo and its reports must not republish raw AMFI TER data (the repo is public). `data/` stays outside git (already the case); reports show aggregates and unmatched names only, not the full TER table.

## Out of scope
Automated fetching or scraping; AMC-site TER pages; TER history before the owner's earliest download; model scoring or return forecasts; any order or switch execution; fund holdings (item 1.2).
