-- 0147_npsb_saf_stan.sql — durable NPSB STAN allocation + SAF reversal queue.
--
-- spec/11 §A requires STAN uniqueness inside (connector, business_date,
-- session_ref) and store-and-forward reversals that never disappear before an
-- acknowledged 0430. These tables move that rail state out of process memory.

CREATE TABLE IF NOT EXISTS connector_npsb_stan_counters (
    connector_id    TEXT    NOT NULL,
    business_date   DATE    NOT NULL,
    session_ref     TEXT    NOT NULL,
    next_stan       INTEGER NOT NULL CHECK (next_stan BETWEEN 1 AND 999999),
    updated_at      TIMESTAMPTZ NOT NULL,
    schema_version  SMALLINT NOT NULL DEFAULT 1,
    PRIMARY KEY (connector_id, business_date, session_ref)
);

CREATE TABLE IF NOT EXISTS connector_npsb_stan_allocations (
    allocation_id   TEXT PRIMARY KEY,
    connector_id    TEXT NOT NULL,
    business_date   DATE NOT NULL,
    session_ref     TEXT NOT NULL,
    stan            TEXT NOT NULL CHECK (stan ~ '^[0-9]{6}$'),
    rrn             TEXT NOT NULL,
    instruction_id  TEXT,
    connector_ref   TEXT,
    allocated_at    TIMESTAMPTZ NOT NULL,
    schema_version  SMALLINT NOT NULL DEFAULT 1,
    UNIQUE (connector_id, business_date, session_ref, stan)
);

CREATE INDEX IF NOT EXISTS connector_npsb_stan_ref_idx
    ON connector_npsb_stan_allocations(connector_ref)
    WHERE connector_ref IS NOT NULL;
CREATE INDEX IF NOT EXISTS connector_npsb_stan_stan_idx
    ON connector_npsb_stan_allocations(stan, allocated_at DESC);

CREATE TABLE IF NOT EXISTS connector_npsb_saf_items (
    saf_id                    TEXT PRIMARY KEY,
    connector_id              TEXT NOT NULL,
    connector_ref             TEXT NOT NULL,
    original_stan             TEXT NOT NULL CHECK (original_stan ~ '^[0-9]{6}$'),
    reversal_fields           JSONB NOT NULL,
    reversal_mti              TEXT NOT NULL CHECK (reversal_mti IN ('0420','0421')),
    attempt_count             INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    acked                     BOOLEAN NOT NULL DEFAULT FALSE,
    queued_at                 TIMESTAMPTZ,
    acked_at                  TIMESTAMPTZ,
    acked_rail_transaction_id TEXT,
    ack_raw_response_hash     TEXT,
    updated_at                TIMESTAMPTZ NOT NULL,
    schema_version            SMALLINT NOT NULL DEFAULT 1,
    UNIQUE (connector_id, connector_ref, original_stan),
    CHECK (
        (acked = FALSE AND acked_at IS NULL)
        OR (acked = TRUE AND acked_at IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS connector_npsb_saf_pending_idx
    ON connector_npsb_saf_items(connector_id, queued_at)
    WHERE acked = FALSE;
CREATE INDEX IF NOT EXISTS connector_npsb_saf_ref_idx
    ON connector_npsb_saf_items(connector_ref);
