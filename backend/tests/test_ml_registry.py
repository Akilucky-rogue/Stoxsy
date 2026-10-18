"""Horizon / label / cost registry: single source of truth and no drift."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from qsde.ml import registry as R

BACKEND = Path(__file__).resolve().parents[1]


class TestHorizonSpecs:
    def test_every_horizon_is_complete_and_consistent(self):
        for h in R.HORIZONS.values():
            assert h.label_horizon_days >= 1
            assert h.entry_price_rule == "next_open"
            assert h.label in R.LABELS and h.cost_model in R.COST_MODELS
            assert h.purge_days >= h.label_horizon_days and h.embargo_days >= 1
            assert h.barrier.pt_mult > h.barrier.sl_mult > 0
            assert h.min_adv_cr > 0 and h.eligible_universe

    def test_legacy_aliases_resolve_and_unknown_raises(self):
        assert R.get_horizon("swing").id == "swing_5d"
        assert R.get_horizon("long").id == "position_20d"
        assert R.get_horizon("rotation").id == "swing_10d"
        assert R.get_horizon("intraday").id == "intraday_1d"
        assert R.get_horizon("swing_5d") is R.get_horizon("SWING")
        with pytest.raises(KeyError):
            R.get_horizon("monthly")

    def test_event_driven_is_not_registered_yet(self):
        assert "event_driven" not in R.HORIZONS

    def test_proposed_horizon_is_flagged(self):
        assert R.HORIZONS["swing_10d"].status == "proposed"

    def test_manifest_is_json_serialisable(self):
        import json
        json.dumps(R.to_manifest())


class TestLegacyConstantsAreDerivedAndUnchanged:
    """These pin the numbers the old modules hard-coded, so moving them into the
    registry could not change behaviour."""

    def test_sessions(self):
        from qsde.models.purged_cv import HORIZON_DAYS
        from qsde.scorecard.resolver import _HORIZON_SESSIONS as RES
        from qsde.execution.paper_journal import _HORIZON_SESSIONS as PAPER
        assert HORIZON_DAYS == {"intraday": 1, "swing": 5, "long": 20}
        assert PAPER == {"intraday": 1, "swing": 5, "long": 20}
        assert RES == {"intraday": 1, "swing": 5, "rotation": 10, "long": 20}

    def test_barriers(self):
        from qsde.models.triple_barrier import _HORIZON_BARRIERS
        assert _HORIZON_BARRIERS == {
            "intraday": {"pt_mult": 1.5, "sl_mult": 0.75, "t1_sessions": 1},
            "swing":    {"pt_mult": 2.5, "sl_mult": 1.50, "t1_sessions": 5},
            "long":     {"pt_mult": 4.5, "sl_mult": 2.50, "t1_sessions": 20},
        }

    def test_costs(self):
        from qsde.risk import costs
        assert costs.COSTS_BPS == {"intraday": 6.0, "btst": 24.0, "swing": 24.0, "long": 24.0}
        assert costs.PAPER_DEFAULT_BPS == {"intraday": 8.0, "btst": 30.0, "swing": 30.0, "long": 30.0}
        assert costs.cost_bps("swing") == 24.0 and costs.cost_bps("swing", paper_default=True) == 30.0
        assert costs.cost_bps("unknown-horizon") == 15.0                      # legacy fallback kept

    def test_promotion_gate_charges_the_scorecard_cost(self):
        from qsde.risk.costs import cost_bps
        for h in ("intraday", "swing", "long"):
            assert R.promotion_cost_bps(h) == cost_bps(h, paper_default=True)


class TestCostModel:
    def test_tiered_slippage_is_additive_and_ordered(self):
        cm = R.get_cost_model("cnc_delivery_v1")
        big, mid, small = (cm.round_trip_bps(t) for t in ("adv_ge_50cr", "adv_10_50cr", "adv_lt_10cr"))
        assert cm.statutory_bps < big < mid < small
        assert cm.round_trip_bps("adv_10_50cr", paper=True) == mid + cm.paper_extra_bps

    def test_stress_levels_cover_the_old_gate_default_and_the_paper_cost(self):
        assert 10.0 in R.COST_STRESS_BPS and 30.0 in R.COST_STRESS_BPS


class TestNoHardCodedHorizonNumbers:
    """Fail on any NEW dict literal that re-states horizon -> number outside the registry."""

    PATTERNS = [
        r'"intraday"\s*:\s*\d+\s*,\s*"swing"\s*:\s*\d+',
        r'"swing"\s*:\s*\d+\s*,\s*"long"\s*:\s*\d+',
        r"'intraday'\s*:\s*\d+\s*,\s*'swing'\s*:\s*\d+",
        r'"swing"\s*:\s*\{\s*"pt_mult"',
    ]

    def test_no_literal_horizon_session_or_barrier_tables(self):
        offenders = []
        files = list((BACKEND / "qsde").rglob("*.py")) + list((BACKEND / "api").rglob("*.py")) \
            + list((BACKEND / "scripts").glob("*.py"))
        for p in files:
            if p.name == "registry.py" or "_archive" in p.parts:
                continue
            txt = p.read_text(encoding="utf-8", errors="ignore")
            for pat in self.PATTERNS:
                if re.search(pat, txt):
                    offenders.append(f"{p.relative_to(BACKEND)} :: {pat}")
        assert not offenders, "hard-coded horizon tables (use qsde.ml.registry):\n" + "\n".join(offenders)
