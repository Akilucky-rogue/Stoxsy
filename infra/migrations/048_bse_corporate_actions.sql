-- Migration 048: BSE corporate actions, evidence-first. BSE's own Corporate Actions export is landed raw (hash, retrieval time, known_at), parsed into
-- `bse_corporate_actions`, and reconciled against BSE price behaviour into `bse_ca_reconciliation` (confirmed | needs_review | rejected).
-- NOTHING here adjusts a price: a separate BSE adjusted-price / total-return materialisation is built only after validation and owner approval.
-- Until then BSE stays `raw_prices_only` and BSE-only names stay out of the universe, the defensive signal and decision cards.
BEGIN;

CREATE TABLE IF NOT EXISTS bse_ca_files (
    id            SERIAL      PRIMARY KEY,
    sha256        CHAR(64)    NOT NULL UNIQUE,
    filename      TEXT,
    origin        TEXT        NOT NULL,                 -- e.g. manual_download:<page> (BSE's API blocks automated clients)
    n_rows        INTEGER     NOT NULL,
    n_parsed      INTEGER     NOT NULL,
    n_bad         INTEGER     NOT NULL,
    issues        JSONB       NOT NULL DEFAULT '[]'::jsonb,
    retrieved_at  TIMESTAMPTZ NOT NULL,
    known_at      TIMESTAMPTZ NOT NULL,                 -- when the system learned these rows: never earlier than the download
    landing_path  TEXT,
    loaded_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS bse_corporate_actions (
    id            BIGSERIAL   PRIMARY KEY,
    file_id       INTEGER     NOT NULL REFERENCES bse_ca_files (id),
    scrip_code    INTEGER     NOT NULL,
    name          TEXT,
    purpose       TEXT        NOT NULL,                 -- BSE's text, verbatim
    action_type   TEXT        NOT NULL,                 -- bonus | split | consolidation | dividend | rights | demerger_or_scheme | buyback | other
    ratio_text    TEXT,
    factor        DOUBLE PRECISION,                     -- price multiplier BEFORE the ex-date, only for bonus / split / consolidation
    amount        DOUBLE PRECISION,                     -- cash dividend per share
    ex_date       DATE        NOT NULL,
    record_date   DATE,
    bc_start      DATE,
    bc_end        DATE,
    nd_start      DATE,
    nd_end        DATE,
    payment_date  DATE,
    isin          VARCHAR(12),                          -- from the BSE listing master by scrip code; NULL if unknown
    known_at      TIMESTAMPTZ NOT NULL,
    content_hash  CHAR(64)    NOT NULL UNIQUE,
    raw_row       JSONB       NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bse_ca_scrip ON bse_corporate_actions (scrip_code, ex_date);
CREATE INDEX IF NOT EXISTS idx_bse_ca_type ON bse_corporate_actions (action_type, ex_date);

CREATE TABLE IF NOT EXISTS bse_ca_reconciliation (
    action_id          BIGINT      PRIMARY KEY REFERENCES bse_corporate_actions (id),
    version            TEXT        NOT NULL,
    classification     TEXT        NOT NULL CHECK (classification IN ('confirmed', 'needs_review', 'rejected')),
    reason             TEXT        NOT NULL,
    listed_factor      DOUBLE PRECISION,
    observed_ratio     DOUBLE PRECISION,
    factor_error       DOUBLE PRECISION,
    gap_date           DATE,
    shift_sessions     INTEGER,
    untraded_sessions  INTEGER,
    persistence_ratio  DOUBLE PRECISION,
    reconciled_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_bse_ca_rec_class ON bse_ca_reconciliation (classification);

COMMENT ON TABLE bse_ca_reconciliation IS 'Evidence only. `confirmed` means the listed action matches the BSE price behaviour within the hard bounds; it does not adjust anything.';
COMMIT;
