"""Tax lots: FIFO reconstruction from trades, corporate-action handling, reconciliation against the holdings snapshot, and holding-period classes. Pure: no database, no clock (`today` is passed).

Rules this module enforces (each has a test):
  * FIFO per security (equity: the resolved NSE symbol, which survives renames and the new ISIN an exchange issues at a split; mutual funds: folio + ISIN). Inventory never goes negative: a sell that
    finds no lot to take is counted in `unmatched_sell` and surfaces as a gap, never as a guessed purchase date.
  * A SPLIT scales every open lot (quantity x s, unit cost / s) and KEEPS its purchase date. A BONUS leaves every open lot untouched and adds a NEW zero-cost lot dated at the allotment; the NSE feed
    carries the ex-date only, so the ex-date is used and the lot says so (`bonus_ex_date_proxy`). Events whose terms cannot be parsed, or disagree with the factor NSE lists, are not applied.
  * The holdings snapshot decides what is reconciled. Holdings above the lots become an undated gap lot with the exact missing quantity; lots above the holdings are flagged as a surplus.
  * Holding period: more than 12 calendar months = long-term (acquisition date + 12 months + 1 day). Only listed equity and equity-oriented funds are classified; everything else says why not.
  * Nothing here computes a tax amount. See infra/data/tax_policy.json for what is and is not computed, and its UNVERIFIED status.
"""
from __future__ import annotations

import json
import math
import re
from collections import deque
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

from dateutil.relativedelta import relativedelta

POLICY_PATH = Path(__file__).resolve().parents[3] / "infra" / "data" / "tax_policy.json"
EPS = 1e-9
TOL_EQUITY = 1e-6
TOL_UNITS = 1e-3


def load_policy() -> dict:
    return json.loads(POLICY_PATH.read_text(encoding="utf-8"))


# ── holding period ────────────────────────────────────────────────────────────────────────────────────────────

def long_term_from(buy: date, months: int = 12) -> date:
    """First day a sale is long-term: held MORE than `months` calendar months."""
    return buy + relativedelta(months=months) + timedelta(days=1)


def classify_lot(buy_date: Optional[date], today: date, policy: Optional[dict] = None, classifiable: bool = True, why_not: str = "") -> dict:
    p = (policy or load_policy())["listed_securities_and_equity_funds"]
    if not classifiable:
        return {"tax_class": "NOT_CLASSIFIED", "long_term_from": None, "days_to_long_term": None, "reason": why_not or "holding-period rules for this kind of holding are not implemented"}
    if buy_date is None:
        return {"tax_class": "UNKNOWN_DATE", "long_term_from": None, "days_to_long_term": None, "reason": "the purchase date is not in any loaded file"}
    lt = long_term_from(buy_date, int(p["long_term_after_months"]))
    days = (lt - today).days
    cls = "LONG_TERM" if days <= 0 else ("NEAR_LONG_TERM" if days <= int(p["near_long_term_days"]) else "SHORT_TERM")
    return {"tax_class": cls, "long_term_from": lt, "days_to_long_term": max(days, 0), "reason": ""}


# ── corporate actions ─────────────────────────────────────────────────────────────────────────────────────────

_SPLIT = re.compile(r"from\s+r[se]\.?\s*([0-9]+(?:\.[0-9]+)?)\s*/?-?\s*per\s+share\s+to\s+r[se]\.?\s*([0-9]+(?:\.[0-9]+)?)", re.I)
_BONUS = re.compile(r"bonus\s*(\d+)\s*:\s*(\d+)", re.I)


@dataclass
class Event:
    ex_date: date
    split: float = 1.0                      # shares per share after a face-value split (10 -> 2 is 5)
    bonus: float = 0.0                      # extra shares per share held (Bonus 1:2 is 0.5), counted on the post-split holding
    subject: str = ""
    ok: bool = True
    reason: str = ""


def parse_event(ex_date: date, subject: str, listed_factor: Optional[float]) -> Event:
    """Read a split / bonus subject. ok=False (with the reason) for anything not understood or inconsistent with the factor NSE lists; such an event is never applied."""
    s = subject or ""
    ev = Event(ex_date=ex_date, subject=s)
    m, b = _SPLIT.search(s), _BONUS.search(s)
    if m:
        old, new = float(m.group(1)), float(m.group(2))
        if new <= 0 or old <= 0:
            return Event(ex_date, subject=s, ok=False, reason="face values in the subject are not positive")
        ev.split = old / new
    if b:
        a, c = float(b.group(1)), float(b.group(2))
        if c <= 0:
            return Event(ex_date, subject=s, ok=False, reason="bonus ratio has a zero denominator")
        ev.bonus = a / c
    if not m and not b:
        return Event(ex_date, subject=s, ok=False, reason="terms are not a recognisable split or bonus")
    if ev.split < 1.0 - 1e-9:
        return Event(ex_date, subject=s, ok=False, reason="a consolidation / reverse split needs manual review: fractional entitlements are cash-settled")
    if listed_factor and listed_factor > 0:
        implied = 1.0 / ((1.0 + ev.bonus) * ev.split)
        if abs(implied / listed_factor - 1.0) > 0.02:
            return Event(ex_date, subject=s, ok=False, reason=f"terms imply a price factor of {implied:.4f} but NSE lists {listed_factor:.4f}")
    return ev


@dataclass
class Lot:
    date: Optional[date]
    qty: float
    cost: Optional[float]
    origin: str                             # buy | bonus | report | holding_snapshot | gap
    date_basis: str                         # trade | bonus_ex_date_proxy | report | unknown
    note: str = ""


@dataclass
class FifoResult:
    lots: list = field(default_factory=list)
    unmatched_sell: float = 0.0
    applied: list = field(default_factory=list)
    skipped: list = field(default_factory=list)


def _apply_event(open_: deque, ev: Event, res: FifoResult) -> None:
    if not open_:                           # nothing held before the ex-date: the event does not touch this book
        return
    if not ev.ok:
        res.skipped.append(ev)
        return
    if ev.split != 1.0:
        for l in open_:
            l.qty *= ev.split
            if l.cost is not None:
                l.cost /= ev.split
    if ev.bonus > 0:
        held = sum(l.qty for l in open_)
        new = math.floor(held * ev.bonus + 1e-9)
        if new > 0:
            open_.append(Lot(ev.ex_date, float(new), 0.0, "bonus", "bonus_ex_date_proxy", "bonus shares: cost nil; the allotment date is not in the NSE feed, the ex-date is used. Confirm the allotment date."))
    res.applied.append(ev)


def fifo(txns: list, events: Optional[list] = None, today: Optional[date] = None, tol: float = TOL_EQUITY) -> FifoResult:
    """txns: dicts with trade_date, side, quantity, price (and optional trade_time). Events apply to the lots held BEFORE their ex-date; a trade on the ex-date is already in post-event terms."""
    res = FifoResult()
    evs = sorted(events or [], key=lambda e: e.ex_date)
    ei, open_ = 0, deque()
    ordered = sorted(txns, key=lambda t: (t["trade_date"], t.get("trade_time") or "", 0 if t["side"] == "buy" else 1))
    for t in ordered:
        while ei < len(evs) and evs[ei].ex_date <= t["trade_date"]:
            _apply_event(open_, evs[ei], res)
            ei += 1
        q = float(t["quantity"])
        if t["side"] == "buy":
            open_.append(Lot(t["trade_date"], q, float(t["price"]), "buy", "trade"))
        else:
            while q > tol and open_:
                take = min(q, open_[0].qty)
                open_[0].qty -= take
                q -= take
                if open_[0].qty <= tol:
                    open_.popleft()
            if q > tol:
                res.unmatched_sell += q
    while ei < len(evs) and (today is None or evs[ei].ex_date <= today):
        _apply_event(open_, evs[ei], res)
        ei += 1
    res.lots = [l for l in open_ if l.qty > tol]
    for l in res.lots:
        l.qty = round(l.qty, 6)
    return res


# ── reconciliation ────────────────────────────────────────────────────────────────────────────────────────────

def _row(l: Lot, status: str, note: str = "") -> dict:
    return {"buy_date": l.date, "quantity": round(l.qty, 6), "unit_cost": None if l.cost is None else round(l.cost, 6), "origin": l.origin, "date_basis": l.date_basis, "status": status,
            "note": "; ".join(x for x in (l.note, note) if x) or None}


def reconcile(lots_by_source: dict, hold_qty: Optional[float], hold_cost: Optional[float], unmatched: dict, notes: list, tol: float) -> dict:
    """lots_by_source: {'broker_lot_report': [Lot], 'tradebook_fifo': [Lot]} (either may be absent). hold_qty None = there is no holdings snapshot to compare with.
    Returns {'rows': [...], 'status', 'source', 'gap_qty', 'notes'}; gap_qty = holdings - lots (positive: shares with no purchase record)."""
    notes = list(notes)
    if not lots_by_source:
        if hold_qty is None or hold_qty <= tol:
            return {"rows": [], "status": "NONE", "source": None, "gap_qty": 0.0, "notes": notes}
        l = Lot(None, hold_qty, hold_cost, "holding_snapshot", "unknown", "from the holdings snapshot: purchase date not loaded yet")
        return {"rows": [_row(l, "DATE_PENDING")], "status": "DATE_PENDING", "source": "holding_snapshot", "gap_qty": 0.0, "notes": notes}
    order = [s for s in ("broker_lot_report", "tradebook_fifo") if s in lots_by_source]
    totals = {s: sum(l.qty for l in lots_by_source[s]) for s in order}
    if hold_qty is None:
        src = order[0]
        return {"rows": [_row(l, "UNVERIFIED") for l in lots_by_source[src]], "status": "UNVERIFIED", "source": src, "gap_qty": None,
                "notes": notes + ["no holdings snapshot for this account: the lots could not be checked against what you hold"]}
    gaps = {s: hold_qty - totals[s] for s in order}
    exact = [s for s in order if abs(gaps[s]) <= tol and unmatched.get(s, 0.0) <= tol]
    src = exact[0] if exact else min(order, key=lambda s: (abs(gaps[s]), order.index(s) != len(order) - 1))
    lots, gap = lots_by_source[src], gaps[src]
    if abs(gap) <= tol and unmatched.get(src, 0.0) <= tol:
        return {"rows": [_row(l, "RECONCILED") for l in lots], "status": "RECONCILED", "source": src, "gap_qty": 0.0, "notes": notes}
    if gap > tol:
        rows = [_row(l, "UNRECONCILED_LOT_GAP") for l in lots]
        g = Lot(None, gap, None, "gap", "unknown", f"{gap:g} shares in your holdings have no purchase record in the loaded files: load an older tradebook or the broker's lot report")
        rows.append(_row(g, "DATE_PENDING"))
        return {"rows": rows, "status": "UNRECONCILED_LOT_GAP", "source": src, "gap_qty": round(gap, 6), "notes": notes}
    if gap < -tol:
        return {"rows": [_row(l, "UNRECONCILED_SURPLUS") for l in lots], "status": "UNRECONCILED_SURPLUS", "source": src, "gap_qty": round(gap, 6),
                "notes": notes + [f"the loaded files imply {-gap:g} more shares than the holdings snapshot: sold or transferred outside the files, or a corporate action that was not applied"]}
    return {"rows": [_row(l, "UNRECONCILED_LOT_GAP") for l in lots], "status": "UNRECONCILED_LOT_GAP", "source": src, "gap_qty": 0.0,
            "notes": notes + [f"{unmatched.get(src, 0):g} shares were sold in the files with no earlier purchase record: older purchases are missing"]}


def build_equity(*, txns: list, reported: list, holdings: Optional[dict], events: dict, today: date) -> tuple:
    """txns / reported / holdings keyed by resolved symbol. Returns (lot_rows, symbol_summaries). Each lot row carries symbol and isin; a summary has symbol, status, source, gap_qty, notes."""
    by_t, by_r = {}, {}
    isin_of: dict = {}
    for t in txns:
        by_t.setdefault(t["symbol"], []).append(t)
        isin_of[t["symbol"]] = t.get("isin") or isin_of.get(t["symbol"])
    for r in reported:
        by_r.setdefault(r["symbol"], []).append(r)
        isin_of[r["symbol"]] = r.get("isin") or isin_of.get(r["symbol"])
    for s, h in (holdings or {}).items():
        isin_of[s] = h.get("isin") or isin_of.get(s)
    rows, summaries = [], []
    for sym in sorted(set(by_t) | set(by_r) | set(holdings or {})):
        src: dict = {}
        unmatched: dict = {}
        notes: list = []
        if sym in by_r:
            src["broker_lot_report"] = [Lot(r["buy_date"], float(r["quantity"]), float(r["unit_cost"]), "report", "report") for r in by_r[sym]]
        if sym in by_t:
            res = fifo(by_t[sym], events.get(sym, []), today)
            src["tradebook_fifo"] = res.lots
            unmatched["tradebook_fifo"] = res.unmatched_sell
            for e in res.skipped:
                notes.append(f"corporate action on {e.ex_date} not applied: {e.reason}")
            if res.applied:
                notes.append("applied: " + ", ".join(f"{e.ex_date} (split x{e.split:g}, bonus {e.bonus:g})" if e.bonus else f"{e.ex_date} (split x{e.split:g})" for e in res.applied))
        h = (holdings or {}).get(sym) if holdings is not None else None
        hold_qty = None if holdings is None else (float(h["quantity"]) if h else 0.0)
        out = reconcile(src, hold_qty, None if not h else float(h["avg_cost"]), unmatched, notes, TOL_EQUITY)
        if out["status"] == "NONE" or not out["rows"]:                  # nothing held now (sold in full): no lots to show
            continue
        if out["status"] in ("UNRECONCILED_LOT_GAP", "UNRECONCILED_SURPLUS") and any(n.startswith("corporate action") for n in notes):
            out["status"] = "NEEDS_REVIEW"
            for r in out["rows"]:
                if r["status"].startswith("UNRECONCILED"):
                    r["status"] = "NEEDS_REVIEW"
        for r in out["rows"]:
            rows.append({"asset": "equity", "symbol": sym, "isin": isin_of.get(sym), "folio": None, **r})
        summaries.append({"asset": "equity", "symbol": sym, "isin": isin_of.get(sym), "status": out["status"], "source": out["source"], "gap_qty": out["gap_qty"], "notes": out["notes"],
                          "n_lots": len(out["rows"])})
    return rows, summaries


def build_mf(*, txns: list, closing: dict) -> tuple:
    """Mutual fund units, FIFO per (folio, ISIN). closing: {(folio, isin): units on the statement}; without it the lots are UNVERIFIED, never 'reconciled'."""
    groups: dict = {}
    for t in txns:
        groups.setdefault((t.get("folio") or "", t["isin"]), []).append(t)
    rows, summaries = [], []
    for (folio, isin), ts in sorted(groups.items()):
        res = fifo(ts, [], None, tol=TOL_UNITS)
        cl = closing.get((folio, isin))
        out = reconcile({"tradebook_fifo": res.lots}, cl, None, {"tradebook_fifo": res.unmatched_sell}, [], TOL_UNITS) if (res.lots or res.unmatched_sell or cl) else None
        if out is None or out["status"] == "NONE" or not out["rows"]:
            continue
        if cl is None and out["status"] == "UNVERIFIED":
            out["notes"] = ["no closing unit balance was supplied, so the units could not be checked against the statement"]
        name = next((t.get("scheme_name") for t in ts if t.get("scheme_name")), None)
        for r in out["rows"]:
            rows.append({"asset": "mf", "symbol": None, "isin": isin, "folio": folio or None, "scheme_name": name, **r})
        summaries.append({"asset": "mf", "symbol": None, "isin": isin, "folio": folio or None, "status": out["status"], "source": out["source"], "gap_qty": out["gap_qty"], "notes": out["notes"],
                          "n_lots": len(out["rows"])})
    return rows, summaries
