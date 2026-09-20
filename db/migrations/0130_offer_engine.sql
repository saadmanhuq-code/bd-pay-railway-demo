-- 0130_offer_engine.sql — spec/18 dining-wedge offer engine (BUILD AUTHORIZED).
--
-- Activation-track DDL from spec/18 §Data model, plus the deferred seam FK
-- that errata S18-E2 pinned to "the migration that creates the offers table".
-- Numbering: 0130 is the first free slot after the spec/17 reserved block
-- 0105-0129 (errata S18-E6).
--
-- Documented divergences from the spec DDL (all additive; errata S18-E5 and
-- S18-E10, pinned by tests/kernel/offers/test_migrations_offers.py):
--   * offers.created_idempotency_key + UNIQUE (merchant_id, created_idempotency_key)
--     — the off_<sha> preimage includes the client create key; the column makes
--     create replay detection a keyed lookup instead of an id recomputation.
--   * offer_versions — spec/18 mandates edit-by-versioning ("a new immutable
--     version row with version = n+1") but its offers DDL has PK offer_id and
--     cannot hold two version rows; the history lives here, the offers row is
--     the CURRENT version. Reservations pin offer_version and read economics
--     from this table.
--   * offer_redemptions.refunded_minor — accumulation column behind the
--     intent_reversed_or_refunded_full trigger (partial refunds accumulate
--     without transitioning; reaching net_amount_minor = full refund).
--   * offer_event_consumption — durable event_id dedupe (spec/18: consumers
--     idempotent on event_id).

CREATE TABLE offers (
    offer_id            TEXT PRIMARY KEY,            -- off_<sha24>
    version             INTEGER NOT NULL DEFAULT 1,
    merchant_id         TEXT NOT NULL REFERENCES merchants(merchant_id),
    kind                TEXT NOT NULL CHECK (kind IN ('PERCENT_OFF','BOGO')),
    percent_bps         INTEGER NULL CHECK (percent_bps BETWEEN 500 AND 5000),
    bogo_config         JSONB NULL,
    title               TEXT NOT NULL,
    title_bn            TEXT NOT NULL,
    windows             JSONB NOT NULL,              -- [{days:[],start_local,end_local}] Asia/Dhaka
    valid_from          TIMESTAMPTZ NOT NULL,
    valid_until         TIMESTAMPTZ NOT NULL,
    min_spend_minor     BIGINT NOT NULL DEFAULT 0 CHECK (min_spend_minor >= 0),
    max_discount_minor  BIGINT NULL CHECK (max_discount_minor > 0),
    cap_per_day         INTEGER NULL CHECK (cap_per_day >= 1),
    cap_total           INTEGER NULL CHECK (cap_total >= 1),
    cap_per_customer    INTEGER NULL CHECK (cap_per_customer >= 1),
    allowed_methods     TEXT[] NOT NULL,
    cap_recredit_on_refund BOOLEAN NOT NULL DEFAULT TRUE,
    commission_bps      INTEGER NULL CHECK (commission_bps BETWEEN 0 AND 1000),
    subsidy_bps         INTEGER NULL CHECK (subsidy_bps BETWEEN 0 AND 10000),
    state               TEXT NOT NULL DEFAULT 'DRAFT'
                        CHECK (state IN ('DRAFT','ACTIVE','PAUSED','EXHAUSTED','EXPIRED','ARCHIVED')),
    currency            CHAR(3) NOT NULL DEFAULT 'BDT',
    created_idempotency_key TEXT NOT NULL,           -- errata S18-E10 (off_ preimage member)
    created_at          TIMESTAMPTZ NOT NULL,
    updated_at          TIMESTAMPTZ NOT NULL,
    CHECK (valid_from < valid_until),
    CHECK (cap_per_day IS NOT NULL OR cap_total IS NOT NULL),
    CHECK ((kind = 'PERCENT_OFF') = (percent_bps IS NOT NULL)),
    CHECK ((kind = 'BOGO') = (bogo_config IS NOT NULL)),
    UNIQUE (merchant_id, created_idempotency_key)
);
CREATE INDEX idx_offers_merchant_state ON offers (merchant_id, state);
CREATE INDEX idx_offers_state_valid    ON offers (state, valid_until);

-- Immutable economics history (errata S18-E5: edit-by-versioning).
CREATE TABLE offer_versions (
    offer_id            TEXT NOT NULL REFERENCES offers(offer_id),
    version             INTEGER NOT NULL CHECK (version >= 1),
    percent_bps         INTEGER NULL CHECK (percent_bps BETWEEN 500 AND 5000),
    bogo_config         JSONB NULL,
    windows             JSONB NOT NULL,
    valid_from          TIMESTAMPTZ NOT NULL,
    valid_until         TIMESTAMPTZ NOT NULL,
    min_spend_minor     BIGINT NOT NULL DEFAULT 0 CHECK (min_spend_minor >= 0),
    max_discount_minor  BIGINT NULL CHECK (max_discount_minor > 0),
    cap_per_day         INTEGER NULL CHECK (cap_per_day >= 1),
    cap_total           INTEGER NULL CHECK (cap_total >= 1),
    cap_per_customer    INTEGER NULL CHECK (cap_per_customer >= 1),
    allowed_methods     TEXT[] NOT NULL,
    commission_bps      INTEGER NULL CHECK (commission_bps BETWEEN 0 AND 1000),
    subsidy_bps         INTEGER NULL CHECK (subsidy_bps BETWEEN 0 AND 10000),
    created_at          TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (offer_id, version)
);

CREATE TABLE offer_redemptions (
    redemption_id       TEXT PRIMARY KEY,            -- ored_<sha24>
    offer_id            TEXT NOT NULL REFERENCES offers(offer_id),
    offer_version       INTEGER NOT NULL,            -- pins economics at reservation
    payment_intent_id   TEXT NOT NULL UNIQUE REFERENCES payment_intents(payment_intent_id),
    merchant_id         TEXT NOT NULL REFERENCES merchants(merchant_id),
    customer_id         TEXT NOT NULL REFERENCES customers(customer_id),  -- D1: login required
    gross_amount_minor  BIGINT NOT NULL CHECK (gross_amount_minor > 0),
    discount_minor      BIGINT NOT NULL CHECK (discount_minor >= 0 AND discount_minor < gross_amount_minor),
    net_amount_minor    BIGINT NOT NULL,             -- == payment_intents.amount_minor
    commission_minor    BIGINT NOT NULL DEFAULT 0 CHECK (commission_minor >= 0),
    subsidy_minor       BIGINT NOT NULL DEFAULT 0 CHECK (subsidy_minor >= 0),
    refunded_minor      BIGINT NOT NULL DEFAULT 0 CHECK (refunded_minor >= 0),  -- errata S18-E10
    currency            CHAR(3) NOT NULL DEFAULT 'BDT',
    state               TEXT NOT NULL DEFAULT 'RESERVED'
                        CHECK (state IN ('RESERVED','APPLIED','RELEASED','REVERSED','SETTLED')),
    reserved_at         TIMESTAMPTZ NOT NULL,
    reserved_expires_at TIMESTAMPTZ NOT NULL,        -- = intent expires_at
    resolved_at         TIMESTAMPTZ NULL,
    CHECK (net_amount_minor = gross_amount_minor - discount_minor)
);
CREATE INDEX idx_ored_offer_state ON offer_redemptions (offer_id, state);
CREATE INDEX idx_ored_sweep       ON offer_redemptions (state, reserved_expires_at)
    WHERE state = 'RESERVED';

CREATE TABLE offer_counters (
    counter_id   TEXT PRIMARY KEY,                   -- octr_<sha24>
    offer_id     TEXT NOT NULL REFERENCES offers(offer_id),
    scope        TEXT NOT NULL CHECK (scope IN ('day','total','customer')),
    scope_key    TEXT NOT NULL,                      -- '2026-07-01' (Asia/Dhaka date) | 'ALL' | cust_<id>
    cap_limit    INTEGER NOT NULL CHECK (cap_limit >= 1),
    used         INTEGER NOT NULL DEFAULT 0 CHECK (used >= 0 AND used <= cap_limit),
    updated_at   TIMESTAMPTZ NOT NULL,
    UNIQUE (offer_id, scope, scope_key)
);

-- Durable event_id dedupe for the redemption FSM consumer (errata S18-E10).
CREATE TABLE offer_event_consumption (
    event_id    TEXT PRIMARY KEY,                    -- obx_<sha24>
    consumed_at TIMESTAMPTZ NOT NULL
);

-- The seam FK deferred by 0104 / errata S18-E2: now that offers exists,
-- payment_intents.offer_id gains its declared reference.
ALTER TABLE payment_intents
    ADD CONSTRAINT payment_intents_offer_id_fkey
    FOREIGN KEY (offer_id) REFERENCES offers(offer_id);

COMMENT ON TABLE offers IS
    'spec/18 merchant-defined discount programs. Current-version row; immutable '
    'economics history in offer_versions (errata S18-E5). 12-yr retention: '
    'ARCHIVED tombstones, never hard-deleted.';
COMMENT ON TABLE offer_counters IS
    'spec/18 fail-closed atomic cap slots. Reservation = single-statement '
    'conditional UPDATE (used < cap_limit) inside the intent-creating txn; '
    'no row returned = cap full = refused. Service-role only (RLS posture).';
