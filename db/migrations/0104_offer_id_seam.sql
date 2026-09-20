-- 0104_offer_id_seam.sql — spec/18 §Data model "Seam (ships with core)" directive.
--
-- Purpose: add the nullable `offer_id` column to `payment_intents` so that
-- the offer-engine activation track has a plug-and-play landing point from
-- day one, with ZERO behaviour change while the `offers` table does not exist.
--
-- Zero-offer invariant (spec/18 §Zero-offer invariant):
--   With no rows in `offers`, every code path in spec/18 is unreachable.
--   The PRE_FLIGHT guard short-circuits on `offer_id IS NULL`.
--   This migration adds only the column; the FK to `offers(offer_id)` is
--   deferred until the activation-track migration creates `offers` (requires
--   gates G1-G5 to pass).  A NULL-permitting column with no FK is
--   byte-identical to the pre-wedge schema from the application's perspective.
--
-- Pinned regression: tests/platform/test_offer_id_seam.py::test_offer_id_null_is_no_op
--
-- Errata note: none — additive ALTER TABLE on an unpartitioned table (E1/E19
-- defect class does not apply; `payment_intents` is already unpartitioned per
-- migration 0032).

ALTER TABLE payment_intents ADD COLUMN offer_id TEXT NULL;

COMMENT ON COLUMN payment_intents.offer_id IS
    'spec/18 seam: FK to offers(offer_id) added at activation-track migration. '
    'NULL = no offer applied; non-NULL requires authenticated customer_id (D1). '
    'PRE_FLIGHT guard: short-circuits on IS NULL (zero-offer invariant).';
