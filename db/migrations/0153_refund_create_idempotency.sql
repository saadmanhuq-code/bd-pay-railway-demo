-- Persist refund-create idempotency at the kernel boundary.

ALTER TABLE refunds
    ADD COLUMN IF NOT EXISTS idempotency_key TEXT;

ALTER TABLE refunds
    ADD COLUMN IF NOT EXISTS instruction_id TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS rfnd_intent_idem_unique
    ON refunds (payment_intent_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;
