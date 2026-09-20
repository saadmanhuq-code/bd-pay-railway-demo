-- 0084_gateway_webhook_endpoints.sql — outbound merchant webhooks
-- (spec/01 §Data model WEBHOOK ENDPOINTS / WEBHOOK DELIVERIES, plus the
-- spec/16 LR-5 §D rotation-grace columns registered as additive amendments).
--
-- Deviations from the spec/01 DDL, each errata-pinned (E-S16-06):
--   * webhook_deliveries UNPARTITIONED. The spec declares PARTITION BY
--     RANGE (created_at) with PRIMARY KEY (delivery_id) — the E1/E19/E35
--     defect class (Postgres requires the partition key inside every unique
--     constraint). True PK wins; retention is indexed deletes; partitioning
--     is a later explicit migration.
--   * The spec's max_webhooks_per_merchant EXCLUDE constraint is dropped:
--     EXCLUDE USING btree (merchant_id WITH =) WHERE (status = 'ACTIVE')
--     would forbid a SECOND active endpoint per merchant, contradicting the
--     same section's "max 10 active webhook endpoints" rule and the
--     UNIQUE (merchant_id, url) row. The max-10 rule is application-enforced
--     (count-before-insert), exactly as the spec's own comment describes.
--   * signing_secret_encrypted / signing_secret_prev_encrypted columns:
--     spec/01 stores only sha256(signing_secret), but the outbound signer
--     must produce the delivery HMAC, so the secret is also stored
--     AES-256-GCM-encrypted under the gateway master key (the spec/01 §A
--     HMAC-material pattern). The raw secret is still shown exactly once.
--   * signing_secret_prev_hash / secret_rotation_grace_until: spec/16 LR-5
--     additive columns (dual-read rotation grace).

CREATE TYPE webhook_endpoint_status AS ENUM ('ACTIVE', 'DISABLED', 'DELETED');

CREATE TABLE webhook_endpoints (
    webhook_id          TEXT PRIMARY KEY,            -- whe_<sha256_canonical[:24]>
    merchant_id         TEXT NOT NULL,               -- cross-package ref (app-enforced, E20)
    url                 TEXT NOT NULL,
    description         TEXT,
    signing_secret_hash TEXT NOT NULL,               -- sha256(secret); raw shown once
    signing_secret_encrypted TEXT NOT NULL,          -- AES-256-GCM under master key
    enabled_events      TEXT[] NOT NULL,
    status              webhook_endpoint_status NOT NULL DEFAULT 'ACTIVE',
    -- spec/16 LR-5 §D rotation grace (dual-read window)
    signing_secret_prev_hash TEXT,
    signing_secret_prev_encrypted TEXT,
    secret_rotation_grace_until TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL,
    updated_at          TIMESTAMPTZ NOT NULL,
    schema_version      SMALLINT NOT NULL DEFAULT 1,
    CONSTRAINT webhook_url_must_be_https CHECK (url LIKE 'https://%'),
    CONSTRAINT webhook_url_merchant_unique UNIQUE (merchant_id, url),
    -- grace columns travel together (both set or both clear)
    CONSTRAINT webhook_rotation_grace_complete CHECK (
        (signing_secret_prev_hash IS NULL) = (secret_rotation_grace_until IS NULL)
        AND (signing_secret_prev_hash IS NULL) = (signing_secret_prev_encrypted IS NULL)
    )
);
-- Max 10 ACTIVE endpoints per merchant: application-enforced before INSERT.
CREATE INDEX idx_webhook_merchant ON webhook_endpoints (merchant_id)
    WHERE status = 'ACTIVE';

CREATE TYPE webhook_delivery_status AS ENUM
    ('PENDING', 'RETRYING', 'DELIVERED', 'EXHAUSTED');

CREATE TABLE webhook_deliveries (
    delivery_id         TEXT PRIMARY KEY,            -- whd_<sha256_canonical[:24]>
    webhook_id          TEXT NOT NULL REFERENCES webhook_endpoints(webhook_id),
    event_id            TEXT NOT NULL,               -- outbox.outbox_id (app-enforced)
    event_type          TEXT NOT NULL,
    payload             JSONB NOT NULL,              -- PII-redacted data.object
    payload_hash        TEXT NOT NULL,               -- sha256_canonical(payload)
    status              webhook_delivery_status NOT NULL DEFAULT 'PENDING',
    attempt_count       SMALLINT NOT NULL DEFAULT 0
        CHECK (attempt_count >= 0 AND attempt_count <= 8),
    next_attempt_at     TIMESTAMPTZ,
    last_attempted_at   TIMESTAMPTZ,
    last_response_status SMALLINT,
    last_error          TEXT,                        -- PII-free
    delivered_at        TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL,
    schema_version      SMALLINT NOT NULL DEFAULT 1,
    -- one delivery per (endpoint, event): redelivered events re-use the row
    CONSTRAINT webhook_delivery_event_unique UNIQUE (webhook_id, event_id)
);

CREATE INDEX idx_whd_next_attempt ON webhook_deliveries (next_attempt_at)
    WHERE status IN ('PENDING', 'RETRYING');
CREATE INDEX idx_whd_webhook_event ON webhook_deliveries (webhook_id, event_id);
