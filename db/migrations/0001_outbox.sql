-- 0001_outbox.sql — the atomic event spine (spec/02 §Data model OUTBOX;
-- envelope shape spec/00 §5). Producers INSERT a row in the SAME transaction
-- as their business write; the outbox-worker drains and publishes.
--
-- Errata PR-1 (SPEC_ERRATA-LANE-A-platform-runtime.md): spec/02 declares
-- PARTITION BY RANGE (produced_at) together with PRIMARY KEY (outbox_id).
-- Postgres requires the partition key inside every unique constraint (same
-- defect class as SPEC_ERRATA E1), which would destroy the true uniqueness
-- of outbox_id that INSERT .. ON CONFLICT idempotency depends on. The table
-- therefore ships UNPARTITIONED with the true primary key; the 90-day
-- retention runs as indexed deletes on produced_at until volume demands an
-- explicit partitioning migration.

CREATE TYPE outbox_status AS ENUM ('PENDING', 'CLAIMED', 'PUBLISHED', 'POISON');

CREATE TABLE outbox (
    outbox_id           TEXT          PRIMARY KEY,        -- obx_<sha256_canonical[:24]>
    event_type          TEXT          NOT NULL,           -- canonical event type (spec/00 §5)
    subject_type        TEXT          NOT NULL,           -- e.g. 'PaymentAttempt'
    subject_id          TEXT          NOT NULL,           -- e.g. 'pa_<id>'
    topic               TEXT          NOT NULL,           -- closed registry: '<domain>.events'
    payload             JSONB         NOT NULL,           -- PII-redacted event payload
    schema_version      SMALLINT      NOT NULL DEFAULT 1,
    status              outbox_status NOT NULL DEFAULT 'PENDING',
    producer            TEXT          NOT NULL,           -- '<service>@<version>'
    produced_at         TIMESTAMPTZ   NOT NULL,
    claimed_by          TEXT,                             -- worker instance id
    claimed_at          TIMESTAMPTZ,
    lease_expires_at    TIMESTAMPTZ,                      -- claimed_at + lease; expired => re-claimable
    published_at        TIMESTAMPTZ,
    attempt_count       SMALLINT      NOT NULL DEFAULT 0,
    last_error          TEXT,
    CONSTRAINT outbox_attempt_cap CHECK (attempt_count <= 25)
);

-- Retention: 90 days (matches Redpanda topic retention); deletes by produced_at.
-- Ordering: within a subject_id, rows are consumed in produced_at ASC order.

CREATE INDEX outbox_pending_idx ON outbox (status, produced_at ASC)
    WHERE status IN ('PENDING', 'CLAIMED');
CREATE INDEX outbox_subject_idx ON outbox (subject_type, subject_id, produced_at ASC);
CREATE INDEX outbox_lease_idx   ON outbox (lease_expires_at)
    WHERE status = 'CLAIMED';
CREATE INDEX outbox_produced_at_idx ON outbox (produced_at);
