-- 0010_ledger_core.sql — BD-PAY ledger core (Postgres 16; spec/03 Data model).
-- Ported from bd-pay-fable-pilot/sql/001_ledger_core.sql (verified pilot) and
-- extended with audit_events (spec/03 AuditEvent design — descoped in the
-- pilot, implemented here).
--
-- Binding errata applied (SPEC_ERRATA.md):
--  * E1: journal_entries (and audit_events) are UNPARTITIONED. The spec's
--    monthly RANGE partitioning plus a global UNIQUE(idempotency_key) is
--    impossible as written: Postgres 16 unique indexes on partitioned tables
--    MUST include the partition key. Unpartitioned tables preserve the
--    stronger invariant (true global uniqueness + a declared
--    postings->journal_entries FK). Partitioning is a later explicit project.
--  * E2: chain appends serialize via pg_advisory_xact_lock per chain_domain
--    (row-locking clauses need UPDATE privilege the append-only model denies).
--  * E3: ledger_chain uses PRIMARY KEY (chain_domain, chain_index) with
--    application-assigned contiguous per-domain indexes (genesis = 0/domain).
--  * E9: one trigger maintains accounts.balance_minor AND account_balances.
--  * E10: write_gate is a persistent table (fails CLOSED across restarts);
--    the app role can engage but never clear it.
--  * E11: system-account uniqueness is (subtype, purpose_tag).
--
-- Roles: ledger_app_role (append-only writer) and ledger_ro (inspector) are
-- NOLOGIN roles; the application connects with its own credentials and runs
-- SET ROLE. No credentials live in DDL.

CREATE SCHEMA IF NOT EXISTS ledger;

DO $$
BEGIN
    CREATE ROLE ledger_app_role NOLOGIN;
EXCEPTION WHEN duplicate_object THEN
    NULL;
END $$;

DO $$
BEGIN
    CREATE ROLE ledger_ro NOLOGIN;
EXCEPTION WHEN duplicate_object THEN
    NULL;
END $$;

GRANT USAGE ON SCHEMA ledger TO ledger_app_role, ledger_ro;
GRANT ledger_app_role, ledger_ro TO CURRENT_USER;

-- ============================================================
-- 1. Accounts — chart of accounts
-- ============================================================
CREATE TABLE IF NOT EXISTS ledger.accounts (
    account_id      TEXT        PRIMARY KEY,
    account_type    TEXT        NOT NULL,
    account_subtype TEXT        NOT NULL,
    currency        CHAR(3)     NOT NULL DEFAULT 'BDT',
    owner_id        TEXT,
    owner_type      TEXT,
    parent_id       TEXT        REFERENCES ledger.accounts(account_id),
    purpose_tag     TEXT,
    is_system       BOOLEAN     NOT NULL DEFAULT FALSE,
    balance_minor   BIGINT      NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL,
    closed_at       TIMESTAMPTZ,
    schema_version  SMALLINT    NOT NULL DEFAULT 1,

    CONSTRAINT accounts_type_check CHECK (
        account_type IN ('ASSET','LIABILITY','EQUITY','INCOME','EXPENSE')
    ),
    CONSTRAINT accounts_subtype_check CHECK (
        account_subtype IN (
            'SPONSOR_BANK_TCSA','IN_TRANSIT','FEE_RECEIVABLE','SUSPENSE','CONNECTOR_CLEARING',
            'CUSTOMER_FLOAT','CUSTOMER_HOLD_RESERVE','MERCHANT_SETTLEMENT',
            'MERCHANT_HOLD_RESERVE','MERCHANT_ROLLING_RESERVE','REFUND_RESERVE',
            'PARTICIPANT_POSITION','NET_DEBIT_CAP',
            'RETAINED_EARNINGS','PAID_UP_CAPITAL',
            'MDR_INCOME','SWITCHING_FEE_INCOME','INTERCHANGE_INCOME','FLOAT_INTEREST_INCOME',
            'SPONSOR_BANK_FEE','NETWORK_FEE','REFUND_EXPENSE','CONNECTIVITY_EXPENSE'
        )
    ),
    CONSTRAINT accounts_owner_type_check CHECK (
        owner_type IS NULL OR owner_type IN ('MERCHANT','CUSTOMER','PARTICIPANT','SYSTEM')
    ),
    CONSTRAINT accounts_currency_check CHECK (currency = 'BDT')
);

CREATE UNIQUE INDEX IF NOT EXISTS accounts_owner_subtype_tag_uidx
    ON ledger.accounts(owner_id, account_subtype, purpose_tag)
    WHERE owner_id IS NOT NULL AND purpose_tag IS NOT NULL;

-- E11: (subtype, purpose_tag) for system accounts — the spec's bootstrap
-- table itself has three IN_TRANSIT system accounts.
CREATE UNIQUE INDEX IF NOT EXISTS accounts_system_subtype_tag_uidx
    ON ledger.accounts(account_subtype, purpose_tag)
    WHERE is_system = TRUE;

CREATE INDEX IF NOT EXISTS accounts_owner_idx ON ledger.accounts(owner_id);
CREATE INDEX IF NOT EXISTS accounts_subtype_idx ON ledger.accounts(account_subtype);

-- ============================================================
-- 2. Journal entries (unpartitioned — E1)
-- ============================================================
CREATE TABLE IF NOT EXISTS ledger.journal_entries (
    entry_id        TEXT        PRIMARY KEY,
    entry_index     BIGINT      GENERATED ALWAYS AS IDENTITY,
    reference_id    TEXT        NOT NULL,
    reference_type  TEXT        NOT NULL,
    entry_type      TEXT        NOT NULL,
    description     TEXT        NOT NULL,
    produced_by     TEXT        NOT NULL DEFAULT 'ledger-service@1',
    produced_at     TIMESTAMPTZ NOT NULL,
    idempotency_key TEXT        NOT NULL,
    schema_version  SMALLINT    NOT NULL DEFAULT 1,

    CONSTRAINT je_entry_type_check CHECK (
        entry_type IN (
            'payment_authorized','payment_captured','payment_failed','payment_reversed',
            'refund_initiated','refund_settled','fee_collected',
            'hold_opened','hold_captured','hold_voided','hold_expired',
            'rolling_reserve_withheld','rolling_reserve_released',
            'settlement_batch_open','settlement_batch_close','settlement_confirmed',
            'tcsa_balance_update','participant_debit_cap_set','position_updated',
            'suspense_entry','suspense_cleared',
            'connector_clearing_in','connector_clearing_out',
            'adjustment_debit','adjustment_credit'
        )
    ),
    CONSTRAINT je_reference_type_check CHECK (
        reference_type IN (
            'PAYMENT','SETTLEMENT','REFUND','FEE',
            'REVERSAL','ADJUSTMENT','TCSA','HOLD','RESERVE'
        )
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS je_idempotency_uidx ON ledger.journal_entries(idempotency_key);
CREATE UNIQUE INDEX IF NOT EXISTS je_entry_index_uidx ON ledger.journal_entries(entry_index);
CREATE INDEX IF NOT EXISTS je_reference_idx ON ledger.journal_entries(reference_id, reference_type);
CREATE INDEX IF NOT EXISTS je_entry_type_idx ON ledger.journal_entries(entry_type);
CREATE INDEX IF NOT EXISTS je_produced_at_idx ON ledger.journal_entries(produced_at);

-- ============================================================
-- 3. Postings — double-entry lines
-- ============================================================
CREATE TABLE IF NOT EXISTS ledger.postings (
    posting_id      TEXT        NOT NULL PRIMARY KEY,
    entry_id        TEXT        NOT NULL REFERENCES ledger.journal_entries(entry_id),
    account_id      TEXT        NOT NULL REFERENCES ledger.accounts(account_id),
    side            TEXT        NOT NULL,
    amount_minor    BIGINT      NOT NULL,
    currency        CHAR(3)     NOT NULL DEFAULT 'BDT',
    produced_at     TIMESTAMPTZ NOT NULL,
    schema_version  SMALLINT    NOT NULL DEFAULT 1,

    CONSTRAINT postings_side_check CHECK (side IN ('DEBIT','CREDIT')),
    CONSTRAINT postings_amount_positive CHECK (amount_minor > 0),
    CONSTRAINT postings_currency_check CHECK (currency = 'BDT')
);

CREATE INDEX IF NOT EXISTS postings_entry_idx ON ledger.postings(entry_id);
CREATE INDEX IF NOT EXISTS postings_account_idx ON ledger.postings(account_id, produced_at);

-- ============================================================
-- SUM-TO-ZERO ENFORCEMENT — Layer 2 (deferred constraint trigger)
-- ============================================================
CREATE OR REPLACE FUNCTION ledger.check_posting_balance()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ledger, pg_temp
AS $$
DECLARE
    v_debit_sum  BIGINT;
    v_credit_sum BIGINT;
BEGIN
    SELECT
        COALESCE(SUM(amount_minor) FILTER (WHERE side = 'DEBIT'),  0),
        COALESCE(SUM(amount_minor) FILTER (WHERE side = 'CREDIT'), 0)
    INTO v_debit_sum, v_credit_sum
    FROM ledger.postings
    WHERE entry_id = NEW.entry_id;

    IF v_debit_sum <> v_credit_sum THEN
        RAISE EXCEPTION
            'Ledger imbalance for entry_id=%: debit=% credit=%',
            NEW.entry_id, v_debit_sum, v_credit_sum;
    END IF;
    RETURN NULL;
END;
$$;

DROP TRIGGER IF EXISTS postings_balance_check ON ledger.postings;
CREATE CONSTRAINT TRIGGER postings_balance_check
    AFTER INSERT ON ledger.postings
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION ledger.check_posting_balance();

-- ============================================================
-- 4. Account balances — materialized running totals (E9: one trigger,
--    both truths kept consistent)
-- ============================================================
CREATE TABLE IF NOT EXISTS ledger.account_balances (
    account_id      TEXT        PRIMARY KEY REFERENCES ledger.accounts(account_id),
    balance_minor   BIGINT      NOT NULL DEFAULT 0,
    last_posting_id TEXT,
    updated_at      TIMESTAMPTZ NOT NULL
);

CREATE OR REPLACE FUNCTION ledger.update_account_balance()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ledger, pg_temp
AS $$
DECLARE
    v_account_type TEXT;
    v_delta        BIGINT;
BEGIN
    SELECT account_type INTO v_account_type
    FROM ledger.accounts
    WHERE account_id = NEW.account_id;

    IF v_account_type IN ('ASSET','EXPENSE') THEN
        v_delta := CASE WHEN NEW.side = 'DEBIT' THEN NEW.amount_minor
                        ELSE -NEW.amount_minor END;
    ELSE
        v_delta := CASE WHEN NEW.side = 'CREDIT' THEN NEW.amount_minor
                        ELSE -NEW.amount_minor END;
    END IF;

    INSERT INTO ledger.account_balances (account_id, balance_minor, last_posting_id, updated_at)
    VALUES (NEW.account_id, v_delta, NEW.posting_id, NEW.produced_at)
    ON CONFLICT (account_id) DO UPDATE
        SET balance_minor   = ledger.account_balances.balance_minor + v_delta,
            last_posting_id = EXCLUDED.last_posting_id,
            updated_at      = EXCLUDED.updated_at;

    UPDATE ledger.accounts
       SET balance_minor = balance_minor + v_delta
     WHERE account_id = NEW.account_id;

    RETURN NULL;
END;
$$;

DROP TRIGGER IF EXISTS postings_update_balance ON ledger.postings;
CREATE TRIGGER postings_update_balance
    AFTER INSERT ON ledger.postings
    FOR EACH ROW EXECUTE FUNCTION ledger.update_account_balance();

-- ============================================================
-- 6. Hash-chained ledger_chain (per-domain contiguous sequence — E3)
-- ============================================================
CREATE TABLE IF NOT EXISTS ledger.ledger_chain (
    chain_domain    TEXT        NOT NULL DEFAULT 'MONEY',
    chain_index     BIGINT      NOT NULL,
    entry_type      TEXT        NOT NULL,
    payload_hash    TEXT        NOT NULL,
    payload_pointer TEXT        NOT NULL,
    prev_chain_hash TEXT        NOT NULL,
    amount_minor_sum BIGINT     NOT NULL DEFAULT 0,
    chain_hash      TEXT        NOT NULL,
    is_checkpoint   BOOLEAN     NOT NULL DEFAULT FALSE,
    checkpoint_sequence_in_domain BIGINT,
    checkpoint_sig  TEXT,
    checkpoint_public_key_b64 TEXT,
    producer        TEXT        NOT NULL,
    produced_at     TIMESTAMPTZ NOT NULL,
    schema_version  SMALLINT    NOT NULL DEFAULT 1,

    PRIMARY KEY (chain_domain, chain_index),
    CONSTRAINT chain_domain_check CHECK (chain_domain IN ('MONEY','AUDIT')),
    CONSTRAINT chain_index_nonnegative CHECK (chain_index >= 0),
    CONSTRAINT chain_amount_sum_nonnegative CHECK (amount_minor_sum >= 0),
    CONSTRAINT chain_pointer_mandatory CHECK (payload_pointer <> ''),
    CONSTRAINT chain_checkpoint_fields_check CHECK (
        (is_checkpoint
            AND checkpoint_sig IS NOT NULL
            AND checkpoint_public_key_b64 IS NOT NULL
            AND checkpoint_sequence_in_domain IS NOT NULL)
        OR
        (NOT is_checkpoint
            AND checkpoint_sig IS NULL
            AND checkpoint_public_key_b64 IS NULL
            AND checkpoint_sequence_in_domain IS NULL)
    )
);

CREATE INDEX IF NOT EXISTS ledger_chain_checkpoint_idx
    ON ledger.ledger_chain(chain_domain, is_checkpoint, checkpoint_sequence_in_domain)
    WHERE is_checkpoint = TRUE;
CREATE INDEX IF NOT EXISTS ledger_chain_entry_type_idx ON ledger.ledger_chain(entry_type);

-- ============================================================
-- 7. Ledger checkpoints (summary table)
-- ============================================================
CREATE TABLE IF NOT EXISTS ledger.ledger_checkpoints (
    checkpoint_id               TEXT        PRIMARY KEY,
    chain_domain                TEXT        NOT NULL,
    checkpoint_sequence_in_domain BIGINT    NOT NULL,
    chain_index_from            BIGINT      NOT NULL,
    chain_index_to              BIGINT      NOT NULL,
    chain_hash_at_checkpoint    TEXT        NOT NULL,
    checkpoint_sig              TEXT        NOT NULL,
    checkpoint_public_key_b64   TEXT        NOT NULL,
    entries_in_segment          INT         NOT NULL,
    signed_at                   TIMESTAMPTZ NOT NULL,
    schema_version              SMALLINT    NOT NULL DEFAULT 1,

    CONSTRAINT checkpoints_entries_positive CHECK (entries_in_segment > 0),
    CONSTRAINT checkpoints_range_check CHECK (
        entries_in_segment = chain_index_to - chain_index_from + 1
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS checkpoints_domain_seq_uidx
    ON ledger.ledger_checkpoints(chain_domain, checkpoint_sequence_in_domain);

-- ============================================================
-- 8. Outbox (ledger-local; the atomic-bundle row — errata LA-L12: drained
--    by the platform OutboxWorker through an adapter at wiring time)
-- ============================================================
CREATE TABLE IF NOT EXISTS ledger.outbox (
    event_id        TEXT        PRIMARY KEY,
    topic           TEXT        NOT NULL,
    event_type      TEXT        NOT NULL,
    subject_type    TEXT        NOT NULL,
    subject_id      TEXT        NOT NULL,
    payload_json    TEXT        NOT NULL,
    producer        TEXT        NOT NULL,
    occurred_at     TIMESTAMPTZ NOT NULL,
    published       BOOLEAN     NOT NULL DEFAULT FALSE,
    published_at    TIMESTAMPTZ,
    schema_version  SMALLINT    NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS outbox_unpublished_idx
    ON ledger.outbox(occurred_at) WHERE published = FALSE;

-- ============================================================
-- 9. Audit events — append-only, PII-redacted at write, AUDIT-chained
--    (spec/03 §AuditEvent design; unpartitioned per E1; payload_json stands
--    in for audit_payload_store until the object-store uploader exists)
-- ============================================================
CREATE TABLE IF NOT EXISTS ledger.audit_events (
    event_id        TEXT        PRIMARY KEY,
    entry_index     BIGINT      GENERATED ALWAYS AS IDENTITY,
    event_type      TEXT        NOT NULL,
    actor_id        TEXT        NOT NULL,
    actor_type      TEXT        NOT NULL,
    subject_type    TEXT        NOT NULL,
    subject_id      TEXT        NOT NULL,
    from_state      TEXT,
    to_state        TEXT,
    payload_hash    TEXT        NOT NULL,
    payload_pointer TEXT        NOT NULL,
    payload_json    TEXT        NOT NULL,
    prev_chain_hash TEXT        NOT NULL,
    chain_hash      TEXT        NOT NULL,
    pii_redacted    BOOLEAN     NOT NULL DEFAULT TRUE,
    occurred_at     TIMESTAMPTZ NOT NULL,
    schema_version  SMALLINT    NOT NULL DEFAULT 1,

    CONSTRAINT ae_actor_type_check CHECK (
        actor_type IN ('OPERATOR','CUSTOMER','MERCHANT','SERVICE','BB_INSPECTOR')
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS ae_entry_index_uidx ON ledger.audit_events(entry_index);
CREATE INDEX IF NOT EXISTS ae_subject_idx
    ON ledger.audit_events(subject_type, subject_id, occurred_at);
CREATE INDEX IF NOT EXISTS ae_actor_idx ON ledger.audit_events(actor_id, occurred_at);
CREATE INDEX IF NOT EXISTS ae_event_type_idx ON ledger.audit_events(event_type, occurred_at);

-- ============================================================
-- 10. Write gate (fail-closed halt switch; table, not advisory lock — E10)
-- ============================================================
CREATE TABLE IF NOT EXISTS ledger.write_gate (
    gate_id     BIGINT      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    reason      TEXT        NOT NULL,
    engaged_at  TIMESTAMPTZ NOT NULL,
    cleared_at  TIMESTAMPTZ,
    cleared_by  TEXT
);

-- ============================================================
-- Append-only enforcement: privileges + RLS for ledger_app_role.
-- The app role can INSERT and SELECT; it can never UPDATE or DELETE the
-- append-only tables (clearing the write gate is an operator/DBA action).
-- ============================================================
GRANT SELECT, INSERT ON ledger.accounts            TO ledger_app_role;
GRANT SELECT, INSERT ON ledger.journal_entries     TO ledger_app_role;
GRANT SELECT, INSERT ON ledger.postings            TO ledger_app_role;
GRANT SELECT, INSERT ON ledger.ledger_chain        TO ledger_app_role;
GRANT SELECT, INSERT ON ledger.ledger_checkpoints  TO ledger_app_role;
GRANT SELECT, INSERT ON ledger.outbox              TO ledger_app_role;
GRANT SELECT, INSERT ON ledger.audit_events        TO ledger_app_role;
GRANT SELECT, INSERT ON ledger.write_gate          TO ledger_app_role;
GRANT SELECT         ON ledger.account_balances    TO ledger_app_role;

GRANT SELECT ON ALL TABLES IN SCHEMA ledger TO ledger_ro;

ALTER TABLE ledger.journal_entries    ENABLE ROW LEVEL SECURITY;
ALTER TABLE ledger.postings           ENABLE ROW LEVEL SECURITY;
ALTER TABLE ledger.ledger_chain       ENABLE ROW LEVEL SECURITY;
ALTER TABLE ledger.ledger_checkpoints ENABLE ROW LEVEL SECURITY;
ALTER TABLE ledger.outbox             ENABLE ROW LEVEL SECURITY;
ALTER TABLE ledger.audit_events       ENABLE ROW LEVEL SECURITY;

DO $$
DECLARE
    t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY['journal_entries','postings','ledger_chain',
                             'ledger_checkpoints','outbox','audit_events']
    LOOP
        BEGIN
            EXECUTE format(
                'CREATE POLICY %I_append_only ON ledger.%I FOR ALL TO ledger_app_role '
                'USING (TRUE) WITH CHECK (TRUE)', t, t);
        EXCEPTION WHEN duplicate_object THEN NULL;
        END;
        BEGIN
            EXECUTE format(
                'CREATE POLICY %I_no_update ON ledger.%I AS RESTRICTIVE FOR UPDATE '
                'TO ledger_app_role USING (FALSE)', t, t);
        EXCEPTION WHEN duplicate_object THEN NULL;
        END;
        BEGIN
            EXECUTE format(
                'CREATE POLICY %I_no_delete ON ledger.%I AS RESTRICTIVE FOR DELETE '
                'TO ledger_app_role USING (FALSE)', t, t);
        EXCEPTION WHEN duplicate_object THEN NULL;
        END;
        BEGIN
            EXECUTE format(
                'CREATE POLICY %I_ro_read ON ledger.%I FOR SELECT TO ledger_ro '
                'USING (TRUE)', t, t);
        EXCEPTION WHEN duplicate_object THEN NULL;
        END;
    END LOOP;
END $$;
