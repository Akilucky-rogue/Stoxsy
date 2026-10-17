"""
PairsStatArbEngine — the StatArb family on the one TradeIdea contract.

Market-neutral mean reversion: when a cointegrated pair's spread is stretched
(|z| >= entry_z), SHORT the rich leg and LONG the cheap leg, betting the spread
reverts. DEFINED-RISK by construction — the loss if z runs from entry to `stop_z`
is finite and is what sizes the position (max_loss_rupees), so validate() passes.

PURE brain: candidate pairs + a price source (dict {symbol: close Series}) are
INJECTED, so `_to_idea` is fully unit-tested with synthetic series. The DB source
(sector-grouped liquid pairs from `ohlcv`) is a thin default — sector grouping is
deliberate: screening ALL C(n,2) pairs would data-mine spurious cointegration
(the textbook StatArb trap), so we only test economically-related names.

The idea carries a self-contained resolution plan (beta/alpha/mean/sigma/spread_0/
z-levels/side) in `adjustment_plan` (JSON), so the Scorecard's pair resolver can
replay the spread forward and score the TRUE net-of-cost outcome with no re-fit.
"""
from __future__ import annotations

import json
import logging
from math import floor
from typing import Callable, Optional

import pandas as pd

from qsde.engines.base import Engine, Horizon, Leg, Structure, TradeIdea
from qsde.stats.pairs_math import evaluate_pair

log = logging.getLogger(__name__)

PriceSource = Callable[[], dict]                 # () -> {symbol: pd.Series(close, index=date)}
PairList = list[tuple[str, str]]


class PairsStatArbEngine(Engine):
    name = "pairs_statarb"
    horizon = Horizon.ROTATION                   # mean-reversion holding ~ days to weeks

    def __init__(self, *, pairs: Optional[PairList] = None, lookback: int = 250,
                 entry_z: float = 2.0, exit_z: float = 0.5, stop_z: float = 3.5,
                 min_half_life: float = 1.0, max_half_life: float = 30.0,
                 max_pvalue: float = 0.05, max_hold: int = 20,
                 budget: float = 1_000_000.0, risk_rupees: float = 2000.0,
                 max_loss_cap: float = float("inf"),
                 price_source: Optional[PriceSource] = None) -> None:
        self.pairs = pairs
        self.lookback = int(lookback)
        self.entry_z = float(entry_z)
        self.exit_z = float(exit_z)
        self.stop_z = float(stop_z)
        self.min_half_life = float(min_half_life)
        self.max_half_life = float(max_half_life)
        self.max_pvalue = float(max_pvalue)
        self.max_hold = int(max_hold)
        self.budget = float(budget)
        self.risk_rupees = float(risk_rupees)
        self.max_loss_cap = float(max_loss_cap)
        self._price_source = price_source or self._default_price_source

    def scan(self) -> list[TradeIdea]:
        prices = self._price_source() or {}
        pairs = self.pairs if self.pairs is not None else self._default_pairs(prices)
        ideas: list[TradeIdea] = []
        for sym_a, sym_b in pairs:
            sa, sb = prices.get(sym_a), prices.get(sym_b)
            if sa is None or sb is None:
                continue
            try:
                idea = self._to_idea(sym_a, sym_b, sa, sb)
            except Exception as e:  # one bad pair must not kill the scan
                log.warning("pairs_statarb: skipping %s/%s (%s)", sym_a, sym_b, e)
                idea = None
            if idea is not None:
                ideas.append(idea)
        return ideas

    def _to_idea(self, sym_a: str, sym_b: str, sa, sb) -> Optional[TradeIdea]:
        """Pure: two price series -> a defined-risk market-neutral PAIR idea, or None."""
        ev = evaluate_pair(sa, sb, lookback=self.lookback, min_half_life=self.min_half_life,
                           max_half_life=self.max_half_life, max_pvalue=self.max_pvalue)
        if not ev.get("ok") or not ev.get("tradeable"):
            return None
        z = ev["z"]
        if abs(z) < self.entry_z:
            return None                          # spread not stretched enough
        if abs(z) >= self.stop_z:
            return None                          # already at/through the stop — no edge left

        beta, sigma = ev["beta"], ev["spread_sigma"]
        price_a, price_b = ev["price_a"], ev["price_b"]
        # Adverse spread move from entry |z| to the stop, in A's price units.
        adverse = (self.stop_z - abs(z)) * sigma
        if adverse <= 0:
            return None

        # Size A so the z-stop loss ~= the per-trade risk budget; hedge B with beta.
        qty_a = floor(self.risk_rupees / adverse)
        # Budget cap on gross two-leg notional.
        per_unit_notional = price_a + beta * price_b
        if per_unit_notional > 0:
            qty_a = min(qty_a, floor(self.budget / per_unit_notional))
        qty_b = round(qty_a * beta)
        if qty_a < 1 or qty_b < 1:
            return None                          # untradeable in this budget / hedge

        max_loss = round(qty_a * adverse, 2)
        if max_loss <= 0 or max_loss > self.max_loss_cap:
            return None

        # side: +1 LONG spread (z<0, expect spread up); -1 SHORT spread (z>0, expect down).
        side = -1 if z > 0 else +1
        a_side = side                            # spread = A - beta*B -> A moves with the spread
        b_side = -side
        gross_notional = round(qty_a * price_a + qty_b * price_b, 2)

        legs = [
            Leg(sym_a, a_side, int(qty_a)),
            Leg(sym_b, b_side, int(qty_b)),
        ]
        plan = {
            "sym_a": sym_a, "sym_b": sym_b, "beta": beta, "alpha": ev["alpha"],
            "spread_mean": ev["spread_mean"], "spread_sigma": sigma,
            "spread_0": ev["spread_now"], "entry_z": z, "exit_z": self.exit_z,
            "stop_z": self.stop_z, "side": side, "qty_a": int(qty_a), "qty_b": int(qty_b),
            "max_loss": max_loss, "gross_notional": gross_notional, "max_hold": self.max_hold,
        }
        direction_word = "SHORT" if side < 0 else "LONG"
        idea = TradeIdea(
            engine=self.name,
            symbol=f"{sym_a}/{sym_b}",
            structure=Structure.PAIR, horizon=self.horizon, direction=0,
            legs=legs,
            qty=int(qty_a), capital_required=gross_notional,
            max_loss_rupees=max_loss,
            confidence=round(min(1.0, max(0.0, 1.0 - ev["pvalue"])), 3),
            adjustment_plan=json.dumps(plan),
            reasons=[f"{direction_word} spread {sym_a}-{beta:.2f}*{sym_b} @ z={z:+.2f} "
                     f"(p={ev['pvalue']:.3f}, hl={ev['half_life']:.1f}d); "
                     f"exit |z|<={self.exit_z}, stop |z|>={self.stop_z}; "
                     f"qty {int(qty_a)}/{int(qty_b)}, risk Rs{max_loss:.0f}"],
        )
        return idea.validate()

    # ── Default DB source (sector-grouped liquid pairs) ──────────────────

    @staticmethod
    def _default_pairs(prices: dict) -> PairList:
        """Sector-grouped candidate pairs from the universe (anti-data-mining). Pairs
        only genuinely SAME-sector names — this is the load-bearing guard against
        spurious cointegration: screening all C(n,2) pairs would surface ~5% false
        positives by chance (the textbook StatArb trap). A REAL sector is required
        (NULL/blank excluded), so unsectored names are never paired cross-sector."""
        from qsde.db.connection import read_sql
        syms = list(prices.keys())
        if len(syms) < 2:
            return []
        df = read_sql(
            "SELECT symbol, sector FROM universe "
            "WHERE is_active = TRUE AND sector IS NOT NULL AND btrim(sector) <> ''")
        in_prices = df[df["symbol"].isin(syms)]
        out: PairList = []
        for _sector, grp in in_prices.groupby("sector"):
            members = sorted(grp["symbol"].tolist())
            for i in range(len(members)):
                for j in range(i + 1, len(members)):
                    out.append((members[i], members[j]))
        if not out:
            log.warning("pairs_statarb: no same-sector candidate pairs — universe.sector "
                        "is sparse (%d of %d symbols sectored). Populate sectors to enable "
                        "the engine; refusing to pair cross-sector (anti-data-mining).",
                        len(in_prices), len(syms))
        else:
            log.info("pairs_statarb: %d same-sector candidate pairs from %d sectored names",
                     len(out), len(in_prices))
        return out

    def _default_price_source(self) -> dict:
        """Trailing close series per liquid (tradeable-tier) symbol, from `ohlcv`."""
        from qsde.db.connection import read_sql
        need = self.lookback + self.max_half_life + 40
        df = read_sql(
            """SELECT o.symbol, o.date, o.close
                 FROM ohlcv o
                 JOIN universe u ON u.symbol = o.symbol
                WHERE u.is_active = TRUE
                  AND COALESCE(u.liquidity_tier, 'tradeable') = 'tradeable'
                  AND o.date >= (CURRENT_DATE - (:days)::int)
             ORDER BY o.symbol, o.date""",
            params={"days": int(need * 2 + 30)},
        )
        if df.empty:
            return {}
        df["date"] = pd.to_datetime(df["date"])
        return {sym: g.set_index("date")["close"].astype(float)
                for sym, g in df.groupby("symbol")}


__all__ = ["PairsStatArbEngine", "PriceSource", "PairList"]
