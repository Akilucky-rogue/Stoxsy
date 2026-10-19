-- Migration 044: a BSE scrip that failed the price check but belongs to an NSE symbol that is ALREADY linked_verified through another BSE scrip is a
-- legacy / secondary scrip (e.g. an old-ISIN line kept for a few months after a split). It stays unlinked, but needs no review.
BEGIN;
ALTER TABLE listing_xref DROP CONSTRAINT IF EXISTS listing_xref_link_status_check;
ALTER TABLE listing_xref ADD CONSTRAINT listing_xref_link_status_check
    CHECK (link_status IN ('linked_verified', 'linked_unchecked', 'unlinked_pending_review', 'unlinked_secondary_scrip'));
COMMIT;
