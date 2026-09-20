-- 0002_idempotency_keys.sql — durable Idempotency-Key records
-- (spec/01 §Data model IDEMPOTENCY KEYS; claim algorithm spec/02 §660-667).
-- Keys are scoped per (idempotency_key, key_id, request_path) — NEVER a bare
-- unique on the key alone.
--
-- Errata PR-2 (SPEC_ERRATA-LANE-A-platform-runtime.md): spec/01 declares
-- key_id REFERENCES api_keys(key_id); api_keys is a gateway-schema table that
-- does not exist at this point in migration order, so the FK is deferred to a
-- gateway-owned ALTER TABLE migration. Cross-package integrity is
-- application-enforced meanwhile (the same posture spec/01 itself takes for
-- webhook_endpoints.merchant_id).
--
-- Errata PR-3a (same file): spec/01's plain UNIQUE leaves customer-JWT rows
-- (key_id IS NULL) non-deduplicating because SQL NULLs compare distinct.
-- NULLS NOT DISTINCT (Postgres 15+) restores the intended single-record-per-
-- (key, caller, path) invariant; pinned by the integration test.

CREATE TABLE idempotency_keys (
    idem_id             TEXT        PRIMARY KEY,          -- idem_<sha256(key_id+key)[:24]>
    idempotency_key     TEXT        NOT NULL,             -- client-supplied header value
    key_id              TEXT,                             -- api_keys(key_id); FK deferred (PR-2)
    customer_id         TEXT,                             -- customers FK is cross-package; app-enforced
    http_method         TEXT        NOT NULL,             -- GET|POST|PUT|PATCH|DELETE
    request_path        TEXT        NOT NULL,             -- e.g. '/v1/payment-intents'
    request_body_hash   TEXT        NOT NULL,             -- sha256(canonical_json(request_body))
    response_status     SMALLINT,                         -- NULL until first response
    response_body_hash  TEXT,                             -- sha256(canonical_json(response_body))
    response_body       JSONB,                            -- stored for replay; size-limited to 64KB
    state               TEXT        NOT NULL DEFAULT 'IN_FLIGHT'
        CHECK (state IN ('IN_FLIGHT', 'COMPLETED', 'CONFLICTED')),
    locked_at           TIMESTAMPTZ,                      -- kernel claim marker; NULL = not held
    created_at          TIMESTAMPTZ NOT NULL,
    completed_at        TIMESTAMPTZ,
    expires_at          TIMESTAMPTZ NOT NULL,             -- created_at + 24h; GC by scheduler
    schema_version      SMALLINT    NOT NULL DEFAULT 1,
    CONSTRAINT idempotency_key_path_unique
        UNIQUE NULLS NOT DISTINCT (idempotency_key, key_id, request_path)
);

CREATE INDEX idx_idem_key_lookup ON idempotency_keys (idempotency_key, key_id, request_path)
    WHERE state = 'IN_FLIGHT' OR state = 'COMPLETED';
CREATE INDEX idx_idem_expires ON idempotency_keys (expires_at)
    WHERE state != 'CONFLICTED';
