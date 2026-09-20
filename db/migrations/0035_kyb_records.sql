-- 0035_kyb_records.sql — spec/08 DDL §Section 1 (CORE KYB RECORD).
--
-- Kernel-used columns; encrypted-PII columns the OCR/portal flow owns
-- (contact_email_encrypted, bank_account_number_encrypted, address JSONB)
-- are declared per spec for forward compatibility. Two kernel-added
-- TIMESTAMPTZ columns (last_document_request_at, agreement_requested_at)
-- carry the 8-FSM-1 TTL anchors the spec names but gives no column for
-- (errata K-16). DEFAULT NOW() from the spec DDL is intentionally NOT
-- carried: every timestamp is written from the injected clock (spec/00 §7).

CREATE TYPE kyb_status_enum AS ENUM (
    'APPLICATION_SUBMITTED',
    'DOCUMENTS_PENDING',
    'KYB_IN_PROGRESS',
    'ENHANCED_DUE_DILIGENCE',
    'PENDING_HUMAN_REVIEW',
    'SANCTIONS_REVIEW',
    'RISK_ASSESSED',
    'AGREEMENT_PENDING',
    'ACTIVE',
    'SUSPENDED',
    'KYB_REFRESH_PENDING',
    'REJECTED',
    'TERMINATED'
);

CREATE TYPE risk_tier_enum AS ENUM ('LOW', 'MEDIUM', 'HIGH');

CREATE TYPE registration_type_enum AS ENUM (
    'RJSC_PRIVATE_LIMITED',
    'RJSC_PUBLIC_LIMITED',
    'SOLE_PROPRIETORSHIP',
    'PARTNERSHIP',
    'NGO',
    'FOREIGN_COMPANY'
);

CREATE TABLE kyb_records (
    kyb_record_id            TEXT PRIMARY KEY,
    -- make_id('kyb', {subject_type, subject_id, submitted_at})

    subject_type             TEXT NOT NULL CHECK (subject_type IN ('MERCHANT', 'PARTICIPANT')),
    subject_id               TEXT NOT NULL,

    kyb_status               kyb_status_enum NOT NULL DEFAULT 'APPLICATION_SUBMITTED',
    risk_tier                risk_tier_enum,

    legal_name               TEXT,
    legal_name_bn            TEXT,
    registration_type        registration_type_enum,
    rjsc_registration_number TEXT,
    trade_license_number     TEXT,
    trade_license_authority  TEXT,
    tin_number               TEXT,          -- 12 ASCII digits post-normalization
    vat_bin_number           TEXT,
    primary_business_mcc     CHAR(4),
    secondary_mccs           TEXT[],
    website_url              TEXT,

    contact_email_encrypted  BYTEA,
    contact_email_hash       TEXT,
    contact_phone_e164       TEXT,
    registered_address       JSONB NOT NULL DEFAULT '{}',

    bank_account_number_encrypted BYTEA,
    bank_account_number_last4     TEXT,
    bank_routing_number           TEXT,
    bank_account_holder_name      TEXT,
    bank_name                     TEXT,
    bank_branch_name              TEXT,

    requested_services       TEXT[] NOT NULL DEFAULT '{}',
    approved_services        TEXT[] NOT NULL DEFAULT '{}',

    mdr_basis_points         SMALLINT,
    settlement_cycle_days    SMALLINT CHECK (settlement_cycle_days BETWEEN 1 AND 7),
    rolling_reserve_pct      SMALLINT CHECK (rolling_reserve_pct BETWEEN 0 AND 2500),

    institution_type         TEXT,
    bb_license_number        TEXT,
    bb_license_type          TEXT,
    bb_license_expiry        DATE,

    sanctions_cleared_at     TIMESTAMPTZ,
    sanctions_screened_at    TIMESTAMPTZ,

    approval_request_id      TEXT,

    application_submitted_at TIMESTAMPTZ NOT NULL,
    kyb_submitted_at         TIMESTAMPTZ,
    activated_at             TIMESTAMPTZ,
    rejected_at              TIMESTAMPTZ,
    rejection_reason_code    TEXT,
    rejection_notes          TEXT,
    suspended_at             TIMESTAMPTZ,
    suspension_reason        TEXT,
    terminated_at            TIMESTAMPTZ,
    termination_reason       TEXT,

    sla_target_at            TIMESTAMPTZ,
    sla_status               TEXT NOT NULL DEFAULT 'ON_TRACK'
        CHECK (sla_status IN ('ON_TRACK', 'AT_RISK', 'BREACHED')),

    next_refresh_due_at      TIMESTAMPTZ,
    last_refreshed_at        TIMESTAMPTZ,
    refresh_interval_months  SMALLINT DEFAULT 24,

    last_document_request_at TIMESTAMPTZ,  -- K-16: DOCUMENTS_PENDING TTL anchor
    agreement_requested_at   TIMESTAMPTZ,  -- K-16: AGREEMENT_PENDING TTL anchor

    schema_version           SMALLINT NOT NULL DEFAULT 1,
    created_at               TIMESTAMPTZ NOT NULL,
    updated_at               TIMESTAMPTZ NOT NULL,

    CONSTRAINT kyb_records_subject_unique UNIQUE (subject_type, subject_id)
);

CREATE INDEX idx_kyb_records_subject ON kyb_records (subject_type, subject_id);
CREATE INDEX idx_kyb_records_status  ON kyb_records (kyb_status);
CREATE INDEX idx_kyb_records_sla     ON kyb_records (sla_target_at)
    WHERE kyb_status NOT IN ('ACTIVE', 'REJECTED', 'TERMINATED');
CREATE INDEX idx_kyb_records_refresh ON kyb_records (next_refresh_due_at)
    WHERE kyb_status = 'ACTIVE';
