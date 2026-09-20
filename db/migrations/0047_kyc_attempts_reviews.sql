-- 0047_kyc_attempts_reviews.sql — spec/09 DDL kyc_biometric_attempts +
-- kyc_manual_reviews.

CREATE TABLE kyc_biometric_attempts (
    attempt_id              TEXT        PRIMARY KEY,
    -- make_id('kycbio', {kyc_record_id, attempt_number}) (kernel additive prefix, K-2)
    kyc_record_id           TEXT        NOT NULL REFERENCES kyc_records (kyc_record_id),
    attempt_number          SMALLINT    NOT NULL CHECK (attempt_number BETWEEN 1 AND 3),
    attempted_at            TIMESTAMPTZ NOT NULL,
    matched                 BOOLEAN     NOT NULL,
    porichoy_ref            TEXT,
    porichoy_response_hash  TEXT        NOT NULL,
    connector_result_id     TEXT,
    liveness_token_valid    BOOLEAN     NOT NULL DEFAULT TRUE,
    liveness_sdk_version    TEXT,
    schema_version          SMALLINT    NOT NULL DEFAULT 1,
    UNIQUE (kyc_record_id, attempt_number)
);

CREATE TABLE kyc_manual_reviews (
    review_id               TEXT        PRIMARY KEY,
    -- make_id('kycrev', {kyc_record_id, created_at}) (kernel additive prefix, K-2)
    kyc_record_id           TEXT        NOT NULL REFERENCES kyc_records (kyc_record_id),
    review_state            TEXT        NOT NULL DEFAULT 'REVIEW_OPEN',
    review_reason           TEXT        NOT NULL,
    approval_request_id     TEXT,
    assigned_to_operator_id TEXT,
    reviewer_notes          TEXT,
    decision                TEXT,
    rejection_reason_code   TEXT,
    decided_at              TIMESTAMPTZ,
    decided_by_operator_id  TEXT,
    created_at              TIMESTAMPTZ NOT NULL,
    schema_version          SMALLINT    NOT NULL DEFAULT 1,
    CONSTRAINT kyc_review_state_check CHECK (review_state IN (
        'REVIEW_OPEN', 'REVIEW_IN_PROGRESS', 'REVIEW_ESCALATED', 'REVIEW_CLOSED'
    ))
    -- Two-eyes enforcement lives at the approval layer: every review row
    -- carries approval_request_id, and approval_requests (migration 0003)
    -- DB-CHECKs approver != initiator. The spec's "decided_by must differ
    -- from assigned_to" prose is satisfied there, not duplicated here
    -- (an operator may pick up a queue item and then be the second pair of
    -- eyes relative to the system initiator).
);

CREATE INDEX idx_kyc_reviews_record ON kyc_manual_reviews (kyc_record_id);
CREATE INDEX idx_kyc_reviews_open   ON kyc_manual_reviews (created_at)
    WHERE review_state <> 'REVIEW_CLOSED';
