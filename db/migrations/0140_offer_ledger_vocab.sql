-- 0140_offer_ledger_vocab.sql — spec/18 offer-engine ledger vocabulary (MONEY-02).
--
-- The offer ledger legs (bdpay/kernel/offers/ledger_legs.py) emit four
-- entry_types and post the platform side of deal commission / launch subsidy to
-- two dedicated chart accounts. The Python vocabulary (bdpay/ledger/types.py
-- ENTRY_TYPES + ACCOUNT_SUBTYPES) accepts them; this migration brings the
-- Postgres CHECK constraints into lockstep so a Postgres-backed ledger accepts
-- the same writes (audit 2026-06-13, MONEY-02 companion).
--
-- Numbering: 0140 is the next free slot. The 013x band is pinned to the single
-- offer-engine schema migration 0130 by errata S18-E6
-- (tests/kernel/offers/test_migrations_offers.py asserts no other 013x exists),
-- so this follow-on vocabulary migration takes the next free slot above it.
--
-- A Postgres CHECK constraint cannot be appended to — it must be dropped and
-- re-added with the full value set. The je_entry_type_check list below is the
-- CURRENT canonical set from 0120_disbursement_batches.sql (which itself
-- re-ALTERed 0010's list to add the three disbursement_* types) plus the four
-- additive offer values; this is a pure superset, no value is removed. The
-- accounts_subtype_check list mirrors 0010 (its only definition) plus the two
-- offer subtypes.

-- ============================================================
-- 1. journal_entries.je_entry_type_check — add the four offer entry_types
--    on top of the current canonical set (0120 = 0010 + disbursement_*).
-- ============================================================
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
            -- carried forward from 0120_disbursement_batches.sql (must NOT be dropped):
            'disbursement_funded','disbursement_item_paid','disbursement_item_returned',
            -- spec/18 offer engine ledger legs (MONEY-02):
            'offer_commission_collected','offer_commission_reversed',
            'offer_subsidy_granted','offer_subsidy_reversed'
        )
    );

-- ============================================================
-- 2. accounts.accounts_subtype_check — add the two offer system subtypes
--    (OFFER_COMMISSION_INCOME=INCOME, OFFER_MARKETING_EXPENSE=EXPENSE).
-- ============================================================
ALTER TABLE ledger.accounts
    DROP CONSTRAINT IF EXISTS accounts_subtype_check;

ALTER TABLE ledger.accounts
    ADD CONSTRAINT accounts_subtype_check CHECK (
        account_subtype IN (
            'SPONSOR_BANK_TCSA','IN_TRANSIT','FEE_RECEIVABLE','SUSPENSE','CONNECTOR_CLEARING',
            'CUSTOMER_FLOAT','CUSTOMER_HOLD_RESERVE','MERCHANT_SETTLEMENT',
            'MERCHANT_HOLD_RESERVE','MERCHANT_ROLLING_RESERVE','REFUND_RESERVE',
            'PARTICIPANT_POSITION','NET_DEBIT_CAP',
            'RETAINED_EARNINGS','PAID_UP_CAPITAL',
            'MDR_INCOME','SWITCHING_FEE_INCOME','INTERCHANGE_INCOME','FLOAT_INTEREST_INCOME',
            'SPONSOR_BANK_FEE','NETWORK_FEE','REFUND_EXPENSE','CONNECTIVITY_EXPENSE',
            -- spec/18 offer engine system accounts (MONEY-02):
            'OFFER_COMMISSION_INCOME','OFFER_MARKETING_EXPENSE'
        )
    );
