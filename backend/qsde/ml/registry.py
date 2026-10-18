"""Single source of truth for horizons, labels and costs.

Before this module the same numbers lived in `purged_cv.HORIZON_DAYS`,
`triple_barrier._HORIZON_BARRIERS`, `scorecard.resolver._HORIZON_SESSIONS`,
`paper_journal._HORIZON_SESSIONS`, `risk.costs` and an env var
(`QSDE_PROMOTION_COST_BPS`). Those now DERIVE from here, and a test fails on any
new hard-coded horizon number.

A horizon is a machine-readable research specification: it states when the
decision is taken, how the position is entered and exited, how the label is
built, which cost model applies and which universe is eligible. "swing" can no
longer mean 5 days in one place and 10 in another.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Optional

# ── costs ────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class CostModel:
    id: str
    description: str
    components_bps: dict            # statutory + exchange components, ROUND TRIP, in bps
    slippage_bps_by_tier: dict      # extra round-trip bps by liquidity tier
    paper_extra_bps: float          # conservative add-on used for paper trading / the Scorecard

    @property
    def statutory_bps(self) -> float:
        return float(sum(self.components_bps.values()))

    def round_trip_bps(self, tier: Optional[str] = None, paper: bool = False) -> float:
        """tier=None keeps the legacy numbers exactly (statutory only; +paper add-on if paper)."""
        bps = self.statutory_bps
        if tier is not None:
            bps += float(self.slippage_bps_by_tier[tier])
        if paper:
            bps += float(self.paper_extra_bps)
        return bps


COST_MODELS: dict[str, CostModel] = {
    # Zerodha MIS: brokerage ~3, STT 0.025% sell side 2.5, exchange+GST+SEBI+stamp 0.5 (incl. 0.5 slippage) = 6
    "mis_intraday_v1": CostModel(
        id="mis_intraday_v1",
        description="Intraday MIS round trip (Zerodha-realistic, small ticket)",
        components_bps={"brokerage": 3.0, "stt": 2.5, "exchange_gst_sebi_stamp_slippage": 0.5},
        slippage_bps_by_tier={"adv_ge_50cr": 0.5, "adv_10_50cr": 1.5, "adv_lt_10cr": 6.0},
        paper_extra_bps=2.0,                        # 6 -> 8 (legacy PAPER_DEFAULT_BPS)
    ),
    # CNC delivery: STT 0.1% each side = 20 bps (does NOT amortise over the holding period) + ~4 other
    "cnc_delivery_v1": CostModel(
        id="cnc_delivery_v1",
        description="Delivery (CNC) round trip; STT 20 bps dominates",
        components_bps={"brokerage": 0.0, "stt": 20.0, "exchange_gst_sebi_stamp_dp": 4.0},
        slippage_bps_by_tier={"adv_ge_50cr": 2.0, "adv_10_50cr": 6.0, "adv_lt_10cr": 15.0},
        paper_extra_bps=6.0,                        # 24 -> 30 (legacy PAPER_DEFAULT_BPS)
    ),
}

# Cost levels every evaluation reports, so a result is never shown at one cost only.
COST_STRESS_BPS: tuple[float, ...] = (10.0, 25.0, 30.0)


# ── labels ───────────────────────────────────────────────────────────

@dataclass(frozen=True)
class LabelSpec:
    id: str
    description: str
    entry_rule: str                 # next_open
    window: str                     # bars t+1 .. t+h (bar t is NOT in the window)
    barrier_vol: str                # sigma known at close of t
    tie_rule: str                   # stop_first
    gap_rule: str                   # exit at the open when the open gaps through a barrier
    time_exit: str                  # close of bar t+h
    target: str
    exclusions: tuple = ()


LABELS: dict[str, LabelSpec] = {
    "tb_nextopen_v1": LabelSpec(
        id="tb_nextopen_v1",
        description="Triple barrier, entry at next open, high/low walk, pessimistic tie, gap-aware exits",
        entry_rule="next_open",
        window="bars t+1 .. t+h inclusive; decision at close of t, bar t excluded",
        barrier_vol="EWM std of daily returns (span per horizon) as of close t",
        tie_rule="stop_first (pessimistic)",
        gap_rule="open beyond a barrier fills at the open (not at the barrier price)",
        time_exit="close of bar t+h",
        target="side-adjusted net return after the horizon's cost model",
        exclusions=("label windows touching an UNEXPLAINED corporate-action event",
                    "feature dates within 282 sessions after an UNEXPLAINED event"),
    ),
}


# ── horizons ─────────────────────────────────────────────────────────

@dataclass(frozen=True)
class BarrierSpec:
    pt_mult: float                  # profit-take, in units of sigma
    sl_mult: float                  # stop-loss, in units of sigma
    vol_span: int = 100


@dataclass(frozen=True)
class HorizonSpec:
    id: str
    legacy: str                     # name used by the older code paths
    label_horizon_days: int         # NSE sessions
    decision_time: str
    entry_price_rule: str
    exit_rule: str
    barrier: BarrierSpec
    cost_model: str
    label: str
    min_adv_cr: float               # trailing median traded value filter
    eligible_universe: str
    purge_days: int                 # sessions; FLOOR - the harness purges by each sample's own label_end
    embargo_days: int               # sessions; floor
    status: str = "active"          # active | proposed (not yet validated)
    notes: str = ""


HORIZONS: dict[str, HorizonSpec] = {
    "intraday_1d": HorizonSpec(
        id="intraday_1d", legacy="intraday", label_horizon_days=1,
        decision_time="close of t (pre-open of t+1)", entry_price_rule="next_open",
        exit_rule="triple_barrier_hl_pessimistic_gap_aware (same-session close at the latest)",
        barrier=BarrierSpec(1.5, 0.75), cost_model="mis_intraday_v1", label="tb_nextopen_v1",
        min_adv_cr=10.0, eligible_universe="adv60_top500_v1+adv10", purge_days=1, embargo_days=1,
        notes="Legacy 'intraday' was close-to-close overnight (AUDIT H3); this spec is open-to-barrier."),
    "swing_5d": HorizonSpec(
        id="swing_5d", legacy="swing", label_horizon_days=5,
        decision_time="close of t", entry_price_rule="next_open",
        exit_rule="triple_barrier_hl_pessimistic_gap_aware",
        barrier=BarrierSpec(2.5, 1.5), cost_model="cnc_delivery_v1", label="tb_nextopen_v1",
        min_adv_cr=10.0, eligible_universe="adv60_top500_v1+adv10", purge_days=5, embargo_days=5),
    "swing_10d": HorizonSpec(
        id="swing_10d", legacy="rotation", label_horizon_days=10,
        decision_time="close of t", entry_price_rule="next_open",
        exit_rule="triple_barrier_hl_pessimistic_gap_aware",
        barrier=BarrierSpec(3.5, 2.0), cost_model="cnc_delivery_v1", label="tb_nextopen_v1",
        min_adv_cr=10.0, eligible_universe="adv60_top500_v1+adv10", purge_days=10, embargo_days=10,
        status="proposed", notes="Barrier multipliers scaled from swing_5d by ~sqrt(2); not yet validated."),
    "position_20d": HorizonSpec(
        id="position_20d", legacy="long", label_horizon_days=20,
        decision_time="close of t", entry_price_rule="next_open",
        exit_rule="triple_barrier_hl_pessimistic_gap_aware",
        barrier=BarrierSpec(4.5, 2.5), cost_model="cnc_delivery_v1", label="tb_nextopen_v1",
        min_adv_cr=10.0, eligible_universe="adv60_top500_v1+adv10", purge_days=20, embargo_days=20),
}
# event_driven is deliberately absent until corporate actions are reliable (see ADR 0001).

LEGACY_ALIASES: dict[str, str] = {h.legacy: h.id for h in HORIZONS.values()}
LEGACY_ALIASES["btst"] = "swing_5d"       # cost/tax treatment only; no separate horizon


def get_horizon(name: str) -> HorizonSpec:
    """Accepts a registry id ('swing_5d') or a legacy name ('swing')."""
    key = str(name).lower().strip()
    key = LEGACY_ALIASES.get(key, key)
    try:
        return HORIZONS[key]
    except KeyError:
        raise KeyError(f"unknown horizon {name!r}; registered: {sorted(HORIZONS)} "
                       f"(legacy: {sorted(LEGACY_ALIASES)})") from None


def get_label(name: str) -> LabelSpec:
    return LABELS[name]


def get_cost_model(name: str) -> CostModel:
    return COST_MODELS[name]


# ── derived views for the older code paths ───────────────────────────

LEGACY_CORE = ("intraday", "swing", "long")


def legacy_sessions(names: tuple = LEGACY_CORE) -> dict[str, int]:
    """Legacy horizon name -> sessions, e.g. for the three core names (+ 'rotation' if asked)."""
    return {n: get_horizon(n).label_horizon_days for n in names}


def legacy_barriers(names: tuple = LEGACY_CORE) -> dict[str, dict[str, float]]:
    out = {}
    for n in names:
        h = get_horizon(n)
        out[n] = {"pt_mult": h.barrier.pt_mult, "sl_mult": h.barrier.sl_mult,
                  "t1_sessions": h.label_horizon_days}
    return out


def round_trip_bps(horizon: str, tier: Optional[str] = None, paper: bool = False) -> float:
    return get_cost_model(get_horizon(horizon).cost_model).round_trip_bps(tier=tier, paper=paper)


def promotion_cost_bps(horizon: str) -> float:
    """Cost the promotion gate charges: the SAME conservative number the Scorecard
    charges to paper trades, so a model is judged at what it would actually pay."""
    return round_trip_bps(horizon, paper=True)


def to_manifest() -> dict:
    """JSON-able snapshot, stored with every evaluation/model card."""
    return {"horizons": {k: asdict(v) for k, v in HORIZONS.items()},
            "labels": {k: asdict(v) for k, v in LABELS.items()},
            "cost_models": {k: {**asdict(v), "statutory_bps": v.statutory_bps} for k, v in COST_MODELS.items()},
            "cost_stress_bps": list(COST_STRESS_BPS)}
