-- Migration 045: user-imported holdings (the portfolio cockpit). Holdings are the USER's own data: every query filters on user_id, imports are immutable
-- (a new import supersedes the previous one for display; old imports stay for audit), and nothing here can place an order.
-- user_id is TEXT because unenforced local mode uses the principal id 'local'.
BEGIN;

CREATE TABLE IF NOT EXISTS portfolio_imports (
    id           BIGSERIAL   PRIMARY KEY,
    user_id      TEXT        NOT NULL,
    label        TEXT,
    filename     TEXT,
    sha256       CHAR(64)    NOT NULL,
    n_rows_file  INTEGER     NOT NULL,
    n_accepted   INTEGER     NOT NULL,
    n_rejected   INTEGER     NOT NULL,
    issues       JSONB       NOT NULL DEFAULT '[]'::jsonb,
    imported_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_portfolio_imports_user ON portfolio_imports (user_id, imported_at DESC);

CREATE TABLE IF NOT EXISTS portfolio_holdings (
    import_id    BIGINT      NOT NULL REFERENCES portfolio_imports (id) ON DELETE CASCADE,
    symbol       TEXT        NOT NULL,
    isin         TEXT,
    quantity     NUMERIC(20,4) NOT NULL CHECK (quantity > 0),
    avg_cost     NUMERIC(20,4) NOT NULL CHECK (avg_cost >= 0),
    PRIMARY KEY (import_id, symbol)
);

COMMIT;
