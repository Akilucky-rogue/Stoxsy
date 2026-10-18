"""Accounts API: /api/auth/* (login, refresh, logout, change-password, me) and /api/admin/* (users, audit).

Access token: JWT in the response body, held in memory by the SPA. Refresh token: opaque, httpOnly + SameSite=Strict cookie scoped to
/api/auth, rotated on every use. /auth/refresh and /auth/logout also require the custom header `X-Stoxsy-CSRF: 1` (a cross-site form
cannot set it).
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from api.auth_deps import Principal, principal_dict, require_auth, require_role
from qsde.auth import service
from qsde.config.settings import settings

router = APIRouter()
COOKIE = "stoxsy_refresh"


class LoginIn(BaseModel):
    email: str
    password: str


class ChangePasswordIn(BaseModel):
    old_password: str
    new_password: str


class NewUserIn(BaseModel):
    email: str
    name: str = ""
    role: str = Field(pattern="^(admin|trader|viewer)$")
    temp_password: str


class UpdateUserIn(BaseModel):
    role: Optional[str] = None
    status: Optional[str] = None


class ResetIn(BaseModel):
    temp_password: str


def _ip(request: Request) -> Optional[str]:
    return request.client.host if request.client else None


def _csrf(request: Request) -> None:
    if request.headers.get("x-stoxsy-csrf") != "1":
        raise HTTPException(status_code=403, detail="missing CSRF header")


def _set_cookie(resp: Response, raw: str) -> None:
    resp.set_cookie(COOKIE, raw, max_age=settings.refresh_token_days * 86400, httponly=True, samesite="strict",
                    secure=settings.app_env != "local", path="/api/auth")


def _session_body(s: dict) -> dict:
    return {"access_token": s["access_token"], "token_type": "bearer", "expires_in": s["expires_in"], "user": s["user"],
            "must_change_password": s["user"]["must_change_password"]}


def _guard(fn):
    try:
        return fn()
    except service.AuthError as e:
        raise HTTPException(status_code=e.status, detail=str(e))


@router.post("/auth/login")
def login(body: LoginIn, request: Request, response: Response):
    s = _guard(lambda: service.authenticate(body.email, body.password, _ip(request), request.headers.get("user-agent")))
    _set_cookie(response, s["refresh_token"])
    return _session_body(s)


@router.post("/auth/refresh")
def refresh(request: Request, response: Response):
    _csrf(request)
    raw = request.cookies.get(COOKIE)
    if not raw:
        raise HTTPException(status_code=401, detail="no session")
    s = _guard(lambda: service.refresh(raw, _ip(request), request.headers.get("user-agent")))
    _set_cookie(response, s["refresh_token"])
    return _session_body(s)


@router.post("/auth/logout")
def logout(request: Request, response: Response):
    _csrf(request)
    service.logout(request.cookies.get(COOKIE), _ip(request))
    response.delete_cookie(COOKIE, path="/api/auth")
    return {"ok": True}


@router.get("/auth/me")
def me(p: Principal = Depends(require_auth)):
    return {**principal_dict(p), "authenticated": p.authenticated, "must_change_password": p.must_change,
            "auth_enforced": settings.auth_enforced}


@router.post("/auth/change-password")
def change_password(body: ChangePasswordIn, request: Request, response: Response, p: Principal = Depends(require_auth)):
    if not p.authenticated:
        raise HTTPException(status_code=400, detail="sign in with an account first")
    s = _guard(lambda: service.change_password(p.id, body.old_password, body.new_password, _ip(request)))
    _set_cookie(response, s["refresh_token"])
    return _session_body(s)


# ── admin ────────────────────────────────────────────────────────────

@router.get("/admin/users")
def admin_list_users(p: Principal = Depends(require_role("admin"))):
    return [{k: (str(v) if k == "id" else v) for k, v in u.items()} for u in service.list_users()]


@router.post("/admin/users", status_code=201)
def admin_create_user(body: NewUserIn, request: Request, p: Principal = Depends(require_role("admin"))):
    u = _guard(lambda: service.create_user(body.email, body.name, body.temp_password, body.role, must_change=True,
                                           actor=principal_dict(p), ip=_ip(request)))
    return service.public_user(u)


@router.patch("/admin/users/{user_id}")
def admin_update_user(user_id: str, body: UpdateUserIn, request: Request, p: Principal = Depends(require_role("admin"))):
    u = _guard(lambda: service.update_user(user_id, principal_dict(p), body.role, body.status, _ip(request)))
    return service.public_user(u)


@router.post("/admin/users/{user_id}/reset")
def admin_reset(user_id: str, body: ResetIn, request: Request, p: Principal = Depends(require_role("admin"))):
    _guard(lambda: service.admin_reset_password(user_id, body.temp_password, principal_dict(p), _ip(request)))
    return {"ok": True, "must_change_password": True}


@router.get("/admin/audit")
def admin_audit(limit: int = 200, p: Principal = Depends(require_role("admin"))):
    rows = service.recent_audit(min(max(limit, 1), 1000))
    return [{**r, "ts": r["ts"].isoformat(), "user_id": None if r["user_id"] is None else str(r["user_id"])} for r in rows]
