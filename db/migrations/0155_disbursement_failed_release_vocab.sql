-- 0155_disbursement_failed_release_vocab.sql
--
-- Adds the disbursement_failed_release entry_type to the journal_entries CHECK
-- constraint so the real Postgres-backed LedgerService accepts the ledger leg
-- that DisbursementService._post_item_failed posts when mark_item_failed is
-- called (spec/17 VX4 FAILED terminal state hold-release fix).
--
-- Pattern follows 0140_offer_ledger_vocab.sql: DROP + re-ADD the full set
-- (Postgres CHECK constraints cannot be appended to in-place). The list below
-- is the canonical set from 0140 (0120 + offer types) plus the new additive
-- value; no existing value is removed.

ALTER TABLE ledger.journal_entries
    DROP CONSTRAINT IF EXISTS je_entry_type_check;

ALTER TABLE ledger.journal_entries
    ADD CONSTRAINT je_entry_type_check CHECK (
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
            -- carried forward from 0120_disbursement_batches.sql:
            'disbursement_funded','disbursement_item_paid','disbursement_item_returned',
            -- carried forward from 0140_offer_ledger_vocab.sql:
            'offer_commission_collected','offer_commission_reversed',
            'offer_subsidy_granted','offer_subsidy_reversed',
            -- spec/17 FAILED terminal state hold-release (0155):
            'disbursement_failed_release'
        )
    );
