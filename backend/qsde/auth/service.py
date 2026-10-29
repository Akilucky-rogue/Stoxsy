"""Accounts, sessions and the audit log (PostgreSQL, migration 038).

Behaviour enforced here:
  * login throttling: >= 5 failed attempts for an email OR an ip in 15 minutes -> refused (and the refusal is audited); a user is also locked
    with exponential back-off after repeated failures; messages never say whether the email exists
  * refresh tokens rotate on every use; presenting an already-rotated token revokes the whole family (theft signal)
  * changing a password revokes every session; a disabled user's tokens stop working at the next refresh
  * every sign-in, failure, refresh reuse, password change and admin action is written to audit_log
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Optional

import psycopg2.extras

from qsde.auth import security as sec
from qsde.config.settings import settings
from qsde.db.connection import get_sync_conn

THROTTLE_WINDOW_MIN = 15
THROTTLE_MAX_FAILS = 5
GENERIC_FAIL = "invalid email or password"


class AuthError(Exception):
    def __init__(self, message: str, status: int = 401):
        super().__init__(message)
        self.status = status


def _now() -> datetime:
    return datetime.now(timezone.utc)


def secret() -> str:
    s = settings.jwt_secret
    if len(s) < 32:
        raise AuthError("server is not configured for authentication (JWT_SECRET missing or shorter than 32 characters)", 503)
    return s


def audit(action: str, user_id=None, email: Optional[str] = None, ok: bool = True, ip: Optional[str] = None, entity_type=None,
          entity_id=None, payload: Optional[dict] = None, conn=None) -> None:
    sql = ("INSERT INTO audit_log (user_id, email, action, entity_type, entity_id, ok, ip, payload) "
           "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)")
    args = (user_id, email, action, entity_type, None if entity_id is None else str(entity_id), ok, ip,
            None if payload is None else psycopg2.extras.Json(payload))
    if conn is not None:
        conn.cursor().execute(sql, args)
        return
    with get_sync_conn() as c:
        c.cursor().execute(sql, args)


def _one(sql: str, args=()) -> Optional[dict]:
    with get_sync_conn() as c:
        cur = c.cursor()
        cur.execute(sql, args)
        return cur.fetchone()


def _all(sql: str, args=()) -> list:
    with get_sync_conn() as c:
        cur = c.cursor()
        cur.execute(sql, args)
        return cur.fetchall()


def public_user(u: dict) -> dict:
    return {"id": str(u["id"]), "email": u["email"], "name": u["name"], "role": u["role"], "status": u["status"],
            "must_change_password": u["must_change_pw"]}


# ── users ────────────────────────────────────────────────────────────

def create_user(email: str, name: str, password: str, role: str, must_change: bool = True, actor: Optional[dict] = None,
                ip: Optional[str] = None) -> dict:
    email = email.strip().lower()
    if "@" not in email or len(email) > 254:
        raise AuthError("invalid email", 422)
    if role not in sec.ROLES:
        raise AuthError(f"role must be one of {sec.ROLES}", 422)
    try:
        sec.validate_password(password, email)
    except sec.PasswordPolicyError as e:
        raise AuthError(str(e), 422) from e
    with get_sync_conn() as c:
        cur = c.cursor()
        try:
            cur.execute("INSERT INTO users (email, name, password_hash, role, must_change_pw) VALUES (%s,%s,%s,%s,%s) RETURNING *",
                        (email, name.strip(), sec.hash_password(password), role, must_change))
        except psycopg2.errors.UniqueViolation as e:
            raise AuthError("a user with this email already exists", 409) from e
        u = cur.fetchone()
        audit("user_created", actor["id"] if actor else None, actor["email"] if actor else email, True, ip, "user", u["id"],
              {"email": email, "role": role}, conn=c)
    return u


def get_user(user_id: str) -> Optional[dict]:
    return _one("SELECT * FROM users WHERE id = %s", (user_id,))


def list_users() -> list:
    return _all("SELECT id, email, name, role, status, must_change_pw, last_login_at, created_at FROM users ORDER BY created_at")


def update_user(user_id: str, actor: dict, role: Optional[str] = None, status: Optional[str] = None, ip: Optional[str] = None) -> dict:
    sets, args = [], []
    if role is not None:
        if role not in sec.ROLES:
            raise AuthError(f"role must be one of {sec.ROLES}", 422)
        sets.append("role = %s")
        args.append(role)
    if status is not None:
        if status not in ("active", "disabled"):
            raise AuthError("status must be active or disabled", 422)
        sets.append("status = %s")
        args.append(status)
    if not sets:
        raise AuthError("nothing to update", 422)
    if str(actor["id"]) == str(user_id) and (role not in (None, "admin") or status == "disabled"):
        raise AuthError("you cannot demote or disable your own account", 409)
    with get_sync_conn() as c:
        cur = c.cursor()
        cur.execute(f"UPDATE users SET {', '.join(sets)} WHERE id = %s RETURNING *", (*args, user_id))
        u = cur.fetchone()
        if not u:
            raise AuthError("user not found", 404)
        if status == "disabled":
            cur.execute("UPDATE refresh_tokens SET revoked_at = NOW() WHERE user_id = %s AND revoked_at IS NULL", (user_id,))
        audit("user_updated", actor["id"], actor["email"], True, ip, "user", user_id, {"role": role, "status": status}, conn=c)
    return u


# ── login / sessions ─────────────────────────────────────────────────

def _throttled(email: str, ip: Optional[str]) -> bool:
    since = _now() - timedelta(minutes=THROTTLE_WINDOW_MIN)
    r = _one("""SELECT (SELECT count(*) FROM audit_log WHERE action = 'login_failed' AND email = %s AND ts > %s) AS by_email,
                       (SELECT count(*) FROM audit_log WHERE action = 'login_failed' AND ip = %s AND ts > %s) AS by_ip""",
             (email, since, ip, since))
    return r["by_email"] >= THROTTLE_MAX_FAILS or (ip is not None and r["by_ip"] >= THROTTLE_MAX_FAILS * 4)


def authenticate(email: str, password: str, ip: Optional[str] = None, user_agent: Optional[str] = None) -> dict:
    email = email.strip().lower()
    if _throttled(email, ip):
        audit("login_throttled", None, email, False, ip)
        raise AuthError("too many failed attempts; try again later", 429)
    u = _one("SELECT * FROM users WHERE email = %s", (email,))
    ok = sec.verify_password(password, u["password_hash"] if u else None)
    locked = bool(u and u["locked_until"] and u["locked_until"] > _now())
    if not ok or not u or u["status"] != "active" or locked:
        with get_sync_conn() as c:
            cur = c.cursor()
            if u:
                fails = u["failed_logins"] + 1
                lock = _now() + timedelta(minutes=min(2 ** (fails - THROTTLE_MAX_FAILS), 240)) if fails >= THROTTLE_MAX_FAILS else None
                cur.execute("UPDATE users SET failed_logins = %s, locked_until = COALESCE(%s, locked_until) WHERE id = %s", (fails, lock, u["id"]))
            audit("login_failed", u["id"] if u else None, email, False, ip, payload={"disabled": bool(u and u["status"] != "active"),
                                                                                       "locked": locked}, conn=c)
        raise AuthError(GENERIC_FAIL, 401)
    with get_sync_conn() as c:
        cur = c.cursor()
        cur.execute("UPDATE users SET failed_logins = 0, locked_until = NULL, last_login_at = NOW() WHERE id = %s", (u["id"],))
        if sec.needs_rehash(u["password_hash"]):
            cur.execute("UPDATE users SET password_hash = %s WHERE id = %s", (sec.hash_password(password), u["id"]))
        audit("login", u["id"], email, True, ip, conn=c)
    return issue_session(u, ip, user_agent)


def issue_session(u: dict, ip: Optional[str], user_agent: Optional[str], family_id: Optional[str] = None) -> dict:
    raw, h = sec.new_refresh_token()
    with get_sync_conn() as c:
        cur = c.cursor()
        if family_id is None:
            cur.execute("SELECT gen_random_uuid() AS f")
            family_id = str(cur.fetchone()["f"])
        cur.execute("INSERT INTO refresh_tokens (user_id, family_id, token_hash, expires_at, ip, user_agent) VALUES (%s,%s,%s,%s,%s,%s) RETURNING id",
                    (u["id"], family_id, h, _now() + timedelta(days=settings.refresh_token_days), ip, (user_agent or "")[:200]))
        rid = cur.fetchone()["id"]
    access = sec.make_access_token(str(u["id"]), u["role"], family_id, secret(), settings.access_token_minutes, bool(u["must_change_pw"]))
    return {"access_token": access, "expires_in": settings.access_token_minutes * 60, "refresh_token": raw, "refresh_id": str(rid),
            "family_id": family_id, "user": public_user(u)}


def refresh(raw_token: str, ip: Optional[str] = None, user_agent: Optional[str] = None) -> dict:
    h = sec.hash_refresh(raw_token)
    with get_sync_conn() as c:
        cur = c.cursor()
        cur.execute("SELECT * FROM refresh_tokens WHERE token_hash = %s FOR UPDATE", (h,))
        t = cur.fetchone()
        if not t:
            raise AuthError("invalid session", 401)
        if t["revoked_at"] is not None:                       # a rotated/revoked token came back: assume theft, kill the family
            cur.execute("UPDATE refresh_tokens SET revoked_at = COALESCE(revoked_at, NOW()) WHERE family_id = %s", (t["family_id"],))
            audit("refresh_reuse", t["user_id"], None, False, ip, "refresh_family", t["family_id"], conn=c)
            c.commit()
            raise AuthError("session revoked", 401)
        if t["expires_at"] < _now():
            raise AuthError("session expired", 401)
        cur.execute("SELECT * FROM users WHERE id = %s", (t["user_id"],))
        u = cur.fetchone()
        if not u or u["status"] != "active":
            cur.execute("UPDATE refresh_tokens SET revoked_at = NOW() WHERE family_id = %s AND revoked_at IS NULL", (t["family_id"],))
            raise AuthError("session revoked", 401)
        cur.execute("UPDATE refresh_tokens SET revoked_at = NOW() WHERE id = %s", (t["id"],))
    s = issue_session(u, ip, user_agent, family_id=str(t["family_id"]))
    with get_sync_conn() as c:
        c.cursor().execute("UPDATE refresh_tokens SET replaced_by = %s WHERE id = %s", (s["refresh_id"], t["id"]))
    return s


def logout(raw_token: Optional[str], ip: Optional[str] = None) -> None:
    if not raw_token:
        return
    h = sec.hash_refresh(raw_token)
    t = _one("SELECT user_id, family_id FROM refresh_tokens WHERE token_hash = %s", (h,))
    if t:
        with get_sync_conn() as c:
            c.cursor().execute("UPDATE refresh_tokens SET revoked_at = COALESCE(revoked_at, NOW()) WHERE family_id = %s", (t["family_id"],))
            audit("logout", t["user_id"], None, True, ip, conn=c)


def family_active(family_id: str) -> bool:
    r = _one("SELECT 1 AS x FROM refresh_tokens WHERE family_id = %s AND revoked_at IS NULL AND expires_at > NOW() LIMIT 1", (family_id,))
    return bool(r)


def change_password(user_id: str, old: str, new: str, ip: Optional[str] = None) -> dict:
    u = get_user(user_id)
    if not u or not sec.verify_password(old, u["password_hash"]):
        audit("password_change_failed", user_id, u["email"] if u else None, False, ip)
        raise AuthError("current password is incorrect", 403)
    try:
        sec.validate_password(new, u["email"])
    except sec.PasswordPolicyError as e:
        raise AuthError(str(e), 422) from e
    if sec.verify_password(new, u["password_hash"]):
        raise AuthError("the new password must differ from the current one", 422)
    with get_sync_conn() as c:
        cur = c.cursor()
        cur.execute("UPDATE users SET password_hash = %s, must_change_pw = FALSE WHERE id = %s RETURNING *", (sec.hash_password(new), user_id))
        u = cur.fetchone()
        cur.execute("UPDATE refresh_tokens SET revoked_at = NOW() WHERE user_id = %s AND revoked_at IS NULL", (user_id,))
        audit("password_changed", user_id, u["email"], True, ip, conn=c)
    return issue_session(u, ip, None)


def admin_reset_password(user_id: str, temp_password: str, actor: dict, ip: Optional[str] = None) -> None:
    u = get_user(user_id)
    if not u:
        raise AuthError("user not found", 404)
    try:
        sec.validate_password(temp_password, u["email"])
    except sec.PasswordPolicyError as e:
        raise AuthError(str(e), 422) from e
    with get_sync_conn() as c:
        cur = c.cursor()
        cur.execute("UPDATE users SET password_hash = %s, must_change_pw = TRUE, failed_logins = 0, locked_until = NULL WHERE id = %s",
                    (sec.hash_password(temp_password), user_id))
        cur.execute("UPDATE refresh_tokens SET revoked_at = NOW() WHERE user_id = %s AND revoked_at IS NULL", (user_id,))
        audit("password_reset_by_admin", actor["id"], actor["email"], True, ip, "user", user_id, conn=c)


def recent_audit(limit: int = 200) -> list:
    return _all("SELECT id, ts, user_id, email, action, entity_type, entity_id, ok, ip, payload FROM audit_log ORDER BY ts DESC LIMIT %s", (limit,))
