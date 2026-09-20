-- 0103_replay_window_mode.sql — spec/19 PSO-3 (D-19-5 migration range).
--
-- Three additive changes:
--
-- 1. CREATE ledger.ledger_replay_records. spec/19 prints this migration as a
--    CHECK widening (DROP/ADD rr_mode_check), but the spec/03 table was never
--    shipped by any migration (spec/16 errata E-S16-12 recorded the absent
--    ReplayService; the table went with it). The table is therefore CREATED
--    here with the widened mode CHECK ('WINDOW_REPLAY' included from birth).
--    Errata E-S19R-01 pins this deviation.
--
-- 2. journal_entries.metadata (JSONB, additive). D-19-6 (binding): every
--    journal entry whose postings touch a PARTICIPANT_POSITION account MUST
--    record settlement_window_id in the entry metadata, written by the
--    posting path in the same transaction — the replay anchor. spec/03's
--    journal_entries DDL carries no metadata column, so it is added here
--    additively (DEFAULT '{}', no row rewrite semantics change).
--    Errata E-S19R-02 pins this.
--
-- 3. dossier_exports.dossier_type CHECK gains PSO_DISPUTE_BUNDLE and
--    FOUNDING_PARTICIPANT_PACK (spec/19 §Entities additive amendments).
--    Guarded on table existence: in the full sequence 0093 precedes 0103;
--    partial (ledger-only) test schemas skip the widening and inserts of the
--    new types fail loudly there (fail closed).

-- ============================================================
-- 1. Ledger replay records (spec/03 §10 DDL + spec/19 additive mode)
-- ============================================================

CREATE TABLE IF NOT EXISTS ledger.ledger_replay_records (
    replay_id       TEXT        PRIMARY KEY,
    -- rply_<sha256_canonical({reference_id, reference_type, replay_mode, requested_at_str})[0:24]>
    -- Not in the canonical prefix table; internal ledger record only (LA-L5 shim).

    reference_id    TEXT        NOT NULL,
    reference_type  TEXT        NOT NULL,
    -- journal reference vocabulary + 'ACCOUNT' (mode 1) + 'SETTLEMENT_WINDOW'
    -- (spec/19 additive); app-side closed registry, no CHECK in the spec DDL.
    replay_mode     TEXT        NOT NULL,

    status          TEXT        NOT NULL DEFAULT 'QUEUED',
    requested_by    TEXT        NOT NULL,
    -- actor_id (PII-redacted operator/inspector ID)

    requested_at    TIMESTAMPTZ NOT NULL,
    started_at      TIMESTAMPTZ,
    completed_at    TIMESTAMPTZ,

    result_summary  JSONB,
    -- spec/03 shape; WINDOW_REPLAY additionally carries
    -- {result_bytes_identical, chain_deep_verified, netting_run_id,
    --  recomputed_result_hash} (spec/19 PSO-3).

    result_pointer  TEXT,
    -- Pointer to full replay output in the object store (encrypted)

    schema_version  SMALLINT    NOT NULL DEFAULT 1,

    CONSTRAINT rr_status_check CHECK (
        status IN ('QUEUED','RUNNING','COMPLETED','FAILED')
    ),
    CONSTRAINT rr_mode_check CHECK (
        replay_mode IN ('ACCOUNT_BALANCE','ENTRY_SEQUENCE','DISPUTE_TRAIL','WINDOW_REPLAY')
    )
);

CREATE INDEX IF NOT EXISTS rr_reference_idx
    ON ledger.ledger_replay_records (reference_id, requested_at);

-- Output discipline: replay results are appended, status advances only via
-- the ReplayRequest FSM, and terminal rows are NEVER overwritten (spec/03 +
-- spec/19 failure modes). The app role gets INSERT/SELECT/UPDATE; DELETE is
-- denied by policy and terminal mutation by trigger (defense in depth behind
-- the application-store guard).
GRANT SELECT, INSERT, UPDATE ON ledger.ledger_replay_records TO ledger_app_role;

ALTER TABLE ledger.ledger_replay_records ENABLE ROW LEVEL SECURITY;
DO $$
BEGIN
    BEGIN
        CREATE POLICY ledger_replay_records_rw ON ledger.ledger_replay_records
            FOR ALL TO ledger_app_role USING (TRUE) WITH CHECK (TRUE);
    EXCEPTION WHEN duplicate_object THEN NULL;
    END;
    BEGIN
        CREATE POLICY ledger_replay_records_no_delete ON ledger.ledger_replay_records
            AS RESTRICTIVE FOR DELETE TO ledger_app_role USING (FALSE);
    EXCEPTION WHEN duplicate_object THEN NULL;
    END;
END $$;

CREATE OR REPLACE FUNCTION ledger.replay_record_terminal_guard()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF OLD.status IN ('COMPLETED','FAILED') THEN
        RAISE EXCEPTION 'REPLAY_RECORD_TERMINAL: % is % and immutable',
            OLD.replay_id, OLD.status
            USING ERRCODE = 'P0001';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS replay_record_terminal_guard ON ledger.ledger_replay_records;
CREATE TRIGGER replay_record_terminal_guard
    BEFORE UPDATE ON ledger.ledger_replay_records
    FOR EACH ROW EXECUTE FUNCTION ledger.replay_record_terminal_guard();

-- ============================================================
-- 2. D-19-6 attribution column (additive; written by the posting path)
-- ============================================================

ALTER TABLE ledger.journal_entries
    ADD COLUMN IF NOT EXISTS metadata JSONB NOT NULL DEFAULT '{}'::jsonb;

-- The replay/netting selection index: entries by window attribution.
CREATE INDEX IF NOT EXISTS je_window_attribution_idx
    ON ledger.journal_entries ((metadata->>'settlement_window_id'))
    WHERE metadata ? 'settlement_window_id';

-- ============================================================
-- 3. dossier_exports.dossier_type widening (spec/19 §Entities)
-- ============================================================

DO $$
BEGIN
    IF to_regclass('dossier_exports') IS NOT NULL THEN
        ALTER TABLE dossier_exports
            DROP CONSTRAINT IF EXISTS dossier_exports_dossier_type_check;
        ALTER TABLE dossier_exports
            ADD CONSTRAINT dossier_exports_dossier_type_check CHECK (
                dossier_type IN (
                    'BB_PHASE2',
                    'SPONSOR_BANK_PACK',
                    'BFIU_EVIDENCE',
                    'PSO_DISPUTE_BUNDLE',
                    'FOUNDING_PARTICIPANT_PACK'
                )
            );
    END IF;
END $$;
