-- Migration 042: mutual funds / ETFs from AMFI (scheme master + daily NAV). A SEPARATE asset-class layer: nothing here enters the equity universe,
-- the defensive stock signal or the decision cards.
--
-- Source: AMFI's NAV files (portal.amfiindia.com). Raw files are landed first and hashed; every NAV row points at the load that supplied it.
-- NAV is the fund's price per unit. For a GROWTH option NAV change is the investor's return (after expenses); for a payout (IDCW / dividend) option the
-- NAV drops when income is paid out, so NAV change is only a PRICE return (distributions are not in this file).
BEGIN;

CREATE TABLE IF NOT EXISTS mf_nav_loads (
    id            SERIAL      PRIMARY KEY,
    sha256        CHAR(64)    NOT NULL UNIQUE,
    source        TEXT        NOT NULL,           -- amfi_nav_history | amfi_navall
    window_from   DATE,
    window_to     DATE,
    known_at      TIMESTAMPTZ NOT NULL,
    n_rows        BIGINT,
    loaded_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS mf_scheme_master (
    scheme_code     INTEGER     PRIMARY KEY,
    name            TEXT        NOT NULL,
    amc             TEXT,
    category_raw    TEXT,                          -- the AMFI section heading, verbatim
    scheme_type     TEXT,                          -- Open Ended | Close Ended | Interval
    broad_category  TEXT,                          -- Equity Scheme | Debt Scheme | Hybrid Scheme | Solution Oriented Scheme | Other Scheme | older labels
    sub_category    TEXT,
    plan            TEXT,
    option_type     TEXT,
    isin_growth_or_payout VARCHAR(12),
    isin_reinvest   VARCHAR(12),
    asset_class     TEXT,                          -- equity | debt | hybrid | commodity | international | solution | other
    is_etf          BOOLEAN NOT NULL DEFAULT FALSE,
    is_index        BOOLEAN NOT NULL DEFAULT FALSE,
    is_liquid       BOOLEAN NOT NULL DEFAULT FALSE,
    is_fof          BOOLEAN NOT NULL DEFAULT FALSE,
    is_direct       BOOLEAN,
    is_growth       BOOLEAN,                       -- growth/accumulation option: NAV change is a total return
    classification_evidence TEXT,
    first_nav_date  DATE,
    last_nav_date   DATE,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_mf_master_isin ON mf_scheme_master (isin_growth_or_payout);
CREATE INDEX IF NOT EXISTS idx_mf_master_class ON mf_scheme_master (asset_class, is_etf);

CREATE TABLE IF NOT EXISTS mf_nav (
    scheme_code  INTEGER      NOT NULL,
    date         DATE         NOT NULL,
    nav          DOUBLE PRECISION NOT NULL,
    load_id      INTEGER      NOT NULL REFERENCES mf_nav_loads(id),
    PRIMARY KEY (scheme_code, date)
);
SELECT create_hypertable('mf_nav', 'date', if_not_exists => TRUE, migrate_data => TRUE);

COMMENT ON TABLE mf_nav IS 'Daily NAV per AMFI scheme code (raw as published). Source file hash: mf_nav_loads.sha256 via load_id.';
COMMIT;
