# AMFI expense-ratio (TER) pipeline: how the data flows

Status 2026-10-30. Part A (ingestion) is built and loaded on real data; Parts B (API and Funds page) and C (cost-aware ranking) are not started. Design rules: `AMFI_TER_DESIGN.md`. Progress: `AMFI_TER_PROGRESS.md`. Operator steps: `docs/runbooks/amfi_ter.md`.
Real data only: nothing in this pipeline is synthetic, and no AMFI value is committed to the repository.

## 1. The flow

```
 AMFI "TER of MF Schemes" page   (the only source)
        |
        |  (a) owner saves "Download Excel" by hand          (b) `stoxsy fetch-ter`  (owner-run, ~monthly)
        |                                                        month list -> plan() -> ONE workbook request per month
        |                                                        -> check_payload (type, zip, workbook.xml) -> save as ter_revised_MM-YYYY__<date>.xlsx
        v                                                        v
 +-------------------------------------------------------------------------------------------------------+
 | INBOX  data/inbox/amfi_ter/      ignores ~$ lock files, .part/.crdownload/.tmp, files younger than 60 s |
 +-------------------------------------------------------------------------------------------------------+
        |   nightly: daily_after_close step `amfi_ter_inbox` (not required)   or by hand: load_amfi_ter.py --inbox
        v
 1  SHA-256 of the bytes already in the ledger?  -> "already loaded", stop            (before any parsing: cost stays flat)
 2  read_export  (pure, touches no database)      header found by the NSDL column; xlsx or csv; footer cut at the first blank row;
                                                  dates and numbers parsed; bad rows rejected (>2% rejected = whole file refused);
                                                  duplicate (code, date) rows rejected, first kept; UTF-16 / corrupt / unknown file = refused with a message
 3  layout gate   column-header hash approved?   no -> REFUSED (message, not a failure) until a person runs --dry-run, checks the column mapping, then --mapping-ok
 4  land the raw bytes unchanged     data/landing/amfi_ter/YYYY/MM/DD/..., origin recorded (fetch_ter or manual_download)
 5  ONE all-or-nothing database transaction
        mf_ter_layouts   approved layouts
        mf_ter_files     ledger: one row per file (hash, counts, rejected, flagged, overlap and restatement counts, landing path, retrieved_at)
        mf_ter_scheme    one row per NSDL scheme code (names seen, first/last seen)
        mf_ter           daily rows, PK (nsdl_code, ter_date); ten exact NUMERIC values + flags + file_id + known_at; first-seen kept, new days and new schemes inserted
        mf_ter_map       NSDL code -> mf_scheme_master scheme_code per plan (name_key_v1 + option-word rule + ETF single-plan rule); rebuilt only by the NEWEST file
        mf_ter_unmatched what could not be matched, by reason
 6  checks   `stoxsy doctor`: "fund expense ratios" (latest COMPLETE day, coverage with 7-day carry-forward, stale after 45 days, files waiting); report data/reports/amfi/amfi_ter.md
        |
        v   (not built yet)
 Part B: API + Funds page   (components shown separately: base ratio, brokerage, transaction cost, GST, total)
 Part C: cost-aware peer ranking (migration 064)
```

## 2. What each stage guarantees (and the test that proves it, all on real files)

| Guarantee | How |
|---|---|
| Re-running changes nothing | file identity is the SHA-256 of its bytes; rows are keyed (code, date) with ON CONFLICT DO NOTHING |
| A bad file cannot half-load | one transaction per file; a refused file is rejected before anything is written; one bad file does not stop the others in the inbox |
| No published value is ever changed | values stored exactly as published (NUMERIC); problems are flags (`total_gt_3_regular`, `direct_not_below_regular`, `sum_mismatch`), never corrections; an all-zero plan means "not offered" (NULL) |
| The original is always recoverable | raw bytes landed before parsing, path in the ledger |
| A changed layout is noticed | header hash gate: unknown layout is refused until reviewed |
| Partial days do not mislead | a day with under 70% of the recent median is partial; coverage uses the latest COMPLETE day |
| Late filings are absorbed | AMFI adds rows after the fact: observed 2026-10-30 (see section 4); new rows are inserted and stamped with `known_at` |

## 3. Foundation audit: what is solid, what is not yet perfect

Solid (verified on real files): the reader on four months, the overlap and duplicate paths, CSV equals workbook, same month in two formats adds nothing, all-or-nothing, wedge-proof inbox, exact values.

Open items, most important first. "Decision" means it changes the data model and is yours to make.

| # | Item | Why it matters | Proposed fix |
|---|---|---|---|
| H1 | **Restated values are counted, not kept.** If AMFI changes an already-published value, the first-seen value stays and the new one survives only in the landed raw file. | Part C ranks on these numbers, and the project rule is point-in-time (no lookahead). A revision history is the standard answer. No real restatement has been observed yet (0 in four months). | Decision: add an append-only `mf_ter_revisions` table (old, new, file_id, known_at) written in the same transaction. Small, additive, no change to existing reads. |
| H2 | `known_at` is when WE loaded a row, not when AMFI published it (unknowable from the file). | Conservative for no-lookahead, so safe; but a row backfilled for 1 Oct is "known" on 30 Oct. | Keep; state it in Part B and C docs. |
| H3 | One source (AMFI). No cross-check. | A wrong AMFI figure would pass. | Flags already catch total > 3% and sum mismatch. A second source (fund factsheets) is a later, separate decision. |
| H4 | Daily rows are mostly repeats (a TER is a standing rate). | 250k rows in 4 months, 57 MB; about 190 MB a year: fine for years. | No change. A change-only table can be derived later. |
| H5 | Timing logic uses `date.today()` (machine time) while AMFI days are IST. | Wrong only within hours of midnight. | Deferred minor. |
| H6 | Inbox "settled" check uses the file's modified time; an open `~$x.xlsx` does not stop `x.xlsx` loading. | A half-saved file could be read. The 60-second rule and the corrupt-file refusal contain it. | Deferred minors. |
| H7 | The TER is not yet in `factor_pit`. | Project rule 1: factor reads go through `factor_pit`. Part C must publish there with `valid_from = known_at`. | Part C design. |
| H8 | Unverified on real input: decimal-comma numbers, UTF-16, corrupt workbook, in-file conflicting duplicates (the real duplicates seen so far had identical values). | Refusal paths exist but were not exercised by a real file. | Keep refusing; add a real example if AMFI ever sends one. |

## 4. What real data taught us (2026-10-30)

- AMFI's October file downloaded at 01:35 held 58,095 rows; the same month at 16:25 held 59,942. The extra 1,847 rows were 796 schemes' rows for 29 Oct (the last day was still being filed) and about 150 schemes' rows for 1 to 7 Oct (late filers). Nothing already held changed.
- Consequence for the schedule: the last two or three days of a month are incomplete until AMFI finishes filing. `fetch-ter` therefore fetches a finished month once more at least three days after it ends (`SETTLE_DAYS = 3`), and fetches the current month when its newest loaded day is more than two days old.
- Nine rows in July repeat a scheme and day with identical values: one fund, renamed, under two names. The first is kept.
- Brokerage and transaction costs are monthly cumulative averages that reset each month. "Statutory levies" is not a clean GST figure: with no trading costs it is about 18% of the base ratio (the GST on it; median 0.178 on 15 Oct 2026), with trading costs it is 0.17 to 0.76 of base because it also carries trade-related charges. So the stable fund cost is the base expense ratio (BER) alone. Part B shows every component as published; Part C ranks on the base ratio (assumed 2026-10-30; a flat GST rate would not change the order; change it if you disagree).

## 5. Scale and flexibility

- Volume: about 2,200 schemes by about 365 days = roughly 800k rows a year. Postgres handles that without partitioning; a hypertable is not needed.
- Nightly cost: flat. Already-loaded files are recognised by hash before parsing.
- A new month is one command (or one saved file). A new column layout is one reviewed approval. A new source would add its own reader producing the same `Export` frame; the loader, ledger and mapping do not change.
