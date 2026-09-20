-- 0146_ledger_account_holds.sql — durable AccountHold register (M3 hold legs).
--
-- spec/03 defines account_holds for authorization holds opened by manual
-- capture flows. The implementation keeps the spec columns and adds
-- source_account_id so voids and partial captures can reverse the exact
-- transit/clearing leg that funded the reserve.

CREATE TABLE IF NOT EXISTS ledger.account_holds (
    hold_id                 TEXT        PRIMARY KEY,
    account_id              TEXT        NOT NULL REFERENCES ledger.accounts(account_id),
    hold_reserve_account_id TEXT        NOT NULL REFERENCES ledger.accounts(account_id),
    source_account_id       TEXT        NOT NULL REFERENCES ledger.accounts(account_id),
    reference_id            TEXT        NOT NULL,
    amount_minor            BIGINT      NOT NULL,
    hold_reason             TEXT        NOT NULL,
    status                  TEXT        NOT NULL DEFAULT 'OPEN',
    hold_opened_at          TIMESTAMPTZ NOT NULL,
    hold_expires_at         TIMESTAMPTZ NOT NULL,
    hold_closed_at          TIMESTAMPTZ,
    open_je_id              TEXT        NOT NULL REFERENCES ledger.journal_entries(entry_id),
    close_je_id             TEXT        REFERENCES ledger.journal_entries(entry_id),
    schema_version          SMALLINT    NOT NULL DEFAULT 1,

    CONSTRAINT holds_status_check CHECK (
        status IN ('OPEN','CAPTURED','VOIDED','EXPIRED')
    ),
    CONSTRAINT holds_amount_positive CHECK (amount_minor > 0),
    CONSTRAINT holds_closed_requires_close_je CHECK (
        (status = 'OPEN' AND hold_closed_at IS NULL AND close_je_id IS NULL)
        OR (status <> 'OPEN' AND hold_closed_at IS NOT NULL AND close_je_id IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS holds_account_status_idx
    ON ledger.account_holds(account_id, status);
CREATE INDEX IF NOT EXISTS holds_reference_idx
    ON ledger.account_holds(reference_id);
CREATE INDEX IF NOT EXISTS holds_expiry_idx
    ON ledger.account_holds(hold_expires_at)
    WHERE status = 'OPEN';

GRANT SELECT, INSERT ON ledger.account_holds TO ledger_app_role;
GRANT UPDATE(status, hold_closed_at, close_je_id, schema_version)
    ON ledger.account_holds TO ledger_app_role;
GRANT SELECT ON ledger.account_holds TO ledger_ro;

ALTER TABLE ledger.account_holds ENABLE ROW LEVEL SECURITY;

DO $$
BEGIN
    BEGIN
        CREATE POLICY account_holds_app
            ON ledger.account_holds
            FOR ALL TO ledger_app_role
            USING (TRUE)
            WITH CHECK (TRUE);
    EXCEPTION WHEN duplicate_object THEN NULL;
    END;
END
$$;
