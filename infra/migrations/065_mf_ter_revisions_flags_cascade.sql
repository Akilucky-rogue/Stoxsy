-- Migration 065: two fixes to mf_ter_revisions (064), found in review before any revision existed.
-- 1. A revision carries the quality flags of ITS values (computed on the incoming row), so a restated figure is not shown with the first-seen figure's warning (or without its own).
-- 2. Deleting a file from the ledger (drop and reload) must remove its revisions, like mf_ter and mf_ter_unmatched do.
BEGIN;
ALTER TABLE mf_ter_revisions ADD COLUMN IF NOT EXISTS flags TEXT[] NOT NULL DEFAULT '{}';
ALTER TABLE mf_ter_revisions DROP CONSTRAINT IF EXISTS mf_ter_revisions_file_id_fkey;
ALTER TABLE mf_ter_revisions ADD CONSTRAINT mf_ter_revisions_file_id_fkey FOREIGN KEY (file_id) REFERENCES mf_ter_files (id) ON DELETE CASCADE;
COMMIT;
