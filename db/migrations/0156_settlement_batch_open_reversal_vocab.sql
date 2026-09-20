-- migration 0156: extend je_entry_type_check to include settlement_batch_open_reversal
--
-- spec/18 PT-C fix: SettlementEngine.cancel() and manual_retry()-at-max now post
-- settlement_batch_open_reversal (DR transit_account / CR TCSA) to unstage the
-- float that was moved into TCSA at assign_due_instructions time.  Without this
-- vocab extension the LedgerService._validate_entry_spec guard rejects the new
-- entry_type and the constraint also blocks the Postgres store.
--
-- The je_entry_type_check constraint is DROP + ADD (additive list only; this is
-- the established pattern: see 0120_disbursement_batches.sql, 0140_offer_ledger_vocab.sql).
-- Every previously valid value is carried forward verbatim.

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
            -- carried forward from 0155_disbursement_failed_release_vocab.sql:
            'disbursement_failed_release',
            -- spec/18 PT-C: settlement cancel/fail reversal leg:
            'settlement_batch_open_reversal'
        )
    );
