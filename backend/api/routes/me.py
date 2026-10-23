"""Trader / Investor mode: the signed-in user's profile, capital partition and trader limits.

GET is open to any signed-in user (viewers included, read-only). PUT needs admin or trader. A mode change touches only the mode;
capital and limit changes are validated against infra/data/mode_policy.json and written to the append-only mode_audit table.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from api.auth_deps import Principal, require_auth, require_role
from qsde.db.connection import get_sync_conn
from qsde.modes import policy as pol, profile as prof

router = APIRouter()
DISCLAIMER = ("Research and decision support, not investment advice. No engine is approved: trader ideas are RESEARCH or PAPER only, and no order can be "
              "placed from this product.")


class ProfilePut(BaseModel):
    mode: Optional[str] = None
    investment_capital: Optional[float] = Field(default=None, ge=0)
    trading_capital: Optional[float] = Field(default=None, ge=0)
    emergency_fund: Optional[float] = Field(default=None, ge=0)
    limits: Optional[dict[str, float]] = None
    income_stability: Optional[str] = None
    dependents: Optional[int] = Field(default=None, ge=0, le=20)
    drawdown_reaction: Optional[str] = None
    risk_profile: Optional[str] = None
    approval_note: Optional[str] = Field(default=None, max_length=500)
    trading_override_note: Optional[str] = Field(default=None, max_length=500)


class ProfileOut(BaseModel):
    user_id: str
    mode: str
    lens_pot: str
    risk_profile: Optional[str]
    income_stability: Optional[str] = None
    dependents: Optional[int] = None
    drawdown_reaction: Optional[str] = None
    investment_capital: Optional[float]
    trading_capital: Optional[float]
    emergency_fund: Optional[float]
    trading_share_of_investable: Optional[float]
    effective_limits: dict
    limit_defaults: dict
    limit_ceilings: dict
    loosened_limits: list[str]
    engine_approved: bool
    warnings: list[str]
    policy_version: str
    rules: dict
    disclaimer: str = DISCLAIMER


class AuditRow(BaseModel):
    ts: str
    action: str
    actor_role: Optional[str]
    payload: dict


def _view(user_id: str, row: dict, warnings: list) -> ProfileOut:
    policy = pol.load_policy()
    approved = pol.engine_approved()
    eff = pol.effective_limits(row["limits"], policy)
    inv, trd = row["investment_capital"] or 0.0, row["trading_capital"] or 0.0
    chk = pol.check_pots(row["investment_capital"], row["trading_capital"], row["emergency_fund"], None, False, policy)
    notes = list(warnings) + [w for w in chk["warnings"] if "override" not in w and w not in warnings]
    if chk["errors"]:                      # an earlier override-approved state stays visible
        notes.append("trading pot is above the guard (an audited override is on record)")
    return ProfileOut(user_id=user_id, mode=row["mode"], lens_pot=pol.pot_for_mode(row["mode"]), risk_profile=row["risk_profile"], income_stability=row.get("income_stability"),
                      dependents=row.get("dependents"), drawdown_reaction=row.get("drawdown_reaction"),
                      investment_capital=row["investment_capital"], trading_capital=row["trading_capital"], emergency_fund=row["emergency_fund"],
                      trading_share_of_investable=(chk["trading_share"] if inv + trd > 0 else None), effective_limits=eff, limit_defaults=pol.defaults(policy),
                      limit_ceilings={k: pol.ceiling_for(k, policy, approved) for k in policy["trader_limits"]},
                      loosened_limits=[k for k in eff if eff[k] > policy["trader_limits"][k]["default"]], engine_approved=approved, warnings=notes,
                      policy_version=policy["version"], rules={"trader": policy["trader_rules_fixed"], "pots": policy["pots"], "guardrails": policy["guardrails"]})


@router.get("/me/profile", response_model=ProfileOut)
def get_profile(p: Principal = Depends(require_auth)):
    with get_sync_conn() as c:
        row = prof.load(c, p.id)
    return _view(p.id, row, [])


@router.put("/me/profile", response_model=ProfileOut)
def put_profile(body: ProfilePut, p: Principal = Depends(require_role("admin", "trader"))):
    sent = body.model_fields_set
    u = prof.Update(mode=body.mode, investment_capital=body.investment_capital, trading_capital=body.trading_capital, emergency_fund=body.emergency_fund,
                    limits=body.limits, approval_note=body.approval_note, trading_override_note=body.trading_override_note,
                    income_stability=body.income_stability, dependents=body.dependents, drawdown_reaction=body.drawdown_reaction, risk_profile=body.risk_profile,
                    set_fields={f for f in prof.CAPITAL_FIELDS if f in sent})
    try:
        with get_sync_conn() as c:
            row, plan = prof.apply_update(c, p.id, p.role, u)
    except prof.ProfileError as e:
        raise HTTPException(status_code=e.status, detail=e.errors)
    return _view(p.id, row, plan.warnings)


@router.get("/me/audit", response_model=list[AuditRow])
def my_audit(limit: int = 50, p: Principal = Depends(require_auth)):
    with get_sync_conn() as c:
        cur = c.cursor()
        cur.execute("SELECT ts, action, actor_role, payload FROM mode_audit WHERE user_id = %s ORDER BY id DESC LIMIT %s", (p.id, max(1, min(limit, 200))))
        rows = cur.fetchall()
    return [AuditRow(ts=r["ts"].isoformat(), action=r["action"], actor_role=r["actor_role"], payload=r["payload"]) for r in rows]
