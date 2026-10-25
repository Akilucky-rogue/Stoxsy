"""Load the official NSE symbol-change list into `symbol_changes` and reconcile the price-archive candidates against it.

The list is NSE's public file (https://nsearchives.nseindia.com/content/equities/symbolchange.csv):
NAME OF THE COMPANY, OLD SYMBOL, NEW SYMBOL, EFFECTIVE DATE. It has no ISIN. Fund-series rows (mutual-fund FTF plans)
are kept but typed `fund_series` and are irrelevant to equities.

    python scripts/load_nse_symbol_changes.py                       # parse the stored file, load, reconcile
    python scripts/load_nse_symbol_changes.py --fetch               # one HTTP GET first (public NSE archive)
"""
from __future__ import annotations

import argparse
import hashlib
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from qsde.config.paths import data_path

import pandas as pd

URL = "https://nsearchives.nseindia.com/content/equities/symbolchange.csv"
DEFAULT_FILE = data_path("nse", "symbolchange.csv")
SOURCE = "nse_symbolchange_csv"
FUND_HINT = ("MF", "FTF", "Plan", "SERIES", "Sr.", "SCHEME", "FUND", "ETF")


def parse(path: Path):
    raw = pd.read_csv(path, dtype=str, skipinitialspace=True, on_bad_lines="skip")
    raw.columns = [c.strip().upper() for c in raw.columns]
    df = pd.DataFrame({"name": raw["NAME OF THE COMPANY"].str.strip(), "old_symbol": raw["OLD SYMBOL"].str.strip(),
                       "new_symbol": raw["NEW SYMBOL"].str.strip(),
                       "effective_date": pd.to_datetime(raw["EFFECTIVE DATE"].str.strip(), format="%d-%b-%Y", errors="coerce")})
    bad = df["effective_date"].isna() | df["old_symbol"].isna() | df["new_symbol"].isna()
    df = df[~bad & (df["old_symbol"] != df["new_symbol"])].drop_duplicates(["old_symbol", "new_symbol", "effective_date"])
    df["change_type"] = df["name"].map(lambda n: "fund_series" if any(h in str(n) for h in FUND_HINT) else "symbol_change")
    return df.reset_index(drop=True), int(bad.sum())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default=str(DEFAULT_FILE))
    ap.add_argument("--fetch", action="store_true")
    args = ap.parse_args()
    path = Path(args.file)
    if args.fetch:
        import urllib.request
        req = urllib.request.Request(URL, headers={"User-Agent": "Mozilla/5.0", "Accept": "text/csv,*/*"})
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(urllib.request.urlopen(req, timeout=40).read())
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    retrieved = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    df, n_bad = parse(path)

    from qsde.db.connection import execute_sql, read_sql

    execute_sql("DELETE FROM symbol_changes WHERE source = %(s)s", {"s": SOURCE})
    for r in df.itertuples():
        execute_sql("""INSERT INTO symbol_changes (old_symbol, new_symbol, effective_date, effective_after, status, evidence,
                          source, source_url, retrieved_at, verified, verification_status, change_type, notes)
                       VALUES (%(o)s,%(n)s,%(d)s,%(d)s,'official',%(ev)s,%(src)s,%(u)s,%(t)s,TRUE,'official',%(ct)s,%(nm)s)
                       ON CONFLICT DO NOTHING""",
                    {"o": r.old_symbol, "n": r.new_symbol, "d": r.effective_date.date(), "ev": "file sha256 " + sha[:16],
                     "src": SOURCE, "u": URL, "t": retrieved, "ct": r.change_type, "nm": r.name})
    print("loaded %d official rows (%d unparsable lines skipped); sha256 %s" % (len(df), n_bad, sha[:16]))

    # reconcile the price-archive candidates against the official list
    cand = read_sql("SELECT id, old_symbol, new_symbol, effective_after FROM symbol_changes WHERE source <> :s",
                    params={"s": SOURCE})
    off = df[df["change_type"] == "symbol_change"]
    pairs = {(r.old_symbol, r.new_symbol): r.effective_date for r in off.itertuples()}
    by_old = {}
    for (o, n), d in pairs.items():
        by_old.setdefault(o, []).append((n, d))

    def reachable(o, n, depth=4):
        stack = [(o, 0)]
        while stack:
            cur, k = stack.pop()
            for nxt, _ in by_old.get(cur, []):
                if nxt == n:
                    return True
                if k < depth:
                    stack.append((nxt, k + 1))
        return False

    res = []
    for r in cand.itertuples():
        if (r.old_symbol, r.new_symbol) in pairs:
            d = pairs[(r.old_symbol, r.new_symbol)]
            execute_sql("UPDATE symbol_changes SET verification_status='official', verified=TRUE, effective_date=%(d)s, "
                        "notes=%(n)s WHERE id=%(i)s",
                        {"d": d.date(), "i": r.id,
                         "n": "matches NSE list (%s); price-archive last bar under old symbol %s" % (d.date(), r.effective_after)})
            res.append((r.old_symbol, r.new_symbol, "official", d.date(), r.effective_after))
        elif reachable(r.old_symbol, r.new_symbol):
            execute_sql("UPDATE symbol_changes SET verification_status='official', verified=TRUE, notes=%(n)s WHERE id=%(i)s",
                        {"i": r.id, "n": "reachable through a chain of NSE-listed changes"})
            res.append((r.old_symbol, r.new_symbol, "official(chain)", None, r.effective_after))
        else:
            execute_sql("UPDATE symbol_changes SET notes=%(n)s WHERE id=%(i)s",
                        {"i": r.id, "n": "NOT in the NSE symbol-change list: remains inferred, unused"})
            res.append((r.old_symbol, r.new_symbol, "inferred (not in NSE list)", None, r.effective_after))
    print(pd.DataFrame(res, columns=["old", "new", "status", "nse_effective_date", "archive_last_bar_old"]).to_string(index=False))

    uni = read_sql("SELECT DISTINCT symbol FROM universe_pit WHERE adv_cr >= 10")["symbol"]
    s = set(uni)
    hit = off[off["old_symbol"].isin(s) | off["new_symbol"].isin(s)]
    print("\nofficial equity symbol changes touching universe symbols (adv>=10cr): %d" % len(hit))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
