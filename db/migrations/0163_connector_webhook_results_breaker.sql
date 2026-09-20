-- 0163_connector_webhook_results_breaker.sql — durable connector webhook /
-- results / breaker stores (CONN-03 / BP-G4c).
--
-- Canonical shapes already land in 0050_connectors.sql (with connector_registry
-- FKs). This migration is IF NOT EXISTS + FK-free so isolated store/integration
-- tests can apply 0163 alone; on a full migrate chain 0050 wins and this is a
-- no-op. PostgresWebhookInboundStore / PostgresConnectorResultStore /
-- PostgresBreakerStateStore target these table names.

CREATE TABLE IF NOT EXISTS circuit_breaker_states (
    breaker_id            TEXT PRIMARY KEY,
    connector_id          TEXT NOT NULL UNIQUE,
    state                 TEXT NOT NULL DEFAULT 'CLOSED'
        CHECK (state IN ('CLOSED','OPEN','HALF_OPEN')),
    consecutive_failures  INTEGER NOT NULL DEFAULT 0,
    window_started_at     TIMESTAMPTZ,
    opened_at             TIMESTAMPTZ,
    half_open_successes   INTEGER NOT NULL DEFAULT 0,
    half_open_probes      INTEGER NOT NULL DEFAULT 0,
    failure_threshold     INTEGER NOT NULL DEFAULT 5,
    window_s              INTEGER NOT NULL DEFAULT 60,
    cooldown_s            INTEGER NOT NULL DEFAULT 30,
    probe_quota           INTEGER NOT NULL DEFAULT 3,
    updated_at            TIMESTAMPTZ NOT NULL,
    schema_version        SMALLINT NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS connector_results (
    result_id            TEXT PRIMARY KEY,
    instruction_id       TEXT NOT NULL,
    connector_ref        TEXT NOT NULL,
    connector_id         TEXT NOT NULL,
    attempt_no           INTEGER NOT NULL DEFAULT 1,
    call_kind            TEXT NOT NULL
        CHECK (call_kind IN (
            'SUBMIT','QUERY_STATUS','REVERSE','WEBHOOK','MANUAL','HEALTH',
            'BATCH_SUBMIT','BATCH_STATUS'
        )),
    status               TEXT NOT NULL
        CHECK (status IN (
            'success','pending','failed','rejected','timed_out','reversed'
        )),
    rail_transaction_id  TEXT,
    responded_at         TIMESTAMPTZ,
    error_code           TEXT,
    raw_response_hash    TEXT NOT NULL,
    raw_response_pointer TEXT NOT NULL,
    mode_at_call         TEXT NOT NULL,
    webhook_inbound_id   TEXT,
    produced_at          TIMESTAMPTZ NOT NULL,
    schema_version       SMALLINT NOT NULL DEFAULT 1,
    UNIQUE (connector_id, connector_ref, call_kind, attempt_no)
);

CREATE INDEX IF NOT EXISTS idx_cres_ref_0163
    ON connector_results (connector_ref, produced_at DESC);
CREATE INDEX IF NOT EXISTS idx_cres_rail_txn_0163
    ON connector_results (rail_transaction_id)
    WHERE rail_transaction_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS webhook_inbound (
    inbound_id           TEXT PRIMARY KEY,
    connector_id         TEXT NOT NULL,
    content_hash         TEXT NOT NULL,
    headers_hash         TEXT NOT NULL,
    raw_pointer          TEXT NOT NULL,
    source_ip_hash       TEXT NOT NULL,
    status               TEXT NOT NULL DEFAULT 'RECEIVED'
        CHECK (status IN (
            'RECEIVED','VERIFIED','REJECTED','DUPLICATE','PARSED',
            'HANDED_OFF','PROCESSED','PARKED'
        )),
    reject_reason        TEXT,
    parsed_connector_ref TEXT,
    result_id            TEXT,
    received_at          TIMESTAMPTZ NOT NULL,
    processed_at         TIMESTAMPTZ,
    schema_version       SMALLINT NOT NULL DEFAULT 1,
    UNIQUE (connector_id, content_hash)
);
