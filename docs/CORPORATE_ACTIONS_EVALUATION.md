# Corporate-actions source evaluation (ADR 0001, step 4)

Question: can we close the adjustment gap (mild bonuses, face-value splits the price detector misses) without
inventing data, and without making network calls?

## What exists offline

`D:\Stoxsy\data\stocktrack_data\cache\historical\corp_actions.parquet` - the NSE corporate-action feed saved by
the old StockTrack project: 26,688 rows, 2014-01-02 .. 2026-05-05, 2,703 symbols; columns `symbol, ex_date,
action_type, ratio_old, ratio_new, raw_purpose, adjustment_factor`.

Its own parser only understood bonuses: `bonus` 449, `split` 6, `rights` 283, `demerger` 97, `dividend`
15,211 and `unsupported` 10,642. Face-value splits (HDFCBANK 2019, IRCTC 2021, EICHERMOT 2020, ADANIPOWER 2025)
sit in `unsupported` with `adjustment_factor` 1.0, but the ratio is in `raw_purpose`
("Face Value Split (Sub-Division) - From Rs 10/- Per Share To Rs 2/- Per Share"). Re-parsing the 86 text templates
(`qsde/ingestion/corp_actions_source.py`) gives **863 split/bonus rows + 1 consolidation** (vs 455 before),
including combinations ("Bonus 1:1 / Face Value Split From Rs 10 To Rs 5" -> 0.25).

## What the evaluation found about the first (price-only) detector

Measured against the source on the 1,195 events it had applied:

| Finding | Number |
|---|---|
| Applied events at factor exactly 0.8 | 458 (38%); only **3%** matched any NSE action |
| Cause | An NSE 20% lower circuit opens at ~0.80 of the previous close and stays there: indistinguishable from a 1:4 bonus by price alone |
| Match rate for liquid (>= Rs10cr) factor-0.8 events | 4% |
| Factors 0.2 / 0.1 matched only 3-4% | because the source mislabelled those face-value splits `unsupported`; real |
| Recall of price-only on source events with factor <= 0.8 | 89% (332 of 372) |
| Source events in the mild class (0.8-0.97) | 29: never detectable from price |

Conclusion: price-only detection is trustworthy only for gaps larger than a circuit limit; everything between
0.78 and 0.97 needs the source.

## Method `source_confirmed_v2` (what is now applied)

1. **Source decides, prices confirm.** Each parsed split/bonus/consolidation row is checked against the overnight gap
   (`open / previous close`) within +-2 sessions of the listed ex-date; tolerance 10% (6% for mild factors).
   Confirmed -> applied with the **exact** parsed factor and the session whose gap matched (`ex_date_shift`).
   No gap -> `SOURCE_UNCONFIRMED` (logged, not applied).
2. **Price-inferred events only for gaps beyond the circuit limit** (open ratio <= 0.78, snapped to a short list
   of common ratios, with persistence and day-range checks). 4/5 removed from the list.
3. **Merge:** an inferred event next to a source split row is dropped (source decides); next to a source
   demerger/scheme row it becomes `UNEXPLAINED` (not applied).
4. Not adjusted, by design: rights, cash dividends, demergers/schemes, symbol changes.

## Results of the rebuild (2026-10-18)

| | v1 price-only | v2 source-first |
|---|---|---|
| Events applied | 1,195 | **772** |
| of which from the NSE source, price-confirmed, exact factor | 0 | **640** (incl. 15 mild bonuses 0.8-0.97 the price detector could never see) |
| of which inferred from prices only (gap <= 0.78, no source row) | 1,195 | **132** (34 traded >= Rs10cr on the ex-date) |
| Applied at factor 0.8 (circuit-limit false positives) | 458 | **0 inferred** (10 source-confirmed genuine 1:4 bonuses) |
| Source split/bonus rows listed but NOT visible in prices | n/a | **85** `SOURCE_UNCONFIRMED` (logged, not applied) |
| Gaps logged `UNEXPLAINED` (not applied) | 132 | 91 |
| Liquid days (>= Rs5cr) with an adjusted move >= 30% | 70 | **62** |

Source recall (verified): 830 parsed split/bonus rows (same-day listings merged); 725 fall inside the price window
(2016-01-05 .. 2026-04-30) and every one produced an event row: **640 confirmed by prices and applied (88%)**,
85 `SOURCE_UNCONFIRMED` (12%). The other 105 predate the price archive (2014-2015).

Known remaining issues (documented, not hidden):
* The source itself is incomplete: 25-64 split/bonus rows per year, far fewer than the market produced, so the 132
  price-inferred events are the only handling those real actions get. Several of the liquid ones are demergers or
  rights-related drops rather than ratios (TATAMOTORS 2025-10-14 -> 0.6, KPIT 2019, CROMPGREAV 2016). Adjusting a
  demerger by its measured gap matches vendor practice but cannot be verified; they are tagged
  `source='price_inferred'` so they can be excluded wholesale.
* The 85 `SOURCE_UNCONFIRMED` rows need a look (illiquid names with non-consecutive sessions, symbol renames, or an
  ex-date more than 2 sessions from the gap).
* Rights issues and cash dividends are still not adjusted; `adj_close == close`.

Effect downstream: the pilot feature snapshot was rebuilt on the corrected prices
(`20261018T114706Z-d92fb0f0e9`, dataset hash `d92fb0f0...`); the earlier snapshot is no longer current. Its hash differs
because the input fingerprint (corporate-action events) changed, which is the intended behaviour.
