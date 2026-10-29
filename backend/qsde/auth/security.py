"""Auth primitives: argon2id passwords, password policy, short-lived JWT access tokens, opaque refresh tokens. No database here."""
from __future__ import annotations

import hashlib
import secrets
import time
from dataclasses import dataclass
from typing import Optional

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

ISSUER = "stoxsy"
ROLES = ("admin", "trader", "viewer")
MIN_PASSWORD_LEN = 12
_COMMON = {"password1234", "passwordpassword", "123456789012", "qwertyuiop12", "letmein12345", "administrator", "stoxsy123456", "changeme1234"}

# argon2id: time 3, memory 64 MiB, parallelism 2 (the platform's stated parameters)
_ph = PasswordHasher(time_cost=3, memory_cost=64 * 1024, parallelism=2)
_DUMMY = _ph.hash("dummy-password-for-constant-time-verification")


class PasswordPolicyError(ValueError):
    pass


class TokenError(Exception):
    """Invalid, expired or wrongly-typed access token."""


def validate_password(password: str, email: str = "") -> None:
    if len(password) < MIN_PASSWORD_LEN:
        raise PasswordPolicyError(f"password must be at least {MIN_PASSWORD_LEN} characters")
    low = password.lower()
    local = email.split("@")[0].lower() if email else ""
    if local and len(local) >= 3 and local in low:
        raise PasswordPolicyError("password must not contain your email name")
    if low in _COMMON or len(set(low)) < 5:
        raise PasswordPolicyError("password is too common or too repetitive")
    classes = sum([any(c.islower() for c in password), any(c.isupper() for c in password),
                   any(c.isdigit() for c in password), any(not c.isalnum() for c in password)])
    if classes < 2 and len(password) < 20:
        raise PasswordPolicyError("use at least two character classes (or a passphrase of 20+ characters)")


def hash_password(password: str) -> str:
    return _ph.hash(password)


def verify_password(password: str, stored: Optional[str]) -> bool:
    """Constant-work verification: an unknown user is checked against a dummy hash so timing does not reveal which emails exist."""
    try:
        _ph.verify(stored or _DUMMY, password)
        return stored is not None
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(stored: str) -> bool:
    return _ph.check_needs_rehash(stored)


# ── tokens ───────────────────────────────────────────────────────────

@dataclass(frozen=True)
class AccessClaims:
    sub: str
    role: str
    sid: str                   # refresh-token family: logging out revokes it
    must_change: bool
    exp: int


def make_access_token(user_id: str, role: str, family_id: str, secret: str, minutes: int = 15, must_change: bool = False,
                      now: Optional[float] = None) -> str:
    t = int(now if now is not None else time.time())
    return jwt.encode({"iss": ISSUER, "sub": str(user_id), "role": role, "sid": str(family_id), "mcp": bool(must_change),
                       "jti": secrets.token_hex(8), "iat": t, "exp": t + minutes * 60}, secret, algorithm="HS256")


def decode_access_token(token: str, secret: str, now: Optional[float] = None) -> AccessClaims:
    try:
        d = jwt.decode(token, secret, algorithms=["HS256"], issuer=ISSUER, options={"require": ["exp", "iat", "sub", "role"]},
                       leeway=0)
    except jwt.PyJWTError as e:
        raise TokenError(str(e)) from e
    if d["role"] not in ROLES:
        raise TokenError("unknown role")
    return AccessClaims(sub=d["sub"], role=d["role"], sid=d.get("sid", ""), must_change=bool(d.get("mcp", False)), exp=int(d["exp"]))


def new_refresh_token() -> tuple[str, str]:
    """(opaque token for the cookie, sha256 hex to store). The raw token is never persisted."""
    raw = secrets.token_urlsafe(48)
    return raw, hash_refresh(raw)


def hash_refresh(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()
