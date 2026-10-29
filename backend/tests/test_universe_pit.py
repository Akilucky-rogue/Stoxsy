"""Point-in-time universe: membership only uses data available on each date."""
from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from qsde.universe.pit import build_universe_pit


def _days(n, start=date(2020, 1, 6)):
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def _panel(spec: dict[str, tuple[int, int, float, float]], n=300) -> pd.DataFrame:
    """spec: symbol -> (first_idx, last_idx_exclusive, turnover_lacs, close)."""
    days = _days(n)
    rows = []
    for sym, (a, b, tl, px) in spec.items():
        for i in range(a, min(b, n)):
            rows.append(dict(symbol=sym, date=days[i], close=px, turnover_lacs=tl))
    return pd.DataFrame(rows)


KW = dict(window=20, min_history=40, min_periods=15)


class TestMembership:
    def test_top_n_by_trailing_median_traded_value(self):
        p = _panel({"A": (0, 300, 5000, 100), "B": (0, 300, 4000, 100), "C": (0, 300, 10, 100)})
        u = build_universe_pit(p, top_n=2, **KW)
        last = u[u["date"] == u["date"].max()].set_index("symbol")
        assert set(last.index) == {"A", "B"} and last.loc["A", "rank"] == 1 and last.loc["B", "rank"] == 2

    def test_delisted_name_is_a_member_while_it_was_liquid_and_not_after(self):
        # DEAD trades heavily until day 200 then stops; LIVE is steady and smaller
        p = _panel({"DEAD": (0, 200, 9000, 100), "LIVE": (0, 300, 3000, 100)})
        u = build_universe_pit(p, top_n=1, **KW)
        days = _days(300)
        early = u[u["date"] == days[150]]
        late = u[u["date"] == days[290]]
        assert list(early["symbol"]) == ["DEAD"]          # survivorship-free: the loser is there
        assert list(late["symbol"]) == ["LIVE"]

    def test_no_lookahead_rank_on_a_date_ignores_later_data(self):
        p = _panel({"A": (0, 300, 5000, 100), "B": (0, 300, 4000, 100)})
        full = build_universe_pit(p, top_n=1, **KW)
        cut = _days(300)[120]
        trunc = build_universe_pit(p[p["date"] <= cut], top_n=1, **KW)
        a = full[full["date"] == cut][["symbol", "rank", "adv_cr"]].reset_index(drop=True)
        b = trunc[trunc["date"] == cut][["symbol", "rank", "adv_cr"]].reset_index(drop=True)
        pd.testing.assert_frame_equal(a, b)

    def test_history_and_price_gates(self):
        p = _panel({"OLD": (0, 300, 5000, 100), "NEW": (270, 300, 9000, 100), "PENNY": (0, 300, 9000, 5)})
        u = build_universe_pit(p, top_n=5, **KW)
        last = set(u[u["date"] == u["date"].max()]["symbol"])
        assert last == {"OLD"}          # NEW has <40 sessions; PENNY is below min_price

    def test_rights_warrants_and_etfs_excluded(self):
        p = _panel({"GOOD": (0, 300, 1000, 100), "ABC-RE": (0, 300, 9000, 100),
                    "NIFTYBEES": (0, 300, 9000, 100), "XYZ-W1": (0, 300, 9000, 100)})
        u = build_universe_pit(p, top_n=10, **KW)
        assert set(u["symbol"]) == {"GOOD"}

    def test_a_symbol_that_stops_trading_loses_liquidity_gradually_not_instantly(self):
        # median over the window: a few missing days do not evict a heavy trader
        p = _panel({"A": (0, 300, 8000, 100), "B": (0, 300, 500, 100)})
        p = p[~((p["symbol"] == "A") & (p["date"] == _days(300)[200]))]
        u = build_universe_pit(p, top_n=1, **KW)
        assert list(u[u["date"] == _days(300)[201]]["symbol"]) == ["A"]

    def test_output_shape_and_method_tag(self):
        p = _panel({"A": (0, 300, 5000, 100)})
        u = build_universe_pit(p, top_n=500, **KW)
        assert {"date", "symbol", "rank", "adv_cr", "close", "top_n", "method"} <= set(u.columns)
        assert (u["method"] == "adv20_top500_v1").all() and u["rank"].min() == 1
        assert np.isclose(u["adv_cr"].iloc[-1], 5000 * 1e5 / 1e7)
