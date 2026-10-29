"""FastAPI dependencies for authentication and role checks.

`require_auth` is installed APPLICATION-WIDE (api/main.py), so a new route is protected by default; the public allow-list below is the
only way out and `tests/test_auth_api.py` pins it. With AUTH_ENFORCED=false (default, local development) a request without a token
passes as the local owner, exactly as the API behaved before accounts existed; a request WITH a token is always validated.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from fastapi import Depends, HTTPException, Request

from qsde.auth import security as sec
from qsde.auth import service
from qsde.config.settings import settings

PUBLIC_PATHS = frozenset({"/", "/api/health", "/api/kite/callback", "/api/auth/login", "/api/auth/refresh", "/api/auth/logout"})
DOC_PATHS = frozenset({"/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"})
PASSWORD_CHANGE_ALLOWED = frozenset({"/api/auth/change-password", "/api/auth/me", "/api/auth/logout"})


@dataclass(frozen=True)
class Principal:
    id: str
    email: str
    role: str
    must_change: bool = False
    authenticated: bool = False


LOCAL_OWNER = Principal(id="local", email="local@dev", role="admin")


def _unauth(detail: str = "not authenticated") -> HTTPException:
    return HTTPException(status_code=401, detail=detail, headers={"WWW-Authenticate": "Bearer"})


def _bearer(request: Request) -> Optional[str]:
    h = request.headers.get("authorization", "")
    return h[7:].strip() if h.lower().startswith("bearer ") else None


def require_auth(request: Request) -> Principal:
    path = request.url.path
    if path in PUBLIC_PATHS or (path in DOC_PATHS and settings.app_env == "local"):
        return LOCAL_OWNER
    token = _bearer(request)
    if token is None:
        if settings.auth_enforced:
            raise _unauth()
        return LOCAL_OWNER
    try:
        claims = sec.decode_access_token(token, service.secret())
    except sec.TokenError:
        raise _unauth("invalid or expired token")
    except service.AuthError as e:
        raise HTTPException(status_code=e.status, detail=str(e))
    try:
        if claims.sid and not service.family_active(claims.sid):
            raise _unauth("session ended")
        u = service.get_user(claims.sub)
    except HTTPException:
        raise
    except Exception:                                      # database unreachable: fail closed, say why
        raise HTTPException(status_code=503, detail="authentication backend unavailable")
    if not u or u["status"] != "active":
        raise _unauth("account disabled")
    if u["must_change_pw"] and path not in PASSWORD_CHANGE_ALLOWED:
        raise HTTPException(status_code=403, detail="password change required")
    return Principal(id=str(u["id"]), email=u["email"], role=u["role"], must_change=bool(u["must_change_pw"]), authenticated=True)


def require_role(*roles: str):
    """Route dependency: the principal must hold one of `roles`. Unenforced local mode counts as the owner (admin)."""
    def dep(p: Principal = Depends(require_auth)) -> Principal:
        if p.role not in roles:
            raise HTTPException(status_code=403, detail=f"requires role: {' or '.join(roles)}")
        return p
    return dep


def principal_dict(p: Principal) -> dict:
    return {"id": p.id, "email": p.email, "role": p.role}
