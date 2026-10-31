-- Migration 064: append-only history of AMFI TER values that AMFI later changed.
-- mf_ter keeps the FIRST value seen for each (scheme code, day) and is never updated. When a later file carries a different value for a day we already hold, the change is recorded here
-- (old = the value current just before that file, new = what AMFI now says) so a reader can ask "what did we know on date T": the newest revision with known_at <= T, else the first value.
-- known_at is when WE loaded the file, not when AMFI published it (the file does not say). Additive: no existing table or read changes.
BEGIN;
CREATE TABLE IF NOT EXISTS mf_ter_revisions (
    id         BIGSERIAL   PRIMARY KEY,
    nsdl_code  TEXT        NOT NULL,
    ter_date   DATE        NOT NULL,
    file_id    BIGINT      NOT NULL REFERENCES mf_ter_files (id),
    known_at   TIMESTAMPTZ NOT NULL,
    old_values JSONB       NOT NULL,          -- the ten value columns as they stood (numbers or null)
    new_values JSONB       NOT NULL,          -- the ten value columns as AMFI now publishes them
    changed    TEXT[]      NOT NULL           -- the columns that differ
);
CREATE INDEX IF NOT EXISTS idx_mf_ter_revisions_key ON mf_ter_revisions (nsdl_code, ter_date, id);
CREATE INDEX IF NOT EXISTS idx_mf_ter_revisions_file ON mf_ter_revisions (file_id);
COMMIT;
