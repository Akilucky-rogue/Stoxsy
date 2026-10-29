"""User profile: mode, capital partition and trader limits. `plan_update` is pure (and unit-tested); `load`/`apply_update` touch the database.

Invariants this module guarantees:
  * a mode change touches ONLY the mode column (never a pot amount, never a holdings import);
  * capital and limit changes are validated against the policy, loosening needs an admin approval note, and every accepted change is
    written to the append-only mode_audit table in the same transaction.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import psycopg2.extras

from qsde.modes import policy as pol

CAPITAL_FIELDS = ("investment_capital", "trading_capital", "emergency_fund")


class ProfileError(Exception):
    def __init__(self, status: int, errors: list):
        super().__init__("; ".join(errors))
        self.status, self.errors = status, errors


@dataclass
class Update:
    mode: Optional[str] = None
    investment_capital: Optional[float] = None
    trading_capital: Optional[float] = None
    emergency_fund: Optional[float] = None
    limits: Optional[dict] = None
    approval_note: Optional[str] = None           # admin note to loosen a limit above its default
    trading_override_note: Optional[str] = None   # admin note to exceed the trading-pot share guard
    set_fields: set = field(default_factory=set)  # which capital fields were explicitly sent (so null can clear)
    income_stability: Optional[str] = None
    dependents: Optional[int] = None
    drawdown_reaction: Optional[str] = None
    risk_profile: Optional[str] = None            # an optional choice that may only LOWER the derived profile; checked by the plan, stored as given


@dataclass
class Plan:
    changes: dict = field(default_factory=dict)   # column -> new value
    audits: list = field(default_factory=list)    # (action, payload)
    errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)


def blank(policy_version: str) -> dict:
    return {"mode": None, "risk_profile": None, "capacity_score": None, "tolerance_score": None, "investment_capital": None,
            "trading_capital": None, "emergency_fund": None, "limits": {}, "policy_version": policy_version,
            "income_stability": None, "dependents": None, "drawdown_reaction": None}


def plan_update(current: dict, u: Update, is_admin: bool, policy: Optional[dict] = None, approved_engine: bool = False) -> Plan:
    p = policy or pol.load_policy()
    plan = Plan()
    if u.mode is not None:
        if u.mode not in pol.MODES:
            plan.errors.append(f"mode must be one of {', '.join(pol.MODES)}")
        elif u.mode != current["mode"]:
            plan.changes["mode"] = u.mode
            plan.audits.append(("mode.change", {"from": current["mode"], "to": u.mode}))
    sent = {f for f in CAPITAL_FIELDS if f in u.set_fields}
    if sent:
        merged = {f: (getattr(u, f) if f in sent else _num(current.get(f))) for f in CAPITAL_FIELDS}
        chk = pol.check_pots(merged["investment_capital"], merged["trading_capital"], merged["emergency_fund"], u.trading_override_note, is_admin, p)
        plan.errors += chk["errors"]
        plan.warnings += chk["warnings"]
        if not chk["errors"]:
            before = {f: _num(current.get(f)) for f in sent}
            after = {f: merged[f] for f in sent}
            if before != after:
                plan.changes.update(after)
                payload = {"before": before, "after": after, "trading_share": round(chk["trading_share"], 6)}
                if chk["overridden"]:
                    payload["override_note"] = u.trading_override_note.strip()
                plan.audits.append(("capital.change", payload))
    q = {}
    opts = p["investor"]["questionnaire"]
    if u.income_stability is not None:
        if u.income_stability not in opts["income_stability"]["options"]:
            plan.errors.append(f"income_stability must be one of {', '.join(opts['income_stability']['options'])}")
        elif u.income_stability != current.get("income_stability"):
            q["income_stability"] = u.income_stability
    if u.dependents is not None:
        if u.dependents < 0 or u.dependents > 20:
            plan.errors.append("dependents must be between 0 and 20")
        elif u.dependents != current.get("dependents"):
            q["dependents"] = u.dependents
    if u.drawdown_reaction is not None:
        if u.drawdown_reaction not in opts["drawdown_reaction"]["options"]:
            plan.errors.append(f"drawdown_reaction must be one of {', '.join(opts['drawdown_reaction']['options'])}")
        elif u.drawdown_reaction != current.get("drawdown_reaction"):
            q["drawdown_reaction"] = u.drawdown_reaction
    if u.risk_profile is not None:
        if u.risk_profile not in p["investor"]["risk_profiles"]:
            plan.errors.append(f"risk_profile must be one of {', '.join(p['investor']['risk_profiles'])}")
        elif u.risk_profile != current.get("risk_profile"):
            q["risk_profile"] = u.risk_profile
    if q:
        plan.changes.update(q)
        plan.audits.append(("questionnaire.change", {"before": {k: current.get(k) for k in q}, "after": q}))
    if u.limits:
        chk = pol.check_limit_changes(u.limits, current.get("limits"), p, approved_engine)
        plan.errors += chk.errors
        if not chk.errors:
            if chk.needs_approval and not pol.approval_ok(u.approval_note, is_admin):
                plan.errors.append("loosening " + ", ".join(chk.needs_approval) + " above the default needs an admin approval note (min "
                                   f"{pol.NOTE_MIN_CHARS} characters)")
            elif chk.new != (current.get("limits") or {}):
                plan.changes["limits"] = chk.new
                payload = {"before": current.get("limits") or {}, "after": chk.new}
                if chk.needs_approval:
                    payload["approved"] = chk.needs_approval
                    payload["approval_note"] = u.approval_note.strip()
                plan.audits.append(("limits.change", payload))
    return plan


def _num(x):
    return None if x is None else float(x)


def _row_to_profile(row: Optional[dict], policy_version: str) -> dict:
    if row is None:
        return blank(policy_version)
    d = dict(row)
    for f in CAPITAL_FIELDS + ("capacity_score", "tolerance_score"):
        d[f] = _num(d.get(f))
    d["limits"] = d.get("limits") or {}
    return d


def load(conn, user_id: str, policy_version: Optional[str] = None) -> dict:
    cur = conn.cursor()
    cur.execute("SELECT * FROM user_profiles WHERE user_id = %s", (user_id,))
    return _row_to_profile(cur.fetchone(), policy_version or pol.load_policy()["version"])


def record(conn, user_id: str, role: str, action: str, payload: dict) -> None:
    conn.cursor().execute("INSERT INTO mode_audit (user_id, actor_role, action, payload) VALUES (%s,%s,%s,%s)", (user_id, role, action, psycopg2.extras.Json(payload)))


def apply_update(conn, user_id: str, role: str, u: Update) -> tuple[dict, Plan]:
    """Validate and persist in one transaction (the caller's connection). Raises ProfileError(422) with every problem found."""
    policy = pol.load_policy()
    current = load(conn, user_id, policy["version"])
    plan = plan_update(current, u, role == "admin", policy, pol.engine_approved())
    if plan.errors:
        raise ProfileError(422, plan.errors)
    if plan.changes:
        cols = list(plan.changes)
        vals = [psycopg2.extras.Json(plan.changes[c]) if c == "limits" else plan.changes[c] for c in cols]
        conn.cursor().execute(
            f"INSERT INTO user_profiles (user_id, policy_version, {', '.join(cols)}) VALUES (%s, %s, {', '.join(['%s'] * len(cols))}) "
            f"ON CONFLICT (user_id) DO UPDATE SET {', '.join(f'{c} = EXCLUDED.{c}' for c in cols)}, policy_version = EXCLUDED.policy_version, updated_at = NOW()",
            [user_id, policy["version"], *vals])
    for action, payload in plan.audits:
        record(conn, user_id, role, action, payload)
    return load(conn, user_id, policy["version"]), plan
