"""Monthly return history of the three asset-class proxies the Investor engine uses, with every choice stated (proxy, basis, window, breaks dropped).

Equity = NIFTYBEES price return (dividends excluded); gold = GOLDBEES price return; debt = cross-sectional median monthly return of liquid / overnight direct-growth AMFI schemes
(total return after expenses). A single-day move above 50% in any series is a unit change or a bad print and that day is DROPPED from the returns, never treated as a return.
`monthly_returns` is pure (given daily series); `load` reads the database.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd

BREAK = 0.5
CLASSES = ("equity", "gold", "debt")


@dataclass
class History:
    monthly: pd.DataFrame                              # index = month end, columns = CLASSES, simple monthly returns
    notes: list = field(default_factory=list)
    breaks: dict = field(default_factory=dict)         # class -> list of dropped dates
    proxies: dict = field(default_factory=dict)
    daily: Optional[pd.DataFrame] = None               # NSE trading days, columns = CLASSES, simple daily returns (used for drawdowns, which monthly data understates)

    @property
    def window(self) -> dict:
        m = self.monthly
        return {"start": str(m.index[0].date()), "end": str(m.index[-1].date()), "months": int(len(m))}


def daily_returns(price: pd.Series, break_threshold: float = BREAK) -> tuple[pd.Series, list]:
    """Simple daily returns with unit-change days removed. Returns (returns, dropped dates)."""
    p = price.dropna().astype(float).sort_index()
    p = p[~p.index.duplicated(keep="last")]
    r = p.pct_change().dropna()
    bad = r[r.abs() > break_threshold]
    return r.drop(bad.index), [str(d.date()) for d in bad.index]


def to_monthly(daily: pd.Series) -> pd.Series:
    """Compound daily returns within each calendar month (months with no observations are dropped)."""
    g = (1.0 + daily).groupby(daily.index.to_period("M"))
    out = g.prod() - 1.0
    out = out[g.size() > 0]
    out.index = out.index.to_timestamp("M")
    return out


def monthly_returns(daily_by_class: dict, last_complete_month: Optional[pd.Timestamp] = None) -> pd.DataFrame:
    """Align the three monthly series on their common months. A trailing partial month is excluded when `last_complete_month` is given."""
    m = pd.concat({k: to_monthly(v) for k, v in daily_by_class.items()}, axis=1).dropna()
    if last_complete_month is not None:
        m = m[m.index <= last_complete_month]
    return m[[c for c in CLASSES if c in m.columns]]


def daily_aligned(daily_by_class: dict, last_day: Optional[pd.Timestamp] = None) -> pd.DataFrame:
    """Daily returns on NSE trading days. Debt (published on calendar days) is compounded across the days between two trading days so no accrual is lost."""
    days = daily_by_class["equity"].index.intersection(daily_by_class["gold"].index)
    out = {"equity": daily_by_class["equity"].reindex(days), "gold": daily_by_class["gold"].reindex(days)}
    cum = (1.0 + daily_by_class["debt"]).cumprod()
    c = cum.reindex(cum.index.union(days)).ffill().reindex(days)
    out["debt"] = c.pct_change()
    d = pd.DataFrame(out).dropna()
    if last_day is not None:
        d = d[d.index <= last_day]
    return d[[c for c in CLASSES]]


def last_complete_month(today: Optional[pd.Timestamp] = None) -> pd.Timestamp:
    t = (today or pd.Timestamp.today()).normalize()
    return (t.to_period("M") - 1).to_timestamp("M")


def debt_basket(nav: pd.DataFrame, min_funds: int = 3) -> pd.Series:
    """Daily return of the debt proxy: the median across schemes of each scheme's daily NAV return, for dates where at least `min_funds` schemes have a return.
    `nav` columns = scheme codes, index = dates (calendar days, including weekends when AMFI publishes them)."""
    r = nav.sort_index().pct_change(fill_method=None)
    r = r.where(r.abs() <= BREAK)
    n = r.notna().sum(axis=1)
    return r.median(axis=1)[n >= min_funds].dropna()


def load(read_sql=None, today: Optional[pd.Timestamp] = None, policy: Optional[dict] = None) -> History:
    from qsde.db.connection import read_sql as _rs
    from qsde.modes import policy as pol

    rs = read_sql or _rs
    policy = policy or pol.load_policy()
    prox = policy["investor"]["proxies"]
    daily, breaks, notes = {}, {}, []
    for cls in ("equity", "gold"):
        sym = prox[cls]["series"]
        d = rs("SELECT date, close FROM ohlcv WHERE symbol = :s ORDER BY date", params={"s": sym})
        r, br = daily_returns(pd.Series(d["close"].to_numpy(), index=pd.to_datetime(d["date"])))
        daily[cls], breaks[cls] = r, br
        if br:
            notes.append(f"{sym}: dropped {len(br)} unit-change day(s) {', '.join(br)} (a move above {BREAK:.0%} in one day)")
    codes = rs("""SELECT scheme_code FROM mf_scheme_master WHERE fund_group = 'liquid_or_overnight' AND is_direct AND is_growth AND first_nav_date <= DATE '2016-01-31'
                  AND last_nav_date >= CURRENT_DATE - INTERVAL '14 days'""")["scheme_code"].tolist()
    if len(codes) < 3:
        raise ValueError(f"only {len(codes)} liquid direct-growth schemes with history from 2016: the debt proxy needs at least 3")
    nav = rs("SELECT scheme_code, date, nav FROM mf_nav WHERE scheme_code = ANY(:c) AND date >= DATE '2015-12-01'", params={"c": [int(c) for c in codes]})
    nav["date"] = pd.to_datetime(nav["date"])
    wide = nav.pivot_table(index="date", columns="scheme_code", values="nav", aggfunc="last")
    daily["debt"] = debt_basket(wide)
    breaks["debt"] = []
    notes.append(f"debt proxy: median of {len(codes)} liquid / overnight direct-growth schemes")
    lcm = last_complete_month(today)
    m = monthly_returns(daily, lcm)
    d = daily_aligned(daily, lcm)
    d = d[d.index >= m.index[0].to_period("M").to_timestamp()]
    return History(monthly=m, notes=notes, breaks=breaks, proxies=prox, daily=d)
