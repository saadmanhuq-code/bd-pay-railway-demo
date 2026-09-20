-- Shared durable binary object store.
--
-- Used for deterministic evidence artifacts such as dossier ZIPs, connector
-- raw archives, and goAML XML payloads. The store is write-once by key at the
-- application layer: a same-key/same-bytes put is idempotent; same-key with
-- different bytes is refused.

CREATE TABLE IF NOT EXISTS object_blobs (
    object_key TEXT PRIMARY KEY,
    data BYTEA NOT NULL,
    sha256_hex TEXT NOT NULL,
    size_bytes BIGINT NOT NULL CHECK (size_bytes >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    schema_version SMALLINT NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_object_blobs_created_at
    ON object_blobs (created_at);
