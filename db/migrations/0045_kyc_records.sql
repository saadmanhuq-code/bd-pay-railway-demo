-- 0045_kyc_records.sql — spec/09 DDL kyc_records.
--
-- Divergences from the spec DDL, all errata-pinned:
--   * K-1 class: the spec partitions by RANGE (created_at) while relying on
--     PRIMARY KEY (kyc_record_id), a self-referencing FK, and the partial
--     unique index on (customer_id) WHERE state='ACTIVE' — none of which a
--     partitioned table can carry without the partition key. Ships
--     UNPARTITIONED; the retention partitioning is a later explicit project.
--   * K-19: the spec declares guardian_customer_id REFERENCES
--     kyc_records(kyc_record_id) while its own prose stores "the guardian's
--     customer_id". The FK points at customers(customer_id).
--   * The spec's tier_assigned_at_dup GENERATED column is dropped (it
--     duplicated tier_assigned_at "for index convenience"; the index is on
--     the real column). updated_at and last_biometric_at are kernel-added
--     timestamp anchors for FSM sweeps (K-16 class).

CREATE TABLE kyc_records (
    kyc_record_id           TEXT        PRIMARY KEY,
    -- make_id('kyc', {customer_id, initiated_at})
    customer_id             TEXT        NOT NULL REFERENCES customers (customer_id),
    state                   TEXT        NOT NULL,
    tier_requested          TEXT,
    tier                    TEXT,
    tier_assigned_at        TIMESTAMPTZ,
    previous_kyc_record_id  TEXT        REFERENCES kyc_records (kyc_record_id),
    channel                 TEXT        NOT NULL DEFAULT 'SELF_SERVICE',
    is_minor                BOOLEAN     NOT NULL DEFAULT FALSE,
    is_joint_account        BOOLEAN     NOT NULL DEFAULT FALSE,
    guardian_customer_id    TEXT        REFERENCES customers (customer_id),  -- K-19

    -- NID capture — one-way hash + AES-256-GCM ciphertext; never raw
    nid_hash                TEXT,
    nid_encrypted           BYTEA,
    nid_type                TEXT,
    nid_dob_iso             TEXT,
    name_en_norm            TEXT,
    name_bn_norm            TEXT,
    father_name_bn_norm     TEXT,
    mother_name_bn_norm     TEXT,
    address_bn_norm         TEXT,
    address_en_norm         TEXT,
    post_code               CHAR(4),

    nid_front_object_key    TEXT,
    nid_back_object_key     TEXT,
    selfie_object_key       TEXT,

    risk_tier               TEXT        NOT NULL DEFAULT 'LOW',
    refresh_due_at          TIMESTAMPTZ,
    refresh_hard_deadline_at TIMESTAMPTZ,

    rejection_reason_code   TEXT,
    rejection_notes         TEXT,

    created_at              TIMESTAMPTZ NOT NULL,
    submitted_at            TIMESTAMPTZ,
    last_biometric_at       TIMESTAMPTZ,
    archived_at             TIMESTAMPTZ,
    expired_at              TIMESTAMPTZ,
    updated_at              TIMESTAMPTZ NOT NULL,

    produced_by             TEXT        NOT NULL DEFAULT 'kyc-service@1',
    schema_version          SMALLINT    NOT NULL DEFAULT 1,

    CONSTRAINT kyc_records_state_check CHECK (state IN (
        'REFRESH_PENDING', 'SUBMITTED', 'NID_OCR_RECEIVED', 'BIOMETRIC_PENDING',
        'BIOMETRIC_RETRY_PENDING', 'BIOMETRIC_PASSED',
        'PENDING_HUMAN_REVIEW', 'ACTIVE', 'REJECTED',
        'REFRESH_OVERDUE', 'EXPIRED_HARD', 'ARCHIVED'
    )),
    CONSTRAINT kyc_tier_check CHECK (tier IN ('SIMPLIFIED', 'REGULAR') OR tier IS NULL),
    CONSTRAINT kyc_risk_tier_check CHECK (risk_tier IN ('LOW', 'MEDIUM', 'HIGH')),
    CONSTRAINT minor_guardian_required CHECK (
        (is_minor = FALSE)
        OR (is_minor = TRUE AND state != 'ACTIVE')
        OR (is_minor = TRUE AND state = 'ACTIVE' AND guardian_customer_id IS NOT NULL)
    )
);

-- The refresh no-gap invariant: at most one ACTIVE record per customer.
CREATE UNIQUE INDEX kyc_records_customer_active_idx
    ON kyc_records (customer_id) WHERE state = 'ACTIVE';

CREATE INDEX kyc_records_refresh_due_idx
    ON kyc_records (refresh_due_at) WHERE state = 'ACTIVE';

CREATE INDEX kyc_records_review_queue_idx
    ON kyc_records (created_at) WHERE state = 'PENDING_HUMAN_REVIEW';
