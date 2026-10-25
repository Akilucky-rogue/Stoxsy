-- Migration 060: the owner's own restricted list. A registered adviser/broker may be barred from personal trades in a security they advise on or cover for clients; which securities and until when
-- is for the owner and their compliance officer to say, so NOTHING is preset: the table starts empty. An entry blocks the trader desk from proposing or logging that symbol until it is released
-- or its end date passes. Releasing keeps the row (history); every change is also written to mode_audit.
BEGIN;
CREATE TABLE IF NOT EXISTS compliance_restricted_list (
    id               BIGSERIAL   PRIMARY KEY,
    user_id          TEXT        NOT NULL,
    symbol           TEXT        NOT NULL,
    reason           TEXT        NOT NULL CHECK (length(btrim(reason)) >= 3),
    restricted_until DATE,
    added_by_role    TEXT        NOT NULL,
    added_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    released_at      TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_restricted_user_active ON compliance_restricted_list (user_id, symbol) WHERE released_at IS NULL;
COMMIT;
