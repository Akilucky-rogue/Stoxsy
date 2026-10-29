# AMFI TER, Part A (ingestion) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Load AMFI's downloaded TER file (daily rows per scheme) into Postgres raw-first and idempotently, join it to `mf_scheme_master`, and report coverage, using only real data.

**Architecture:** A pure reader/matcher module (`qsde/ingestion/amfi_ter.py`) with no database, a loader script in the BSE-inbox style (`scripts/load_amfi_ter.py`) that lands raw bytes then writes one transaction, migration 062, plus a doctor check, a daily-routine step and a runbook.

**Tech Stack:** Python 3.13, pandas, openpyxl (already installed), psycopg2 via `qsde.db.connection`, pytest (`real_data` marker), TimescaleDB/Postgres 16.

**Spec:** `docs/design/AMFI_TER_DESIGN.md` (Part A). Evidence and milestones: `docs/design/AMFI_TER_PROGRESS.md`. Real file: `C:\Stoxsy\data\inbox\amfi_ter\ter-of-mf-schemes.xlsx` (sha256 starts `0C1DD64F0A4EB231`).

## Global Constraints
- **Real data only. No synthetic data, no mutated copies of real files.** Every test reads the owner's real files and the real database; with no real file present a test skips with the message "no real AMFI TER file in the inbox". All tests carry `@pytest.mark.real_data` (run locally; CI skips them).
- **No AMFI rows in git** (public repo): no fixtures, no pasted values in tests, docs or commit messages; counts and header names only. Reports go to `data/reports/amfi/` (outside the repo).
- Values are stored **as published**; flags never change a value. Zero for a whole plan means not offered and is stored NULL.
- `TOTAL_MAX = 3.0` percent; `SUM_TOL = 0.02`; `REJECT_MAX_SHARE = 0.02` (more than 2 percent rejected data rows refuses the file); partial day = row count below 70 percent of the median of the previous five dates; stale = latest complete date more than 45 days old; key version `name_key_v1`.
- Columns are `numeric(7,3)`; `mf_ter` primary key `(nsdl_code, ter_date)`; inserts `ON CONFLICT DO NOTHING`; `known_at` = retrieval time of the landed file.
- Writes happen in **one transaction**; the database write functions take a connection and never commit; only the CLI commits.
- Applying migration 062 (and 063 later) to the live database, and the first real load, need the owner's explicit OK at the step marked **OWNER GATE**.
- Commits are local; nothing is pushed without the owner's OK (public repo).
- Run tests from `C:\Stoxsy\stoxsy\backend` with `C:\Stoxsy\.venv\Scripts\python.exe -m pytest -m real_data <path> -q`.

## Review Focus
1. **Excel lock and half-copied files in the inbox.** An open workbook leaves a `~$ter-of-mf-schemes.xlsx` lock file; a download in progress leaves `.crdownload`/`.part`/`.tmp`; a file modified in the last 60 seconds may be mid-copy. Expected: ignored, with a message, never parsed (Task 5).
2. **A later month's file overlapping dates already loaded, with different values.** Expected: first-seen value kept, restatements counted and reported, no duplicate rows, no error (Task 5, runs when a second real file exists).
3. **The owner re-saves the workbook as CSV from Excel.** Dates and numbers change shape. Expected: the same rows and flags as the `.xlsx` (Task 1, uses a real CSV the owner makes from the real workbook).
4. **A real file of the wrong layout** (any other real export, for example the BSE corporate-actions CSV) dropped in the inbox. Expected: a clear refusal naming the missing columns, nothing written (Task 1).
5. **A failure half-way through a load** (database restart, disk full). Expected: all or nothing: no `mf_ter_files` row without its `mf_ter` rows, no mapping rows without a file (Task 5).

## File Structure
- Create `backend/qsde/ingestion/amfi_ter.py`: pure reader, flags, partial-day detection, name key, matching. No database imports.
- Create `backend/scripts/load_amfi_ter.py`: inbox listing, CLI, landing, database writes, coverage, report.
- Create `infra/migrations/062_mf_ter.sql`.
- Modify `backend/qsde/ops/checks.py` (`check_fund_ter`, registered in `run_all`), `backend/scripts/daily_after_close.py` (`STEPS`).
- Create `docs/runbooks/amfi_ter.md`.
- Create `backend/tests/amfi_ter_support.py` (real-file helpers) and `backend/tests/test_amfi_ter_reader.py`, `test_amfi_ter_matching.py`, `test_amfi_ter_loader.py`, `test_amfi_ter_ops.py`.
- Update `docs/design/AMFI_TER_PROGRESS.md` at the end of each task (milestone evidence).

---

### Task 1: Reader (milestone M3)

**Files:**
- Create: `backend/qsde/ingestion/amfi_ter.py`, `backend/tests/amfi_ter_support.py`, `backend/tests/test_amfi_ter_reader.py`

**Interfaces:**
- Produces:
  - `TER_COLS: list[str]` = `nsdl_code, nsdl_code_raw, name, scheme_type, scheme_category, ter_date, r_ber, r_brokerage, r_transaction, r_statutory, r_total, d_ber, d_brokerage, d_transaction, d_statutory, d_total`
  - `normalise_code(raw: str) -> str` (remove all whitespace and one trailing `?`)
  - `@dataclass Export`: `frame: pd.DataFrame` (columns `TER_COLS`, one row per parsed data row), `mapping: dict[str, str]` (source header to column), `unmatched_headers: list[str]`, `layout_hash: str`, `sha256: str`, `sheet: str`, `n_data_rows: int`, `n_parsed: int`, `n_rejected: int`, `rejected: list[dict]` (`{"row": int, "reason": str}`), `n_footer_rows: int`, `footer_head: str`
  - `read_export(payload: bytes, filename: str) -> Export`; raises `ValueError` with a message naming the cause for every hard refusal
- Test helpers (`amfi_ter_support.py`): `INBOX: Path`, `real_files() -> list[Path]` (`.xlsx`/`.csv`, ignoring lock and partial files), `require_real_file() -> Path` (calls `pytest.skip` with the message above), `independent_frame(path) -> pd.DataFrame` (the file read with plain `pandas.read_excel(dtype=str)` and cut at the first blank row, for recounts that do not use the code under test).

- [ ] **Step 1: Write the failing tests** in `test_amfi_ter_reader.py` (all `real_data`, using `require_real_file()`):
  - `test_every_source_column_is_mapped`: `ex.unmatched_headers == []` and `len(ex.mapping) == 15`.
  - `test_rows_reconcile_and_footer_is_excluded`: `ex.n_data_rows == ex.n_parsed + ex.n_rejected`; `ex.n_data_rows == len(independent_frame(f))`; `ex.n_footer_rows > 0`; `ex.footer_head.startswith("Disclaimer")`; no `frame.nsdl_code` value starts with "Disclaimer".
  - `test_codes_are_normalised`: no `nsdl_code` contains whitespace or `?`; `frame.nsdl_code.nunique() == independent_frame(f)` code column normalised with the same rule counted independently (strip whitespace, drop trailing `?`).
  - `test_all_zero_plan_is_stored_missing`: for rows where the independent frame has all five regular values equal to 0, the same row in `ex.frame` has `r_total` NaN and `r_ber` NaN (and likewise for the direct plan); no stored value is exactly 0 for a total.
  - `test_values_are_unchanged`: for 200 random rows (seeded) every non-missing numeric equals the independent text parsed as float.
  - `test_october_file_facts` (only when `ex.sha256[:16].upper() == "0C1DD64F0A4EB231"`): `n_data_rows == 58095`, `frame.nsdl_code.nunique() == 2173`, `frame.ter_date.nunique() == 29`, `n_rejected == 0`.
  - `test_wrong_layout_real_file_is_refused` (Review Focus 4): pick any real non-TER file under `C:\Stoxsy\data` (first existing of `data/landing/bse_corporate_actions/**/*.csv`, `data/inbox/**/*.csv`); `read_export` raises `ValueError` whose message contains "NSDL" or "TER column"; skip if no such real file exists.
  - `test_csv_resaved_from_excel_gives_same_rows` (Review Focus 3): looks for a real `.csv` in the inbox with the same stem as the `.xlsx`; skips with "ask the owner to Save As CSV" if absent; otherwise the CSV `Export` has equal `n_parsed`, equal `frame[TER_COLS]` values (dates compared as dates) to the xlsx `Export`.
- [ ] **Step 2: Run to verify they fail** (`ImportError: amfi_ter`): `... -m pytest -m real_data backend/tests/test_amfi_ter_reader.py -q`.
- [ ] **Step 3: Implement the reader** in `amfi_ter.py` per the spec's "Reading rules": sheet = first sheet whose header row contains the NSDL-code column (aliases tolerated for case, spacing, punctuation; header row found, not assumed row 1); `.xlsx` via `openpyxl` read-only values, `.csv` via `pandas` with `dtype=str`; data ends at the first completely blank row; a later row with a scheme code and a parseable date is a hard refusal ("data after the footer"); text to number and date parsed explicitly (ISO, `dd-mm-yyyy`, `dd/mm/yyyy`, Excel serial), failure rejects that row with a reason; more than `REJECT_MAX_SHARE` rejected refuses the file; all-zero plan to NaN; `layout_hash` = SHA-256 of the normalised, ordered header list.
- [ ] **Step 4: Run the tests until green**; run the two files together with `-m "not real_data"` to confirm CI still skips them.
- [ ] **Step 5: Update the tracker** (M3 evidence so far: reader results on the real file, counts only) and **commit** `feat: AMFI TER reader (real-file tests)`.

---

### Task 2: Row flags and partial days (milestone M3)

**Files:**
- Modify: `backend/qsde/ingestion/amfi_ter.py`; Test: `backend/tests/test_amfi_ter_reader.py`

**Interfaces:**
- Consumes: `Export.frame` (Task 1).
- Produces: `TOTAL_MAX = 3.0`, `SUM_TOL = 0.02`, `FLAGS = ("total_gt_3_regular", "total_gt_3_direct", "direct_not_below_regular", "sum_mismatch")`; `flag_rows(frame: pd.DataFrame) -> pd.Series` (list of flag strings per row, aligned to the index); `partial_dates(frame: pd.DataFrame) -> list[datetime.date]`.

- [ ] **Step 1: Write the failing tests** (`real_data`): `test_flags_match_an_independent_recount`: counts per flag from `flag_rows(ex.frame)` equal counts computed in the test directly from `independent_frame` (regular total above 3.0 where offered; direct likewise; direct total not below regular total where both are offered and non-missing; absolute difference between total and the sum of the four parts above 0.02); `test_flagged_values_are_not_modified`: the flagged rows' numeric columns equal the unflagged source text; `test_partial_last_day_is_detected` (when sha prefix is the October file): `partial_dates(ex.frame) == [date(2026, 10, 29)]`; `test_no_partial_day_in_the_middle`: no date other than the last is returned for the October file.
- [ ] **Step 2: Run to fail; Step 3: Implement** `flag_rows` and `partial_dates` (a date is partial if its row count is below 0.70 times the median row count of the previous five dates; the first five dates are never partial).
- [ ] **Step 4: Run green. Step 5: Commit** `feat: TER row flags and partial-day detection`.

---

### Task 3: Name key and matching to the scheme master (milestone M5, first half)

**Files:**
- Modify: `backend/qsde/ingestion/amfi_ter.py`; Test: `backend/tests/test_amfi_ter_matching.py`

**Interfaces:**
- Consumes: `Export.frame`; the master as `read_sql("SELECT scheme_code, name, amc, category_raw, is_direct, last_nav_date FROM mf_scheme_master")` (read-only).
- Produces: `KEY_VERSION = "name_key_v1"`; `name_key(name: str) -> str`; `@dataclass Match`: `mapped: pd.DataFrame` (`nsdl_code, scheme_code, plan` with plan in `regular`/`direct`), `unmatched: pd.DataFrame` (`side` in `ter`/`master`, `nsdl_code, scheme_code, name, reason`), `suggestions: pd.DataFrame` (fuzzy candidates, never applied); `match_schemes(ter: pd.DataFrame, master: pd.DataFrame, today: datetime.date) -> Match` where `ter` has `nsdl_code, name, scheme_category` (one row per code).

- [ ] **Step 1: Write the failing tests** (`real_data`, real file plus real master):
  - `test_every_ter_scheme_is_mapped_or_listed`: distinct TER codes = codes in `mapped` plus `unmatched[side == "ter"]` codes (no code lost or counted twice).
  - `test_mapping_uses_the_right_plan`: every `mapped` row with plan `regular` points to a master row with `is_direct == False`, `direct` to `True`.
  - `test_no_master_scheme_gets_two_codes_for_one_plan`: `mapped.groupby(["scheme_code", "plan"]).nsdl_code.nunique().max() == 1`.
  - `test_unmatched_master_rows_have_reasons`: every master scheme active in the 90 days before `today` is either in `mapped` or in `unmatched[side == "master"]` with `reason` in `{"no_name_key", "ambiguous_key", "not_in_file"}`.
  - `test_shared_keys_follow_the_category_rule`: for TER codes whose `name_key` is shared (the real file has four), the code is mapped only when exactly one candidate has the master row's category, otherwise it is listed as `ambiguous_key`.
  - `test_october_coverage_matches_the_exploration` (October file only): TER codes with a master key is 2,174 of 2,174, and master schemes active since 2026-09-01 that get a TER at least 8,700 (the figure found at M1; a lower number is a regression of the key rule).
- [ ] **Step 2: Run to fail. Step 3: Implement** `name_key` per spec (lowercase, `&` to "and", strip plan/option words and payout-frequency words, keep only letters and digits) and `match_schemes` per spec; `suggestions` uses `difflib.get_close_matches` over unmatched keys, cutoff 0.92, only for the report.
- [ ] **Step 4: Run green. Step 5: Update the tracker** (M5 first half: counts) **and commit** `feat: TER name-key matching to the scheme master`.

---

### Task 4: Migration 062 (milestone M4, first half)

**Files:**
- Create: `infra/migrations/062_mf_ter.sql`; Test: `backend/tests/test_amfi_ter_loader.py` (schema test only)

**Interfaces:**
- Produces tables (spec "Storage"): `mf_ter_layouts`, `mf_ter_files`, `mf_ter_scheme`, `mf_ter`, `mf_ter_map`, `mf_ter_unmatched`, exactly as the spec lists columns; `mf_ter` plain table with an index on `ter_date`; idempotent (`CREATE TABLE IF NOT EXISTS`) in one `BEGIN; ... COMMIT;`, same style as `059_exchange_calendar.sql`.

- [ ] **Step 1: Write the failing test** `test_062_tables_and_keys_exist` (`real_data`): the six tables exist; `mf_ter` primary key columns are `(nsdl_code, ter_date)`; `mf_ter.r_total` data type is `numeric` with precision 7 and scale 3; `mf_ter_files.sha256` is unique.
- [ ] **Step 2: Run to fail** (tables missing). **Step 3: Write the migration.**
- [ ] **Step 4: OWNER GATE.** Ask the owner for explicit OK to apply migration 062 to the live `qsde` database (it only creates empty tables). Apply with `python backend/scripts/apply_migrations.py` and confirm the output shows `062_mf_ter.sql` applied and `0 failed`.
- [ ] **Step 5: Run the schema test green. Step 6: Update the tracker (M4 first half) and commit** `feat: migration 062 mf_ter`.

---

### Task 5: Loader with dry run, layout gate and landing (milestone M4)

**Files:**
- Create: `backend/scripts/load_amfi_ter.py`; Test: `backend/tests/test_amfi_ter_loader.py`

**Interfaces:**
- Consumes: Tasks 1 to 4.
- Produces:
  - `INBOX = data_path("inbox", "amfi_ter")`
  - `inbox_files(inbox: Path, now: float | None = None) -> tuple[list[Path], list[str]]` (files to load, messages for ignored ones: names starting `~$`, suffix `.crdownload`/`.part`/`.tmp`, anything modified in the last 60 seconds)
  - `load_file(path: Path, dry_run: bool, mapping_ok: bool = False, conn=None, landing: LandingZone | None = None) -> dict` with `status` in `dry run | REFUSED: ... | already loaded | loaded` and the counts and mapping the spec lists; when `conn` is given it never commits or closes it
  - `main() -> int` with `--file`, `--inbox`, `--dry-run`, `--mapping-ok`; exit 0 for empty inbox, a waiting-review refusal and success; exit 2 for a rejected file; commits via `get_sync_conn()`
- Write order inside `load_file`: parse (no side effects) then compare `sha256` to `mf_ter_files`; then layout gate; then `landing.write("amfi_ter", RawItem(payload, now, None, {"origin": "manual_download", "filename": ..., "page": "AMFI TER of MF Schemes (Download Excel)"}, ext))`; then one transaction on `conn`: `mf_ter_layouts` (first sight), `mf_ter_files`, `mf_ter_scheme` (upsert `last_seen`, `name_variants`), `mf_ter` (`execute_values`, `ON CONFLICT DO NOTHING`, restatement count = existing rows with a different value for the same key, compared before insert, recorded in `issues`), `mf_ter_map` and `mf_ter_unmatched` rebuilt from `match_schemes`.

- [ ] **Step 1: Write the failing tests** (`real_data`; each opens `psycopg2.connect` on the live database inside a transaction that is rolled back at the end, and uses `LandingZone(tmp_path)` so nothing is landed in the real landing folder. Each test first deletes, inside its own transaction, any `mf_ter_files` row with the real file's SHA-256 and its `mf_ter_layouts` row, so the tests behave the same before and after the owner's real load in Task 7; the rollback restores the real rows):
  - `test_dry_run_writes_nothing`: row counts of the six tables are equal before and after; `landing` folder empty; result `status == "dry run"` with a non-empty `sample`.
  - `test_first_file_of_a_layout_is_refused_until_mapping_ok`: with `mapping_ok=False` status starts with `REFUSED`, tables unchanged; with `mapping_ok=True` status is `loaded`.
  - `test_load_reconciles_with_the_file`: after a load, `count(*) FROM mf_ter WHERE file_id = :id` equals `n_parsed`; `mf_ter_files.n_data_rows == n_parsed + n_rejected`; `n_footer_rows > 0`.
  - `test_raw_file_is_landed_byte_identical`: the landed file's SHA-256 equals the source file's and equals `mf_ter_files.sha256`.
  - `test_reload_is_skipped`: the second `load_file` returns `already loaded` and no table count changes.
  - `test_load_is_all_or_nothing` (Review Focus 5): after `conn.rollback()` no `mf_ter_files` row for that sha256 remains and `mf_ter`/`mf_ter_map` counts equal their starting values.
  - `test_overlapping_month_counts_restatements` (Review Focus 2): needs a second real file in the inbox that overlaps dates already loaded; skip with "needs a second real month" otherwise; asserts `restatements` equals an independent recount and total `mf_ter` rows grew only by the new keys.
  - `test_inbox_ignores_lock_and_partial_files` (Review Focus 1): in `tmp_path`, create empty files named `~$ter-of-mf-schemes.xlsx`, `x.xlsx.crdownload`, `y.part` and a copy of the real file just modified (`os.utime` now); `inbox_files` returns none of them and a message for each; the same copy with `os.utime` 5 minutes ago is returned.
- [ ] **Step 2: Run to fail. Step 3: Implement** as specified. **Step 4: Run green.** **Step 5: Commit** `feat: AMFI TER loader with dry run, layout gate and raw landing`.

---

### Task 6: Coverage, report, doctor line, daily step, runbook (milestones M5 and M6)

**Files:**
- Modify: `backend/scripts/load_amfi_ter.py` (add `coverage`, `write_report`), `backend/qsde/ops/checks.py`, `backend/scripts/daily_after_close.py`; Create: `docs/runbooks/amfi_ter.md`, `backend/tests/test_amfi_ter_ops.py`

**Interfaces:**
- Produces: `coverage(conn, today: datetime.date) -> dict` with keys `latest_complete_date`, `in_scope`, `mapped`, `pct`, `by_reason` (dict), `files_waiting` (int); `write_report(info: dict) -> Path` (`data/reports/amfi/amfi_ter.md`, aggregates and unmatched names only); `check_fund_ter(today: datetime.date | None = None) -> Check` (name `"fund expense ratios"`, levels `ok`/`warn` only; warn when the latest complete date is more than 45 days old, when no TER is loaded, or when a file waits for review); registration in `run_all()` after the fund-NAV freshness checks via `_safe`; step `("amfi_ter_inbox", False, "AMFI TER files waiting in the inbox (manual download; a first-layout file is refused until reviewed)", [PY, str(HERE / "load_amfi_ter.py"), "--inbox"])` placed before `fund_snapshot` in `STEPS`.
- In-scope definition: master schemes with `last_nav_date` within 90 days before `today` and `is_growth` or IDCW option lines (the spec's "growth and IDCW lines").

- [ ] **Step 1: Write the failing tests** (`real_data`, after the owner's first real load in Task 7, otherwise skipped with "no TER loaded yet"):
  - `test_coverage_reproduces_by_hand`: `coverage(...)` `mapped` and `in_scope` equal counts computed in the test straight from `mf_scheme_master`, `mf_ter_map` and `mf_ter` with independent SQL; `pct == round(100 * mapped / in_scope, 1)`.
  - `test_latest_complete_date_excludes_the_partial_day`: for the October load `latest_complete_date == date(2026, 10, 28)`.
  - `test_every_gap_has_a_reason`: `sum(by_reason.values()) == in_scope - mapped`.
  - `test_report_contains_no_value_table`: the report file contains the strings "coverage" and "unmatched" and contains none of the stored `r_total` values as a table (no line with more than 4 numeric TER columns).
  - `test_doctor_line`: `check_fund_ter().name == "fund expense ratios"` and its level is `ok` right after loading a current month, `warn` when evaluated with a `today` 60 days later (the function takes `today` as an optional argument).
  - `test_daily_step_is_registered`: `("amfi_ter_inbox")` appears in `STEPS` before `fund_snapshot` and is non-required.
- [ ] **Step 2: Run to fail. Step 3: Implement. Step 4: Run green.** Write the runbook (download steps from `AMFI_TER_PROGRESS.md`, the dry run then `--mapping-ok` sequence, what refusals mean). **Step 5: Commit** `feat: TER coverage report, doctor line, daily inbox step, runbook`.

---

### Task 7: First real load, owner-reviewed (milestones M4, M5, M6 sign-off)

**Files:** none new; evidence goes into `docs/design/AMFI_TER_PROGRESS.md`.

- [ ] **Step 1: Dry run on the real file** (`python backend/scripts/load_amfi_ter.py --inbox --dry-run`). Show the owner `column_mapping`, `unmatched_headers` (must be empty), counts, the partial day and a 5-row sample. **OWNER GATE: owner confirms the mapping and sample match the file.**
- [ ] **Step 2: Load with `--mapping-ok`.** Expected: `loaded`, rows equal parsed, the landed file hash equals the source.
- [ ] **Step 3: Re-run** without flags: expected `already loaded`, no count changes.
- [ ] **Step 4: Run the report and `stoxsy doctor`.** Expected: the "fund expense ratios" line OK with latest complete date 2026-10-28; coverage figure and every gap reason recorded; the unmatched list reviewed with the owner (**OWNER GATE: owner accepts the in-scope definition and the explained gaps**).
- [ ] **Step 5: Run the whole `real_data` TER test set** (`-m real_data -k amfi_ter`): all green or skipped only for "needs a second real month" and "ask the owner to Save As CSV".
- [ ] **Step 6: Ask the owner to Save As CSV** the real workbook once (same folder, same stem) and re-run `test_csv_resaved_from_excel_gives_same_rows`.
- [ ] **Step 7: Update the tracker** (M3 to M6 DONE with evidence and the numbers) and commit `docs: AMFI TER part A evidence`. Then ask the owner whether to push.

**After the plan (milestone M7, owner-paced):** each further month the owner downloads goes through `--dry-run`, `--inbox`; `test_overlapping_month_counts_restatements` is run once a second month exists; the history range is stated in the tracker. Parts B and C get their own plans after Part A is signed off.
