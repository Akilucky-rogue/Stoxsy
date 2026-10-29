-- Migration 037: closed-loop prediction ledger (immutable predictions, separately-written outcomes).
--
-- A prediction is a fact about what the model said BEFORE the outcome existed. prediction_log is append-only: UPDATE and DELETE are
-- rejected by trigger, so nothing can be edited after the fact. Outcomes live in a different table, written once per prediction
-- by the outcome joiner, and only for predictions whose full label window has elapsed. Re-logging the same key with different
-- content is an error: a changed model must carry a new model_version.
BEGIN;

CREATE TABLE IF NOT EXISTS prediction_log (
    prediction_id    CHAR(32)    PRIMARY KEY,                -- sha256(model_version|symbol|as_of_date|horizon|side)[:32]
    model_version    TEXT        NOT NULL,
    model_card_id    TEXT,
    symbol           VARCHAR(20) NOT NULL,
    security_id      TEXT,
    exchange         VARCHAR(8)  NOT NULL DEFAULT 'NSE',
    as_of_date       DATE        NOT NULL,                   -- decision at the close of this session
    prediction_time  TIMESTAMPTZ NOT NULL,                   -- when the model ran
    known_at         TIMESTAMPTZ NOT NULL,                   -- never earlier than the data it used was published
    horizon_id       TEXT        NOT NULL,
    label_id         TEXT        NOT NULL,
    side             SMALLINT    NOT NULL CHECK (side IN (-1, 1)),
    ref_price        DOUBLE PRECISION,                       -- close of as_of_date (signal-time reference)
    sigma            DOUBLE PRECISION,                       -- EWM vol at the close, the unit of the barriers
    score            DOUBLE PRECISION NOT NULL,
    rank_pct         DOUBLE PRECISION,
    p_win            DOUBLE PRECISION,                       -- calibrated probability, if the model emits one
    expected_return  DOUBLE PRECISION,
    ci_low           DOUBLE PRECISION,
    ci_high          DOUBLE PRECISION,
    position_size    DOUBLE PRECISION,                       -- proposed size / filter decision
    regime           TEXT,
    snapshot_id      TEXT,
    factor_version   TEXT,
    universe_version TEXT,
    feature_hash     TEXT,
    ca_source_hash   TEXT,
    news_refs        JSONB,
    git_sha          TEXT,
    params           JSONB,
    content_hash     CHAR(64)    NOT NULL,
    logged_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_pred_log_asof ON prediction_log (as_of_date, horizon_id);
CREATE INDEX IF NOT EXISTS idx_pred_log_model ON prediction_log (model_version, as_of_date);

CREATE OR REPLACE FUNCTION prediction_log_immutable() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'prediction_log is append-only (% rejected): log a new model_version instead', TG_OP;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_prediction_log_immutable ON prediction_log;
CREATE TRIGGER trg_prediction_log_immutable BEFORE UPDATE OR DELETE ON prediction_log
    FOR EACH ROW EXECUTE FUNCTION prediction_log_immutable();

CREATE TABLE IF NOT EXISTS prediction_outcomes (
    prediction_id  CHAR(32) PRIMARY KEY REFERENCES prediction_log(prediction_id),
    resolved_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    data_through   DATE        NOT NULL,                   -- last price date the resolver was allowed to see
    entry_date     DATE,
    entry_price    DOUBLE PRECISION,
    stop_price     DOUBLE PRECISION,
    target_price   DOUBLE PRECISION,
    status         TEXT        NOT NULL CHECK (status IN ('WIN', 'LOSS', 'TIME')),
    exit_date      DATE,
    exit_price     DOUBLE PRECISION,
    gap_exit       BOOLEAN,
    ret_gross      DOUBLE PRECISION,
    ret_net        DOUBLE PRECISION,
    cost_bps       DOUBLE PRECISION,
    excluded       BOOLEAN     NOT NULL DEFAULT FALSE,     -- window touches an unexplained corporate action
    sigma_check    DOUBLE PRECISION                        -- |logged sigma / recomputed sigma - 1|, audit only
);

COMMENT ON TABLE prediction_log IS 'Immutable model predictions with full lineage. Outcomes are in prediction_outcomes.';
COMMIT;
