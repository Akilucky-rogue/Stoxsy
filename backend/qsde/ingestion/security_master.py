"""Stable security ids across symbol changes, from the OFFICIAL NSE symbol-change list + a price-archive hand-off check.

An NSE rename (old -> new on date E) is accepted for stitching only if the archive agrees:
  * old's last bar is before E and new's first bar is on/after the day before E, within HANDOFF_SESSIONS of each other
  * the two series do not overlap in time (two live stocks are not a rename)
Rows that fail the check stay in `rejected` with the reason; they are never used. Chains (A->B->C) are followed; a symbol that
appears in two securities with overlapping validity (symbol re-use) is reported as a conflict and dropped.

security_id = 'S:' + the earliest symbol of the chain, so it does not change when a later rename is added.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Optional

import numpy as np
import pandas as pd

HANDOFF_SESSIONS = 15
FOREVER = pd.Timestamp("9999-12-31")


@dataclass
class SecurityMap:
    intervals: pd.DataFrame                     # security_id, symbol, valid_from, valid_to
    rejected: pd.DataFrame = field(default_factory=lambda: pd.DataFrame(columns=["old_symbol", "new_symbol", "effective_date", "reason"]))
    conflicts: list = field(default_factory=list)

    def security_of(self, symbol: str, on: Optional[pd.Timestamp] = None) -> Optional[str]:
        d = self.intervals[self.intervals["symbol"] == symbol]
        if on is not None:
            d = d[(d["valid_from"] <= on) & (d["valid_to"] >= on)]
        return None if d.empty else str(d.iloc[0]["security_id"])

    def symbol_on(self, symbol: str, on: pd.Timestamp) -> str:
        """The symbol the SAME security traded under on `on` (follows renames in both directions). Unchanged if unmapped."""
        sid = self.security_of(symbol)
        if sid is None:
            return symbol
        d = self.intervals[(self.intervals["security_id"] == sid) & (self.intervals["valid_from"] <= on) & (self.intervals["valid_to"] >= on)]
        return symbol if d.empty else str(d.iloc[0]["symbol"])


def build_security_map(official: pd.DataFrame, bars: pd.DataFrame, calendar: np.ndarray) -> SecurityMap:
    """official: old_symbol, new_symbol, effective_date. bars: symbol, first_bar, last_bar (archive). calendar: sorted market dates."""
    cal = np.sort(pd.to_datetime(pd.Series(calendar)).to_numpy())
    b = bars.set_index("symbol")
    rej, ok = [], []
    for r in official.sort_values("effective_date").itertuples():
        E = pd.Timestamp(r.effective_date)
        if r.old_symbol not in b.index or r.new_symbol not in b.index:
            rej.append((r.old_symbol, r.new_symbol, E, "no archive bars for one side"))
            continue
        o_last, n_first = pd.Timestamp(b.loc[r.old_symbol, "last_bar"]), pd.Timestamp(b.loc[r.new_symbol, "first_bar"])
        if n_first <= o_last:
            rej.append((r.old_symbol, r.new_symbol, E, "series overlap in time"))
            continue
        gap = int(((cal > o_last.to_datetime64()) & (cal < n_first.to_datetime64())).sum())
        if gap > HANDOFF_SESSIONS or abs((o_last - E).days) > 40 or abs((n_first - E).days) > 40:
            rej.append((r.old_symbol, r.new_symbol, E, f"hand-off not consistent with the archive ({gap} sessions between series)"))
            continue
        ok.append((r.old_symbol, r.new_symbol, E))

    # chains via union-find on symbols
    parent: dict = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for o, n, _ in ok:
        parent[find(o)] = find(n)
    groups: dict = {}
    for o, n, E in ok:
        groups.setdefault(find(o), []).append((o, n, E))

    rows, conflicts = [], []
    for g in groups.values():
        g = sorted(g, key=lambda t: t[2])
        chain = [g[0][0]] + [n for _, n, _ in g]
        if len(set(chain)) != len(chain):
            conflicts.append(f"cycle or symbol re-use in chain {chain}")
            continue
        first_bar = pd.Timestamp(b.loc[chain[0], "first_bar"]) if chain[0] in b.index else pd.Timestamp("1900-01-01")
        sid = "S:" + chain[0]
        starts = [first_bar] + [E for _, _, E in g]
        ends = [E - pd.Timedelta(days=1) for _, _, E in g] + [FOREVER]
        for sym, vf, vt in zip(chain, starts, ends):
            rows.append((sid, sym, vf, vt))
    iv = pd.DataFrame(rows, columns=["security_id", "symbol", "valid_from", "valid_to"])
    dup = iv[iv.duplicated("symbol", keep=False)]
    if len(dup):
        conflicts.append(f"symbols in more than one security: {sorted(dup['symbol'].unique())[:5]}")
        iv = iv[~iv["symbol"].isin(dup["symbol"])]
    return SecurityMap(iv.reset_index(drop=True), pd.DataFrame(rej, columns=["old_symbol", "new_symbol", "effective_date", "reason"]), conflicts)
