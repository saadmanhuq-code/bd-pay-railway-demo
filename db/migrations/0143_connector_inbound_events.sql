-- Durable inbound connector event queue.
--
-- The gateway verifies connector IPN signatures synchronously, then persists
-- the parsed ConnectorResult here so kernel workers can drain it without the
-- HTTP layer dropping the event on process restart.

CREATE TABLE IF NOT EXISTS connector_inbound_events (
    event_id TEXT PRIMARY KEY,
    connector_id TEXT NOT NULL,
    connector_ref TEXT NOT NULL,
    instruction_id TEXT NOT NULL,
    status_value TEXT NOT NULL
        CHECK (status_value IN ('success','pending','failed','rejected','timed_out','reversed')),
    rail_transaction_id TEXT,
    responded_at TEXT,
    error_code TEXT,
    raw_response_hash TEXT NOT NULL,
    processing_status TEXT NOT NULL DEFAULT 'RECEIVED'
        CHECK (processing_status IN ('RECEIVED', 'DISPATCHED', 'SKIPPED')),
    received_at TIMESTAMPTZ NOT NULL,
    dispatched_at TIMESTAMPTZ,
    schema_version SMALLINT NOT NULL DEFAULT 1,
    CONSTRAINT cie_unique_result UNIQUE (connector_id, connector_ref, raw_response_hash)
);

CREATE INDEX IF NOT EXISTS idx_cie_status
    ON connector_inbound_events (processing_status, received_at)
    WHERE processing_status = 'RECEIVED';

CREATE INDEX IF NOT EXISTS idx_cie_connector_ref
    ON connector_inbound_events (connector_id, connector_ref);
