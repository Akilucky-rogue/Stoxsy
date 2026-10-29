-- 014_trade_ideas.sql
--
-- The durable idea-log (Phase 0 increment 1; ARCHITECTURE.md §2 Scorecard).
-- EVERY TradeIdea an engine emits is recorded HERE at emit time — mechanically,
-- with no post-hoc selection. That is what makes the Scorecard's anti-gaming
-- guarantee real: you can't cherry-pick which trades to count if all of them
-- are already logged. Outcome columns are filled later by the resolver
-- (mechanical idea -> realized-outcome match), mirroring paper_trades.

CREATE TABLE IF NOT EXISTS trade_ideas (
    idea_id           VARCHAR(40)  PRIMARY KEY,
    scorecard_id      VARCHAR(40)  NOT NULL,
    engine            VARCHAR(64)  NOT NULL,
    symbol            VARCHAR(40)  NOT NULL,
    structure         VARCHAR(24)  NOT NULL,        -- CASH_LONG | IRON_CONDOR | ...
    horizon           VARCHAR(16)  NOT NULL,
    direction         INT          NOT NULL,        -- +1 / -1 / 0 (neutral income)

    -- cash / future legs
    entry             FLOAT8,
    stop              FLOAT8,
    target            FLOAT8,
    qty               INT,
    capital_required  FLOAT8,

    -- option-structure economics
    net_credit        FLOAT8,
    max_profit        FLOAT8,
    margin_required   FLOAT8,
    expiry            DATE,

    -- risk (defined-risk mandatory — enforced in code by TradeIdea.validate())
    max_loss_rupees   FLOAT8       NOT NULL,

    -- conviction / provenance
    confidence        FLOAT8,
    expected_value    FLOAT8,
    thesis_ref        TEXT,
    adjustment_plan   TEXT,
    valid_until       TIMESTAMPTZ,

    legs              JSONB,
    greeks            JSONB,
    breakevens        JSONB,
    reasons           JSONB,

    created_at        TIMESTAMPTZ  DEFAULT NOW(),

    -- outcome (filled by the resolver; mechanical idea -> outcome match)
    status            VARCHAR(12)  DEFAULT 'OPEN',  -- OPEN | WIN | LOSS | TIME | EXPIRED
    exit_price        FLOAT8,
    exit_date         DATE,
    realized_ret      FLOAT8,                        -- gross, direction-adjusted
    realized_ret_net  FLOAT8,                        -- after cost
    resolved_at       TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_trade_ideas_engine ON trade_ideas(engine, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_trade_ideas_status ON trade_ideas(status, engine);
CREATE INDEX IF NOT EXISTS idx_trade_ideas_symbol ON trade_ideas(symbol, created_at DESC);
