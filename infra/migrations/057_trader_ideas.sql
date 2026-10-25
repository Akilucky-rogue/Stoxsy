-- Migration 057: trader ideas (research and paper only). Each row is a card that PASSED the risk gate; refusals are written to mode_audit, never stored as cards.
-- Ideas belong to the TRADING pot of one user. A paper position is "open" from opened_at at the planned entry until closed by a stop, target, time stop or the owner.
BEGIN;
CREATE TABLE IF NOT EXISTS trader_ideas (
    id            BIGSERIAL    PRIMARY KEY,
    user_id       TEXT         NOT NULL,
    created_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    symbol        TEXT         NOT NULL,
    setup         TEXT         NOT NULL,
    side          TEXT         NOT NULL CHECK (side = 'long'),
    horizon_id    TEXT         NOT NULL,
    evidence      TEXT         NOT NULL CHECK (evidence IN ('RESEARCH', 'PAPER', 'APPROVED')),
    entry         NUMERIC(14,4) NOT NULL CHECK (entry > 0),
    stop          NUMERIC(14,4) NOT NULL CHECK (stop > 0),
    target        NUMERIC(14,4),
    qty           INTEGER      NOT NULL CHECK (qty > 0),
    risk_rupees   NUMERIC(16,2) NOT NULL CHECK (risk_rupees > 0),
    time_stop     DATE         NOT NULL,
    status        TEXT         NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed')),
    opened_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    closed_at     TIMESTAMPTZ,
    exit_price    NUMERIC(14,4),
    exit_reason   TEXT         CHECK (exit_reason IS NULL OR exit_reason IN ('stop', 'target', 'time', 'manual')),
    pnl_rupees    NUMERIC(16,2),
    r_multiple    NUMERIC(10,4),
    notes         TEXT,
    card          JSONB        NOT NULL,
    CHECK ((status = 'open' AND closed_at IS NULL) OR (status = 'closed' AND closed_at IS NOT NULL AND exit_price IS NOT NULL AND exit_reason IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS idx_trader_ideas_user ON trader_ideas (user_id, status, opened_at DESC);
COMMIT;
