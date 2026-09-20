-- 0148_kyb_qr_merchant_profile.sql — QR merchant profile facts on KYB.
--
-- Bangla QR issuance must use configured merchant-directory data, not
-- hardcoded city values or deterministic fake PANs derived from merchant_id.
-- The existing kyb_records.legal_name_bn and registered_address columns carry
-- Bengali name/address facts; these additive fields carry the QR-facing
-- display name and acquirer-issued merchant PAN.

ALTER TABLE kyb_records
    ADD COLUMN IF NOT EXISTS merchant_display_name TEXT,
    ADD COLUMN IF NOT EXISTS bangla_qr_merchant_pan TEXT,
    ADD COLUMN IF NOT EXISTS bangla_qr_enabled BOOLEAN NOT NULL DEFAULT FALSE;

CREATE UNIQUE INDEX IF NOT EXISTS idx_kyb_records_bangla_qr_merchant_pan
    ON kyb_records (bangla_qr_merchant_pan)
    WHERE bangla_qr_merchant_pan IS NOT NULL;
