-- 0090_fact_corrections.sql — FactCorrection entity (spec/16 LR-3).
--
-- Table: compliance.fact_corrections
--   Append-only register of external-fact corrections.  The FSM (UNVERIFIED ->
--   VERIFIED | RETRACTED) advances via the application layer only; the app role
--   has no UPDATE or DELETE on history columns (status advances via the FSM
--   function, same pattern as spec/10 webhook_fsm_advance).
--
-- Seed rows FC-01..FC-06 are created with status=UNVERIFIED, external_blocking=TRUE.
-- The migration DOES NOT set these to VERIFIED — that is an operator action
-- (maker-checker, evidence_pointer required; spec/16 §B).
--
-- The tiered-MDR migration (0091) reads fact_corrections to confirm FC-02 is
-- VERIFIED before seeding the tiered rate rows.

-- Deploy-rehearsal fix (2026-06-13, fresh-database apply on VM3): this file
-- is the first migration to place objects in the `compliance` schema and the
-- first to GRANT to compliance_app_role / compliance_ro, but no earlier
-- migration creates the schema or the roles — on a fresh database the
-- migration-runner failed closed here (`schema "compliance" does not exist`;
-- the file's transaction rolled back whole, so it was never recorded as
-- applied). Guarded creation matches the 0010/0060 pattern (NOLOGIN,
-- idempotent on re-run).
CREATE SCHEMA IF NOT EXISTS compliance;

DO $$
BEGIN
    CREATE ROLE compliance_app_role NOLOGIN;
EXCEPTION WHEN duplicate_object THEN
    NULL;
END $$;

DO $$
BEGIN
    CREATE ROLE compliance_ro NOLOGIN;
EXCEPTION WHEN duplicate_object THEN
    NULL;
END $$;

CREATE TABLE IF NOT EXISTS compliance.fact_corrections (
    correction_id           TEXT PRIMARY KEY,
    -- fcor_<sha256_canonical({claim_source, claim_text})[:24]>

    claim_source            TEXT NOT NULL,
    -- e.g. 'research/05 §2.2'

    claim_text              TEXT NOT NULL,
    -- the stale/unverified claim, verbatim

    corrected_text          TEXT NOT NULL,
    -- the correction to be verified

    status                  TEXT NOT NULL DEFAULT 'UNVERIFIED'
        CHECK (status IN ('UNVERIFIED', 'VERIFIED', 'RETRACTED')),

    external_blocking       BOOLEAN NOT NULL DEFAULT TRUE,
    -- TRUE => this fact gates external-facing doc publication (spec/16 §B release wiring)

    evidence_pointer        TEXT,
    -- object-store key (BB circular PDF, gazette, written bank confirmation)

    evidence_sha256         TEXT,
    -- sha256 of the evidence file at evidence_pointer

    supersedes_correction_id TEXT REFERENCES compliance.fact_corrections(correction_id),
    -- set when this row replaces a previously-retracted or outdated correction

    created_by              TEXT NOT NULL,
    -- operator_id who recorded the correction

    verified_by             TEXT,
    -- operator_id who attached evidence and marked VERIFIED (must differ from created_by)

    created_at              TIMESTAMPTZ NOT NULL,
    verified_at             TIMESTAMPTZ,

    schema_version          SMALLINT NOT NULL DEFAULT 1,

    -- Maker-checker: verifier must differ from recorder
    CONSTRAINT fcor_verifier_differs CHECK (
        verified_by IS NULL OR verified_by <> created_by
    ),

    -- Completeness: VERIFIED row must have all evidence fields populated
    CONSTRAINT fcor_verified_complete CHECK (
        status <> 'VERIFIED'
        OR (
            evidence_pointer IS NOT NULL
            AND evidence_sha256 IS NOT NULL
            AND verified_by IS NOT NULL
            AND verified_at IS NOT NULL
        )
    )
);

CREATE INDEX IF NOT EXISTS idx_fcor_status
    ON compliance.fact_corrections (status, created_at DESC);

-- App role: INSERT and state-advancing UPDATE only (no DELETE; append-only by policy)
GRANT SELECT, INSERT, UPDATE ON compliance.fact_corrections TO compliance_app_role;
GRANT SELECT ON compliance.fact_corrections TO compliance_ro;

-- ============================================================
-- Seed rows FC-01..FC-06 (spec/16 §B, verbatim claim texts)
-- status = UNVERIFIED, external_blocking = TRUE
-- correction_id values are deterministic:
--   sha256_canonical({"claim_source": ..., "claim_text": ...})[:24]
--   computed by the application layer; the values below are the authoritative
--   pre-computed IDs from bdpay.compliance.fact_corrections.FACT_CORRECTION_SEED_KEYS.
-- ============================================================

-- NOTE: the exact correction_id hex values below are computed by the
-- bdpay.compliance.fact_corrections module using E12 canonical JSON over the
-- (claim_source, claim_text) payload.  To re-derive run:
--   python -c "from bdpay.compliance.fact_corrections import FACT_CORRECTION_SEED_KEYS; print(FACT_CORRECTION_SEED_KEYS)"
-- The content-addressed IDs are inserted directly — there is exactly ONE
-- fcor namespace shared by the application, this migration, 0091's FC-02
-- gate, the CI fact gate (scripts/check_facts.py), and the sponsor-bank
-- pack.  The former 'fcor_seed_*' sentinel namespace is retired.

INSERT INTO compliance.fact_corrections (
    correction_id, claim_source, claim_text, corrected_text,
    status, external_blocking, created_by, created_at
) VALUES
(
    'fcor_12a7bfcc105153e3df04c534',
    'research/05 §2.2',
    'Binimoy suspended by BB; MFS/bank interoperability live 1 Nov 2025 over NPSB',
    'VERIFY-BEFORE-EXTERNAL: confirm current Binimoy suspension status and exact '
    'MFS/bank interoperability go-live date via BB official circular or press release.',
    'UNVERIFIED', TRUE, 'system:migration', '2026-06-12T00:00:00Z'
),
(
    'fcor_02c43c9933b107e9d1c6ed26',
    'research/05 §4.1 + spec/04 MDR table',
    'Bangla QR MDR is tiered (micro-merchant debit/prepaid cap, '
    'micro-merchant credit/MFS/PSP cap, personal-retail rate) under the '
    '1.15% ceiling — exact rates and the BB circular reference',
    'VERIFY-BEFORE-EXTERNAL: obtain the BB PSD circular (cited 18 Jan 2024 in '
    'the moat analysis) confirming tiered MDR rates (50 bps debit/prepaid cap, '
    '80 bps credit/MFS/PSP cap, 70 bps personal-retail) under the 1.15% ceiling.',
    'UNVERIFIED', TRUE, 'system:migration', '2026-06-12T00:00:00Z'
),
(
    'fcor_763138795565f23a69274c91',
    'research/02 §1 / interop fee model',
    'NPSB per-leg interop fees (bank→MFS, MFS→MFS/bank max, bank→PSP, '
    'sender-pays) — exact schedule',
    'VERIFY-BEFORE-EXTERNAL: obtain the official NPSB per-leg interop fee schedule '
    'from the sponsor bank or BB/NPSB documentation.',
    'UNVERIFIED', TRUE, 'system:migration', '2026-06-12T00:00:00Z'
),
(
    'fcor_7fa1833043df95815da229b0',
    'research/05 §1/§10, CANONICAL §1',
    'License path + capital table vs PSO Regulation 2025 and draft DEMI '
    'rules (incl. reapplication window)',
    'VERIFY-BEFORE-EXTERNAL: confirm capital tiers and reapplication window against '
    'the final gazetted PSO Regulation 2025 and any published DEMI draft rules.',
    'UNVERIFIED', TRUE, 'system:migration', '2026-06-12T00:00:00Z'
),
(
    'fcor_0bd7599faab51dbaf4fda772',
    'research/07 item 9, spec/12 §H',
    'Veridyn P2 corpus coverage of PSS Act 2024 / BPSSR 2014 (gates any '
    'external use of the 78,411-rule claim)',
    'VERIFY-BEFORE-EXTERNAL: complete the Veridyn P2 corpus census to confirm '
    'coverage of the PSS Act 2024 / BPSSR 2014 78,411-rule claim before any '
    'BB or investor communication.',
    'UNVERIFIED', TRUE, 'system:migration', '2026-06-12T00:00:00Z'
),
(
    'fcor_cd07b361f1a8d7a2c501c37b',
    'spec/08 / BB micro-merchant circular',
    'The BB micro-merchant definition used to set '
    'merchants.merchant_class = ''MICRO''',
    'VERIFY-BEFORE-EXTERNAL: confirm the exact BB micro-merchant definition '
    '(turnover threshold, sector restrictions) from the official BB circular '
    'before using merchant_class=MICRO in any rate calculation.',
    'UNVERIFIED', TRUE, 'system:migration', '2026-06-12T00:00:00Z'
)
ON CONFLICT (correction_id) DO NOTHING;
