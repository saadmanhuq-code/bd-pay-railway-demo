-- 0033_payment_attempts.sql — spec/02 §Data model, PAYMENT ATTEMPTS (verbatim
-- shape; unpartitioned table in the spec already).

CREATE TYPE payment_attempt_status AS ENUM (
    'STARTED',
    'AUTHENTICATION_PENDING',
    'AUTHENTICATION_FAILED',
    'AUTHORIZATION_PENDING',
    'AUTHORIZATION_FAILED',
    'TIMED_OUT',
    'AUTHORIZED',
    'CAPTURE_INITIATED',
    'CHARGED',
    'CAPTURE_FAILED',
    'VOIDED',
    'REVERSED'
);

CREATE TABLE payment_attempts (
    attempt_id          TEXT        PRIMARY KEY,   -- pa_<sha256[:24]>
    payment_intent_id   TEXT        NOT NULL REFERENCES payment_intents (payment_intent_id),
    attempt_number      SMALLINT    NOT NULL,       -- 1-based; max 3 per intent (retry cap)
    connector_id        TEXT        NOT NULL,       -- e.g. 'bkash_pgw_v2'
    instruction_id      TEXT        NOT NULL,       -- ins_<id>; sent to connector
    connector_ref       TEXT        NOT NULL UNIQUE, -- idempotency key for the connector
    status              payment_attempt_status NOT NULL DEFAULT 'STARTED',
    amount_minor        BIGINT      NOT NULL,
    currency            CHAR(3)     NOT NULL DEFAULT 'BDT',
    rail_transaction_id TEXT,
    error_code          TEXT,
    raw_response_hash   TEXT,                       -- sha256(canonical_json(raw)); never the raw
    poll_count          SMALLINT    NOT NULL DEFAULT 0,
    authorized_at       TIMESTAMPTZ,
    auth_expires_at     TIMESTAMPTZ,
    charged_at          TIMESTAMPTZ,
    failed_at           TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL,
    updated_at          TIMESTAMPTZ NOT NULL,
    schema_version      SMALLINT    NOT NULL DEFAULT 1,
    CONSTRAINT amount_positive CHECK (amount_minor > 0),
    CONSTRAINT attempt_number_range CHECK (attempt_number BETWEEN 1 AND 10)
);

CREATE INDEX pa_intent_idx    ON payment_attempts (payment_intent_id, attempt_number);
CREATE INDEX pa_connector_ref ON payment_attempts (connector_ref);
