"""Turn the tables read by `tabular.read_table` into validated rows: holdings, equity trades, broker-reported lots, mutual fund transactions. Pure: symbol / scheme resolution is passed in.

Column titles are matched through alias lists, never assumed: a layout that matches fewer than the required fields is refused with the titles it did find (titles are not personal data).
The exact layout of a given broker's file has to be confirmed by the owner on a dry run before the first real load of that layout; these aliases are a starting point, not a promise.
Every dropped or changed row is reported with a reason. Reasons and issues never carry free text from the file (names, PAN, client codes): a symbol that does not look like a symbol is shown as '<unreadable>'.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Callable, Optional

from qsde.portfolio import holdings as H
from qsde.portfolio.tabular import Table, aliases

SUFFIX = H.SUFFIX
_SYMBOL_LOOKS_OK = re.compile(r"^[A-Z0-9&\-\.]{1,25}$")

HOLDINGS_SPEC = {"symbol": H.SYMBOL_COLS, "qty": [*H.QTY_COLS, "holdingquantity", "netqty", "netquantity", "currentqty", "balanceqty", "quantityfreepledged"],
                 "cost": H.COST_COLS, "isin": H.ISIN_COLS}
TRADE_SPEC = {
    "date": ["tradedate", "date", "trdate", "tradedt", "transactiondate", "businessdate"],
    "symbol": ["symbol", "tradingsymbol", "scrip", "scripname", "security", "securityname", "instrument", "stock", "stockname", "companyname"],
    "isin": ["isin"],
    "side": ["tradetype", "buysell", "buyorsell", "transactiontype", "side", "action", "bs"],
    "qty": ["quantity", "qty", "tradedqty", "tradequantity", "shares"],
    "price": ["price", "rate", "tradeprice", "tradedprice", "netrate", "marketrate"],
    "buy_qty": ["buyqty", "buyquantity"],
    "sell_qty": ["sellqty", "sellquantity"],
    "ref": ["tradeid", "tradeno", "tradenumber", "tradenum"],
    "time": ["ordertime", "orderexecutiontime", "tradetime", "time", "executiontime"],
    "segment": ["segment", "series", "product", "instrumenttype"],
}
LOT_SPEC = {
    "buy_date": ["buydate", "purchasedate", "acquisitiondate", "dateofpurchase", "boughtdate", "dateofacquisition"],
    "symbol": TRADE_SPEC["symbol"],
    "isin": ["isin"],
    "qty": ["quantity", "qty", "openqty", "balanceqty", "holdingqty", "unsoldqty", "units"],
    "cost": ["buyrate", "buyprice", "purchaseprice", "purchaserate", "costprice", "costrate", "rate", "price"],
    "value": ["buyvalue", "purchasevalue", "costvalue", "purchaseamount", "costamount"],
}
MF_SPEC = {
    "folio": ["folio", "foliono", "folionumber", "folionum"],
    "isin": ["isin"],
    "scheme": ["schemename", "scheme", "fundname", "schemedescription"],
    "date": ["date", "transactiondate", "trxndate", "tradedate", "txndate"],
    "type": ["trxntype", "transactiontype", "type", "trxndesc", "transaction", "transactiondescription", "description", "trxn"],
    "amount": ["amount", "value", "transactionamount", "trxnamount"],
    "units": ["units", "unit", "quantity", "trxnunits"],
    "nav": ["nav", "price", "navrate", "purchaseprice"],
    "closing": ["closingunitbalance", "closingunits", "unitbalance", "closingbalance"],
}
HOLDINGS_VOCAB, TRADE_VOCAB, LOT_VOCAB, MF_VOCAB = (aliases(s) for s in (HOLDINGS_SPEC, TRADE_SPEC, LOT_SPEC, MF_SPEC))
VOCAB = {"holdings": HOLDINGS_VOCAB, "tradebook": TRADE_VOCAB, "lots": LOT_VOCAB, "mf": MF_VOCAB}
REQUIRED = {"holdings": ("symbol", "qty", "cost"), "tradebook": ("date", "qty|buy_qty", "price", "symbol|isin"), "lots": ("buy_date", "qty", "cost|value", "symbol|isin"),
            "mf": ("date", "type", "units|amount", "isin")}
DERIVATIVE = ("fo", "fn", "fut", "opt", "der", "cd", "cur", "com", "mcx")


def missing_fields(kind: str, t: Table) -> list:
    out = []
    for req in REQUIRED[kind]:
        if not any(f in t.fields for f in req.split("|")):
            out.append(req.replace("|", " or "))
    return out


def safe_symbol(s: str) -> str:
    s = s.upper().replace(" ", "")
    return s if _SYMBOL_LOOKS_OK.match(s) else "<unreadable>"


def num(x) -> Optional[float]:
    return H._num(x)


_DATE_FORMATS = ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d-%b-%Y", "%d-%b-%y", "%d %b %Y", "%d-%B-%Y", "%d.%m.%Y", "%d/%m/%y", "%Y/%m/%d", "%Y-%m-%d %H:%M:%S")


def parse_date(s: str) -> Optional[date]:
    """Day-first (Indian) unless the year comes first; an unparseable or ambiguous-looking value is None, never guessed."""
    s = (s or "").strip()
    if not s:
        return None
    for f in _DATE_FORMATS:
        try:
            return datetime.strptime(s, f).date()
        except ValueError:
            continue
    return None


@dataclass
class Rows:
    rows: list = field(default_factory=list)
    issues: list = field(default_factory=list)
    n_rows: int = 0
    skipped_other_segment: int = 0
    closing: dict = field(default_factory=dict)       # mf: {(folio, isin): closing units from the statement}

    @property
    def n_rejected(self) -> int:
        return sum(1 for i in self.issues if i["severity"] == "rejected")


class Resolver:
    """Raw scrip text + ISIN -> the canonical NSE symbol. ISIN first (it survives renames), then the symbol as given, then official renames."""

    def __init__(self, known: set, by_isin: Optional[dict] = None, renamed: Optional[dict] = None):
        self.known, self.by_isin, self.renamed = known, by_isin or {}, renamed or {}

    def __call__(self, raw: str, isin: str) -> tuple:
        sym = SUFFIX.sub("", raw.upper().replace(" ", ""))
        isin = (isin or "").strip().upper()
        by = self.by_isin.get(isin) if isin else None
        if by and by in self.known:
            return by, (f"resolved by ISIN to {by}" if by != sym else "")
        if sym in self.known:
            return sym, ""
        alt = self.renamed.get(sym)
        if alt and alt in self.known:
            return alt, f"symbol renamed: using {alt}"
        return None, "no NSE listing found for this symbol or ISIN (mutual funds, bonds, unlisted and BSE-only names are not supported here)"


def _get(r, t: Table, name: str) -> str:
    i = t.fields.get(name)
    return r[i].strip() if i is not None and i < len(r) else ""


def _issue(out: Rows, row: int, sym: str, sev: str, reason: str) -> None:
    out.issues.append({"row": row, "symbol": sym, "severity": sev, "reason": reason})


def parse_holdings_table(t: Table, known: set, by_isin: dict, renamed: dict) -> H.Parsed:
    from qsde.portfolio.tabular import to_csv_text

    return H.parse_holdings(to_csv_text(t), known, by_isin, renamed, delimiter=",")


def parse_trades(t: Table, resolve: Callable) -> Rows:
    out = Rows(n_rows=len(t.rows))
    f = t.fields
    for k, r in enumerate(t.rows, start=t.header_row + 2):
        seg = re.sub(r"[^a-z]", "", _get(r, t, "segment").lower())
        if seg and seg.startswith(DERIVATIVE):
            out.skipped_other_segment += 1
            continue
        raw, isin = _get(r, t, "symbol"), _get(r, t, "isin").upper()
        shown = safe_symbol(raw) if raw else (isin or "<missing>")
        d = parse_date(_get(r, t, "date"))
        if d is None:
            _issue(out, k, shown, "rejected", "trade date is missing or not a recognised date")
            continue
        side, qty = _side_and_qty(r, t)
        if side is None:
            _issue(out, k, shown, "rejected", "could not tell buy from sell, or the quantity is missing / not positive")
            continue
        price = num(_get(r, t, "price"))
        if price is None or price < 0:
            _issue(out, k, shown, "rejected", "price is missing or not a number")
            continue
        sym, why = resolve(raw, isin)
        if sym is None:
            _issue(out, k, shown, "rejected", why)
            continue
        if why:
            _issue(out, k, shown, "warning", why)
        tm = _get(r, t, "time")
        out.rows.append({"asset": "equity", "symbol": sym, "isin": isin or None, "trade_date": d, "trade_time": tm[-8:] if tm else None, "side": side, "quantity": qty, "price": price,
                         "trade_ref": _get(r, t, "ref") or None, "folio": None, "scheme_name": None})
    return out


def _side_and_qty(r, t: Table) -> tuple:
    q = num(_get(r, t, "qty"))
    s = re.sub(r"[^a-z]", "", _get(r, t, "side").lower())
    if s in ("buy", "b", "purchase", "bought", "by", "bt"):
        side = "buy"
    elif s in ("sell", "s", "sale", "sold", "sl", "st"):
        side = "sell"
    else:
        bq, sq = num(_get(r, t, "buy_qty")) or 0.0, num(_get(r, t, "sell_qty")) or 0.0
        if bq > 0 and sq <= 0:
            return "buy", bq
        if sq > 0 and bq <= 0:
            return "sell", sq
        return None, None
    return (side, q) if q is not None and q > 0 and math.isfinite(q) else (None, None)


def parse_reported_lots(t: Table, resolve: Callable) -> Rows:
    out = Rows(n_rows=len(t.rows))
    for k, r in enumerate(t.rows, start=t.header_row + 2):
        raw, isin = _get(r, t, "symbol"), _get(r, t, "isin").upper()
        shown = safe_symbol(raw) if raw else (isin or "<missing>")
        d = parse_date(_get(r, t, "buy_date"))
        q = num(_get(r, t, "qty"))
        if d is None or q is None or q <= 0:
            _issue(out, k, shown, "rejected", "buy date or quantity is missing / not valid")
            continue
        cost = num(_get(r, t, "cost"))
        if cost is None:
            v = num(_get(r, t, "value"))
            cost = None if v is None else v / q
        if cost is None or cost < 0:
            _issue(out, k, shown, "rejected", "buy price (or buy value) is missing")
            continue
        sym, why = resolve(raw, isin)
        if sym is None:
            _issue(out, k, shown, "rejected", why)
            continue
        if why:
            _issue(out, k, shown, "warning", why)
        out.rows.append({"symbol": sym, "isin": isin or None, "buy_date": d, "quantity": q, "unit_cost": cost})
    return out


BUY_WORDS = ("purchase", "sip", "systematicinvestment", "switchin", "stpin", "reinvest", "bonus", "newfundoffer", "allotment", "transferin", "subscription")
SELL_WORDS = ("redemption", "redeem", "switchout", "stpout", "swp", "withdrawal", "transferout")
REVERSAL_WORDS = ("reversal", "reversed", "rejected", "reject", "cancel", "failed", "returned")
IGNORE_WORDS = ("stampduty", "stt", "tds", "dividendpayout", "dividendpaid", "nominee", "kyc", "addressupdate", "bankmandate", "taxdeduct")


def classify_mf_type(text: str) -> tuple:
    """-> (kind, side): kind in ('trade','reversal','ignore','unknown'); side 'buy' | 'sell' | None. The text is matched after removing everything that is not a letter.
    A row that moves units is a trade; rows that only carry charges, tax or payouts are ignored; anything else is reported as unknown, never guessed."""
    s = re.sub(r"[^a-z]", "", text.lower())
    rev = any(w in s for w in REVERSAL_WORDS)
    base = s
    for w in REVERSAL_WORDS:
        base = base.replace(w, "")
    side = "sell" if any(w in base for w in SELL_WORDS) else ("buy" if any(w in base for w in BUY_WORDS) else None)
    if side is None:
        return ("ignore", None) if any(w in s for w in IGNORE_WORDS) else ("unknown", None)
    return ("reversal" if rev else "trade"), side


def parse_mf(t: Table) -> Rows:
    out = Rows(n_rows=len(t.rows))
    pending = []
    for k, r in enumerate(t.rows, start=t.header_row + 2):
        folio, isin = _get(r, t, "folio"), _get(r, t, "isin").upper()
        kind, side = classify_mf_type(_get(r, t, "type"))
        if kind == "ignore":
            continue
        if kind == "unknown":
            _issue(out, k, isin or "<missing>", "rejected", f"transaction type {_get(r, t, 'type')[:40]!r} is not one this importer understands")
            continue
        d = parse_date(_get(r, t, "date"))
        units, nav, amt = num(_get(r, t, "units")), num(_get(r, t, "nav")), num(_get(r, t, "amount"))
        if d is None or not isin:
            _issue(out, k, isin or "<missing>", "rejected", "date or ISIN is missing (the scheme cannot be identified without its ISIN)")
            continue
        if units is None and amt is not None and nav:
            units = amt / nav
        if units is not None:
            units = abs(units)
        if nav is None and amt is not None and units:
            nav = abs(amt) / units
        if units is None or units <= 0 or nav is None or nav < 0:
            _issue(out, k, isin, "rejected", "units or NAV could not be determined")
            continue
        row = {"asset": "mf", "symbol": None, "isin": isin, "folio": folio or None, "scheme_name": _get(r, t, "scheme")[:120] or None, "trade_date": d, "trade_time": None,
               "side": side, "quantity": round(units, 6), "price": round(nav, 6), "trade_ref": None, "_row": k}
        cl = num(_get(r, t, "closing"))
        if cl is not None:
            out.closing[(folio or "", isin)] = cl
        if kind == "reversal":
            pending.append(row)
        else:
            out.rows.append(row)
    for rv in pending:                                                   # a reversal cancels ONE earlier matching row; it is never counted as a trade of its own
        hit = next((i for i, x in enumerate(out.rows) if x["isin"] == rv["isin"] and x["folio"] == rv["folio"] and x["side"] == rv["side"] and abs(x["quantity"] - rv["quantity"]) < 0.0005
                    and x["trade_date"] <= rv["trade_date"]), None)
        if hit is None:
            _issue(out, rv["_row"], rv["isin"], "rejected", "a reversal / rejection with no matching original transaction in this file: nothing was cancelled")
        else:
            out.rows.pop(hit)
            _issue(out, rv["_row"], rv["isin"], "warning", "reversal matched an earlier transaction in this file and cancelled it")
    for x in out.rows:
        x.pop("_row", None)
    return out
