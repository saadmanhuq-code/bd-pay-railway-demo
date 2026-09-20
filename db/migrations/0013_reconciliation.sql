-- 0013_reconciliation.sql — reconciliation records + exceptions (spec/04).
--
-- Errata applied:
--  * LA-L1 (E1 class): unpartitioned with true PKs (the spec's RANGE
--    partitioning + single-column PKs is impossible in Postgres 16).

-- ============================================================
-- RECONCILIATION_RECORDS — one per line in a rail settlement file.
-- ============================================================
CREATE TABLE IF NOT EXISTS ledger.reconciliation_records (
    record_id               TEXT PRIMARY KEY,   -- recon_<sha256[:24]>
    batch_id                TEXT REFERENCES ledger.settlement_batches(batch_id),
    rail                    TEXT NOT NULL,
    rail_transaction_id     TEXT,
    rail_amount_minor       BIGINT,
    rail_direction          TEXT,
    rail_effective_date     DATE,
    rail_merchant_ref       TEXT,
    raw_line_hash           TEXT NOT NULL,
    parse_failed            BOOLEAN NOT NULL DEFAULT FALSE,
    parse_error             TEXT,
    match_status            TEXT NOT NULL DEFAULT 'INGESTED',
    match_level             SMALLINT,
    matched_instruction_id  TEXT REFERENCES ledger.settlement_instructions(instruction_id),
    delta_minor             BIGINT,
    ingested_at             TIMESTAMPTZ NOT NULL,
    matched_at              TIMESTAMPTZ,
    produced_by             TEXT NOT NULL,
    schema_version          SMALLINT NOT NULL DEFAULT 1,

    CONSTRAINT rrecon_status_check CHECK (match_status IN (
        'INGESTED','MATCHED','TIMING_GAP','UNMATCHED','AMOUNT_MISMATCH','DEAD_LETTER'
    )),
    CONSTRAINT rrecon_direction_check CHECK (
        rail_direction IS NULL OR rail_direction IN ('CREDIT','DEBIT')
    ),
    CONSTRAINT rrecon_match_level_check CHECK (
        match_level IS NULL OR match_level IN (1, 2)
    )
);

CREATE INDEX IF NOT EXISTS rrecon_batch_status_idx
    ON ledger.reconciliation_records (batch_id, match_status);
CREATE INDEX IF NOT EXISTS rrecon_rail_txid_idx
    ON ledger.reconciliation_records (rail_transaction_id)
    WHERE rail_transaction_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS rrecon_timing_gap_idx
    ON ledger.reconciliation_records (ingested_at)
    WHERE match_status = 'TIMING_GAP';

-- ============================================================
-- RECONCILIATION_EXCEPTIONS — one per unmatched/mismatched record.
-- ============================================================
CREATE TABLE IF NOT EXISTS ledger.reconciliation_exceptions (
    exception_id            TEXT PRIMARY KEY,   -- rexc_<sha256[:24]>
    record_id               TEXT NOT NULL REFERENCES ledger.reconciliation_records(record_id),
    resolution_tier         TEXT NOT NULL,
    reason_code             TEXT NOT NULL,
    rail_amount_minor       BIGINT,
    internal_amount_minor   BIGINT,
    delta_minor             BIGINT,
    status                  TEXT NOT NULL DEFAULT 'OPEN',
    assigned_to             TEXT,
    raised_at               TIMESTAMPTZ NOT NULL,
    due_by                  TIMESTAMPTZ NOT NULL,
    resolved_at             TIMESTAMPTZ,
    closed_at               TIMESTAMPTZ,
    resolution_action       TEXT,
    adjustment_amount_minor BIGINT,
    journal_entry_id        TEXT,  -- FK -> journal_entries (app-enforced; append-only table)
    approval_request_id     TEXT,  -- FK -> approval_requests (platform; app-enforced)
    notes                   TEXT,
    supporting_ref          TEXT,
    resolved_by             TEXT,
    aml_alert_id            TEXT,  -- FK -> aml_alerts (spec/06; app-enforced)
    produced_by             TEXT NOT NULL,
    schema_version          SMALLINT NOT NULL DEFAULT 1,

    CONSTRAINT rexc_tier_check CHECK (resolution_tier IN (
        'AUTO_TIMING_GAP','FINANCE_QUEUE','DEAD_LETTER'
    )),
    CONSTRAINT rexc_status_check CHECK (status IN (
        'OPEN','IN_REVIEW','RESOLVED','ESCALATED','CLOSED'
    )),
    CONSTRAINT rexc_action_check CHECK (
        resolution_action IS NULL OR resolution_action IN (
            'WRITE_OFF','CHASE_RAIL','REVERSE_INTERNAL','SUSPENSE_TRANSFER','CLOSE_NO_ACTION'
        )
    )
);

CREATE INDEX IF NOT EXISTS rexc_status_tier_idx
    ON ledger.reconciliation_exceptions (status, resolution_tier, due_by);
CREATE INDEX IF NOT EXISTS rexc_record_id_idx
    ON ledger.reconciliation_exceptions (record_id);
CREATE INDEX IF NOT EXISTS rexc_open_sla_idx
    ON ledger.reconciliation_exceptions (due_by)
    WHERE status IN ('OPEN','IN_REVIEW');

-- ============================================================
-- POSTING EXTENSION (spec/04): rexc_id traceability for suspense postings.
-- Nullable, append-only column; populated only on settlement/recon postings.
-- ============================================================
ALTER TABLE ledger.postings ADD COLUMN IF NOT EXISTS rexc_id TEXT;
CREATE INDEX IF NOT EXISTS postings_rexc_id_idx
    ON ledger.postings (rexc_id) WHERE rexc_id IS NOT NULL;

GRANT SELECT, INSERT, UPDATE ON ledger.reconciliation_records    TO ledger_app_role;
GRANT SELECT, INSERT, UPDATE ON ledger.reconciliation_exceptions TO ledger_app_role;
GRANT SELECT ON ledger.reconciliation_records, ledger.reconciliation_exceptions TO ledger_ro;
