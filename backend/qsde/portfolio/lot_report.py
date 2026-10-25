"""Read side of the tax lots: the latest rebuild per broker account, joined to the owner's pot triage, the last price and the holding-period class. Used by the API and the CLI.

Nothing here computes a tax amount (see infra/data/tax_policy.json, which is itself marked UNVERIFIED): a lot gets a holding-period class, the date it turns long-term, and its unrealised gain or loss
at the last price. A lot whose purchase date is not in any loaded file says so; it is never given a guessed date.
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Optional

import pandas as pd

from qsde.portfolio import lots as L

STATUS_TEXT = {
    "RECONCILED": "matches your holdings",
    "UNVERIFIED": "no holdings snapshot or closing balance to check against",
    "DATE_PENDING": "purchase date not loaded yet",
    "UNRECONCILED_LOT_GAP": "some shares have no purchase record",
    "UNRECONCILED_SURPLUS": "the files imply more than you hold",
    "NEEDS_REVIEW": "a corporate action could not be applied: review",
}


def _f(x) -> Optional[float]:
    return None if x is None or pd.isna(x) else float(x)


def build(read_sql, user_id: str, account: Optional[str] = None, today: Optional[date] = None) -> dict:
    today = today or date.today()
    policy = L.load_policy()
    q = "SELECT DISTINCT ON (broker_account) id, broker_account, computed_at, summary::text AS summary FROM lot_runs WHERE user_id = :u" + (" AND broker_account = :a" if account else "") \
        + " ORDER BY broker_account, computed_at DESC, id DESC"
    runs = read_sql(q, params={"u": user_id, **({"a": account} if account else {})})
    out = {"as_of": str(today), "policy": {"version": policy["version"], "status": policy["status"], "not_computed": policy["not_computed"], "not_classified": policy["not_classified"]},
           "accounts": [], "lots": [], "securities": [], "classes": {}, "statuses": {}, "near_long_term": [], "notes": []}
    if runs.empty:
        out["notes"].append("no tax lots yet: import a holdings file, then a tradebook or the broker's lot report")
        return out
    out["accounts"] = [{"account": r.broker_account, "computed_at": pd.Timestamp(r.computed_at).isoformat()} for r in runs.itertuples()]
    for r in runs.itertuples():
        out["securities"] += [{**s, "account": r.broker_account} for s in json.loads(r.summary)]
    lots = read_sql("SELECT t.*, sp.pot FROM portfolio_tax_lots t LEFT JOIN security_pot sp ON sp.user_id = t.user_id AND sp.broker_account = t.broker_account AND sp.asset = t.asset "
                    "AND sp.security = COALESCE(t.symbol, t.isin) AND sp.released_at IS NULL WHERE t.run_id = ANY(:r) ORDER BY t.broker_account, t.asset, t.symbol, t.scheme_name, t.buy_date NULLS LAST",
                    params={"r": [int(i) for i in runs["id"]]})
    syms = sorted(set(lots.loc[lots["asset"] == "equity", "symbol"].dropna()))
    px = {}
    if syms:
        d = read_sql("SELECT DISTINCT ON (symbol) symbol, date, close FROM ohlcv WHERE symbol = ANY(:s) AND date >= CAST(:d AS DATE) ORDER BY symbol, date DESC", params={"s": syms, "d": str(today - timedelta(days=30))})
        px = {r.symbol: (float(r.close), str(r.date)) for r in d.itertuples()}
    codes = sorted({int(c) for c in lots["scheme_code"].dropna()})
    nav, klass = {}, {}
    if codes:
        n = read_sql("SELECT DISTINCT ON (scheme_code) scheme_code, date, nav FROM mf_nav WHERE scheme_code = ANY(:c) AND date >= CAST(:d AS DATE) ORDER BY scheme_code, date DESC", params={"c": codes, "d": str(today - timedelta(days=30))})
        nav = {int(r.scheme_code): (float(r.nav), str(r.date)) for r in n.itertuples()}
        k = read_sql("SELECT scheme_code, asset_class FROM mf_scheme_master WHERE scheme_code = ANY(:c)", params={"c": codes})
        klass = {int(r.scheme_code): r.asset_class for r in k.itertuples()}
    for r in lots.itertuples():
        qty, cost = float(r.quantity), _f(r.unit_cost)
        if r.asset == "equity":
            last, last_date = px.get(r.symbol, (None, None))
            c = L.classify_lot(r.buy_date if not pd.isna(r.buy_date) else None, today, policy)
        else:
            last, last_date = nav.get(int(r.scheme_code), (None, None)) if not pd.isna(r.scheme_code) else (None, None)
            ac = klass.get(int(r.scheme_code)) if not pd.isna(r.scheme_code) else None
            ok = ac == "equity"
            c = L.classify_lot(r.buy_date if not pd.isna(r.buy_date) else None, today, policy, classifiable=ok,
                               why_not="" if ok else f"{ac or 'unknown'} funds: holding-period rules are not implemented here; confirm with your chartered accountant")
        value = None if last is None else qty * last
        gain = None if (last is None or cost is None) else qty * (last - cost)
        row = {"account": r.broker_account, "asset": r.asset, "symbol": r.symbol, "isin": r.isin, "folio": r.folio, "scheme_name": r.scheme_name, "plan_type": r.plan_type, "option_type": r.option_type,
               "pot": None if pd.isna(r.pot) else r.pot, "buy_date": None if pd.isna(r.buy_date) else str(r.buy_date), "quantity": qty, "unit_cost": cost, "last_price": last, "price_date": last_date,
               "value": value, "unrealised": gain, "unrealised_pct": None if (gain is None or not cost) else gain / (qty * cost) if qty * cost else None,
               "tax_class": c["tax_class"], "long_term_from": None if c["long_term_from"] is None else str(c["long_term_from"]), "days_to_long_term": c["days_to_long_term"], "tax_reason": c["reason"],
               "status": r.status, "status_text": STATUS_TEXT.get(r.status, r.status), "origin": r.origin, "date_basis": r.date_basis, "note": r.note}
        out["lots"].append(row)
        out["classes"][c["tax_class"]] = out["classes"].get(c["tax_class"], 0) + 1
        out["statuses"][r.status] = out["statuses"].get(r.status, 0) + 1
    out["near_long_term"] = sorted([x for x in out["lots"] if x["tax_class"] == "NEAR_LONG_TERM"], key=lambda x: x["days_to_long_term"])
    out["notes"] += ["Holding-period classes only: no tax amount is computed. The annual long-term gains exemption needs this year's realised gains across every broker and fund, so it is not computed.",
                     "Selling is your decision on your own platform; this product places no orders.", f"Rules: {policy['status']}"]
    return out


def render_text(rep: dict) -> str:
    if not rep["lots"]:
        return "\n".join(rep["notes"]) or "no tax lots"
    L_ = [f"Tax lots as of {rep['as_of']} ({', '.join(a['account'] for a in rep['accounts'])})", ""]
    L_.append(f"{'security':<26}{'pot':<12}{'bought':<12}{'qty':>12}{'cost':>11}{'last':>11}{'unrealised':>13}  {'class':<16}{'long-term from':<15}status")
    for x in rep["lots"]:
        name = (x["symbol"] or (x["scheme_name"] or x["isin"] or "?")[:20] + (f" {x['plan_type'][0]}" if x["plan_type"] else ""))[:25]
        f = lambda v, n=2: "-" if v is None else f"{v:,.{n}f}"                          # noqa: E731
        L_.append(f"{name:<26}{(x['pot'] or 'NOT ASSIGNED'):<12}{(x['buy_date'] or 'unknown'):<12}{x['quantity']:>12,.4f}{f(x['unit_cost']):>11}{f(x['last_price']):>11}{f(x['unrealised'], 0):>13}  "
                  f"{x['tax_class']:<16}{(x['long_term_from'] or '-'):<15}{x['status']}")
    L_ += ["", "classes: " + ", ".join(f"{k} {v}" for k, v in sorted(rep["classes"].items())), "status:  " + ", ".join(f"{k} {v}" for k, v in sorted(rep["statuses"].items()))]
    if rep["near_long_term"]:
        L_ += ["", "turning long-term soon:"] + [f"  {x['symbol'] or x['scheme_name']}: {x['quantity']:g} units from {x['buy_date']} are long-term from {x['long_term_from']} ({x['days_to_long_term']} days)" for x in rep["near_long_term"][:10]]
    bad = [s for s in rep["securities"] if s["status"] not in ("RECONCILED", "UNVERIFIED", "DATE_PENDING")]
    if bad:
        L_ += ["", "needs attention:"] + [f"  {s.get('symbol') or s.get('isin')}: {s['status']}" + (f", {s['gap_qty']:+g} vs holdings" if s.get("gap_qty") else "") + (" - " + "; ".join(s["notes"]) if s["notes"] else "") for s in bad]
    L_ += [""] + rep["notes"]
    return "\n".join(L_)
