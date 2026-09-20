-- 0158_idempotency_principal_scope.sql — scope idempotency reservations by
-- the calling principal for non-merchant callers.
--
-- Migration 0002 scoped idempotency by (idempotency_key, key_id, request_path).
-- That correctly isolates merchant API keys, but key_id is NULL for customer
-- JWTs and operator sessions. The old NULLS NOT DISTINCT semantics therefore
-- allowed two different customers (or operators) to cross-replay the same
-- Idempotency-Key on shared static paths such as /v1/refunds, /v1/customers,
-- and /v1/exports/dossiers.
--
-- This migration adds scope_id (the principal's stable identity: key_id for
-- merchant keys, customer_id for customer JWTs, operator_id for operator
-- sessions, NULL for unauthenticated/public callers) and changes the unique
-- scope to (idempotency_key, scope_id, request_path). Merchant scoping is
-- unchanged because scope_id == key_id for merchant keys.

BEGIN;

ALTER TABLE idempotency_keys
    ADD COLUMN scope_id TEXT;

-- Backfill existing rows so the new unique constraint can be applied safely.
-- Merchant-key rows already have key_id set; customer rows have customer_id.
--
-- CUTOVER CAVEAT (operator rows): rows written by operator sessions before
-- this migration carry neither key_id nor customer_id, and the operator_id
-- was never stored on this table, so they CANNOT be attributed and stay
-- scope_id = NULL. Post-migration operator lookups use scope_id =
-- operator_id, so those legacy rows are UNREACHABLE to new requests: an
-- operator retrying a pre-migration Idempotency-Key inside the 24h TTL would
-- be treated as NEW and re-execute rather than replay. There is no column on
-- this table that can recover the writing operator's identity (only
-- key_id/customer_id exist pre-migration) — no UPDATE can express this
-- backfill.
--
-- Both-NULL is NOT always an operator row: any RoutePolicy with public=True
-- (bdpay/gateway/policy.py) lets a caller omit Authorization entirely
-- (bdpay/gateway/app.py _authenticate: "if authorization is None: if
-- policy.public: return None") -- not just routes with skip_auth=True. Two
-- routes combine public=True with idempotency_required=True today, verified
-- against the assembled production policy set (bdpay/app.py: build_policies()
-- plus offer_route_policies() + subscription_route_policies() +
-- disbursement_route_policies() + participant_route_policies() --
-- build_policies() alone is not the full route surface): customers.create
-- (POST /v1/customers, public onboarding) and sandbox.demo.diner_pay (POST
-- /v1/sandbox/demo/diner-pay). Both produce principal=None -> key_id/
-- customer_id NULL by design when the caller omits auth -- a permanent,
-- correct NULL scope both before and after this migration, not a
-- legacy-attribution gap. The guard below excludes both paths by name;
-- re-run the query above against the assembled policy set and extend this
-- list if a future route adds public=True + idempotency_required=True.
--
-- CUTOVER GUARD: rather than let the operator-attribution gap ship silently,
-- fail the migration closed if any such row is still live (unexpired) at
-- apply time. This forces the operational precondition below to be met
-- explicitly instead of relying on a comment: apply only on an
-- empty/expired idempotency_keys table, or with operator idempotent writes
-- quiesced for one TTL window (24h) first. There is no live production
-- deployment at the time this migration was authored, so the guard does not
-- fire today; it binds any future in-place upgrade.
UPDATE idempotency_keys
    SET scope_id = COALESCE(key_id, customer_id)
    WHERE scope_id IS NULL;

DO $$
DECLARE
    v_unattributable_live_rows BIGINT;
BEGIN
    SELECT count(*) INTO v_unattributable_live_rows
    FROM idempotency_keys
    WHERE key_id IS NULL
      AND customer_id IS NULL
      AND expires_at > now()
      AND request_path NOT IN ('/v1/sandbox/demo/diner-pay', '/v1/customers');

    IF v_unattributable_live_rows > 0 THEN
        RAISE EXCEPTION
            'MIGRATION_0158_UNSAFE_CUTOVER: % legacy operator idempotency row(s) are still live (unexpired) and cannot be attributed to scope_id (operator_id was never stored pre-migration). Applying now would silently drop their idempotency protection. Quiesce operator writes for one TTL window (24h), or wait for these rows to expire, then re-run this migration.',
            v_unattributable_live_rows
            USING ERRCODE = 'P0001';
    END IF;
END $$;

ALTER TABLE idempotency_keys
    DROP CONSTRAINT idempotency_key_path_unique;

DROP INDEX IF EXISTS idx_idem_key_lookup;

ALTER TABLE idempotency_keys
    ADD CONSTRAINT idempotency_key_scope_path_unique
        UNIQUE NULLS NOT DISTINCT (idempotency_key, scope_id, request_path);

CREATE INDEX idx_idem_key_lookup ON idempotency_keys (
    idempotency_key, scope_id, request_path
) WHERE state = 'IN_FLIGHT' OR state = 'COMPLETED';

COMMIT;
