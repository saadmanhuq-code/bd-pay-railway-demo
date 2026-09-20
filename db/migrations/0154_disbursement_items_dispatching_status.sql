-- AR-2 (claim-before-rail): allow the DISPATCHING claim state on
-- disbursement_items so the atomic claim CAS (QUEUED -> DISPATCHING, before the
-- rail call) is permitted. Appended as a new migration rather than editing
-- 0120 in place (migrations are append-only): an already-migrated database
-- picks up only this ALTER, a fresh database applies 0120 then this and ends in
-- the same state.

ALTER TABLE disbursement_items
    DROP CONSTRAINT IF EXISTS disbursement_items_status_check;

ALTER TABLE disbursement_items
    ADD CONSTRAINT disbursement_items_status_check
    CHECK (status IN ('QUEUED','DISPATCHING','DISPATCHED','PAID','RETURNED','FAILED'));
