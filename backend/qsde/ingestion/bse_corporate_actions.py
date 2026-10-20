"""BSE corporate actions: parse BSE's own Corporate Actions export, classify each action, reconcile it against BSE price behaviour. EVIDENCE ONLY: nothing adjusts a price.

The export comes from BSE's Corporate Actions page (Download). BSE's JSON API sits behind bot protection that rejects automated clients, so the file is downloaded by the owner
and dropped into the inbox folder; it is then landed raw (SHA-256, retrieval time, known_at) exactly like every other source. Expected columns (matched loosely, unknown extra columns
are kept in `raw_row`): Security Code, Security Name, Ex Date, Purpose, Record Date, BC Start Date, BC End Date, ND Start Date, ND End Date, Actual Payment Date.

Reconciliation reuses the NSE v3 gap test (hard bounds: gap bar within MAX_SHIFT sessions of the ex-date, factor within 5% (3% for mild factors), at most MAX_UNTRADED untraded sessions,
3-session persistence). Mapping of its verdicts: pass -> `confirmed`; contradicted by prices -> `rejected`; anything that cannot be tested (rights, demergers / schemes, buy-backs, unparseable
text, no price bars) -> `needs_review`. `confirmed` is not an approval and adjusts nothing.
"""
from __future__ import annotations

import hashlib
import io
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

from qsde.ingestion.corp_actions_source import parse_purpose

VERSION = "bse_ca_reconcile_v1"
MAX_DIV_YIELD = 0.25
ALIASES = {
    "scrip_code": ["securitycode", "scripcode", "scrip code", "security code", "code"],
    "name": ["securityname", "scripname", "security name", "company", "companyname"],
    "ex_date": ["exdate", "ex date", "ex-date"],
    "purpose": ["purpose", "particulars", "details"],
    "record_date": ["recorddate", "record date"],
    "bc_start": ["bcstartdate", "bc start date", "bookclosurestartdate"],
    "bc_end": ["bcenddate", "bc end date", "bookclosureenddate"],
    "nd_start": ["ndstartdate", "nd start date", "nostartdate"],
    "nd_end": ["ndenddate", "nd end date"],
    "payment_date": ["actualpaymentdate", "actual payment date", "paymentdate"],
}
DATE_COLS = ["ex_date", "record_date", "bc_start", "bc_end", "nd_start", "nd_end", "payment_date"]
SCHEME = re.compile(r"demerger|de-merger|arrangement|arangement|amalgam|merger|capital reduction|reduction of capital|spin|restructur", re.I)


def _norm(h: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(h).lower())


@dataclass
class Export:
    frame: pd.DataFrame
    sha256: str
    n_rows: int
    issues: list = field(default_factory=list)
    mapping: dict = field(default_factory=dict)         # canonical column -> the header found in the file
    unmatched: list = field(default_factory=list)       # headers in the file that map to nothing (kept in raw_row)

    @property
    def n_bad(self) -> int:
        return sum(1 for i in self.issues if i["severity"] == "rejected")


def _s(x) -> str:
    return "" if x is None or (not isinstance(x, str) and pd.isna(x)) else str(x).strip()


def parse_date(x) -> Optional[pd.Timestamp]:
    if x is None or (isinstance(x, float) and np.isnan(x)):
        return None
    s = str(x).strip()
    if s in ("", "-", "--", "NA", "N.A.", "nan", "NaT", "None"):
        return None
    for fmt in ("%d %b %Y", "%d-%b-%Y", "%d %B %Y", "%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d", "%d %b %y", "%d-%b-%y"):
        try:
            return pd.Timestamp(pd.to_datetime(s, format=fmt))
        except (ValueError, TypeError):
            continue
    return None


def read_export(payload: bytes, filename: str = "") -> Export:
    """Bytes of a BSE Corporate Actions export (CSV) -> normalised rows + a list of reported issues. Never guesses a missing column."""
    sha = hashlib.sha256(payload).hexdigest()
    if filename.lower().endswith((".xlsx", ".xls")) or payload[:2] == b"PK":
        try:
            raw = pd.read_excel(io.BytesIO(payload), dtype=str)
        except ImportError as e:
            raise ValueError("this is an Excel file and openpyxl is not installed: open it and save as CSV, or install openpyxl") from e
        except Exception as e:      # noqa: BLE001
            raise ValueError(f"not a readable Excel file ({type(e).__name__}); save the export as CSV") from e
    else:
        text = payload.decode("utf-8-sig", errors="replace")
        raw = pd.read_csv(io.StringIO(text), dtype=str, sep=None, engine="python", skip_blank_lines=True)
    raw = raw.dropna(how="all")
    cols = {_norm(c): c for c in raw.columns}
    pick = {k: next((cols[a.replace(" ", "")] for a in al if a.replace(" ", "") in cols), None) for k, al in ALIASES.items()}
    missing = [k for k in ("scrip_code", "ex_date", "purpose") if pick[k] is None]
    if missing:
        raise ValueError("could not find column(s): " + ", ".join(missing) + f". Headers seen: {', '.join(map(str, raw.columns))}")
    issues, rows = [], []
    for i, r in enumerate(raw.to_dict("records"), start=2):
        code = re.sub(r"\D", "", _s(r.get(pick["scrip_code"])))
        ex = parse_date(r.get(pick["ex_date"]))
        purpose = _s(r.get(pick["purpose"]))
        if not code or len(code) < 5 or len(code) > 7:
            issues.append({"row": i, "severity": "rejected", "reason": f"security code {r.get(pick['scrip_code'])!r} is not a BSE scrip code"})
            continue
        if ex is None:
            issues.append({"row": i, "severity": "rejected", "reason": f"ex-date {r.get(pick['ex_date'])!r} is missing or unreadable (scrip {code})"})
            continue
        if not purpose:
            issues.append({"row": i, "severity": "rejected", "reason": f"no purpose text (scrip {code}, ex-date {ex.date()})"})
            continue
        row = {"scrip_code": int(code), "name": _s(r.get(pick["name"])) or None, "purpose": purpose}
        for d in DATE_COLS:
            row[d] = parse_date(r.get(pick[d])) if pick.get(d) else None
        row["ex_date"] = ex
        row["raw_row"] = {str(k): (None if (isinstance(v, float) and np.isnan(v)) else v) for k, v in r.items()}
        rows.append(row)
    if not rows:
        raise ValueError("no valid rows in the file: " + "; ".join(i["reason"] for i in issues[:3]))
    return Export(pd.DataFrame(rows), sha, int(len(raw)), issues, {k: v for k, v in pick.items() if v}, [c for c in raw.columns if c not in pick.values()])


def classify_purpose(purpose: str) -> dict:
    """-> {action_type, factor, amount, ratio_text}. factor is the price multiplier BEFORE the ex-date, only for bonus / split / consolidation."""
    s = purpose.strip()
    if SCHEME.search(s) and not re.search(r"\bbonus\b", s, re.I):
        return {"action_type": "demerger_or_scheme", "factor": None, "amount": None, "ratio_text": None}
    if re.search(r"buy-?\s*back", s, re.I):
        return {"action_type": "buyback", "factor": None, "amount": None, "ratio_text": None}
    if re.search(r"\brights?\b", s, re.I):
        m = re.search(r"(\d+)\s*:\s*(\d+)", s)
        return {"action_type": "rights", "factor": None, "amount": None, "ratio_text": f"{m.group(1)}:{m.group(2)}" if m else None}
    p = parse_purpose(s)
    if p["factor"] is not None and p["category"] in ("SPLIT_BONUS", "CONSOLIDATION"):
        kinds = {part[0] for part in p["parts"]}
        t = "consolidation" if p["category"] == "CONSOLIDATION" else ("bonus" if kinds == {"bonus"} else "split" if kinds == {"split"} else "bonus")
        return {"action_type": t, "factor": float(p["factor"]), "amount": None, "ratio_text": "; ".join(part[2] for part in p["parts"])}
    if re.search(r"dividend", s, re.I):
        m = re.search(r"(?:rs\.?|re\.?|inr)[\s\-:]*([0-9]+(?:\.[0-9]+)?)", s, re.I)
        return {"action_type": "dividend", "factor": None, "amount": float(m.group(1)) if m else None, "ratio_text": None}
    return {"action_type": "other", "factor": None, "amount": None, "ratio_text": None}


def enrich(frame: pd.DataFrame, isin_by_scrip: Optional[dict] = None) -> pd.DataFrame:
    d = frame.copy()
    info = d["purpose"].map(classify_purpose)
    for k in ("action_type", "factor", "amount", "ratio_text"):
        d[k] = [i[k] for i in info]
    d["isin"] = d["scrip_code"].map(lambda c: (isin_by_scrip or {}).get(int(c)))
    return d


def content_hash(r: dict) -> str:
    key = [int(r["scrip_code"]), pd.Timestamp(r["ex_date"]).strftime("%Y-%m-%d"), " ".join(str(r["purpose"]).split()).lower()]
    return hashlib.sha256(json.dumps(key).encode()).hexdigest()


# ── reconciliation ───────────────────────────────────────────────────────────────────────────────────────────────────

def reconcile(actions: pd.DataFrame, bars: pd.DataFrame, calendar=None) -> pd.DataFrame:
    """actions: id, scrip_code, ex_date, purpose, action_type, factor, amount. bars: scrip_code, date, open, close (BSE raw bhavcopy).
    Returns id, classification, reason and the evidence columns. Pure: no database access."""
    from qsde.ingestion.corp_actions_v3 import evaluate as v3

    out = []
    px = bars.assign(symbol=bars["scrip_code"].astype(str))[["symbol", "date", "open", "close"]] if len(bars) else pd.DataFrame(columns=["symbol", "date", "open", "close"])
    split_like = actions[actions["action_type"].isin(["bonus", "split", "consolidation"]) & actions["factor"].notna()]
    if len(split_like):
        ev = v3(split_like.assign(listed_symbol=split_like["scrip_code"].astype(str), listed_ex_date=split_like["ex_date"])[["listed_symbol", "listed_ex_date", "purpose"]], px, None, calendar)
        ev = ev.reset_index(drop=True)
        for aid, (_, e) in zip(split_like["id"], ev.iterrows()):
            c = e["classification"]
            cls = {"v3_candidate": "confirmed", "v3_rejected": "rejected"}.get(c, "needs_review")
            out.append({"id": aid, "classification": cls, "reason": str(e["reason"]), "listed_factor": e["listed_factor"], "observed_ratio": e["observed_ratio"], "factor_error": e["factor_error"],
                        "gap_date": e["gap_date"], "shift_sessions": e["shift_sessions"], "untraded_sessions": e["untraded_sessions"], "persistence_ratio": e["persistence_ratio"]})
    by = {s: g.sort_values("date") for s, g in px.groupby("symbol")}
    for r in actions[~actions["id"].isin([o["id"] for o in out])].itertuples():
        base = {"id": r.id, "listed_factor": None, "observed_ratio": None, "factor_error": None, "gap_date": None, "shift_sessions": None, "untraded_sessions": None, "persistence_ratio": None}
        g = by.get(str(r.scrip_code))
        if r.action_type == "dividend":
            if r.amount is None or not np.isfinite(r.amount):
                out.append({**base, "classification": "needs_review", "reason": "dividend amount could not be read from the purpose text"})
                continue
            prior = None if g is None else g[g["date"] < pd.Timestamp(r.ex_date)]
            if prior is None or prior.empty:
                out.append({**base, "classification": "needs_review", "reason": "no BSE price bar before the ex-date to test the yield against"})
                continue
            y = r.amount / float(prior["close"].iloc[-1])
            after = g[g["date"] >= pd.Timestamp(r.ex_date)]
            ok = y <= MAX_DIV_YIELD and not after.empty
            out.append({**base, "classification": "confirmed" if ok else "needs_review",
                        "reason": f"dividend Rs {r.amount:g} is {y:.1%} of the last close and the scrip traded after the ex-date" if ok else f"dividend yield {y:.1%} above {MAX_DIV_YIELD:.0%} or no trading after the ex-date"})
        elif r.action_type == "rights":
            out.append({**base, "classification": "needs_review", "reason": "rights issue: terms recorded, no price factor is invented; needs event-level review"})
        elif r.action_type == "demerger_or_scheme":
            out.append({**base, "classification": "needs_review", "reason": "demerger / scheme of arrangement: needs event-level review"})
        elif r.action_type == "buyback":
            out.append({**base, "classification": "needs_review", "reason": "buy-back: no price adjustment applies; recorded for completeness"})
        else:
            out.append({**base, "classification": "needs_review", "reason": f"purpose text not recognised as an equity action ({r.purpose[:60]!r})"})
    return pd.DataFrame(out)
