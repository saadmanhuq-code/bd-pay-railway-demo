-- 0083_gateway_idempotency_fk.sql — close the deferred FK from errata PR-2.
--
-- Migration 0002 (platform lane) declared idempotency_keys.key_id without
-- its spec/01 FK because api_keys did not exist yet at that point in the
-- migration order. api_keys now exists (0080); the constraint lands here.
-- Customer-JWT rows (key_id IS NULL) are unaffected.

ALTER TABLE idempotency_keys
    ADD CONSTRAINT idempotency_keys_key_id_fkey
    FOREIGN KEY (key_id) REFERENCES api_keys(key_id);
