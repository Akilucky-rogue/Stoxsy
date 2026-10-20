# Owner checklist: what only you can do

Everything else is automated or done. Items are in the order that unblocks the most.

## One-time

1. **Rotate the exposed credentials** (provider consoles; nothing here can do it for you): FMP, Finnhub, FRED, Alpha Vantage, Telegram bot, Choice, Kite API secret, Google OAuth client secret.
   Put only the new values you actually need into `D:\Stoxsy\stoxsy\.env` (never paste them into chat). The current `.env` holds placeholders and nothing in the product needs them today.
   See `docs/SECURITY_ROTATION.md`. Then tell me "rotation done".
2. **Say "push"** after rotation. I re-run gitleaks on history, working tree and data, confirm the only hit is the local `.env` JWT secret, push `feat/stoxsy-v2`, and (with your go-ahead) enable branch
   protection and require the CI workflow (`.github/workflows/ci.yml`, not yet run on GitHub).
3. **Kite (F&O live data)**: in the Kite developer console create or open the app, set its redirect URL to the one in `.env` (`KITE_REDIRECT_URL`), put `KITE_API_KEY` and `KITE_API_SECRET` into `.env`,
   start the API, and complete the Kite login once. Then tell me: I check the session, load the instrument master and expiry calendar and build the F&O views. Live-order placement stays disabled.
   The Kite session lapses every morning (about 06:00 IST): repeat the login on days you want live F&O data.

## Decisions only you can make

4. **Bucket A, the remaining 59 events**: `D:\Stoxsy\data\reports\corp_actions\packet_bucket_a_decision_remaining.md` (+ `.csv`). Same compact format as the first ten (26 `confirm`, 29 `needs_more_evidence`,
   4 `reject` recommended). Send me confirm / hold / reject per event; I record them in `infra/data/corp_action_bucket_a_decisions.json`. `ohlcv` stays unchanged until all 69 are decided.
   After the last one I rebuild the official series once from the reconciled registry (versioned, prior series kept, before/after cliff and continuity reports for you to approve).

## Files only you can fetch

5. **BSE corporate actions**: download the export (CSV) from BSE's Corporate Actions page and save it in `D:\Stoxsy\data\inbox\bse_corporate_actions\`. Run
   `python backend/scripts/load_bse_corporate_actions.py --inbox --dry-run`, check `column_mapping` and `sample` against the real file, then re-run with `--mapping-ok`
   (see `docs/runbooks/bse_corporate_actions.md`). Until then BSE-only names stay out of the universe, signal and cards.

## Daily, after about 19:00 IST (or schedule it)

6. `python backend/scripts/daily_after_close.py` loads the day's NSE / BSE / F&O / AMFI data, rebuilds the signal, logs the predictions (this step must run before 09:15 IST next session),
   resolves finished windows, refreshes the fund snapshot and the alerts. Log: `D:\Stoxsy\data\reports\daily`. It has not yet been run end to end on a live day: run it once while watching.
   Optional: register it in Windows Task Scheduler for weekdays 19:30.
7. Look at **Alerts** in the app (bell icon). Anything critical needs you; warnings are worth a look.

## Later, when you choose

8. Approve sources for anything new (expense ratios, fund holdings, fundamentals, commodities / MCX). Each needs its own approval; none is loaded.
9. Promote a model, ever: promotion is disabled and needs the offline gate, live-paper evidence and your written decision.
