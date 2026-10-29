# BSE corporate actions: how to load and what the result means

BSE's JSON API for corporate actions sits behind bot protection that rejects automated clients (HTTP 403 "Access Denied"). We do not try to get around it. The source is still BSE's own Corporate Actions
data: you download the export from BSE's website, and the pipeline does the rest.

## Download (page checked 2026-10-23)

1. Open https://www.bseindia.com/corporates/corporate_act in your normal browser (Chrome).
2. Set the form exactly like this and leave everything else on its default:
   - **Segment**: Equity
   - **Select Date**: Ex Date
   - **From Date** / **To Date**: type the period as DD/MM/YYYY. Start with the whole span you want (for example 01/01/2016 to today).
   - **Security Name**, **Industry**, **Purpose**: leave empty / `--Select--` (you want every action and every company).
3. Click **Submit**. The table shows only the first rows; the page says "download the csv file to view all records".
4. Click the small **download icon** at the top right of the "Corporate Actions" heading (next to the title, right edge). A CSV file downloads.
5. If BSE refuses a long range or the file looks truncated (the last Ex Date is not near your To Date), repeat steps 3-4 one year at a time (01/01/2016-31/12/2016, 01/01/2017-31/12/2017, ...). Several files are fine: re-loading
   overlapping rows is harmless (each action is stored once).
6. Segment other than Equity (for example F&O, currency, commodity) is not needed and not used.
7. Save every file into `D:\Stoxsy\data\inbox\bse_corporate_actions\` (any file name, .csv).

The columns on the page are exactly the ones the loader expects: Security Code, Security Name, Ex Date, Purpose, Record Date, BC Start Date, BC End Date, ND Start Date, ND End Date, Actual Payment Date.

## Load

1. `python backend/scripts/load_bse_corporate_actions.py --inbox --dry-run` and read the output: `column_mapping` must map every column above, `unmatched_headers` should be empty, and `sample` must show sensible
   security codes, ex-dates, purposes and action types.
2. `python backend/scripts/load_bse_corporate_actions.py --inbox --mapping-ok` (the first real file is refused without `--mapping-ok`).
3. Read `D:\Stoxsy\data\reports\bse\bse_corporate_actions.md`.

## What happens

The raw file is landed unchanged (SHA-256, retrieval time, `known_at`); rows are parsed (bad rows are listed, never dropped silently); actions are stored once each (content hash); each is reconciled against BSE raw prices
with the same hard bounds as NSE (gap bar within 5 sessions of the ex-date, factor within 5% / 3%, at most 5 untraded sessions, 3-session persistence).
Verdicts: `confirmed` (prices agree with the listed action), `rejected` (prices contradict it), `needs_review` (cannot be tested: rights, demergers / schemes, buy-backs, unreadable text, no price bars).

Nothing is adjusted. `confirmed` is not an approval. A BSE adjusted-price / total-return materialisation is built only after this evidence is validated and the owner approves it. Until then BSE stays `raw_prices_only` and
BSE-only names stay out of the universe, the defensive signal and the decision cards. Excel exports need `openpyxl` (not installed): save as CSV instead.
