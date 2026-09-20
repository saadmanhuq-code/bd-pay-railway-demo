-- 0034_refunds.sql — spec/02 §Data model, REFUNDS.
--
-- K-1 divergence (errata): the spec partitions refunds by RANGE (created_at)
-- with PRIMARY KEY (refund_id) and UNIQUE connector_ref — same defect class
-- as E1/PR-1; ships UNPARTITIONED with the true uniqueness guarantees.

CREATE TYPE refund_status AS ENUM (
    'REFUND_INITIATED',
    'REFUND_PENDING_CONNECTOR',
    'REFUND_SUCCEEDED',
    'REFUND_FAILED'
);

CREATE TYPE refund_reason AS ENUM (
    'customer_request',
    'duplicate',
    'fraudulent',
    'merchant_initiated'
);

CREATE TABLE refunds (
    refund_id           TEXT        PRIMARY KEY,   -- rfnd_<sha256[:24]>
    payment_intent_id   TEXT        NOT NULL REFERENCES payment_intents (payment_intent_id),
    attempt_id          TEXT        NOT NULL REFERENCES payment_attempts (attempt_id),
    amount_minor        BIGINT      NOT NULL,
    currency            CHAR(3)     NOT NULL DEFAULT 'BDT',
    reason              refund_reason NOT NULL,
    status              refund_status NOT NULL DEFAULT 'REFUND_INITIATED',
    connector_id        TEXT,
    connector_ref       TEXT        UNIQUE,
    rail_transaction_id TEXT,
    raw_response_hash   TEXT,
    retry_count         SMALLINT    NOT NULL DEFAULT 0,
    metadata            JSONB       NOT NULL DEFAULT '{}',
    succeeded_at        TIMESTAMPTZ,
    failed_at           TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL,
    updated_at          TIMESTAMPTZ NOT NULL,
    schema_version      SMALLINT    NOT NULL DEFAULT 1,
    CONSTRAINT refund_amount_positive CHECK (amount_minor > 0)
);

CREATE INDEX rfnd_intent_idx ON refunds (payment_intent_id, created_at DESC);
