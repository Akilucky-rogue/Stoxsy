# Daily operations (until the Debian host takes over)

Commands are `stoxsy <command>`. A new PowerShell window does not find it by default; any one of these works:
- `C:\Stoxsy\.venv\Scripts\Activate.ps1` once per window (puts `stoxsy` on the PATH), or
- `cd C:\Stoxsy` then `.\stoxsy <command>` (the `stoxsy.cmd` launcher sits there; PowerShell needs the `.\`), or
- add `C:\Stoxsy` to your user PATH once: `[Environment]::SetEnvironmentVariable("Path", $env:Path + ";C:\Stoxsy", "User")`, then open a new window.

Exit codes: 0 ok, 1 warnings, 2 failures. `STOXSY_ROOT` no longer needs to be set: the data root is found from the repository's location (see `qsde/config/paths.py`).

## Every trading morning (before 09:10 IST)
1. **`kite login`**: the Kite token lapses at 06:00 IST every day, so the login is a daily manual step. It starts the local API if needed, opens Zerodha's login page, waits for the session to register, and tells you how many hours it is valid.
2. **`stream start`**: starts the live minute-bar stream for the point-in-time top-500 universe, waits for its first heartbeat, and refuses to start without a valid session.
3. **`doctor`**: everything should be OK except items you already know about.

Keep the PC **plugged in and awake** until 15:45 IST (Settings > Power: never sleep while plugged in). A stream that stops because the machine slept is an incident: `doctor` and the alerts show a stale heartbeat, and live bars after that point are filled from Kite history at the close.

## Every evening (after 19:00 IST)
1. **`ingest daily`**: prices, corporate actions, rebuild of the official series, point-in-time universe, signal, prediction logging (before the next 09:15 open), resolution, fund snapshot, alerts. Each step is logged under `data\reports\daily`. Safe to re-run the same day (an identical re-log is not an error).
2. **`stream stop`** if it is still running, then the after-close reconcile runs as part of the daily routine when bars were streamed.
3. **`doctor`**, then **`alerts`** to read what is open.

## When something is wrong
| Sign | Meaning | Do |
|---|---|---|
| `heartbeat stream` WARN/FAIL during the session | stream stopped or the machine slept | `stream status`, `stream start`; check the Kite session |
| a price dataset FAIL in `doctor` | the daily routine has not run | `ingest daily`; the F&O step looks back 10 days by itself |
| signal date older than prices | the point-in-time universe is stale | `ingest daily --only universe,signal,log_cards` |
| `Kite session` FAIL | token lapsed | `kite login` |
| disk below 20 GB | space | clear old reports; check `data\ohlcv_versions` and landing |

## Backups
`backup` writes a compressed dump under `data\backup`. Copy it to a second disk and to cloud storage; **never rely on a USB-attached drive as the only copy**, and restore into a scratch database to prove a dump works before trusting it. The repository is pushed to GitHub after every commit.
