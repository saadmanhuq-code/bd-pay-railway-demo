-- 0032_payment_intents.sql — spec/02 §Data model, PAYMENT INTENTS.
--
-- Divergences from the spec DDL, all errata-pinned:
--   * K-1 (same defect class as E1/PR-1): the spec partitions by
--     RANGE (created_at) while keeping PRIMARY KEY (payment_intent_id) and a
--     UNIQUE idempotency_key — Postgres requires the partition key in every
--     unique constraint, which would break both. Ships UNPARTITIONED with
--     the true PK; the 12-year-retention partitioning is a later explicit
--     migration project.
--   * K-15: the spec's bare UNIQUE on idempotency_key would make merchant
--     B's client key collide with merchant A's; keys are client-supplied
--     and scoped per actor (spec/01 idempotency scoping). Uniqueness is
--     (merchant_id, idempotency_key).

CREATE TYPE payment_intent_status AS ENUM (
    'CREATED',
    'REQUIRES_PAYMENT_METHOD',
    'REQUIRES_CONFIRMATION',
    'PRE_FLIGHT',
    'REQUIRES_ACTION',
    'PROCESSING',
    'REQUIRES_CAPTURE',
    'SUCCEEDED',
    'FAILED',
    'CANCELLED',
    'REVERSAL_INITIATED',
    'REVERSED',
    'REFUND_INITIATED',
    'PARTIALLY_REFUNDED',
    'REFUNDED'
);

CREATE TYPE payment_method_type AS ENUM (
    'NPSB_IBFT',
    'BEFTN_CREDIT',
    'BKASH',
    'NAGAD',
    'ROCKET',
    'CARD',
    'BANGLA_QR'
);

CREATE TYPE capture_method AS ENUM ('automatic', 'manual');

CREATE TABLE payment_intents (
    payment_intent_id     TEXT        PRIMARY KEY,   -- pi_<sha256[:24]>
    merchant_id           TEXT        NOT NULL REFERENCES merchants (merchant_id),
    customer_id           TEXT        REFERENCES customers (customer_id),
    amount_minor          BIGINT      NOT NULL,
    currency              CHAR(3)     NOT NULL DEFAULT 'BDT',
    method                payment_method_type NOT NULL,
    capture_method        capture_method NOT NULL DEFAULT 'automatic',
    status                payment_intent_status NOT NULL DEFAULT 'CREATED',
    description           TEXT,
    statement_descriptor  TEXT,
    metadata              JSONB       NOT NULL DEFAULT '{}',
    idempotency_key       TEXT,                      -- client-supplied; stored for replay
    client_secret_hash    TEXT,                      -- sha256 of client_secret; never plaintext
    latest_attempt_id     TEXT,                      -- pa_<id>; updated on each attempt
    succeeded_at          TIMESTAMPTZ,
    delivery_confirmed_at TIMESTAMPTZ,
    cancelled_at          TIMESTAMPTZ,
    reversed_at           TIMESTAMPTZ,
    expires_at            TIMESTAMPTZ NOT NULL,      -- created_at + 30 min default
    created_at            TIMESTAMPTZ NOT NULL,
    updated_at            TIMESTAMPTZ NOT NULL,
    schema_version        SMALLINT    NOT NULL DEFAULT 1,
    CONSTRAINT amount_positive CHECK (amount_minor > 0),
    CONSTRAINT currency_bdt    CHECK (currency = 'BDT'),
    CONSTRAINT pi_idem_scoped_unique UNIQUE (merchant_id, idempotency_key)
);

CREATE INDEX pi_merchant_status_idx ON payment_intents (merchant_id, status, created_at DESC);
CREATE INDEX pi_customer_idx        ON payment_intents (customer_id, created_at DESC)
    WHERE customer_id IS NOT NULL;
CREATE INDEX pi_expiry_sweep_idx    ON payment_intents (expires_at)
    WHERE status IN ('CREATED', 'REQUIRES_PAYMENT_METHOD', 'REQUIRES_CONFIRMATION',
                     'PRE_FLIGHT', 'REQUIRES_ACTION', 'PROCESSING');
