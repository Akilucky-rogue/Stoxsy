# AMFI TER Part B: expense ratios on the Funds API and page: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show each fund's expense ratio, as AMFI publishes it and broken into its parts, on `/api/funds/*` and the Funds page, with sorting, honest statuses and a monthly history. Read-only: no new tables, no migration.

**Architecture:** One small module `qsde/funds/ter.py` answers "what is this scheme's expense as of the latest COMPLETE day" from the existing `mf_ter_map` + `mf_ter` (reusing `amfi_ter_db.latest_complete_date` and the 7-day carry-forward). The API attaches an `expense` object to every `FundRow` (one batched query per page, never one per row), adds two sorts, and the detail endpoint adds a monthly `expense_history`. The page shows one column in the list and a "Costs" panel in the detail. Nothing is computed that AMFI did not publish except labelled display aids.

**Tech Stack:** FastAPI + pandas `read_sql` (existing), pytest on the real database (`real_data`, local only), React + vitest.

**Spec:** `docs/design/AMFI_TER_DESIGN.md` (Part B), `docs/design/AMFI_TER_PIPELINE.md` (what the data is and its limits).

## Global Constraints

- No synthetic data: server tests read the real database; web tests use the shape returned by the real API (captured by the executor from a live call at test-writing time, values left as served; nothing invented, nothing committed from AMFI beyond what a test needs to assert structure: no scheme names or values in the repository).
- Values are shown exactly as stored (NUMERIC). A flagged value (`total_gt_3_regular`, `total_gt_3_direct`, `direct_not_below_regular`, `sum_mismatch`) is **shown with its warning**, never hidden or corrected in Part B (exclusion from rankings is Part C).
- "Total" is labelled "as published". Brokerage and transaction cost are monthly cumulative averages that reset each month (AMFI disclaimer); the UI says so next to them.
- Statutory levies = GST on the base ratio **plus trade-related charges**; with no trading costs it is about 18% of base (real data, 15 Oct 2026: median 0.178), with trading costs 0.17 to 0.76 of base. It is shown as published, never as "GST".
- The page never uses a day AMFI is still filing: `as_of` = `latest_complete_date`.
- Defaults chosen by Claude (owner delegated): list sort offered = base ratio low to high; list column shows the base ratio with the total as published in a tooltip; plan = the row's own plan (`mf_ter_map` has one plan per scheme code).
- Python 3.13, type hints, one-line docstrings, small modules (repo rule). Existing endpoints stay backward compatible (new fields only).

## Review Focus

1. A scheme with no mapping (closed, not on AMFI's list, unmatched): `status: "not_covered"` with a reason, never a zero or blank that reads as free.
2. An all-zero plan (not offered): stored as NULL; shown as "not offered", never 0.00%.
3. A mapped scheme whose newest row is older than 7 days before `as_of`: `status: "stale"` with the date, not silently shown as current.
4. Flagged rows: value shown AND flags returned with plain-language reasons.
5. ETFs: single plan; the row's plan label may be null, the figure must still resolve.
6. Page of 100 rows: constant number of queries (assert the count), sorts put NULL last.
7. Month boundary: brokerage/transaction jump on the 1st; history shows each month's value so the jump is visible, not smoothed.

---

### Task 1: `qsde/funds/ter.py`: expense as of the latest complete day

**Files:**
- Create: `backend/qsde/funds/ter.py`
- Test: `backend/tests/test_funds_ter.py` (module marker `real_data`)

**Interfaces:**
- Consumes: `qsde.ingestion.amfi_ter_db.latest_complete_date(cur) -> date | None`, `CARRY_DAYS`, `STALE_DAYS`; tables `mf_ter_map(scheme_code, plan, nsdl_code)`, `mf_ter`; `qsde.db.connection.read_sql`.
- Produces:
  - `expense_as_of() -> date | None`
  - `expense_for(codes: list[int], as_of: date | None = None) -> dict[int, dict]`: every requested code gets an entry: `{"status": "ok"|"not_offered"|"stale"|"not_covered", "reason": str|None, "as_of": str|None (the TER day used), "plan": "regular"|"direct"|None, "base": float|None, "brokerage": float|None, "transaction": float|None, "statutory": float|None, "total": float|None, "flags": list[str], "flag_text": list[str]}`.
  - `FLAG_TEXT: dict[str, str]`: plain-language text per flag.
  - `expense_history(code: int, months: int = 12) -> list[dict]`: one dict per calendar month with data (`month`, `as_of` (last day in that month), `base`, `brokerage`, `transaction`, `statutory`, `total`), oldest first.

- [ ] **Step 1: Write the failing tests** (names and assertions): `test_values_equal_the_stored_row_exactly` (for 200 mapped schemes, every returned component equals an independent SQL read of `mf_ter` through `mf_ter_map`); `test_every_requested_code_gets_an_entry_with_a_status`; `test_unmapped_code_is_not_covered_with_a_reason`; `test_status_stale_when_newest_row_is_older_than_carry_window` (pick a real mapped scheme whose newest row is old, if none exists in the data the test asserts the rule on `as_of` shifted back, via the `as_of` argument); `test_flags_come_with_text` (a real flagged row returns non-empty `flag_text`); `test_coverage_of_active_schemes_matches_the_doctor_check` (share of active schemes with status ok is within 0.5 points of `check_fund_ter`); `test_history_has_one_point_per_month_and_shows_the_monthly_reset` (for a scheme with brokerage that differs between two months, both points appear unchanged); `test_one_query_for_many_codes` (count statements via a cursor hook: <= 3 for 100 codes).
- [ ] **Step 2: Run:** `pytest tests/test_funds_ter.py -q --import-mode=importlib`. Expected: FAIL (module missing).
- [ ] **Step 3: Implement** the three functions and `FLAG_TEXT` in `backend/qsde/funds/ter.py`. One SQL for `expense_for`: `mf_ter_map` rows for the codes, `LATERAL` newest `mf_ter` row with `ter_date` in `(as_of - CARRY_DAYS, as_of]`; pick the regular or direct column set by the map's plan; NULL `total` for the plan = `not_offered`.
- [ ] **Step 4: Run:** same command. Expected: PASS.
- [ ] **Step 5: Commit:** `feat: fund expense as of the latest complete TER day (qsde/funds/ter.py)`.

### Task 2: API: `expense` on every fund row, two sorts, history on the detail

**Files:**
- Modify: `backend/api/routes/funds.py` (`FundRow.expense`, `_row`, `SORTS`, list query, detail, `DISCLAIMER`, `METHOD`)
- Test: `backend/tests/test_funds.py` (new tests appended; real DB, existing style)

**Interfaces:**
- Consumes: Task 1.
- Produces: `FundRow.expense: Optional[dict]` (the Task 1 entry); `sort` accepts `ter_base` and `ter_total` (NULL last); `FundDetail.expense_history: list[dict]`; `FundList.expense_as_of: Optional[str]`; `DISCLAIMER` no longer says expense ratio is "not loaded" (holdings / overlap and AUM still are); `METHOD` gains one paragraph defining base, brokerage, transaction cost, statutory levies, total, the monthly reset, and "latest complete day".

- [ ] **Step 1: Write the failing tests:** `test_list_rows_carry_expense_with_status`; `test_sort_ter_base_ascending_puts_null_last_and_is_ordered`; `test_sort_ter_total_descending`; `test_list_of_100_rows_makes_a_constant_number_of_queries`; `test_detail_has_expense_history_months_oldest_first`; `test_old_clients_still_parse_the_row` (the pre-existing fields are unchanged); `test_disclaimer_no_longer_claims_expense_ratio_is_missing`.
- [ ] **Step 2: Run:** `pytest tests/test_funds.py -q --import-mode=importlib -k "expense or ter"`. Expected: FAIL.
- [ ] **Step 3: Implement.** The sort needs a CTE of the latest TER per mapped scheme (one DISTINCT ON over the carry window, about 9k rows) left-joined into the list query; the per-row object comes from one `expense_for` call per page, as `_last_navs` does.
- [ ] **Step 4: Run** the new tests and the whole `tests/test_funds.py` and `tests/test_fund_snapshot.py`. Expected: PASS.
- [ ] **Step 5: Commit:** `feat: expense ratios on the funds API (row field, two sorts, monthly history)`.

### Task 3: Web: list column, sort options, Costs panel

**Files:**
- Modify: `web/src/lib/api.ts` (types `FundExpense`, `FundRow.expense`, `FundList.expense_as_of`, `FundDetail.expense_history`), `web/src/pages/Funds.tsx` (column, two sort entries), `web/src/components/FundDetail.tsx` (Costs section)
- Test: `web/src/components/FundDetail.test.tsx` (extend)

**Interfaces:**
- Consumes: Task 2 JSON.
- Produces: exported `ExpenseCell({ e }: { e: FundExpense | undefined })` and `CostsPanel({ fund, history })`.

- [ ] **Step 1: Write the failing tests** with the repo's existing vitest style: `ExpenseCell shows the base ratio with the total as published in its title`; `shows "not offered" for not_offered, "no AMFI figure" for not_covered, "older figure (date)" for stale`; `a flagged row shows the warning chip with the reason text`; `CostsPanel lists base, brokerage, transaction, statutory, total as published and the monthly-reset note`; `history table shows one row per month`.
- [ ] **Step 2: Run:** `cd web && npx vitest run src/components/FundDetail.test.tsx`. Expected: FAIL.
- [ ] **Step 3: Implement** the types, the column "Expense (base)" after "Volatility"/before "Data", sort options "Expense: lowest first / highest first", and `CostsPanel` in the detail's left column under Risk, plain language, no jargon without a gloss ("brokerage: trading cost the fund paid, averaged for the month; resets every month").
- [ ] **Step 4: Run:** the vitest file, then `npx tsc --noEmit` and `npm run build`. Expected: PASS, no type errors, build succeeds.
- [ ] **Step 5: Commit:** `feat: expense ratio column, sorts and Costs panel on the Funds page`.

### Task 4: End-to-end check on the running app, docs, tracker

**Files:**
- Modify: `docs/design/AMFI_TER_PROGRESS.md` (M8 DONE), `docs/runbooks/amfi_ter.md` (what the page shows), `docs/design/AMFI_TER_PIPELINE.md` (Part B built)
- No new test files.

- [ ] **Step 1:** Start the API and the web app locally; load `/funds`; take screenshots of (a) the list sorted by lowest expense, (b) a regular-plan fund detail with trading costs, (c) a scheme with `not_covered`, (d) a flagged row. Confirm by eye each against an independent SQL read of the same scheme.
- [ ] **Step 2:** Run the whole backend suite CI-equivalent (`-m "not real_data"` on an empty TimescaleDB, `ci_equiv.sh`) and the web suite. Expected: green; CI skips every `real_data` test.
- [ ] **Step 3:** Update the three docs; commit `docs: AMFI TER Part B built (M8)`.
- [ ] **Step 4:** Final whole-branch review by a fresh reviewer on the most capable model; fix Critical and Important in one pass; list deferred minors.
