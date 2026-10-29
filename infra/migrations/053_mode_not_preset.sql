-- Migration 053: nothing is preset. The mode is chosen by the user; until then it is NULL (it used to default to 'investor').
BEGIN;
ALTER TABLE user_profiles ALTER COLUMN mode DROP NOT NULL;
ALTER TABLE user_profiles ALTER COLUMN mode DROP DEFAULT;
COMMIT;
