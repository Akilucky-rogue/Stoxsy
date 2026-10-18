-- Migration 038: accounts, rotating refresh tokens and an audit log (multi-user, roles admin | trader | viewer).
-- Passwords are argon2id hashes; refresh tokens are stored only as sha256 hashes and rotate on every use (reuse revokes the family).
BEGIN;

CREATE TABLE IF NOT EXISTS users (
    id             UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    email          TEXT        NOT NULL UNIQUE CHECK (email = lower(email)),
    name           TEXT        NOT NULL DEFAULT '',
    password_hash  TEXT        NOT NULL,
    role           TEXT        NOT NULL CHECK (role IN ('admin', 'trader', 'viewer')),
    status         TEXT        NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled', 'invited')),
    must_change_pw BOOLEAN     NOT NULL DEFAULT TRUE,
    failed_logins  INT         NOT NULL DEFAULT 0,
    locked_until   TIMESTAMPTZ,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_login_at  TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS refresh_tokens (
    id           UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id      UUID        NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    family_id    UUID        NOT NULL,
    token_hash   CHAR(64)    NOT NULL UNIQUE,
    issued_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at   TIMESTAMPTZ NOT NULL,
    revoked_at   TIMESTAMPTZ,
    replaced_by  UUID,
    ip           TEXT,
    user_agent   TEXT
);
CREATE INDEX IF NOT EXISTS idx_refresh_user ON refresh_tokens (user_id);
CREATE INDEX IF NOT EXISTS idx_refresh_family ON refresh_tokens (family_id);

CREATE TABLE IF NOT EXISTS audit_log (
    id          BIGSERIAL   PRIMARY KEY,
    ts          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    user_id     UUID,
    email       TEXT,
    action      TEXT        NOT NULL,
    entity_type TEXT,
    entity_id   TEXT,
    ok          BOOLEAN     NOT NULL DEFAULT TRUE,
    ip          TEXT,
    payload     JSONB
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log (ts DESC);
CREATE INDEX IF NOT EXISTS idx_audit_action_email ON audit_log (action, email, ts DESC);
CREATE INDEX IF NOT EXISTS idx_audit_action_ip ON audit_log (action, ip, ts DESC);

COMMENT ON TABLE users IS 'Stoxsy accounts. Roles: admin (users, kill-switch, model decisions), trader (own portfolios), viewer (read-only).';
COMMIT;
