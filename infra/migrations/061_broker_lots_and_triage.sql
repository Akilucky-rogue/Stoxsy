-- Migration 061: broker account snapshots, trades, reported lots, per-security pot triage and derived tax lots.
--
-- Facts (immutable, deduplicated, one row per file or trade) are kept apart from what is derived from them:
--   lot_loads            one row per imported file (sha256 per user, account and kind: the same file twice is refused). Only non-personal facts are stored: counts, header layout, column mapping.
--                        Banner rows of a back-office report (client name, client code, PAN, DP id) are skipped while reading and are NEVER stored.
--   lot_layouts_approved the owner approved this header layout for this kind of file (once per layout, not once ever)
--   broker_holdings      a demat holdings snapshot for one broker account (Choice, Zerodha ...), with no pot: a demat account mixes investments and trades
--   broker_transactions  equity and mutual fund trades from tradebooks / statements; overlapping files cannot double count (dedupe_key)
--   broker_reported_lots lots as the broker reports them (stated, never adjusted by us)
--   security_pot         the owner's triage: which pot a security belongs to. Set once, audited, and carried to every later import so a weekly re-import cannot reset it
-- portfolio_imports / portfolio_holdings stay the per-pot view the trader and investor code already read; they are regenerated from the snapshot and the triage (source_load_id points back).
-- portfolio_tax_lots is DERIVED and replaceable (one lot_runs row per rebuild, the latest wins). Nothing here can place an order.
BEGIN;

ALTER TABLE portfolio_imports ADD COLUMN IF NOT EXISTS broker_account TEXT;
ALTER TABLE portfolio_imports ADD COLUMN IF NOT EXISTS source_load_id BIGINT;
CREATE INDEX IF NOT EXISTS idx_portfolio_imports_account ON portfolio_imports (user_id, broker_account, pot, imported_at DESC);

CREATE TABLE IF NOT EXISTS lot_loads (
    id             BIGSERIAL   PRIMARY KEY,
    user_id        TEXT        NOT NULL,
    broker_account TEXT        NOT NULL CHECK (length(btrim(broker_account)) >= 2),
    kind           TEXT        NOT NULL CHECK (kind IN ('holdings', 'tradebook', 'lots', 'mf')),
    filename       TEXT,
    sha256         CHAR(64)    NOT NULL,
    layout_hash    CHAR(64)    NOT NULL,
    n_rows_file    INTEGER     NOT NULL,
    n_accepted     INTEGER     NOT NULL,
    n_rejected     INTEGER     NOT NULL,
    n_duplicate    INTEGER     NOT NULL DEFAULT 0,
    issues         JSONB       NOT NULL DEFAULT '[]'::jsonb,
    mapping        JSONB       NOT NULL DEFAULT '{}'::jsonb,
    imported_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (user_id, broker_account, kind, sha256)
);
CREATE INDEX IF NOT EXISTS idx_lot_loads_latest ON lot_loads (user_id, broker_account, kind, imported_at DESC);

CREATE TABLE IF NOT EXISTS lot_layouts_approved (
    user_id     TEXT        NOT NULL,
    kind        TEXT        NOT NULL,
    layout_hash CHAR(64)    NOT NULL,
    mapping     JSONB       NOT NULL DEFAULT '{}'::jsonb,
    approved_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_id, kind, layout_hash)
);

CREATE TABLE IF NOT EXISTS broker_holdings (
    load_id  BIGINT        NOT NULL REFERENCES lot_loads (id) ON DELETE CASCADE,
    symbol   TEXT          NOT NULL,
    isin     TEXT,
    quantity NUMERIC(20,4) NOT NULL CHECK (quantity > 0),
    avg_cost NUMERIC(20,4) NOT NULL CHECK (avg_cost >= 0),
    PRIMARY KEY (load_id, symbol)
);

CREATE TABLE IF NOT EXISTS broker_transactions (
    id             BIGSERIAL     PRIMARY KEY,
    load_id        BIGINT        NOT NULL REFERENCES lot_loads (id) ON DELETE CASCADE,
    user_id        TEXT          NOT NULL,
    broker_account TEXT          NOT NULL,
    asset          TEXT          NOT NULL CHECK (asset IN ('equity', 'mf')),
    symbol         TEXT,
    isin           TEXT,
    folio          TEXT,
    scheme_name    TEXT,
    trade_date     DATE          NOT NULL,
    trade_time     TEXT,
    side           TEXT          NOT NULL CHECK (side IN ('buy', 'sell')),
    quantity       NUMERIC(24,6) NOT NULL CHECK (quantity > 0),
    price          NUMERIC(20,6) NOT NULL CHECK (price >= 0),
    trade_ref      TEXT,
    dedupe_key     CHAR(40)      NOT NULL,
    UNIQUE (user_id, broker_account, dedupe_key)
);
CREATE INDEX IF NOT EXISTS idx_broker_tx_account ON broker_transactions (user_id, broker_account, asset, symbol, trade_date);

CREATE TABLE IF NOT EXISTS broker_reported_lots (
    id        BIGSERIAL     PRIMARY KEY,
    load_id   BIGINT        NOT NULL REFERENCES lot_loads (id) ON DELETE CASCADE,
    symbol    TEXT          NOT NULL,
    isin      TEXT,
    buy_date  DATE          NOT NULL,
    quantity  NUMERIC(24,6) NOT NULL CHECK (quantity > 0),
    unit_cost NUMERIC(20,6) NOT NULL CHECK (unit_cost >= 0)
);
CREATE INDEX IF NOT EXISTS idx_reported_lots_load ON broker_reported_lots (load_id, symbol);

-- security: the symbol for an equity, the ISIN for a mutual fund scheme
CREATE TABLE IF NOT EXISTS security_pot (
    id             BIGSERIAL   PRIMARY KEY,
    user_id        TEXT        NOT NULL,
    broker_account TEXT        NOT NULL,
    asset          TEXT        NOT NULL CHECK (asset IN ('equity', 'mf')),
    security       TEXT        NOT NULL,
    pot            TEXT        NOT NULL CHECK (pot IN ('investment', 'trading')),
    set_by_role    TEXT        NOT NULL,
    set_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    released_at    TIMESTAMPTZ
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_security_pot_active ON security_pot (user_id, broker_account, asset, security) WHERE released_at IS NULL;

-- A security's pot is set once. The only allowed change is releasing the row (the position is gone and a later purchase is a new decision).
CREATE OR REPLACE FUNCTION security_pot_immutable() RETURNS trigger AS $$
BEGIN
    IF NEW.pot IS DISTINCT FROM OLD.pot OR NEW.security IS DISTINCT FROM OLD.security OR NEW.broker_account IS DISTINCT FROM OLD.broker_account
       OR NEW.user_id IS DISTINCT FROM OLD.user_id OR NEW.asset IS DISTINCT FROM OLD.asset THEN
        RAISE EXCEPTION 'security_pot is immutable (% % -> %): release it and record a new decision', OLD.security, OLD.pot, NEW.pot;
    END IF;
    IF OLD.released_at IS NOT NULL AND NEW.released_at IS DISTINCT FROM OLD.released_at THEN
        RAISE EXCEPTION 'security_pot release cannot be undone';
    END IF;
    RETURN NEW;
END $$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS trg_security_pot_immutable ON security_pot;
CREATE TRIGGER trg_security_pot_immutable BEFORE UPDATE ON security_pot FOR EACH ROW EXECUTE FUNCTION security_pot_immutable();

CREATE TABLE IF NOT EXISTS lot_runs (
    id             BIGSERIAL   PRIMARY KEY,
    user_id        TEXT        NOT NULL,
    broker_account TEXT        NOT NULL,
    computed_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    inputs         JSONB       NOT NULL DEFAULT '{}'::jsonb,
    summary        JSONB       NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_lot_runs_latest ON lot_runs (user_id, broker_account, computed_at DESC);

-- buy_date NULL = the date is not in any loaded file (never guessed). status: RECONCILED | UNVERIFIED | DATE_PENDING | UNRECONCILED_LOT_GAP | UNRECONCILED_SURPLUS | NEEDS_REVIEW
CREATE TABLE IF NOT EXISTS portfolio_tax_lots (
    id             BIGSERIAL     PRIMARY KEY,
    run_id         BIGINT        NOT NULL REFERENCES lot_runs (id) ON DELETE CASCADE,
    user_id        TEXT          NOT NULL,
    broker_account TEXT          NOT NULL,
    asset          TEXT          NOT NULL CHECK (asset IN ('equity', 'mf')),
    symbol         TEXT,
    isin           TEXT,
    folio          TEXT,
    scheme_code    INTEGER,
    scheme_name    TEXT,
    plan_type      TEXT,
    option_type    TEXT,
    buy_date       DATE,
    quantity       NUMERIC(24,6) NOT NULL CHECK (quantity > 0),
    unit_cost      NUMERIC(20,6),
    origin         TEXT          NOT NULL,
    date_basis     TEXT          NOT NULL,
    status         TEXT          NOT NULL,
    note           TEXT
);
CREATE INDEX IF NOT EXISTS idx_tax_lots_run ON portfolio_tax_lots (run_id);
COMMIT;
