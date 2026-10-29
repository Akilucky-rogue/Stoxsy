# Importing broker files, assigning pots and reading tax lots

Equity holdings are held at **Choice Equity** (account label `choice`); **Zerodha Kite is used for live market data only** and its holdings are not read. Mutual fund statements use their own account label (for example `cams`). Labels are typed by you; nothing is preset.

Files carry client details. Keep them under `C:\Stoxsy\data\inbox\<account>\` (outside the repository), never commit them, and never paste them into a chat. Reports from Choice that start with a banner (client name, client code, PAN, DP id) are fine: the reader skips the banner and stores only counts, the header layout and column titles.

## Your user id
`--user` is the id the web app uses. With sign-in enforced (`AUTH_ENFORCED=true`, the current setting) it is the UUID of your account, not your email. Find it with the admin Users page, or `select id, email from users` in the database. With sign-in off it is `local`.

## Steps
1. **Dry run first** (writes nothing; shows the column mapping, counts and a few parsed rows):
   `stoxsy import holdings --user <id> --account choice --file <path> --dry-run`
2. **Check the mapping** against the real file, then load with `--mapping-ok`. That approval is stored per header layout: a different layout (a tradebook after a holdings file, or a changed export) needs its own dry run and approval.
3. **Assign every holding to a pot** in the same command, or later: `--investment A,B --trading C,D`, `--pot-all investment`, or `--triage-file file.csv` (columns `symbol,pot`); or `stoxsy triage set ...`; or the Tax lots page. A holding's pot is set once and cannot change while you hold it (database trigger). Until every holding has a pot, **new trade ideas stay locked**.
4. **Tradebook / lots:** `stoxsy import tradebook ...` (columns date, symbol, buy/sell, qty, rate; an ISIN column helps) or `stoxsy import lots ...` (the broker's own lot report: used as stated, never adjusted). Overlapping tradebooks do not double count.
5. **Mutual funds:** `stoxsy import mf ...` (folio, ISIN, date, transaction type, units/amount/NAV). A closing unit balance column, if present, lets the units be checked; without it they are marked unverified. PDF statements are refused (re-export as CSV or XLSX). `.xls` is refused (save as `.xlsx`).
6. **Read the result:** `stoxsy lots --user <id> [--account choice]`, or the Tax lots page.

Re-importing next week's holdings export is safe: the pots you assigned, and the stops you recorded on the trader desk, carry over to the new snapshot; a position that was sold empties its pot.

## What the status words mean
| status | meaning |
|---|---|
| RECONCILED | lots equal what the holdings snapshot says you hold |
| UNRECONCILED_LOT_GAP | you hold more than the files explain; the missing quantity is shown as an undated lot (no date or cost is guessed). Load an older tradebook or the broker's lot report |
| UNRECONCILED_SURPLUS | the files imply more than you hold (sold or transferred elsewhere, or a corporate action was not applied) |
| NEEDS_REVIEW | a split/bonus could not be applied (terms not understood, or not confirmed against prices) |
| DATE_PENDING | holding known, purchase date not loaded |
| UNVERIFIED | no holdings snapshot or closing balance to check against |

## Rules the lots follow
- FIFO per security. Equity lots follow the symbol through renames and the new ISIN an exchange issues at a split. Inventory never goes negative.
- **Split:** quantity and cost are rescaled, the purchase date is kept. **Bonus:** the original lot is untouched and a new zero-cost lot is added at the ex-date (the allotment date is not in the NSE feed), marked as a proxy. Only corporate actions confirmed against prices are applied.
- **Broker reports that are already restated** (Choice restates history for a consolidation or split): load them as they are and do not rely on our corporate-action feed for those names. CCAVENUE's October 2026 consolidation is an example; it is not in our feed yet.
- **Rights issues** (DUCON): the allotted shares appear as an undated gap lot until you load the rights issue price and allotment date.
- **Holding period:** more than 12 calendar months = long-term (acquisition date + 12 months + 1 day). Lots within 35 days of turning long-term are flagged. Debt, hybrid and other funds are not classified. Rules are in `infra/data/tax_policy.json` and are **UNVERIFIED**: confirm with your chartered accountant. No tax amount and no annual exemption is computed.
- Opening-balance lots (held before the start of a statement) carry the statement start date as an upper bound, because the real date is not in the report.

## Choice Equity layouts seen so far
| report | status |
|---|---|
| Holding Overall Report (CSV): `Instrument, Exchange, QTY \| LOT, Avg. Price, LTP, Invested Amt., Current Value, Returns, Returns %, Product` | **works**; dry-run and imported 2026-10-25. Symbols carry an `-EQ` suffix, which is stripped. No purchase dates |
| Global Details Report (PDF, one row per security per date, BSE code) | not read by the importer. It was converted to a tradebook CSV by a local one-off script and checked against the holdings: 11 of 12 securities reconcile exactly |
| Global P&L Report (PDF), Account Ledger (PDF) | cross-checked only (realised total, cash movements); not imported |

The pasted conversions from AI Studio are not used: build the CSV from the original file and reconcile it to the holdings.
