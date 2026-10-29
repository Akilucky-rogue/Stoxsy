"""Which asset class each holding belongs to, and the current allocation, with what could NOT be classified named instead of guessed.

Classes: equity, gold, debt. Source of truth for exchange-traded funds is NSE's official ETF list (underlying class); an ETF whose underlying is silver or another commodity, a hybrid,
or anything unknown is UNCLASSIFIED (never silently equity). A symbol that is not an ETF or fund unit at all is a direct stock: equity. Mutual funds, bonds and unlisted assets
cannot be imported, so they enter as manual balances (dated, with a label). Pure given the instrument tables; `load_instruments` reads the data files.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

DATA = Path(__file__).resolve().parents[3] / "infra" / "data"
CLASSES = ("equity", "gold", "debt")


def load_instruments(official_path: Path = DATA / "non_equity_official_etf_v1.json", provisional_path: Path = DATA / "non_equity_instruments.json") -> dict:
    off = {i["symbol"]: i for i in json.loads(official_path.read_text(encoding="utf-8"))["instruments"]}
    prov = {i["symbol"]: i for i in json.loads(provisional_path.read_text(encoding="utf-8"))["instruments"]}
    return {"official": off, "provisional": prov}


def classify_symbol(symbol: str, inst: dict) -> dict:
    """{class: equity|gold|debt|None, kind: stock|etf|fund_unit, reason}. class None = unclassified."""
    s = symbol.upper()
    off, prov = inst["official"].get(s), inst["provisional"].get(s)
    if off:
        u, name = off["underlying_class"], off.get("name", "").upper()
        if u == "EQUITY":
            return {"class": "equity", "kind": "etf", "reason": "ETF on an equity index (NSE official list)"}
        if u == "GLOBAL INDICES":
            return {"class": "equity", "kind": "etf", "reason": "ETF on a foreign index: counted as equity (international carve-out not modelled)"}
        if u == "DEBT":
            return {"class": "debt", "kind": "etf", "reason": "ETF on debt or liquid instruments (NSE official list)"}
        if u == "COMMODITY" and "GOLD" in name:
            return {"class": "gold", "kind": "etf", "reason": "gold ETF (NSE official list)"}
        return {"class": None, "kind": "etf", "reason": f"ETF with underlying '{u}' ({off.get('name')}): not one of equity, gold or debt in this plan"}
    if prov:
        k = prov.get("kind", "")
        if k == "liquid_fund":
            return {"class": "debt", "kind": "fund_unit", "reason": "liquid fund unit"}
        if "gold" in k:
            return {"class": "gold", "kind": "etf", "reason": "gold ETF (provisional list)"}
        return {"class": None, "kind": "etf", "reason": f"fund unit of kind '{k}' not mapped to equity, gold or debt"}
    return {"class": "equity", "kind": "stock", "reason": "direct stock"}


def current_allocation(holdings: list, balances: list, inst: dict) -> dict:
    """holdings: [{symbol, value}] (priced holdings of the investment pot); balances: [{asset_class, amount, label, as_of}] manual, dated.
    Returns classified values, what could not be classified, unpriced holdings, and the pieces behind each class (for the visual)."""
    values = {c: 0.0 for c in CLASSES}
    parts = {c: [] for c in CLASSES}
    unclassified, unpriced = [], []
    for h in holdings:
        if h.get("value") is None:
            unpriced.append({"symbol": h["symbol"], "reason": "no price"})
            continue
        c = classify_symbol(h["symbol"], inst)
        if c["class"] is None:
            unclassified.append({"symbol": h["symbol"], "value": float(h["value"]), "reason": c["reason"]})
        else:
            values[c["class"]] += float(h["value"])
            parts[c["class"]].append({"label": h["symbol"], "value": float(h["value"]), "source": c["kind"]})
    for b in balances:
        if b["asset_class"] not in CLASSES:
            raise ValueError(f"unknown asset class {b['asset_class']}")
        values[b["asset_class"]] += float(b["amount"])
        parts[b["asset_class"]].append({"label": b["label"], "value": float(b["amount"]), "source": "manual", "as_of": str(b["as_of"])})
    other = sum(x["value"] for x in unclassified)
    return {"values": values, "parts": parts, "unclassified": unclassified, "unpriced": unpriced, "unclassified_value": other, "total_classified": sum(values.values())}


def completeness_notes(alloc: dict, has_balances: bool) -> list:
    notes = []
    if alloc["unclassified"]:
        notes.append(f"{len(alloc['unclassified'])} holding(s) worth ₹{alloc['unclassified_value']:,.0f} could not be classified: " + "; ".join(f"{x['symbol']} ({x['reason']})" for x in alloc["unclassified"][:4]))
    if alloc["unpriced"]:
        notes.append(f"{len(alloc['unpriced'])} holding(s) have no price and are left out: " + ", ".join(x["symbol"] for x in alloc["unpriced"][:6]))
    if not has_balances:
        notes.append("No manual balances recorded: mutual funds, bonds, deposits and any other asset that cannot be imported are NOT in this picture, so the mix shown may look more equity-heavy than your real wealth. Add them as balances.")
    return notes
