-- Migration 063: store AMFI TER values exactly as published.
-- Migration 062 declared the ten value columns NUMERIC(7,3), but the real AMFI file carries up to 4 decimals (18,152 values in the October 2026 file), so 062 rounded them and
-- the stored value no longer equalled the published one (and flags computed on the unrounded value could disagree with the stored one). NUMERIC without a scale keeps the digits sent.
-- Existing rows keep whatever they hold until the file is loaded again; the loader's restatement check and flags use the same exact values.
BEGIN;
ALTER TABLE mf_ter
    ALTER COLUMN r_ber         TYPE NUMERIC,
    ALTER COLUMN r_brokerage   TYPE NUMERIC,
    ALTER COLUMN r_transaction TYPE NUMERIC,
    ALTER COLUMN r_statutory   TYPE NUMERIC,
    ALTER COLUMN r_total       TYPE NUMERIC,
    ALTER COLUMN d_ber         TYPE NUMERIC,
    ALTER COLUMN d_brokerage   TYPE NUMERIC,
    ALTER COLUMN d_transaction TYPE NUMERIC,
    ALTER COLUMN d_statutory   TYPE NUMERIC,
    ALTER COLUMN d_total       TYPE NUMERIC;
COMMIT;
