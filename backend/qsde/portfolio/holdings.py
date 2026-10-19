"""Parse a broker holdings export (CSV text) into validated holdings. Pure: symbol resolution is given the set of known symbols and an ISIN map.

Accepted layouts (headers matched loosely): Zerodha Kite `Instrument, Qty., Avg. cost, ...`, Zerodha console `Symbol, ISIN, Quantity Available, Average Price, ...`,
and a plain `symbol, quantity, avg_cost[, isin]`. Nothing is guessed silently: every row that is dropped or changed is reported with a reason.
"""
from __future__ import annotations

import csv
import hashlib
import io
import math
import re
from dataclasses import dataclass, field
from typing import Optional

MAX_BYTES = 1_000_000
MAX_ROWS = 2000

SYMBOL_COLS = ["instrument", "symbol", "tradingsymbol", "scripname", "scrip", "stock", "security", "securityname"]
QTY_COLS = ["qty", "quantity", "shares", "holdingqty", "totalqty", "quantityavailable"]
COST_COLS = ["avgcost", "averageprice", "avgprice", "buyaverage", "averagecost", "avgcostprice", "buyprice", "avgbuyprice", "averagebuyprice"]
ISIN_COLS = ["isin"]
SUFFIX = re.compile(r"-(?:BE|EQ|BZ|SM|ST|N1|N2|IL)$")


def _norm(h: str) -> str:
    return re.sub(r"[^a-z0-9]", "", h.lower())


def _num(x: str) -> Optional[float]:
    s = re.sub(r"[₹,\s]", "", str(x)).replace("(", "-").replace(")", "")
    if s in ("", "-", "--", "nan", "NaN", "None"):
        return None
    try:
        v = float(s)
    except ValueError:
        return None
    return v if math.isfinite(v) else None


@dataclass
class Parsed:
    holdings: list = field(default_factory=list)       # {"symbol","isin","quantity","avg_cost"}
    issues: list = field(default_factory=list)         # {"row","symbol","severity": "rejected"|"warning","reason"}
    n_rows: int = 0
    sha256: str = ""
    quantity_rule: str = ""

    @property
    def n_rejected(self) -> int:
        return sum(1 for i in self.issues if i["severity"] == "rejected")


def _pick(norm_headers: dict, candidates: list) -> Optional[str]:
    for c in candidates:
        if c in norm_headers:
            return norm_headers[c]
    return None


def parse_holdings(text: str, known_symbols: set, isin_to_symbol: Optional[dict] = None, renamed: Optional[dict] = None) -> Parsed:
    """known_symbols: NSE symbols with price history. isin_to_symbol: fallback resolution. renamed: {old_symbol: new_symbol}."""
    out = Parsed()
    raw = text.encode("utf-8", errors="replace")
    out.sha256 = hashlib.sha256(raw).hexdigest()
    if len(raw) > MAX_BYTES:
        raise ValueError(f"file is larger than {MAX_BYTES // 1000} KB")
    text = text.lstrip("﻿")
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    rows = list(csv.reader(io.StringIO(text), dialect))
    rows = [r for r in rows if any(c.strip() for c in r)]
    if len(rows) < 2:
        raise ValueError("no data rows found (expected a header row and at least one holding)")
    header = rows[0]
    nh = {_norm(h): i for i, h in enumerate(header) if h.strip()}
    sym_i = next((nh[c] for c in SYMBOL_COLS if c in nh), None)
    qty_i = next((nh[c] for c in QTY_COLS if c in nh), None)
    cost_i = next((nh[c] for c in COST_COLS if c in nh), None)
    isin_i = next((nh[c] for c in ISIN_COLS if c in nh), None)
    if sym_i is None or qty_i is None or cost_i is None:
        missing = [n for n, i in (("symbol/instrument", sym_i), ("quantity", qty_i), ("average cost", cost_i)) if i is None]
        raise ValueError("could not find column(s): " + ", ".join(missing) + f". Headers seen: {', '.join(h.strip() for h in header if h.strip())}")
    if "quantityavailable" in nh and _norm(header[qty_i]) == "quantityavailable":
        out.quantity_rule = "uses 'Quantity Available' only: pledged / long-term quantities in the file are NOT added (add them to the file if you hold pledged shares)"
        out.issues.append({"row": 1, "symbol": "", "severity": "warning", "reason": out.quantity_rule})
    body = rows[1:]
    if len(body) > MAX_ROWS:
        raise ValueError(f"more than {MAX_ROWS} rows")
    out.n_rows = len(body)
    agg: dict = {}
    for k, r in enumerate(body, start=2):
        get = lambda i: r[i].strip() if i is not None and i < len(r) else ""      # noqa: E731
        raw_sym, isin = get(sym_i), get(isin_i).upper()
        sym = SUFFIX.sub("", raw_sym.upper().replace(" ", ""))
        if not sym or sym.lower() in ("total", "totals", "grand total"):
            continue
        q, c = _num(get(qty_i)), _num(get(cost_i))
        if q is None or c is None:
            out.issues.append({"row": k, "symbol": sym, "severity": "rejected", "reason": "quantity or average cost is missing or not a number"})
            continue
        if q <= 0:
            out.issues.append({"row": k, "symbol": sym, "severity": "rejected", "reason": f"quantity {q:g} is not positive (short or empty positions are not supported)"})
            continue
        if c < 0:
            out.issues.append({"row": k, "symbol": sym, "severity": "rejected", "reason": "average cost is negative"})
            continue
        if sym not in known_symbols:
            alt = (renamed or {}).get(sym)
            by_isin = (isin_to_symbol or {}).get(isin) if isin else None
            if alt in known_symbols:
                out.issues.append({"row": k, "symbol": sym, "severity": "warning", "reason": f"symbol renamed: using {alt}"})
                sym = alt
            elif by_isin in known_symbols:
                out.issues.append({"row": k, "symbol": sym, "severity": "warning", "reason": f"symbol not found; resolved by ISIN {isin} to {by_isin}"})
                sym = by_isin
            else:
                out.issues.append({"row": k, "symbol": sym, "severity": "rejected",
                                   "reason": "no NSE price history for this symbol (mutual funds, BSE-only names, bonds and unlisted holdings are not supported here)"})
                continue
        if sym in agg:
            a = agg[sym]
            tot = a["quantity"] + q
            a["avg_cost"] = (a["avg_cost"] * a["quantity"] + c * q) / tot
            a["quantity"] = tot
            out.issues.append({"row": k, "symbol": sym, "severity": "warning", "reason": "duplicate symbol: quantities added, average cost weighted"})
        else:
            agg[sym] = {"symbol": sym, "isin": isin or None, "quantity": q, "avg_cost": c}
    out.holdings = sorted(agg.values(), key=lambda h: h["symbol"])
    if not out.holdings:
        raise ValueError("no valid holdings found: " + "; ".join(f"{i['symbol']}: {i['reason']}" for i in out.issues[:3]))
    return out
