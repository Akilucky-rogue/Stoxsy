# AMFI expense ratios (TER): how to download and load

AMFI publishes each scheme's total expense ratio daily on its "TER of MF Schemes" page. AMFI's terms allow personal, non-commercial use and forbid bulk electronic storage and automated access that it has not agreed, so the download is **manual** and the data stays on your PC (it is never committed to the repository). Design: `docs/design/AMFI_TER_DESIGN.md`.

## Download (one file per month; page checked 2026-10-30)
1. Open https://www.amfiindia.com/ter-of-mf-schemes in your normal browser.
2. Choose the **Financial Year**, then the **Month**. Leave **Fund Type**, **Category** and **Mutual Fund** on **All**.
3. Click **Go**, then **Download Excel**.
4. Save the file into `C:\Stoxsy\data\inbox\amfi_ter\` and keep the name AMFI gives it. If you download the current month, its last day may be incomplete; that is expected and handled (the loader marks that day partial and coverage uses the last complete day).
5. **Close the workbook** if you opened it: Excel leaves a `~$...` lock file and the loader ignores a file changed in the last minute. Do not edit or re-save the file; the loader reads AMFI's own bytes (saving a CSV copy next to it is fine, it is a separate file).

## Load
Run from `C:\Stoxsy\stoxsy`:

1. **Dry run** (writes nothing): `C:\Stoxsy\.venv\Scripts\python.exe backend\scripts\load_amfi_ter.py --inbox --dry-run`. Check:
   - `column_mapping` lists 15 columns and `unmatched_headers` is empty;
   - `data_rows` = `parsed` + `rejected` (rejected should be 0 or tiny; a file with more than 2 percent rejected is refused);
   - `footer_rows` is above 0 and `footer_head` starts with "Disclaimer" (the notes below the data are never read as data);
   - `dates` and `partial_dates` match what you downloaded;
   - `flag_counts` (rows with implausible published values; they are kept as published and flagged);
   - `ter_unmatched` and `master_gaps` (schemes that could not be linked, each with a reason);
   - `sample` shows real scheme names, dates and totals.
2. **Load**: `... load_amfi_ter.py --inbox`. The **first file of each layout is refused** until you have reviewed a dry run: repeat with `--mapping-ok`. Later files with the same layout load without it.
3. The loader prints `loaded` with the counts and writes `data\reports\amfi\amfi_ter.md` (coverage, files, flags, unmatched schemes by name). The report holds no TER values.
4. `stoxsy doctor` shows a **fund expense ratios** line: latest complete TER date, coverage of active schemes, files waiting. It warns (never fails) when the latest complete day is more than 45 days old or a file is waiting.

The daily after-close routine has a step `amfi_ter_inbox` that loads any new file already in the inbox. It never downloads anything, and a first file of a new layout is **refused with a message, not a failure**, until you run the dry run and `--mapping-ok` yourself.

## What the messages mean
| Message | Meaning | Do |
|---|---|---|
| `REFUSED: first file of this layout needs a reviewed mapping` | new layout, nothing written | dry run, check, then `--mapping-ok` |
| `already loaded` | this exact file (SHA-256) is already in the database | nothing |
| `ignored ...: Excel lock file / partial download / modified in the last 60 seconds` | not a finished file | close Excel / wait and run again |
| `REJECTED ...: no NSDL scheme code column` | not an AMFI TER download | remove it from the inbox |
| `REJECTED ...: N of M data rows rejected` | unreadable dates or numbers | download again; tell Claude if it repeats |
| `REJECTED ...: data after the footer` | the layout changed | tell Claude; do not edit the file |

## Good to know
- One TER scheme maps to several of your funds (growth and IDCW options, each plan). Schemes the file does not list, or whose plan the file does not offer, are listed in the report with a reason.
- Overlapping months are fine: the first value stored for a scheme and date stays; a later file that shows a different value is counted as a **restatement** in the report.
- Each file is landed unchanged under `data\landing\amfi_ter\` before anything is parsed, so a day can always be re-read from the original download.
- History starts at the earliest month you download; nothing is back-filled automatically.
