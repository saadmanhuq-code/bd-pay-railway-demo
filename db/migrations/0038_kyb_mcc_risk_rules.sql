-- 0038_kyb_mcc_risk_rules.sql — spec/08 DDL §Section 4 (MCC RISK RULES)
-- plus the seed rows (research 03 §5.3 / §6.3).
--
-- Seed rule_ids are the deterministic make_id('mccr', {mcc_code,
-- effective_from}) values, precomputed with bdpay.kernel.ids_ext (the spec
-- writes make_id(...) pseudo-calls inside SQL, which cannot execute; the
-- content-addressed literals below are the equivalent, pinned by
-- tests/kernel/test_migrations_kernel.py). Idempotent: ON CONFLICT DO NOTHING.

CREATE TYPE mcc_disposition_enum AS ENUM ('AUTO_REJECT', 'ENHANCED_DUE_DILIGENCE', 'STANDARD');

CREATE TABLE kyb_mcc_risk_rules (
    rule_id            TEXT PRIMARY KEY,
    mcc_code           CHAR(4) NOT NULL,
    mcc_description    TEXT NOT NULL,
    disposition        mcc_disposition_enum NOT NULL,
    risk_tier_override risk_tier_enum,
    regulatory_basis   TEXT,
    effective_from     DATE NOT NULL,
    effective_to       DATE,
    created_by         TEXT NOT NULL,
    created_at         TIMESTAMPTZ NOT NULL,
    approved_by        TEXT NOT NULL,
    CONSTRAINT mcc_risk_rules_active_unique UNIQUE (mcc_code, effective_from)
);

CREATE INDEX idx_mcc_rules_active ON kyb_mcc_risk_rules (mcc_code)
    WHERE effective_to IS NULL;

INSERT INTO kyb_mcc_risk_rules
    (rule_id, mcc_code, mcc_description, disposition, risk_tier_override,
     regulatory_basis, effective_from, effective_to, created_by, created_at, approved_by)
VALUES
    ('mccr_240e0111a224e8cbc8641223', '7995', 'Gambling - betting and lottery',
     'AUTO_REJECT', NULL, 'research 03 s5.3 - online gambling/unlicensed betting',
     '2026-06-12', NULL, 'system', '2026-06-12T00:00:00Z', 'system'),
    ('mccr_5bf4c488236f82ea8bc987b4', '7994', 'Video amusement game supplies',
     'AUTO_REJECT', NULL, 'research 03 s5.3 - unlicensed betting',
     '2026-06-12', NULL, 'system', '2026-06-12T00:00:00Z', 'system'),
    ('mccr_86868103929cd4bdb4731853', '5912', 'Drug stores - controlled substances unlicensed',
     'AUTO_REJECT', NULL, 'research 03 s5.3 - narcotics',
     '2026-06-12', NULL, 'system', '2026-06-12T00:00:00Z', 'system'),
    ('mccr_6f0c6691725d1bb2f58a270b', '6211', 'Security brokers - unlicensed forex/crypto',
     'AUTO_REJECT', NULL, 'research 03 s5.3 - unlicensed financial schemes',
     '2026-06-12', NULL, 'system', '2026-06-12T00:00:00Z', 'system'),
    ('mccr_788cce10c3efe88064c0aeac', '7273', 'Dating/escort services - adult content',
     'AUTO_REJECT', NULL, 'research 03 s5.3 - adult content',
     '2026-06-12', NULL, 'system', '2026-06-12T00:00:00Z', 'system'),
    ('mccr_ac86a9064fb3889a41a5e780', '5999', 'Miscellaneous specialty retail - high risk',
     'ENHANCED_DUE_DILIGENCE', 'HIGH', 'research 03 s6.3 - high-value goods',
     '2026-06-12', NULL, 'system', '2026-06-12T00:00:00Z', 'system'),
    ('mccr_b528b491ddcfeea61545d854', '7841', 'Video tape rental - adult restricted',
     'ENHANCED_DUE_DILIGENCE', 'HIGH', 'research 03 s6.3 - adult services',
     '2026-06-12', NULL, 'system', '2026-06-12T00:00:00Z', 'system'),
    ('mccr_32d7509936fd63de061372b2', '5933', 'Pawn shops',
     'ENHANCED_DUE_DILIGENCE', 'HIGH', 'research 03 s6.3 - high-risk retail',
     '2026-06-12', NULL, 'system', '2026-06-12T00:00:00Z', 'system'),
    ('mccr_ba81cd2428885a905e65c6fb', '6012', 'Financial institutions - merchandise/services',
     'ENHANCED_DUE_DILIGENCE', 'HIGH', 'research 03 s6.3 - quasi-financial',
     '2026-06-12', NULL, 'system', '2026-06-12T00:00:00Z', 'system')
ON CONFLICT (mcc_code, effective_from) DO NOTHING;
