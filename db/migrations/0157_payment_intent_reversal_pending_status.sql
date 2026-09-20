-- 0157_payment_intent_reversal_pending_status.sql
-- Add a no-money-side-effect claim state for execute_reversal dispatch.

ALTER TYPE payment_intent_status
    ADD VALUE IF NOT EXISTS 'REVERSAL_PENDING' AFTER 'REVERSAL_INITIATED';
