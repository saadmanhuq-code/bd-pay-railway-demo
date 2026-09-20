-- 0152_account_holds_open_reference_unique.sql — one open hold per reference.
--
-- LedgerService.open_hold is idempotent by reference_id. This partial unique
-- index makes that contract durable under concurrent writers and external SQL.

CREATE UNIQUE INDEX IF NOT EXISTS holds_open_reference_unique_idx
    ON ledger.account_holds(reference_id)
    WHERE status = 'OPEN';
