# BSE corporate actions: how to load and what the result means

BSE's JSON API for corporate actions sits behind bot protection that rejects automated clients (HTTP 403 "Access Denied"). We do not try to get around it. The source is still BSE's own
Corporate Actions data: the owner downloads the export from BSE's website and the pipeline does the rest.

1. Open BSE India > Corporates > Corporate Actions, choose the period (all equity, all purposes), and use the page's Download option (CSV).
2. Save the file in `D:\Stoxsy\data\inbox\bse_corporate_actions\` (any file name).
3. Run `python backend/scripts/load_bse_corporate_actions.py --inbox` (add `--dry-run` first to see the parse result without writing).
4. Read `D:\Stoxsy\data\reports\bse\bse_corporate_actions.md`.

What happens: the raw file is landed unchanged (SHA-256, retrieval time, `known_at`); rows are parsed (bad rows are listed, never dropped silently); actions are stored once each (content hash);
each is reconciled against BSE raw prices with the same hard bounds as NSE (gap bar within 5 sessions of the ex-date, factor within 5% / 3%, at most 5 untraded sessions, 3-session persistence).
Verdicts: `confirmed` (prices agree with the listed action), `rejected` (prices contradict it), `needs_review` (cannot be tested: rights, demergers / schemes, buy-backs, unreadable text, no price bars).

Nothing is adjusted. `confirmed` is not an approval. A BSE adjusted-price / total-return materialisation is built only after this evidence is validated and the owner approves it. Until then BSE stays
`raw_prices_only` and BSE-only names stay out of the universe, the defensive signal and the decision cards. Excel exports need `openpyxl` (not installed): save as CSV instead.
