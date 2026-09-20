-- 0082_gateway_notification_dispatches.sql — durable notification queue
-- (spec/01 §Data model NOTIFICATION DISPATCHES; platform errata PR-7: the
-- queue engine lives in bdpay/platform/notifications.py, the Postgres table
-- ships with the gateway lane and plugs into the NotificationStore protocol).
--
-- Deviations from the spec/01 DDL, each errata-pinned (G-10):
--   * UNPARTITIONED. The spec declares PARTITION BY RANGE (created_at) with
--     PRIMARY KEY (dispatch_id) and UNIQUE (idempotency_key) — the same
--     defect class as errata E1/PR-1 (Postgres requires the partition key in
--     every unique constraint). True uniqueness wins; partitioning is a
--     later explicit migration.
--   * template_vars JSONB + language + next_attempt_at columns. The platform
--     FSM record renders the body at send time, so the durable store must
--     round-trip the variables (validated PII-free at intake — PII never
--     enters the queue) and the retry schedule; spec/01's hash-only column
--     assumed an object store this build does not have.

CREATE TYPE notification_channel AS ENUM ('sms', 'email', 'push');
CREATE TYPE notification_status AS ENUM ('QUEUED', 'SENDING', 'DELIVERED', 'FAILED');

CREATE TABLE notification_dispatches (
    dispatch_id         TEXT PRIMARY KEY,            -- ndsp_<sha256_canonical[:24]>
    idempotency_key     TEXT NOT NULL UNIQUE,        -- client-supplied
    recipient_type      TEXT NOT NULL
        CHECK (recipient_type IN ('customer', 'merchant', 'operator')),
    recipient_id        TEXT NOT NULL,
    channel             notification_channel NOT NULL,
    template_id         TEXT NOT NULL,
    template_vars       JSONB NOT NULL DEFAULT '{}'::jsonb,  -- PII-rejected at intake
    template_vars_hash  TEXT NOT NULL,               -- sha256_canonical(vars)
    language            TEXT NOT NULL DEFAULT 'en' CHECK (language IN ('en', 'bn')),
    status              notification_status NOT NULL DEFAULT 'QUEUED',
    attempt_count       SMALLINT NOT NULL DEFAULT 0,
    next_attempt_at     TIMESTAMPTZ,                 -- queue scheduling instant
    connector_ref       TEXT,                        -- connector idempotency key
    connector_message_id TEXT,                       -- external delivery reference
    delivered_at        TIMESTAMPTZ,
    failed_reason       TEXT,                        -- PII-free
    created_at          TIMESTAMPTZ NOT NULL,
    updated_at          TIMESTAMPTZ NOT NULL,
    schema_version      SMALLINT NOT NULL DEFAULT 1
);

CREATE INDEX idx_ndsp_recipient ON notification_dispatches (recipient_id, created_at DESC);
CREATE INDEX idx_ndsp_due ON notification_dispatches (next_attempt_at)
    WHERE status = 'QUEUED';
