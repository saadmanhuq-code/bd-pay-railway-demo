-- 0091_fee_rules_tiering.sql — additive merchant_class / instrument_class columns
--   for Bangla QR class-tagged MDR tiering (spec/16 LR-3).
--
-- SCHEMA-ONLY (2026-06-14 deploy-correctness fix). This migration adds the
-- additive tiering columns and NOTHING ELSE: it seeds NO class-tagged fee rows
-- and contains NO fact-gate.
--
-- Why no seed rows here:
--   The Bangla QR class-tagged MDR rates are an EXTERNAL REGULATORY FACT the
--   platform has NOT yet verified. The fact_corrections register tracks the
--   correction as FC_02_SUPERSEDING_ID (per bdpay.compliance.fact_corrections —
--   BB PSD Circular No. 02/2025, flat 1.15% MDR, NPSB platform fee withdrawn),
--   seeded UNVERIFIED. Under the maker-checker rule, class-tagged rate rows may
--   be seeded ONLY after an operator VERIFIES FC_02_SUPERSEDING_ID with the
--   circular as evidence — at which point they are added by a NEW, higher-
--   numbered migration bound to that verified fact. Asserting the rates here
--   (before verification) would encode an unverified regulatory claim into the
--   schema, so this migration deliberately does not.
--
--   Until that verified seed lands, the spec/04 ceiling row
--   'frule_seed_bangla_qr_115bps_00' (migration 0011; merchant_class=NULL,
--   instrument_class=NULL) resolves every Bangla QR charge — fee resolution
--   falls through to it when no class-tagged row matches.
--
-- History (see git): 0091 previously seeded Jan-2024 tiered rows (50/80/70 bps)
--   behind a hard-abort FC-02 gate. Both are removed here. The tiered rates were
--   superseded; and a hard-abort gate (which raised on the unverified fact)
--   aborts the ENTIRE migration run on a fresh database (blocking every later
--   migration), which is a deploy bug.
--   The schema is additive and applies unconditionally; regulated rate rows stay
--   fail-closed by simply not being seeded until the fact is verified.
--
-- Additive columns (owning specs remain authoritative for all other columns):
--   spec/08 merchants: merchant_class (MICRO | PERSONAL_RETAIL | STANDARD)
--   spec/04 fee_rules:  merchant_class, instrument_class (tiering dimensions)
--
-- Resolution precedence (spec/16 §LR-3 FeeEngine extension):
--   1. (merchant_id, method, mcc)                     — existing, unchanged
--   2. (method, mcc, merchant_class, instrument_class)
--   3. (method, merchant_class, instrument_class)
--   4. (method, merchant_class)
--   5. (method, mcc)                                  — existing
--   6. (method)                                       — existing
--
-- NULL on the new columns = wildcard (matches any value), preserving every
-- existing fee_rules row's behaviour unchanged (NULL IS NULL semantics in
-- the application resolve_fee_rule logic).

-- ============================================================
-- Additive column: merchants.merchant_class
-- ============================================================
ALTER TABLE merchants
    ADD COLUMN IF NOT EXISTS merchant_class TEXT NOT NULL DEFAULT 'STANDARD'
        CHECK (merchant_class IN ('MICRO', 'PERSONAL_RETAIL', 'STANDARD'));

COMMENT ON COLUMN merchants.merchant_class IS
    'Regulated rate tier (spec/16 LR-3). Set during KYB per BB micro-merchant '
    'definition (FC-06 — VERIFY-BEFORE-EXTERNAL). Changing on an ACTIVE merchant '
    'requires two-eyes (changes a regulated rate input).';

-- ============================================================
-- Additive columns: fee_rules.merchant_class, fee_rules.instrument_class
-- ============================================================
ALTER TABLE ledger.fee_rules
    ADD COLUMN IF NOT EXISTS merchant_class TEXT
        CHECK (merchant_class IS NULL
               OR merchant_class IN ('MICRO', 'PERSONAL_RETAIL', 'STANDARD'));

ALTER TABLE ledger.fee_rules
    ADD COLUMN IF NOT EXISTS instrument_class TEXT
        CHECK (instrument_class IS NULL
               OR instrument_class IN ('DEBIT_PREPAID', 'CREDIT', 'MFS_PSP_WALLET'));

COMMENT ON COLUMN ledger.fee_rules.merchant_class IS
    'NULL = wildcard (matches any merchant class). spec/16 LR-3 tiering dimension.';

COMMENT ON COLUMN ledger.fee_rules.instrument_class IS
    'NULL = wildcard (matches any instrument). '
    'DEBIT_PREPAID = debit/prepaid card; CREDIT = credit card; '
    'MFS_PSP_WALLET = bKash/Nagad/Rocket/Upay/PSP-wallet QR. spec/16 LR-3.';

-- ============================================================
-- NO class-tagged seed rows (see header).  Regulated Bangla QR class-tagged
-- rates require operator verification of FC_02_SUPERSEDING_ID (Circular
-- 02/2025) before any such row is seeded by a future numbered migration.
-- Until then the 0011 ceiling row 'frule_seed_bangla_qr_115bps_00' governs.
-- ============================================================
