# Secret rotation checklist (names only; no values are stored here)

Status as of 2026-10-17. Owner action required for every item: only you can log in to these
providers. After rotating, put the new value in `D:\Stoxsy\stoxsy\.env` (git-ignored) and
confirm the old one no longer works.

## What was found

| Where | What | Evidence |
|---|---|---|
| Original `stoxsy\.env` (not copied to the new workspace) | Live values for FMP, Finnhub, FRED, Telegram bot, Kite API key + secret, Choice client id | Variable names listed; values present |
| `StockTrack_archive\.env` (not copied) | Personal free-tier keys | `.env.example` in that repo says so |
| `StockTrack_archive` git history (remote `Akilucky-rogue/StockScreener`) | `CHOICE_API_KEY` and `CHOICE_API_KEY_OFFICE` committed in `.env.example` (commits `3b42e6c`, `4d5a480`), base64-looking, 127/129 chars | Scan of history; values redacted in the workspace copy |
| `stoxy_legacy_archive\AlgoTrader_Finnhub.jsx` | Finnhub token hard-coded in source | Redacted in the workspace copy; original untouched |
| `StockVision\client_secret_*.json` | Google OAuth client secret | File not copied |
| `stoxsy` git history | Clean: `.env` never committed; the only key-like strings are placeholders | History scan, `c97b40f` |

## Rotate (in this order)

1. **Choice FinX** API keys (both) and client id: broker credential, highest impact.
2. **Kite** API secret: Kite developer console, regenerate, then update `KITE_API_SECRET`.
3. **Telegram** bot token: BotFather `/revoke`, then set `TELEGRAM_BOT_TOKEN`; set `TELEGRAM_CHAT_ID`.
4. **Finnhub**, **FMP**, **FRED**, **Alpha Vantage**: regenerate on each provider dashboard.
5. **Google OAuth** client (StockVision): reset the client secret or delete the client in Google Cloud console.

## Clean up (only after you say so)

- `StockScreener` repo history contains the Choice keys. Rotating them makes them harmless; rewriting
  history (`git filter-repo` + force-push) is optional and destructive, so it will only be done on your
  explicit go-ahead, on a backup clone first.
- Delete the original `.env`, `StockTrack_archive\.env` and the StockVision JSON once the new workspace
  is verified and the keys are rotated.

## Going forward

- `.env` stays git-ignored; `.env.example` holds placeholders only.
- A secret scanner (gitleaks) pre-commit hook is added in Phase 11 hardening.
