"""Accounts, sessions, roles, throttling and the protect-everything-by-default guarantee (real DB; test users are removed afterwards)."""
import time
import uuid

import pytest
from fastapi.testclient import TestClient

from qsde.auth import security as sec
from qsde.auth import service
from qsde.config.settings import settings
from qsde.db.connection import execute_sql

SECRET = "x" * 40
PW = "correct horse battery 9"


# ── primitives ───────────────────────────────────────────────────────

class TestSecurity:
    def test_password_policy(self):
        for bad in ("short1A!", "aaaaaaaaaaaaaa", "alice-secret-pass", "passwordpassword"):
            with pytest.raises(sec.PasswordPolicyError):
                sec.validate_password(bad, "alice@example.com")
        sec.validate_password(PW, "alice@example.com")
        sec.validate_password("a long passphrase with only lowercase letters", "alice@example.com")

    def test_hash_verify_and_unknown_user_does_not_match(self):
        h = sec.hash_password(PW)
        assert h.startswith("$argon2id$") and sec.verify_password(PW, h) and not sec.verify_password("nope" * 5, h)
        assert sec.verify_password(PW, None) is False

    def test_jwt_roundtrip_expiry_tamper_and_wrong_secret(self):
        t = sec.make_access_token("u1", "viewer", "fam", SECRET, minutes=1)
        c = sec.decode_access_token(t, SECRET)
        assert (c.sub, c.role, c.sid) == ("u1", "viewer", "fam")
        with pytest.raises(sec.TokenError):
            sec.decode_access_token(t, "y" * 40)
        with pytest.raises(sec.TokenError):
            sec.decode_access_token(sec.make_access_token("u1", "viewer", "fam", SECRET, now=time.time() - 3600), SECRET)
        with pytest.raises(sec.TokenError):
            sec.decode_access_token(t[:-3] + ("AAA" if not t.endswith("AAA") else "BBB"), SECRET)
        with pytest.raises(sec.TokenError):
            sec.decode_access_token(sec.make_access_token("u1", "root", "fam", SECRET), SECRET)       # unknown role

    def test_refresh_tokens_are_stored_hashed_and_unique(self):
        a, ha = sec.new_refresh_token()
        b, hb = sec.new_refresh_token()
        assert a != b and ha != hb and ha == sec.hash_refresh(a) and a not in ha


# ── API ──────────────────────────────────────────────────────────────

@pytest.fixture()
def env(monkeypatch):
    from api.main import app
    monkeypatch.setattr(settings, "jwt_secret", SECRET)
    monkeypatch.setattr(settings, "auth_enforced", True)
    tag = "t" + uuid.uuid4().hex[:8]
    made = []

    def make(role="viewer", must_change=False, pw=PW):
        email = f"test-auth-{tag}-{len(made)}@example.com"
        u = service.create_user(email, "Test", pw, role, must_change=must_change)
        made.append(email)
        return email, u

    client = TestClient(app, base_url="http://127.0.0.1", client=("10.9.%d.%d" % (uuid.uuid4().int % 250, uuid.uuid4().int % 250), 5000))
    yield client, make, tag
    for e in made:
        execute_sql("DELETE FROM audit_log WHERE email = %s", (e,))
        execute_sql("DELETE FROM users WHERE email = %s", (e,))
    execute_sql("DELETE FROM audit_log WHERE email LIKE %s", (f"test-auth-{tag}%",))


def login(client, email, pw=PW):
    return client.post("/api/auth/login", json={"email": email, "password": pw})


def hdr(r):
    return {"Authorization": "Bearer " + r.json()["access_token"]}


class TestProtectedByDefault:
    def test_every_non_public_route_rejects_an_anonymous_request_when_enforced(self, env):
        from api.auth_deps import PUBLIC_PATHS
        from api.main import app
        client, _, _ = env
        checked = 0
        for path, item in app.openapi()["paths"].items():
            if path in PUBLIC_PATHS:
                continue
            for method in item:
                if method not in ("get", "post", "put", "patch", "delete"):
                    continue
                url = path.replace("{", "").replace("}", "") if "{" not in path else __import__("re").sub(r"\{[^}]+\}", "x", path)
                r = client.request(method.upper(), url)
                assert r.status_code == 401, f"{method.upper()} {path} answered {r.status_code} without a token"
                checked += 1
        assert checked > 50                                                          # the whole API surface was exercised

    def test_public_paths_are_exactly_the_documented_ones(self):
        from api.auth_deps import PUBLIC_PATHS
        assert PUBLIC_PATHS == {"/", "/api/health", "/api/kite/callback", "/api/auth/login", "/api/auth/refresh", "/api/auth/logout"}

    def test_unenforced_mode_keeps_the_local_api_open(self, env, monkeypatch):
        client, _, _ = env
        monkeypatch.setattr(settings, "auth_enforced", False)
        r = client.get("/api/auth/me")
        assert r.status_code == 200 and r.json()["authenticated"] is False and r.json()["auth_enforced"] is False

    def test_a_bad_token_is_rejected_even_when_unenforced(self, env, monkeypatch):
        client, _, _ = env
        monkeypatch.setattr(settings, "auth_enforced", False)
        assert client.get("/api/auth/me", headers={"Authorization": "Bearer nonsense"}).status_code == 401


class TestSessions:
    def test_login_me_and_generic_failure_message(self, env):
        client, make, _ = env
        email, _u = make()
        r = login(client, email)
        assert r.status_code == 200 and r.json()["user"]["role"] == "viewer" and "stoxsy_refresh" in r.cookies
        assert "httponly" in r.headers["set-cookie"].lower() and "samesite=strict" in r.headers["set-cookie"].lower()
        me = client.get("/api/auth/me", headers=hdr(r))
        assert me.status_code == 200 and me.json()["email"] == email and me.json()["authenticated"] is True
        bad_pw, no_user = login(client, email, "wrong password 123"), login(client, "nobody-here@example.com", PW)
        assert bad_pw.status_code == no_user.status_code == 401 and bad_pw.json() == no_user.json()      # no user enumeration

    def test_refresh_rotates_and_a_reused_token_kills_the_family(self, env):
        client, make, _ = env
        email, _ = make()
        r1 = login(client, email)
        old = client.cookies.get("stoxsy_refresh")
        assert client.post("/api/auth/refresh").status_code == 403                    # CSRF header required
        r2 = client.post("/api/auth/refresh", headers={"X-Stoxsy-CSRF": "1"})
        assert r2.status_code == 200 and client.cookies.get("stoxsy_refresh") != old
        assert client.get("/api/auth/me", headers=hdr(r2)).status_code == 200
        client.cookies.set("stoxsy_refresh", old, path="/api/auth")                  # an attacker replays the rotated token
        r3 = client.post("/api/auth/refresh", headers={"X-Stoxsy-CSRF": "1"})
        assert r3.status_code == 401
        assert client.get("/api/auth/me", headers=hdr(r2)).status_code == 401         # the whole family is dead, including the newest token

    def test_logout_ends_the_session(self, env):
        client, make, _ = env
        email, _ = make()
        r = login(client, email)
        assert client.post("/api/auth/logout", headers={"X-Stoxsy-CSRF": "1"}).status_code == 200
        assert client.get("/api/auth/me", headers=hdr(r)).status_code == 401

    def test_throttle_blocks_after_five_failures_even_with_the_right_password(self, env):
        client, make, _ = env
        email, _ = make()
        for _ in range(5):
            assert login(client, email, "wrong password 123").status_code == 401
        assert login(client, email).status_code == 429

    def test_first_login_forces_a_password_change(self, env):
        client, make, _ = env
        email, _ = make(must_change=True)
        r = login(client, email)
        assert r.json()["must_change_password"] is True
        assert client.get("/api/scorecard/ideas", headers=hdr(r)).status_code == 403
        assert client.get("/api/auth/me", headers=hdr(r)).status_code == 200
        new_pw = "a brand new passphrase 77"
        assert client.post("/api/auth/change-password", headers=hdr(r), json={"old_password": "wrong password 123", "new_password": new_pw}).status_code == 403
        assert client.post("/api/auth/change-password", headers=hdr(r), json={"old_password": PW, "new_password": PW}).status_code == 422
        r2 = client.post("/api/auth/change-password", headers=hdr(r), json={"old_password": PW, "new_password": new_pw})
        assert r2.status_code == 200 and r2.json()["must_change_password"] is False
        assert client.get("/api/auth/me", headers=hdr(r)).status_code == 401           # old sessions were revoked
        assert login(client, email, PW).status_code == 401 and login(client, email, new_pw).status_code == 200


class TestRolesAndAdmin:
    def test_viewer_cannot_use_admin_endpoints_admin_can_and_actions_are_audited(self, env):
        client, make, _ = env
        v_email, _ = make("viewer")
        a_email, a = make("admin")
        rv, ra = login(client, v_email), login(client, a_email)
        assert client.get("/api/admin/users", headers=hdr(rv)).status_code == 403
        assert client.get("/api/admin/users", headers=hdr(ra)).status_code == 200
        n_email = v_email.replace("-0@", "-new@")
        made = client.post("/api/admin/users", headers=hdr(ra), json={"email": n_email, "name": "N", "role": "trader", "temp_password": PW})
        assert made.status_code == 201 and made.json()["must_change_password"] is True
        try:
            assert client.post("/api/admin/users", headers=hdr(ra), json={"email": n_email, "role": "trader", "temp_password": PW}).status_code == 409
            audit = client.get("/api/admin/audit?limit=50", headers=hdr(ra)).json()
            assert any(x["action"] == "user_created" and x["ok"] for x in audit) and any(x["action"] == "login" for x in audit)
        finally:
            execute_sql("DELETE FROM audit_log WHERE email = %s", (n_email,))
            execute_sql("DELETE FROM users WHERE email = %s", (n_email,))

    def test_an_admin_cannot_demote_or_disable_themselves_and_a_disabled_user_loses_access(self, env):
        client, make, _ = env
        a_email, a = make("admin")
        v_email, v = make("viewer")
        ra, rv = login(client, a_email), login(client, v_email)
        assert client.patch(f"/api/admin/users/{a['id']}", headers=hdr(ra), json={"status": "disabled"}).status_code == 409
        assert client.patch(f"/api/admin/users/{a['id']}", headers=hdr(ra), json={"role": "viewer"}).status_code == 409
        assert client.get("/api/auth/me", headers=hdr(rv)).status_code == 200
        assert client.patch(f"/api/admin/users/{v['id']}", headers=hdr(ra), json={"status": "disabled"}).status_code == 200
        assert client.get("/api/auth/me", headers=hdr(rv)).status_code == 401
        assert login(client, v_email).status_code == 401

    def test_admin_reset_forces_change_and_revokes_sessions(self, env):
        client, make, _ = env
        a_email, _ = make("admin")
        v_email, v = make("viewer")
        ra, rv = login(client, a_email), login(client, v_email)
        assert client.post(f"/api/admin/users/{v['id']}/reset", headers=hdr(ra), json={"temp_password": "temporary passphrase 55"}).status_code == 200
        assert client.get("/api/auth/me", headers=hdr(rv)).status_code == 401
        r = login(client, v_email, "temporary passphrase 55")
        assert r.status_code == 200 and r.json()["must_change_password"] is True
