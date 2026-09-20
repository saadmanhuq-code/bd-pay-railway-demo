-- 0162_mfs_idempotency_stores.sql — durable connector idempotency (CONN-03 / BP-G4b).
--
-- Adapter-local + runner IdempotencyStore: recorded ConnectorResult replays and
-- write-ahead DispatchIntent breadcrumbs must survive process restart so a crash
-- mid-dispatch never loses proof that connector_ref may have reached the rail,
-- and CERT-01 replays stay first-write-wins across instances.

CREATE TABLE IF NOT EXISTS connector_idempotency_results (
    connector_id          TEXT NOT NULL,
    connector_ref         TEXT NOT NULL,
    call_kind             TEXT NOT NULL,
    instruction_id        TEXT NOT NULL,
    status                TEXT NOT NULL
        CHECK (status IN ('success','pending','failed','rejected','timed_out','reversed')),
    rail_transaction_id   TEXT,
    responded_at          TEXT,
    error_code            TEXT,
    raw_response_hash     TEXT NOT NULL,
    recorded_at           TIMESTAMPTZ NOT NULL,
    schema_version        SMALLINT NOT NULL DEFAULT 1,
    PRIMARY KEY (connector_id, connector_ref, call_kind)
);

CREATE INDEX IF NOT EXISTS connector_idempotency_results_ref_idx
    ON connector_idempotency_results(connector_ref);

CREATE TABLE IF NOT EXISTS connector_dispatch_intents (
    connector_id          TEXT NOT NULL,
    connector_ref         TEXT NOT NULL,
    call_kind             TEXT NOT NULL,
    instruction_id        TEXT NOT NULL,
    recorded_at           TIMESTAMPTZ NOT NULL,
    schema_version        SMALLINT NOT NULL DEFAULT 1,
    PRIMARY KEY (connector_id, connector_ref, call_kind)
);

CREATE INDEX IF NOT EXISTS connector_dispatch_intents_ref_idx
    ON connector_dispatch_intents(connector_ref);
