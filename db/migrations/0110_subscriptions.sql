-- 0110_subscriptions.sql — spec/17 VX3 subscriptions / recurring + dunning
-- Interim collection model: payment-link-per-cycle through PaymentIntent.
-- Mandate rows are present as the Phase-2 seam; live mandate rails remain
-- refused until rail capability is activated.

CREATE TABLE subscriptions (
    subscription_id   TEXT PRIMARY KEY,             -- subs_<24>
    merchant_id       TEXT NOT NULL,
    customer_ref      TEXT NOT NULL,                -- opaque/tokenized; never raw MSISDN/PAN
    plan_amount_minor BIGINT NOT NULL CHECK (plan_amount_minor > 0),
    currency          CHAR(3) NOT NULL DEFAULT 'BDT' CHECK (currency = 'BDT'),
    interval          TEXT NOT NULL CHECK (interval IN ('WEEKLY','MONTHLY')),
    anchor_date       DATE NOT NULL,
    end_date          DATE,
    collection        TEXT NOT NULL CHECK (collection IN ('LINK','MANDATE')),
    status            TEXT NOT NULL
        CHECK (status IN ('ACTIVE','PAST_DUE','PAUSED','CANCELLED','COMPLETED')),
    metadata          JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_idempotency_key TEXT NOT NULL,
    past_due_grace_expires_at TIMESTAMPTZ,
    terminal_at       TIMESTAMPTZ,
    created_at        TIMESTAMPTZ NOT NULL,
    updated_at        TIMESTAMPTZ NOT NULL,
    UNIQUE (merchant_id, created_idempotency_key)
);

CREATE INDEX subs_merchant_idx ON subscriptions (merchant_id, status);
CREATE INDEX subs_due_grace_idx ON subscriptions (past_due_grace_expires_at)
    WHERE status = 'PAST_DUE';

CREATE TABLE subscription_cycles (
    cycle_id          TEXT PRIMARY KEY,             -- scyc_<24>
    subscription_id   TEXT NOT NULL REFERENCES subscriptions(subscription_id),
    cycle_number      BIGINT NOT NULL CHECK (cycle_number >= 1),
    amount_minor      BIGINT NOT NULL CHECK (amount_minor > 0),
    due_at            TIMESTAMPTZ NOT NULL,
    status            TEXT NOT NULL CHECK (status IN
        ('SCHEDULED','COLLECTING','DUNNING','PAID','FAILED','SKIPPED','CANCELLED')),
    payment_intent_ids TEXT[] NOT NULL DEFAULT '{}',
    dunning_attempts  SMALLINT NOT NULL DEFAULT 0 CHECK (dunning_attempts >= 0),
    dunning_started_at TIMESTAMPTZ,
    next_dunning_at   TIMESTAMPTZ,
    paid_at           TIMESTAMPTZ,
    failed_at         TIMESTAMPTZ,
    terminal_at       TIMESTAMPTZ,
    created_at        TIMESTAMPTZ NOT NULL,
    updated_at        TIMESTAMPTZ NOT NULL,
    UNIQUE (subscription_id, cycle_number)
);

CREATE INDEX scyc_due_idx ON subscription_cycles (due_at)
    WHERE status = 'SCHEDULED';
CREATE INDEX scyc_dunning_due_idx ON subscription_cycles (next_dunning_at)
    WHERE status = 'DUNNING';
CREATE INDEX scyc_subscription_idx ON subscription_cycles (subscription_id, cycle_number);

CREATE TABLE subscription_dunning_attempts (
    cycle_id          TEXT NOT NULL REFERENCES subscription_cycles(cycle_id),
    attempt_number    SMALLINT NOT NULL CHECK (attempt_number >= 1),
    payment_intent_id TEXT NOT NULL,
    scheduled_at      TIMESTAMPTZ NOT NULL,
    created_at        TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (cycle_id, attempt_number),
    UNIQUE (cycle_id, attempt_number)
);

CREATE TABLE mandates (
    mandate_id       TEXT PRIMARY KEY,              -- mand_<24>
    subscription_id  TEXT NOT NULL REFERENCES subscriptions(subscription_id),
    rail             TEXT NOT NULL CHECK (rail IN ('CARD','BKASH','NAGAD')),
    instrument_ref   TEXT NOT NULL,                 -- vault token / rail mandate ref; never PAN
    status           TEXT NOT NULL CHECK (status IN
        ('PENDING_CONFIRMATION','ACTIVE','SUSPENDED','REVOKED','EXPIRED')),
    confirmed_at     TIMESTAMPTZ,
    revoked_at       TIMESTAMPTZ,
    created_at       TIMESTAMPTZ NOT NULL
);
