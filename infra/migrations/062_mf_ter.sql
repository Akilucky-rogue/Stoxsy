-- Migration 062: AMFI total expense ratios (TER), loaded from the owner's manual download of AMFI's "TER of MF Schemes" page (docs/design/AMFI_TER_DESIGN.md).
-- Source rows are one per scheme per DAY with the regular and the direct plan side by side; AMFI identifies a scheme by its NSDL scheme code (no ISIN, no AMFI numeric code), so mf_ter is keyed by the
-- normalised NSDL code and linked to mf_scheme_master through mf_ter_map (derived, rebuildable: key_version names the name-key rule that produced it).
-- Values are stored as published (numeric(7,3), percent); a plan that is not offered is NULL, never 0. Quality problems are flags on the row, never changes to a value.
-- mf_ter_layouts: one row per file layout (hash of the normalised header row); a layout is approved once, by the owner, after a dry run (--mapping-ok).
BEGIN;
CREATE TABLE IF NOT EXISTS mf_ter_layouts (
    layout_hash CHAR(64)    PRIMARY KEY,
    headers     JSONB       NOT NULL,
    approved    BOOLEAN     NOT NULL DEFAULT FALSE,
    approved_at TIMESTAMPTZ,
    first_seen  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS mf_ter_files (
    id                BIGSERIAL   PRIMARY KEY,
    sha256            CHAR(64)    NOT NULL UNIQUE,
    filename          TEXT        NOT NULL,
    layout_hash       CHAR(64)    NOT NULL REFERENCES mf_ter_layouts (layout_hash),
    n_data_rows       INTEGER     NOT NULL,
    n_parsed          INTEGER     NOT NULL,
    n_rejected        INTEGER     NOT NULL,
    n_flagged         INTEGER     NOT NULL DEFAULT 0,
    n_footer_rows     INTEGER     NOT NULL DEFAULT 0,
    footer_head       TEXT,
    date_min          DATE,
    date_max          DATE,
    last_date_partial BOOLEAN     NOT NULL DEFAULT FALSE,
    issues            JSONB       NOT NULL DEFAULT '{}'::jsonb,
    retrieved_at      TIMESTAMPTZ NOT NULL,
    known_at          TIMESTAMPTZ NOT NULL,
    landing_path      TEXT,
    loaded_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS mf_ter_scheme (
    nsdl_code       TEXT  PRIMARY KEY,
    nsdl_code_raw   TEXT  NOT NULL,
    name            TEXT  NOT NULL,
    scheme_type     TEXT,
    scheme_category TEXT,
    first_seen      DATE  NOT NULL,
    last_seen       DATE  NOT NULL,
    name_variants   JSONB NOT NULL DEFAULT '[]'::jsonb
);

CREATE TABLE IF NOT EXISTS mf_ter (
    nsdl_code       TEXT         NOT NULL REFERENCES mf_ter_scheme (nsdl_code),
    ter_date        DATE         NOT NULL,
    r_ber           NUMERIC(7,3),
    r_brokerage     NUMERIC(7,3),
    r_transaction   NUMERIC(7,3),
    r_statutory     NUMERIC(7,3),
    r_total         NUMERIC(7,3),
    d_ber           NUMERIC(7,3),
    d_brokerage     NUMERIC(7,3),
    d_transaction   NUMERIC(7,3),
    d_statutory     NUMERIC(7,3),
    d_total         NUMERIC(7,3),
    flags           TEXT[]       NOT NULL DEFAULT '{}',
    file_id         BIGINT       NOT NULL REFERENCES mf_ter_files (id) ON DELETE CASCADE,
    known_at        TIMESTAMPTZ  NOT NULL,
    PRIMARY KEY (nsdl_code, ter_date)
);
CREATE INDEX IF NOT EXISTS idx_mf_ter_date ON mf_ter (ter_date);
CREATE INDEX IF NOT EXISTS idx_mf_ter_file ON mf_ter (file_id);

CREATE TABLE IF NOT EXISTS mf_ter_map (
    nsdl_code   TEXT    NOT NULL REFERENCES mf_ter_scheme (nsdl_code),
    scheme_code INTEGER NOT NULL,
    plan        TEXT    NOT NULL CHECK (plan IN ('regular', 'direct')),
    key_version TEXT    NOT NULL,
    PRIMARY KEY (scheme_code, plan)
);
CREATE INDEX IF NOT EXISTS idx_mf_ter_map_code ON mf_ter_map (nsdl_code);

CREATE TABLE IF NOT EXISTS mf_ter_unmatched (
    id          BIGSERIAL PRIMARY KEY,
    file_id     BIGINT    NOT NULL REFERENCES mf_ter_files (id) ON DELETE CASCADE,
    side        TEXT      NOT NULL CHECK (side IN ('ter', 'master')),
    nsdl_code   TEXT,
    scheme_code INTEGER,
    name        TEXT,
    reason      TEXT      NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mf_ter_unmatched_file ON mf_ter_unmatched (file_id);
COMMIT;
