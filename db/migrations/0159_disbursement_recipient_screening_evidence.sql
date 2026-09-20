-- 0159_disbursement_recipient_screening_evidence.sql
--
-- Audit P0 cab6a60 (bd-pay-disbursement-recipient-unscreened): disbursement
-- payout recipients are now sanctions-screened fail-closed at approval and
-- re-screened pre-rail at dispatch. This migration adds the per-item
-- screening evidence columns DisbursementService records on every screen:
--
--   screened_at            when the most recent recipient screen ran
--   screening_list_version the active sanctions list version screened against
--                          (SanctionsScreenResult.list_version_id)
--
-- Additive only; existing rows read as never-screened (NULL), which the
-- service treats as unscreened and screens before any rail handoff.

ALTER TABLE disbursement_items
    ADD COLUMN IF NOT EXISTS screened_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS screening_list_version TEXT;
