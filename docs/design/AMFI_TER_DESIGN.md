# AMFI expense ratios (TER): manual-inbox ingestion, API/Funds page, cost-aware peer ranking

Status: DRAFT v2 for owner review, 2026-10-30, rewritten after inspecting the real AMFI file (progress and evidence: `docs/design/AMFI_TER_PROGRESS.md`). Roadmap item 1.1 (`docs/design/PLATFORM_ROADMAP.md`). Three parts, built in order A, B, C; each ends at its own acceptance checks.

## Decisions already made (owner, 2026-10-30)
- **Source: AMFI's "TER of MF Schemes" page, manual download only.** AMFI's terms allow personal, non-commercial use, forbid storing "any significant portion" electronically and any use not agreed in writing, and say nothing about automated access. The page's data comes from AMFI's internal `/api/` routes, which are not a published API. No automated access is built; if AMFI gives written permission later, that is a new decision.
- **Real data only, no synthetic data anywhere.** Nothing is built against a guessed layout. Tests read the owner's real files from the inbox and are tagged `real_data` (they run locally; CI skips them). No AMFI rows are committed to the repo (public repo, AMFI storage terms). Reports under `data/reports/` hold aggregates and unmatched names only.
- **Load order: latest month first**, verified end to end and against the scheme master; earlier months only after the pipeline is proven.
- **File format:** `.xlsx` (via `openpyxl`, already installed in the project environment) and `.csv`.
- **Scope:** A ingestion plus daily inbox scan, B API and Funds page, C cost-aware ranking.
- **Bad published values:** stored exactly as published, flagged, excluded from rankings; never corrected or deleted.
- **Row shape:** one wide row per scheme per day, mirroring the file. **Retention:** every daily row kept.
- **Progress** is tracked in `docs/design/AMFI_TER_PROGRESS.md`.

## The real file (facts from M1; the reader is written against these)
One sheet `TER_Revised`, header on row 1, then one row per scheme per day. 15 columns: `NSDL Scheme Code`, `Scheme Name`, `Scheme Type`, `Scheme Category`, `TER Date`, then for the Regular Plan and again for the Direct Plan: Base Expense Ratio (BER), Brokerage cost, Transaction Cost, Statutory Levies (including GST), Total TER (all in percent). Numbers are stored as text. A disclaimer block follows the data. There is no AMC, ISIN or AMFI numeric scheme code, so the NSDL code identifies a scheme and the name links it to `mf_scheme_master`. The October file: 58,095 data rows, 2,174 scheme codes, 29 days, last day partial. Total TER equals the sum of its four parts on every row. A plan with all-zero values is "not offered". Some published values are implausible.

## Part A: ingestion (foundation)

### Flow (same shape as `scripts/load_bse_corporate_actions.py`)
1. Owner saves each month's file in `<data>/inbox/amfi_ter/` (any file name, `.xlsx` or `.csv`).
2. `python backend/scripts/load_amfi_ter.py --inbox --dry-run` prints the column mapping, unmatched headers, a sample, and counts (data rows, footer rows, rejected rows, flagged rows, dates, partial last day, matched and unmatched schemes). Nothing is written.
3. The first file of each **layout** (hash of the normalised header row) is refused until the same command is run with `--mapping-ok`; approval is stored per layout in `mf_ter_layouts`.
4. Load: raw bytes land unchanged (`LandingZone`, SHA-256, retrieval time, `known_at` = retrieval time, a conservative stand-in because AMFI's publication time is unknown); a file with an already-loaded SHA-256 is skipped.

### Units (each testable alone)
- `qsde/ingestion/amfi_ter.py`, pure, no database: `read_export(payload, filename) -> Export`; `normalise_code()`; `name_key()`; `flag_row()`; `match_schemes()`.
- `scripts/load_amfi_ter.py`: CLI and database writes.
- `infra/migrations/062_mf_ter.sql`.
- `qsde/ops/checks.py`: `check_fund_ter`. `scripts/daily_after_close.py`: one non-required step.

### Reading rules
- Sheet: the first sheet whose header row contains the NSDL scheme-code column (aliases tolerated for case, spacing, punctuation); header row found by that column, not assumed to be row 1.
- Data ends at the first **completely blank row** after data has started (the real file has one right before the disclaimer). Everything after is the **footer**: counted and its first words recorded in the file record, never parsed as data. A footer row that does carry a parseable date and a scheme code is a hard refusal ("data after the footer"), so a changed layout cannot hide rows in the footer. Before the blank row, a row with a code but no parseable date is a rejected row, not the end of the data.
- Numbers are parsed explicitly from text. An unparseable number or date in a data row **rejects that row** and lists it (never a silent drop). Invariant checked on every file: source data rows = parsed rows + rejected rows.
- Scheme code: `nsdl_code` = the published code with all whitespace removed and a trailing `?` removed; the published spelling is kept in `nsdl_code_raw`.
- A plan whose five values are all 0 is **not offered**: its five columns are stored as NULL (missing), never as a 0% cost. Total 0 with non-zero parts is a `sum_mismatch` flag.

### Row quality flags (stored with the row; the value is never changed)
`total_gt_3_regular`, `total_gt_3_direct` (threshold 3.0 percent, one constant), `direct_not_below_regular` (both plans offered), `sum_mismatch` (|total - (BER + brokerage + transaction + statutory)| greater than 0.02). Flagged rows are shown in reports, and **excluded from rankings and from savings figures** (Parts B and C); they are still shown to the user, marked as an implausible published value.

### Hard refusals (the whole file is refused, nothing written)
No recognisable NSDL-code or TER columns; more than 2 percent of data rows rejected; no parseable dates; layout not approved (reported as a message, exit code 0).

### Partial last day
Dates are counted per file. A date is `partial` if its row count is below 70 percent of the median of the previous five dates (the real October file: 1,027 rows against about 2,040 on the previous day). A scheme missing on a partial day is not treated as stopped. "Latest complete date" excludes a partial last day and is what coverage, `doctor`, the API and the ranking use.

### Scheme matching (nothing guessed)
- `name_key_v1(name)`: lowercase, `&` to "and", plan and option words removed (direct, regular, plan, growth, IDCW, dividend, payout, reinvestment, option, bonus and the payout-frequency words, income distribution cum capital withdrawal), then only letters and digits kept.
- A TER scheme maps to every `mf_scheme_master` scheme with the same key; one TER scheme typically maps to several master rows (growth and IDCW options, each plan). **Plan:** a master scheme with a known plan takes that plan's columns (regular to `is_direct = false`, direct to `is_direct = true`) if AMFI fills them, else it is listed `plan_not_offered`. **ETFs have no plan** (owner decision 2026-10-30): an ETF (`is_etf`) takes the one plan AMFI fills in, ignoring the master's label; if both are filled the label decides. A master scheme with an unknown plan takes the single filled plan, and is listed `plan_unknown` when both are filled (never guessed). **Shared keys:** a key shared by two TER codes (the real file has three ETF pairs whose Growth and IDCW options have separate NSDL codes and the same category) is resolved by the option word in the TER name (growth or IDCW) against the master's `is_growth`; if the option words do not separate the codes, all are listed `ambiguous_key`. Category was dropped as a tie-break: it is identical within the real pairs and formatted differently in the two sources.
- Everything else goes to `mf_ter_unmatched` with a reason in both directions. TER side: `no_name_key`, `ambiguous_key`, `no_plan_match`. Active master side: `not_in_file`, `ambiguous_key`, `plan_unknown`, `plan_not_offered`. Fuzzy candidates appear in the report as suggestions only and are never applied. The mapping table is derived and rebuildable from the stored rows (`load_amfi_ter.py --rematch`, used after the scheme master or the key rule changes); the key version is stored.
- Real result on the October file (2026-10-30): all 2,173 codes map; 9,011 scheme-plan links; 8,594 of 8,796 active master schemes (97.7 percent) have a usable TER; the 202 gaps are listed by name and reason (89 not in AMFI's file, 89 unknown plan on legacy fund lines, 14 linked but no row, 10 stale).

### Storage (migration 062)
- `mf_ter_layouts(layout_hash PK, headers jsonb, approved boolean, approved_at)`; set only by `--mapping-ok`.
- `mf_ter_files(id, sha256 UNIQUE, filename, layout_hash, n_data_rows, n_parsed, n_rejected, n_flagged, n_footer_rows, footer_head text, date_min, date_max, last_date_partial boolean, issues jsonb, retrieved_at, known_at, landing_path)`.
- `mf_ter_scheme(nsdl_code PK, nsdl_code_raw, name, scheme_type, scheme_category, first_seen, last_seen, name_variants jsonb)`.
- `mf_ter(nsdl_code, ter_date, r_ber, r_brokerage, r_transaction, r_statutory, r_total, d_ber, d_brokerage, d_transaction, d_statutory, d_total, flags text[], file_id, known_at, PRIMARY KEY (nsdl_code, ter_date))`, exact `NUMERIC` (no fixed decimals: AMFI publishes up to 4, and migration 062's `NUMERIC(7,3)` rounded them, corrected by migration 063 after the whole-branch review), a plain table with an index on `ter_date` (about 0.7 million rows a year). `ON CONFLICT DO NOTHING`: first-seen value stays; a different value for the same key is counted as a **restatement** in the file's `issues` and the report, not applied.
- `mf_ter_map(nsdl_code, scheme_code, plan, key_version)` and `mf_ter_unmatched(file_id, side, nsdl_code, scheme_code, name, reason)`.

### Reports, operator surface
- `data/reports/amfi/amfi_ter.md`: files, dates, partial day, rejected and flagged counts by type, restatements, the roadmap 1.1 coverage figure and the unmatched lists with reasons.
- **Roadmap gate 1.1:** for the latest complete date, every in-scope master scheme (NAV within the last 90 days, all option lines) has a TER via the map, or is listed with a reason. **"Has a TER" means the scheme's latest TER on or before the latest complete date, at most 7 days old** (owner decision 2026-10-30: a TER is a standing rate that changed for only about a third of schemes in a month, and AMFI publishes progressively, so a scheme not yet reporting for the newest day is not a gap; same-day coverage was 92.5 percent against 97.7 percent). Gap reasons add `stale_over_7_days` and `no_ter_row`.
- `docs/runbooks/amfi_ter.md`: download steps, where to save, dry run then `--mapping-ok`.
- Daily routine step `amfi_ter_inbox` (non-required): runs `load_amfi_ter.py --inbox`. Empty inbox: exit 0, message only. An unapproved layout: refused with exit 0 and a message (a waiting review is not a pipeline failure).
- `stoxsy doctor` line "fund expense ratios": latest complete TER date, coverage percent, files waiting; WARN (never FAIL) when the latest complete date is more than 45 days old or a file waits for review.
- History starts at the earliest month the owner downloads; nothing is back-filled.

### Tests (real data only; all tagged `real_data`, so they run locally and CI skips them)
Against the owner's real files in `<data>/inbox/amfi_ter/` and the loaded database; they skip with a clear message when no real file is present. They check: every header column mapped; footer excluded; data rows = parsed + rejected; all-zero plans stored as missing; flags equal an independent recount from the file; code normalisation (the `?` and whitespace cases that exist in the real file); partial-day detection; dry run writes nothing; raw file landed byte-identical (hash); reload skips the file and changes no row counts; layout gate (refused, then approved); matching counts add up to the file's scheme count and to the master's active count; the coverage figure reproduces by hand from the loaded rows; restatement counting on a later overlapping file once one exists.

### Acceptance (A)
With the October file: dry run shows a sensible mapping and sample; load succeeds with parsed + rejected = source rows; the report shows the 1.1 coverage with every gap explained; the real-data tests pass locally.

## Part B: API and Funds page
- `GET /api/funds/schemes` and `/api/funds/schemes/{code}` gain a `ter` object taken at the latest complete date through `mf_ter_map`: `{plan, base_ter, brokerage, transaction, statutory, total_ter, ter_date, source: "AMFI", stale: bool, flagged: bool, other_plan: {plan, total_ter} | null, annual_saving_pp: number | null}`; `null` when the scheme has no TER. `annual_saving_pp` is computed only when neither plan is flagged. List parameters: `max_ter`, `sort=ter` (flagged rows sort last).
- `web/src/pages/Funds.tsx`: a TER column; `web/src/components/FundDetail.tsx`: a cost panel with regular and direct total TER, the components, the difference in percentage points and "about X rupees per 1 lakh per year" (difference times 1,00,000), each labelled "AMFI, as of <date>, indicative; includes brokerage and transaction costs as published". A flagged value is shown with an "implausible published value" marker and is not used in the saving.
- A scheme's `ter` is its latest TER on or before the latest complete date (the same 7-day carry-forward as the coverage gate); `stale` when that TER is older than 7 days before the latest complete date, or when the latest complete date itself is more than 45 days old: greyed, never hidden or reused as current.
- No recommendation wording. The page states the difference, not what to do.
- Tests (`real_data`): API tests against the loaded real rows (a scheme with no TER, a flagged scheme, one with only one plan offered, filter and sort); component tests use values read from the real API response.
- Acceptance (B): suites green locally; for at least 10 real funds the shown values match the downloaded file by eye (the owner checks 3).

## Part C: cost-aware peer ranking (not a return forecast)
**Correction to the roadmap wording.** A fund's published NAV is already net of its expenses, so historical NAV returns are already "net of cost". Subtracting TER again would double-count. "Net-of-cost ranking" is therefore defined as a **cost position within a like-for-like peer group**, plus the **direct versus regular saving**, shown beside the existing (already net) returns.
- New pure module `qsde/funds/cost.py`, reusing `qsde/funds/snapshot.py` `peer_key()` and `MIN_PEERS = 8`, excluding schemes whose data-quality badge is `poor` and schemes whose TER row is flagged. Per peer group and plan, from each scheme's total TER at the latest complete date: cost percentile (share of peers it is cheaper than), cost quartile, peer median, `cost_vs_median_pp`.
- A group needs at least 8 members **with an unflagged TER**, otherwise `status = "insufficient_peers"`; a stale or missing TER gives `status = "no_current_ter"`.
- Stored in the fund snapshot (`mf_fund_snapshot.cost jsonb`, migration 064 (063 is the exact-values fix for `mf_ter`)), computed by the existing `fund_snapshot` step, exposed as `cost` next to `peers` in the Funds API and as a block on the detail panel.
- The cheapest-quartile-in-peer-group set is documented as **the baseline any later fund model must beat** (roadmap Phase 3). Nothing here forecasts returns, scores a fund as good or bad, or advises a switch.
- Tests (`real_data`): on real peer groups from the loaded data, find and check the cases that actually occur (a group just under and just over 8, ties, a poor-quality member, a flagged member, a stale TER, a scheme with one plan only, percentile edge cases); a case with no real instance is reported as "not covered", not simulated.
- Acceptance (C): suites green locally; one real peer group's percentiles reproduced by hand.

## Order, gating and risks
- Order A, then B, then C, each started only after the previous part's acceptance is met on real data. No synthetic stand-in.
- Risk: AMFI changes the file layout. Contained to `amfi_ter.read_export`; the layout gate refuses the first file of any new layout before anything is written.
- Risk: names stop joining to `mf_scheme_master` (new schemes, renames). Mitigated by the explicit unmatched table and coverage gate; the fix is a reviewed key-rule change with a new key version, never fuzzy matching in the load path.
- Risk: AMFI changes the page or its download. The manual flow degrades to "no new file"; `doctor` warns after 45 days.
- AMFI terms: files are the owner's personal download; the repo, commits and reports must not republish raw AMFI TER data (the repo is public).

## Out of scope
Automated fetching or scraping; AMC-site TER pages; TER history before the owner's earliest download; correcting published values; model scoring or return forecasts; any order or switch execution; fund holdings (item 1.2).
