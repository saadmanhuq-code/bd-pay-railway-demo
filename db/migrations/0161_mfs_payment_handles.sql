-- 0161_mfs_payment_handles.sql — durable MFS rail handles (CONN-02 / BP-G1).
--
-- bKash paymentID, Nagad paymentReferenceId, and Rocket sessionkey must survive
-- process restart so query_status / IPN recovery do not report not_received for
-- a payment that already succeeded at the provider.

CREATE TABLE IF NOT EXISTS connector_mfs_payment_handles (
    handle_id       TEXT PRIMARY KEY,
    connector_id    TEXT NOT NULL,
    connector_ref   TEXT NOT NULL,
    rail_handle     TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL,
    updated_at      TIMESTAMPTZ NOT NULL,
    schema_version  SMALLINT NOT NULL DEFAULT 1,
    UNIQUE (connector_id, connector_ref)
);

CREATE INDEX IF NOT EXISTS connector_mfs_payment_handles_ref_idx
    ON connector_mfs_payment_handles(connector_ref);
