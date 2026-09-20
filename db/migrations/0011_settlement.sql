-- 0011_settlement.sql — settlement batches, instructions, fee rules (spec/04).
--
-- Errata applied:
--  * LA-L1 (E1 class): spec/04 declares settlement_instructions /
--    reconciliation_* PARTITION BY RANGE together with single-column PRIMARY
--    KEYs and UNIQUE(connector_ref) — impossible in Postgres 16 (every unique
--    constraint on a partitioned table must include the partition key).
--    Tables ship UNPARTITIONED with the true PK/UNIQUE constraints.
--  * LA-L9: settlement_instructions.batch_id is NULLABLE while QUEUED behind
--    release holds (the spec's NOT NULL contradicts its own "not added to any
--    dispatch batch until released" rule); a CHECK pins the invariant that
--    every non-QUEUED instruction belongs to a batch.

-- ============================================================
-- SETTLEMENT_BATCHES — one per rail per settlement cycle.
-- ============================================================
CREATE TABLE IF NOT EXISTS ledger.settlement_batches (
    batch_id            TEXT PRIMARY KEY,       -- sbatch_<sha256[:24]>
    rail                TEXT NOT NULL,
    cycle_date          DATE NOT NULL,
    session             TEXT,
    status              TEXT NOT NULL DEFAULT 'OPEN',
    instruction_count   INT NOT NULL DEFAULT 0,
    total_credit_minor  BIGINT NOT NULL DEFAULT 0,
    total_debit_minor   BIGINT NOT NULL DEFAULT 0,
    currency            CHAR(3) NOT NULL DEFAULT 'BDT',
    retry_count         SMALLINT NOT NULL DEFAULT 0,
    two_eyes_required   BOOLEAN NOT NULL DEFAULT FALSE,
    approval_request_id TEXT,   -- FK -> approval_requests (platform; application-enforced)
    held_at             TIMESTAMPTZ,
    held_tcsa_snapshot_id TEXT, -- FK -> tcsa_snapshots (0014; application-enforced)
    sponsor_bank_ref    TEXT,
    recon_file_pointer  TEXT,
    recon_file_hash     TEXT,
    connector_ref       TEXT UNIQUE,
    created_at          TIMESTAMPTZ NOT NULL,
    cutoff_at           TIMESTAMPTZ,
    dispatched_at       TIMESTAMPTZ,
    confirmed_at        TIMESTAMPTZ,
    failed_at           TIMESTAMPTZ,
    cancelled_at        TIMESTAMPTZ,
    fail_reason_code    TEXT,
    produced_by         TEXT NOT NULL,
    schema_version      SMALLINT NOT NULL DEFAULT 1,

    CONSTRAINT sbatch_status_check CHECK (status IN (
        'OPEN','PENDING_DISPATCH','HELD','DISPATCHED','CONFIRMED','FAILED','CANCELLED'
    )),
    CONSTRAINT sbatch_held_consistency CHECK (
        (status != 'HELD') OR (held_at IS NOT NULL)
    ),
    CONSTRAINT sbatch_rail_check CHECK (rail IN (
        'beftn_batch_v2','npsb_iso8583_v28','rtgs_iso20022_v1',
        'bkash_pgw_v2','nagad_pgw_v33','rocket_aggregator_v1','card_acquirer_v1'
    )),
    CONSTRAINT sbatch_session_check CHECK (
        session IS NULL OR session IN ('morning','afternoon','eod')
    ),
    CONSTRAINT sbatch_amounts_nonneg CHECK (
        total_credit_minor >= 0 AND total_debit_minor >= 0
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS sbatch_rail_cycle_session_uidx
    ON ledger.settlement_batches (rail, cycle_date, (COALESCE(session, '')))
    WHERE status NOT IN ('CANCELLED');

CREATE INDEX IF NOT EXISTS sbatch_status_idx
    ON ledger.settlement_batches (status, rail, cycle_date);

-- ============================================================
-- FEE_RULES — MDR configuration; source of truth for fee resolution.
-- ============================================================
CREATE TABLE IF NOT EXISTS ledger.fee_rules (
    rule_id             TEXT PRIMARY KEY,       -- frule_<sha256[:24]>
    rule_name           TEXT NOT NULL,
    merchant_id         TEXT,
    method              TEXT NOT NULL,
    mcc                 TEXT,
    fee_type            TEXT NOT NULL,
    rate_bps            SMALLINT,
    flat_amount_minor   BIGINT,
    regulated           BOOLEAN NOT NULL DEFAULT FALSE,
    valid_from          TIMESTAMPTZ NOT NULL,
    valid_to            TIMESTAMPTZ,
    created_by          TEXT NOT NULL,
    approved_by         TEXT,
    created_at          TIMESTAMPTZ NOT NULL,
    schema_version      SMALLINT NOT NULL DEFAULT 1,

    CONSTRAINT frule_method_check CHECK (method IN (
        'BANGLA_QR','NPSB_IBFT','BEFTN_CREDIT','RTGS_GROSS',
        'BKASH','NAGAD','ROCKET','CARD_DOMESTIC','CARD_INTERNATIONAL','CARD_AMEX'
    )),
    CONSTRAINT frule_fee_type_check CHECK (fee_type IN (
        'PERCENTAGE','FLAT_PAISA','FLAT_PLUS_PERCENTAGE'
    )),
    CONSTRAINT frule_rate_bps_range CHECK (
        rate_bps IS NULL OR (rate_bps >= 0 AND rate_bps <= 10000)
    ),
    CONSTRAINT frule_flat_nonneg CHECK (flat_amount_minor IS NULL OR flat_amount_minor >= 0)
);

-- Seed: the BB-regulated Bangla QR 1.15% rate (spec/04, binding).
INSERT INTO ledger.fee_rules (
    rule_id, rule_name, merchant_id, method, mcc, fee_type, rate_bps, flat_amount_minor,
    regulated, valid_from, valid_to, created_by, approved_by, created_at
) VALUES (
    'frule_seed_bangla_qr_115bps_00', 'Bangla QR Regulated MDR', NULL, 'BANGLA_QR', NULL,
    'PERCENTAGE', 115, NULL, TRUE, '2025-02-01T00:00:00Z', NULL, 'system', 'system',
    '2025-02-01T00:00:00Z'
) ON CONFLICT (rule_id) DO NOTHING;

-- ============================================================
-- SETTLEMENT_INSTRUCTIONS — one per payment obligation (unpartitioned, LA-L1).
-- ============================================================
CREATE TABLE IF NOT EXISTS ledger.settlement_instructions (
    instruction_id          TEXT PRIMARY KEY,   -- sinst_<sha256[:24]>
    batch_id                TEXT REFERENCES ledger.settlement_batches(batch_id),
    payment_intent_id       TEXT NOT NULL,      -- FK -> payment_intents (spec/02; app-enforced)
    merchant_id             TEXT NOT NULL,      -- FK -> merchants (spec/08; app-enforced)
    direction               TEXT NOT NULL,
    amount_minor            BIGINT NOT NULL,
    fee_minor               BIGINT NOT NULL DEFAULT 0,
    net_payout_minor        BIGINT NOT NULL,
    fee_rule_id             TEXT REFERENCES ledger.fee_rules(rule_id),
    currency                CHAR(3) NOT NULL DEFAULT 'BDT',
    rail                    TEXT NOT NULL,
    beneficiary_account_ref TEXT NOT NULL,
    mcc_category            TEXT NOT NULL,
    high_value              BOOLEAN NOT NULL DEFAULT FALSE,
    earliest_release_at     TIMESTAMPTZ NOT NULL,
    latest_release_at       TIMESTAMPTZ NOT NULL,
    delivery_hold_cleared   BOOLEAN NOT NULL DEFAULT FALSE,
    aml_hold                BOOLEAN NOT NULL DEFAULT FALSE,
    status                  TEXT NOT NULL DEFAULT 'QUEUED',
    rail_transaction_id     TEXT,
    return_reason_code      TEXT,
    connector_ref           TEXT UNIQUE NOT NULL,
    created_at              TIMESTAMPTZ NOT NULL,
    submitted_at            TIMESTAMPTZ,
    settled_at              TIMESTAMPTZ,
    returned_at             TIMESTAMPTZ,
    failed_at               TIMESTAMPTZ,
    produced_by             TEXT NOT NULL,
    schema_version          SMALLINT NOT NULL DEFAULT 1,

    CONSTRAINT sinst_status_check CHECK (status IN (
        'QUEUED','SUBMITTED','SETTLED','RETURNED','CANCELLED','FAILED'
    )),
    CONSTRAINT sinst_direction_check CHECK (direction IN ('CREDIT','DEBIT')),
    CONSTRAINT sinst_amounts_check CHECK (
        amount_minor > 0
        AND fee_minor >= 0
        AND net_payout_minor >= 0
        AND net_payout_minor = amount_minor - fee_minor
    ),
    CONSTRAINT sinst_release_order CHECK (earliest_release_at <= latest_release_at),
    CONSTRAINT sinst_mcc_check CHECK (mcc_category IN (
        'DAILY_ESSENTIAL','DIRECT_MERCHANT','OTHER'
    )),
    -- LA-L9: only QUEUED instructions may be batch-less.
    CONSTRAINT sinst_batch_assignment CHECK (
        batch_id IS NOT NULL OR status = 'QUEUED'
    )
);

CREATE INDEX IF NOT EXISTS sinst_batch_idx
    ON ledger.settlement_instructions (batch_id, status);
CREATE INDEX IF NOT EXISTS sinst_payment_intent_idx
    ON ledger.settlement_instructions (payment_intent_id);
CREATE INDEX IF NOT EXISTS sinst_merchant_release_idx
    ON ledger.settlement_instructions (merchant_id, earliest_release_at)
    WHERE status = 'QUEUED' AND delivery_hold_cleared = TRUE AND aml_hold = FALSE;
CREATE INDEX IF NOT EXISTS sinst_rail_txid_idx
    ON ledger.settlement_instructions (rail_transaction_id)
    WHERE rail_transaction_id IS NOT NULL;

-- The FSM tables are mutable state tables (UPDATE permitted to the app role);
-- the money trail lives in the append-only ledger tables of 0010.
GRANT SELECT, INSERT, UPDATE ON ledger.settlement_batches      TO ledger_app_role;
GRANT SELECT, INSERT, UPDATE ON ledger.settlement_instructions TO ledger_app_role;
GRANT SELECT, INSERT         ON ledger.fee_rules               TO ledger_app_role;
GRANT SELECT ON ledger.settlement_batches, ledger.settlement_instructions, ledger.fee_rules
    TO ledger_ro;
