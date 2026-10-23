"""Trader/Investor mode policy: defaults, ceilings and the pure checks that enforce them.

Everything here is pure (no database): the policy file holds NO personal amounts. The rules:
  * tightening a limit is always allowed; loosening it beyond its default needs an admin approval note and stays under its ceiling;
  * a value above the ceiling (or the never_above bound) is always refused;
  * the trading pot may not exceed a share of investable capital unless an admin override note is given;
  * every investor mix sums to 100 and debt is the residual.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Optional

POLICY_PATH = Path(__file__).resolve().parents[3] / "infra" / "data" / "mode_policy.json"
REGISTRY_PATH = Path(__file__).resolve().parents[3] / "infra" / "data" / "model_registry.json"
MODES = ("investor", "trader")
POTS = ("investment", "trading")
NOTE_MIN_CHARS = 8


class PolicyError(ValueError):
    pass


@lru_cache(maxsize=1)
def load_policy() -> dict:
    p = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
    validate_policy(p)
    return p


def validate_policy(p: dict) -> None:
    for name, mix in p["investor"]["risk_profiles"].items():
        total = round(mix["equity"] + mix["gold"] + mix["debt"], 6)
        if total != 100:
            raise PolicyError(f"risk profile {name} sums to {total}, not 100")
    for name, spec in p["trader_limits"].items():
        if not spec["default"] <= spec["ceiling"]:
            raise PolicyError(f"limit {name}: default exceeds ceiling")
        if "never_above" in spec and spec["ceiling"] > spec["never_above"]:
            raise PolicyError(f"limit {name}: ceiling exceeds the never_above bound")


def engine_approved(registry_path: Path = REGISTRY_PATH) -> bool:
    """True only if the owner has set some model to 'approved' in the registry. Today none is."""
    try:
        reg = json.loads(registry_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return any(m.get("state") == "approved" for m in reg.get("models", {}).values())


def ceiling_for(name: str, policy: dict, approved: bool) -> float:
    spec = policy["trader_limits"][name]
    c = spec["ceiling"]
    if approved and "ceiling_if_engine_approved" in spec:
        c = max(c, spec["ceiling_if_engine_approved"])
    if "never_above" in spec:
        c = min(c, spec["never_above"])
    return c


def defaults(policy: Optional[dict] = None) -> dict:
    p = policy or load_policy()
    return {k: v["default"] for k, v in p["trader_limits"].items()}


def effective_limits(stored: Optional[dict], policy: Optional[dict] = None) -> dict:
    out = defaults(policy)
    out.update({k: v for k, v in (stored or {}).items() if k in out})
    return out


@dataclass
class LimitCheck:
    new: dict                                     # the full deviations dict to store
    needs_approval: list = field(default_factory=list)   # limits loosened beyond default
    errors: list = field(default_factory=list)


def check_limit_changes(requested: dict, current_stored: Optional[dict], policy: Optional[dict] = None, approved_engine: bool = False) -> LimitCheck:
    p = policy or load_policy()
    specs = p["trader_limits"]
    new = dict(current_stored or {})
    res = LimitCheck(new=new)
    for k, v in requested.items():
        if k not in specs:
            res.errors.append(f"unknown limit '{k}'")
            continue
        try:
            v = float(v)
        except (TypeError, ValueError):
            res.errors.append(f"{k}: not a number")
            continue
        if not v > 0:
            res.errors.append(f"{k}: must be positive")
            continue
        spec, ceil = specs[k], ceiling_for(k, p, approved_engine)
        if v > ceil:
            why = f"; {spec['never_above']} is never enabled" if "never_above" in spec and v > spec["never_above"] else ""
            res.errors.append(f"{k}: {v:g} is above the ceiling {ceil:g}{why}")
        elif v > spec["default"]:
            res.needs_approval.append(k)
            new[k] = v
        elif v == spec["default"]:
            new.pop(k, None)
        else:
            new[k] = v                                    # tightening
    return res


def check_pots(investment: Optional[float], trading: Optional[float], emergency: Optional[float], override_note: Optional[str], is_admin: bool,
               policy: Optional[dict] = None) -> dict:
    """Validate the capital partition. Returns {errors, warnings, overridden, trading_share, investable}."""
    p = policy or load_policy()
    cap = p["pots"]["trading_max_share_of_investable"]
    errors: list = []
    warnings: list = []
    for name, v in (("investment_capital", investment), ("trading_capital", trading), ("emergency_fund", emergency)):
        if v is not None and (v < 0 or v != v):
            errors.append(f"{name} must be zero or positive")
    inv, trd = investment or 0.0, trading or 0.0
    investable = inv + trd
    share = (trd / investable) if investable > 0 else 0.0
    overridden = False
    if investable > 0 and share > cap + 1e-12:
        if override_note and len(override_note.strip()) >= NOTE_MIN_CHARS and is_admin:
            overridden = True
            warnings.append(f"trading pot is {share:.1%} of investable capital, above the {cap:.0%} guard; allowed by an audited override")
        else:
            errors.append(f"trading pot is {share:.1%} of investable capital; the guard is {cap:.0%}. Lower it, or an admin may record an override note (min {NOTE_MIN_CHARS} characters)")
    if emergency is None:
        warnings.append("emergency fund not recorded: investor plans will state that the constraint is unknown")
    return {"errors": errors, "warnings": warnings, "overridden": overridden, "trading_share": share, "investable": investable, "cap": cap}


def approval_ok(note: Optional[str], is_admin: bool) -> bool:
    return bool(is_admin and note and len(note.strip()) >= NOTE_MIN_CHARS)


def pot_for_mode(mode: Optional[str]) -> str:
    if mode not in MODES:
        raise PolicyError(f"unknown mode '{mode}'")
    return "trading" if mode == "trader" else "investment"
