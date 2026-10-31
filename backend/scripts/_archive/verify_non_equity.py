"""Verify the provisional non-equity list against NSE's official ETF list (already landed raw) and write the official list.

    python scripts/verify_non_equity.py --landed D:\\Stoxsy\\data\\landing\\nse_etf_list\\2026\\10\\19\\005642-9d90ef6e.csv

Outputs (both versioned, both recorded in the universe-version tag):
  infra/data/non_equity_official_etf_v1.json   every NSE-listed ETF symbol (ISIN INF*, underlying class), with the landed file's sha256
  infra/data/non_equity_instruments.json       the provisional name/behaviour list, each item stamped official_confirmed or
                                               not_on_current_official_list (an ETF that was delisted or merged is not on today's file)
Operating companies whose names merely contain GOLD / SILVER / GILT are never touched: only symbols on the official file or already on the
provisional list are excluded.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

DATA = Path(__file__).resolve().parents[2] / "infra" / "data"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--landed", required=True)
    args = ap.parse_args()
    raw = Path(args.landed).read_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    e = pd.read_csv(Path(args.landed))
    e.columns = [c.strip() for c in e.columns]
    e["Symbol"] = e["Symbol"].str.strip().str.upper()
    e = e.drop_duplicates("Symbol")
    official = {"version": "non_equity_official_v1", "source": "NSE eq_etfseclist.csv (official ETF list)",
                "source_url": "https://nsearchives.nseindia.com/content/equities/eq_etfseclist.csv", "landed_file": str(args.landed),
                "sha256": sha, "retrieved_note": "landed raw before parsing (data/landing/nse_etf_list)", "count": int(len(e)),
                "instruments": [{"symbol": r.Symbol, "isin": str(r.ISINNumber).strip(), "name": str(r.SecurityName).strip(),
                                 "underlying_class": str(r._8).strip() if hasattr(r, "_8") else ""} for r in e.itertuples(index=False)]}
    # itertuples renames "ETF Underlying" -> positional; rebuild the class column explicitly
    cls = dict(zip(e["Symbol"], e["ETF Underlying"].astype(str).str.strip()))
    for i in official["instruments"]:
        i["underlying_class"] = cls[i["symbol"]]
    (DATA / "non_equity_official_etf_v1.json").write_text(json.dumps(official, indent=1), encoding="utf-8")
    prov = json.loads((DATA / "non_equity_instruments.json").read_text(encoding="utf-8"))
    off = set(e["Symbol"])
    n_ok = 0
    for i in prov["instruments"]:
        ok = i["symbol"] in off
        n_ok += ok
        i["verification"] = "official_confirmed" if ok else "not_on_current_official_list"
    prov["status"] = f"verified_against_nse_official_etf_list: {n_ok} of {len(prov['instruments'])} confirmed; the rest are ETFs by name that are not on today's file (delisted or merged) and stay excluded"
    (DATA / "non_equity_instruments.json").write_text(json.dumps(prov, indent=2), encoding="utf-8")
    print(f"official ETFs {len(e)}; provisional confirmed {n_ok}/{len(prov['instruments'])}; sha256 {sha[:12]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
