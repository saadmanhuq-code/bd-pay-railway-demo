-- spec/17 VX4 bulk disbursement batches.
-- Prefixes: dbat (DisbursementBatch), ditm (DisbursementItem).

CREATE TABLE IF NOT EXISTS disbursement_batches (
    batch_id              TEXT PRIMARY KEY,
    merchant_id           TEXT NOT NULL,
    client_batch_ref      TEXT NOT NULL,
    item_count            BIGINT NOT NULL CHECK (
        item_count > 0 OR status = 'VALIDATION_FAILED'
    ),
    total_amount_minor    BIGINT NOT NULL CHECK (total_amount_minor >= 0),
    fees_total_minor      BIGINT NOT NULL CHECK (fees_total_minor >= 0),
    status                TEXT NOT NULL CHECK (
        status IN (
            'DRAFT','VALIDATED','VALIDATION_FAILED','PENDING_APPROVAL','APPROVED',
            'DISPATCHING','COMPLETED','PARTIALLY_RETURNED','CANCELLED'
        )
    ),
    maker_user_id         TEXT NOT NULL,
    approval_request_id   TEXT,
    reconciliation_tag    TEXT NOT NULL,
    created_at            TIMESTAMPTZ NOT NULL,
    submitted_at          TIMESTAMPTZ,
    approved_at           TIMESTAMPTZ,
    dispatched_at         TIMESTAMPTZ,
    completed_at          TIMESTAMPTZ,
    cancelled_at          TIMESTAMPTZ,
    schema_version        SMALLINT NOT NULL DEFAULT 1,
    UNIQUE (merchant_id, client_batch_ref)
);

CREATE INDEX IF NOT EXISTS disb_batches_merchant_status_idx
    ON disbursement_batches (merchant_id, status, created_at);
CREATE INDEX IF NOT EXISTS disb_batches_recon_tag_idx
    ON disbursement_batches (reconciliation_tag);
CREATE INDEX IF NOT EXISTS disb_batches_approval_idx
    ON disbursement_batches (approval_request_id)
    WHERE approval_request_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS disbursement_items (
    item_id                       TEXT PRIMARY KEY,
    batch_id                      TEXT NOT NULL REFERENCES disbursement_batches(batch_id),
    ordinal                       BIGINT NOT NULL,
    beneficiary_ref               TEXT NOT NULL,
    beneficiary_name_normalized   TEXT NOT NULL,
    rail                          TEXT NOT NULL CHECK (rail IN ('BEFTN_CREDIT','BKASH','NAGAD')),
    amount_minor                  BIGINT NOT NULL CHECK (amount_minor > 0),
    fee_minor                     BIGINT NOT NULL CHECK (fee_minor >= 0),
    purpose_code                  TEXT NOT NULL,
    item_ref                      TEXT NOT NULL,
    status                        TEXT NOT NULL CHECK (
        status IN ('QUEUED','DISPATCHED','PAID','RETURNED','FAILED')
    ),
    rail_transaction_id           TEXT,
    settled_at                    TIMESTAMPTZ,
    schema_version                SMALLINT NOT NULL DEFAULT 1,
    UNIQUE (batch_id, ordinal)
);

CREATE INDEX IF NOT EXISTS disb_items_batch_status_idx
    ON disbursement_items (batch_id, status, ordinal);
CREATE INDEX IF NOT EXISTS disb_items_item_ref_idx
    ON disbursement_items (batch_id, item_ref);

ALTER TABLE ledger.journal_entries DROP CONSTRAINT IF EXISTS je_entry_type_check;
ALTER TABLE ledger.journal_entries ADD CONSTRAINT je_entry_type_check CHECK (
    entry_type IN (
        'payment_authorized','payment_captured','payment_failed','payment_reversed',
        'refund_initiated','refund_settled','fee_collected',
        'hold_opened','hold_captured','hold_voided','hold_expired',
        'rolling_reserve_withheld','rolling_reserve_released',
        'settlement_batch_open','settlement_batch_close','settlement_confirmed',
        'tcsa_balance_update','participant_debit_cap_set','position_updated',
        'suspense_entry','suspense_cleared',
        'connector_clearing_in','connector_clearing_out',
        'adjustment_debit','adjustment_credit',
        'disbursement_funded','disbursement_item_paid','disbursement_item_returned'
    )
);
