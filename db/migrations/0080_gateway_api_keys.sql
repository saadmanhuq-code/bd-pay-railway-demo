-- 0080_gateway_api_keys.sql — merchant API keys (spec/01 §Data model, API KEYS).
--
-- Hashed secrets at rest: secret_hash = bcrypt(sha256(secret)); the raw
-- secret is shown exactly once at creation and never stored. hmac_key_enc is
-- the spec/01 §Authentication CORRECTION column (HKDF-derived HMAC key,
-- AES-256-GCM sealed under the gateway master key) folded into the base DDL
-- instead of the spec's afterthought ALTER TABLE.
--
-- merchants(merchant_id) is a cross-package reference (spec/08); integrity is
-- application-enforced — the same posture spec/01 itself takes for
-- webhook_endpoints.merchant_id (errata PR-2 precedent).

CREATE TYPE api_key_status AS ENUM ('ACTIVE', 'ROTATION_PENDING', 'REVOKED', 'EXPIRED');

CREATE TABLE api_keys (
    key_id              TEXT PRIMARY KEY,            -- akey_<sha256_canonical[:24]>
    merchant_id         TEXT NOT NULL,               -- merchants.merchant_id (app-enforced)
    key_name            TEXT NOT NULL,
    key_prefix          TEXT NOT NULL
        CHECK (key_prefix IN ('bdpk_live_', 'bdpk_test_')),
    secret_hash         TEXT NOT NULL,               -- bcrypt(sha256(secret))
    hmac_key_enc        TEXT NOT NULL,               -- AES-256-GCM(HKDF(secret, key_id))
    status              api_key_status NOT NULL DEFAULT 'ACTIVE',
    ip_allowlist        TEXT[] NOT NULL DEFAULT '{}',  -- CIDR/IP strings; empty = unrestricted
    expires_at          TIMESTAMPTZ,                 -- NULL = no expiry
    rotation_successor  TEXT REFERENCES api_keys(key_id),
    rotation_grace_ends_at TIMESTAMPTZ,
    last_used_at        TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL,
    revoked_at          TIMESTAMPTZ,
    schema_version      SMALLINT NOT NULL DEFAULT 1,
    CONSTRAINT api_key_name_merchant_unique UNIQUE (merchant_id, key_name)
);

CREATE INDEX idx_api_keys_merchant ON api_keys (merchant_id) WHERE status = 'ACTIVE';
CREATE INDEX idx_api_keys_expires ON api_keys (expires_at)
    WHERE expires_at IS NOT NULL AND status = 'ACTIVE';
CREATE INDEX idx_api_keys_prefix_live ON api_keys (key_prefix)
    WHERE status IN ('ACTIVE', 'ROTATION_PENDING');

CREATE TYPE api_scope AS ENUM (
    'payment:write', 'payment:read',
    'refund:write', 'refund:read',
    'customer:write', 'customer:read',
    'webhook:write', 'webhook:read',
    'settlement:read',
    'apikey:write', 'apikey:read',
    'otp:request'
);

CREATE TABLE api_key_scopes (
    key_id              TEXT NOT NULL REFERENCES api_keys(key_id),
    scope               api_scope NOT NULL,
    PRIMARY KEY (key_id, scope)
);
