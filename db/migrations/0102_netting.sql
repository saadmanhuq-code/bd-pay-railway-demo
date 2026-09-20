-- 0102_netting.sql — spec/19 PSO-2: multilateral netting tables, the D-19-4
-- settlement_instructions amendment, the D-19-6 attribution write path, and
-- the D-19 suspended-participant guard added to the 0012 NDC triggers.
--
-- Errata applied (SPEC_ERRATA-LANE-A-spec19.md):
--  * N19-1: D-19-6 attribution — the posting path writes
--    reference_type='SETTLEMENT' / reference_id=<swin_...> on every position
--    fact; a journal_entries BEFORE INSERT trigger mirrors it into
--    metadata->>'settlement_window_id' (the spec-literal column added by
--    0103/this file, order-independent) so the PSO-3 replay selector and the
--    netting derivation provably select the same entry set.
--  * N19-4: the suspended-participant guard fires only when a
--    public.participants row EXISTS and kyb_status <> 'ACTIVE' (cross-package
--    integrity is app-enforced, E20 posture; zero-PSO-rows behavior stays
--    bit-identical). The guard is mirrored on the credit trigger — the FSM
--    side-effect row says "refuses all FURTHER postings".
--  * N19-5: D-19-2 unwind reversals (payment_reversed entries attributed
--    to a CUTOFF_REACHED/SETTLEMENT_FAILED window) post WITHOUT cap
--    enforcement and apply the maintained-position delta — the 0012 lookup
--    only admits ACCUMULATING windows and would strand every unwind. The
--    netting close entry never touches position accounts.
--  * N19-2: tables live in the ledger schema (LA-L4 precedent: they join
--    ledger.settlement_windows / ledger.postings).
--  * N19-6: sinst_batch_assignment is relaxed for PSO_NETTING rows —
--    netting legs dispatch per-window through the window FSM, never through
--    merchant-payout batches, so batch_id stays NULL past QUEUED.

-- ============================================================
-- NETTING RUNS — append-only results (one deterministic computation each)
-- ============================================================
CREATE TABLE IF NOT EXISTS ledger.netting_runs (
    netting_run_id        TEXT PRIMARY KEY
                            CHECK (netting_run_id LIKE 'nrun_%'),
    settlement_window_id  TEXT NOT NULL,  -- swin_<...> (spec/05 owns settlement_windows)
    status                TEXT NOT NULL DEFAULT 'COMPUTING'
        CHECK (status IN ('COMPUTING','PASSED','FAILED_INTEGRITY','SUPERSEDED')),
    participant_count     INTEGER NOT NULL,
    inputs_hash           TEXT NOT NULL,  -- sha256_canonical(ordered position list — ledger facts only, D-19-7)
    result_hash           TEXT,           -- sha256_canonical(result document); set at PASSED
    result_pointer        TEXT,           -- object store: the canonical netting result file
    total_gross_minor     BIGINT,         -- sum of |all position movements| (window volume)
    total_net_pay_minor   BIGINT,         -- sum of |PAY obligations| == sum of RECEIVE (zero-sum)
    excluded_participant_id TEXT,         -- set on an unwind re-run (D-19-2)
    supersedes_netting_run_id TEXT REFERENCES ledger.netting_runs(netting_run_id),
    failure_detail        JSONB,          -- FAILED_INTEGRITY forensic detail
    computed_at           TIMESTAMPTZ,    -- row column; NEVER inside the hashed result (D-19-7)
    created_at            TIMESTAMPTZ NOT NULL,
    schema_version        SMALLINT NOT NULL DEFAULT 1,
    CONSTRAINT nrun_passed_complete CHECK (
        status <> 'PASSED' OR (result_hash IS NOT NULL AND result_pointer IS NOT NULL
                               AND total_net_pay_minor IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS idx_nrun_window
    ON ledger.netting_runs (settlement_window_id, created_at DESC);
-- at most one non-superseded PASSED run per window:
CREATE UNIQUE INDEX IF NOT EXISTS uidx_nrun_one_live
    ON ledger.netting_runs (settlement_window_id)
    WHERE status = 'PASSED';

-- ============================================================
-- NETTING OBLIGATIONS — one participant's net obligation per run
-- ============================================================
CREATE TABLE IF NOT EXISTS ledger.netting_obligations (
    obligation_id         TEXT PRIMARY KEY
                            CHECK (obligation_id LIKE 'nobl_%'),
    netting_run_id        TEXT NOT NULL REFERENCES ledger.netting_runs(netting_run_id),
    participant_id        TEXT NOT NULL,
    net_position_minor    BIGINT NOT NULL,   -- signed: negative = net debtor
    direction             TEXT NOT NULL CHECK (direction IN ('PAY','RECEIVE','FLAT')),
    obligation_minor      BIGINT NOT NULL CHECK (obligation_minor >= 0),
    settlement_instruction_id TEXT,          -- sinst_<...>; NULL for FLAT
    schema_version        SMALLINT NOT NULL DEFAULT 1,
    UNIQUE (netting_run_id, participant_id),
    CONSTRAINT nobl_direction_sign CHECK (
        (direction = 'PAY'     AND net_position_minor < 0 AND obligation_minor = -net_position_minor) OR
        (direction = 'RECEIVE' AND net_position_minor > 0 AND obligation_minor =  net_position_minor) OR
        (direction = 'FLAT'    AND net_position_minor = 0 AND obligation_minor = 0))
);

-- ============================================================
-- D-19-4: settlement_instructions amendment (spec/04 table; additive)
-- ============================================================
ALTER TABLE ledger.settlement_instructions
    ADD COLUMN IF NOT EXISTS instruction_kind TEXT NOT NULL DEFAULT 'MERCHANT_PAYOUT';
ALTER TABLE ledger.settlement_instructions
    DROP CONSTRAINT IF EXISTS sinst_instruction_kind_check;
ALTER TABLE ledger.settlement_instructions
    ADD CONSTRAINT sinst_instruction_kind_check
    CHECK (instruction_kind IN ('MERCHANT_PAYOUT','PSO_NETTING'));
ALTER TABLE ledger.settlement_instructions ADD COLUMN IF NOT EXISTS participant_id TEXT;
ALTER TABLE ledger.settlement_instructions ADD COLUMN IF NOT EXISTS netting_run_id TEXT;
ALTER TABLE ledger.settlement_instructions ALTER COLUMN payment_intent_id DROP NOT NULL;
ALTER TABLE ledger.settlement_instructions ALTER COLUMN merchant_id DROP NOT NULL;
ALTER TABLE ledger.settlement_instructions DROP CONSTRAINT IF EXISTS sinst_kind_shape;
ALTER TABLE ledger.settlement_instructions ADD CONSTRAINT sinst_kind_shape CHECK (
    (instruction_kind = 'MERCHANT_PAYOUT' AND payment_intent_id IS NOT NULL AND merchant_id IS NOT NULL
       AND participant_id IS NULL AND netting_run_id IS NULL)
    OR
    (instruction_kind = 'PSO_NETTING' AND participant_id IS NOT NULL AND netting_run_id IS NOT NULL
       AND payment_intent_id IS NULL AND merchant_id IS NULL
       AND fee_minor = 0 AND net_payout_minor = amount_minor AND mcc_category = 'OTHER'));
-- Existing rows: instruction_kind defaults to MERCHANT_PAYOUT; behavior
-- bit-identical to spec/04 (golden test pins: with zero PSO_NETTING rows,
-- every spec/04 query/recon path is unchanged).

-- N19-6: netting legs dispatch per-window (window FSM), never via
-- merchant-payout batches — batch_id stays NULL through the leg lifecycle.
ALTER TABLE ledger.settlement_instructions DROP CONSTRAINT IF EXISTS sinst_batch_assignment;
ALTER TABLE ledger.settlement_instructions ADD CONSTRAINT sinst_batch_assignment CHECK (
    batch_id IS NOT NULL OR status = 'QUEUED' OR instruction_kind = 'PSO_NETTING'
);

CREATE INDEX IF NOT EXISTS sinst_netting_run_idx
    ON ledger.settlement_instructions (netting_run_id, participant_id)
    WHERE instruction_kind = 'PSO_NETTING';

-- ============================================================
-- D-19-6: window attribution written by the posting path (N19-1).
-- Column identical to 0103's (ADD COLUMN IF NOT EXISTS keeps 0102/0103
-- order-independent); the trigger mirrors the reference-encoded attribution
-- into the spec-literal metadata key in the SAME insert.
-- ============================================================
ALTER TABLE ledger.journal_entries
    ADD COLUMN IF NOT EXISTS metadata JSONB NOT NULL DEFAULT '{}'::jsonb;

CREATE OR REPLACE FUNCTION ledger.attribute_window_entry()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.reference_type = 'SETTLEMENT'
       AND NEW.entry_type IN ('position_updated','payment_reversed')
       AND NOT (NEW.metadata ? 'settlement_window_id') THEN
        NEW.metadata := NEW.metadata
            || jsonb_build_object('settlement_window_id', NEW.reference_id);
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_attribute_window_entry ON ledger.journal_entries;
CREATE TRIGGER trg_attribute_window_entry
    BEFORE INSERT ON ledger.journal_entries
    FOR EACH ROW EXECUTE FUNCTION ledger.attribute_window_entry();

-- ============================================================
-- D-19 trigger guard: suspended participants refused mid-window, plus the
-- N19-5 unwind-reversal path. Everything else matches 0012 exactly.
-- ============================================================
CREATE OR REPLACE FUNCTION ledger.enforce_net_debit_cap()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ledger, pg_temp
AS $$
DECLARE
  v_account_subtype       TEXT;
  v_participant_id        TEXT;
  v_posn_row              ledger.position_accounts%ROWTYPE;
  v_active_cap            BIGINT;
  v_new_net_position      BIGINT;
  v_headroom              BIGINT;
  v_kyb_status            TEXT;
  v_entry_type            TEXT;
  v_ref_type              TEXT;
  v_ref_id                TEXT;
  v_rows                  INTEGER;
BEGIN
  IF NEW.side <> 'DEBIT' THEN RETURN NEW; END IF;

  SELECT a.account_subtype, a.owner_id
  INTO v_account_subtype, v_participant_id
  FROM ledger.accounts a
  WHERE a.account_id = NEW.account_id;

  IF v_account_subtype IS DISTINCT FROM 'PARTICIPANT_POSITION' THEN RETURN NEW; END IF;

  -- Serialize per participant (prevents concurrent window races).
  PERFORM pg_advisory_xact_lock(hashtext('NDC:' || v_participant_id));

  -- N19-5: D-19-2 unwind reversals — operator-approved offsetting entries
  -- on a post-cutoff window apply the maintained delta with NO cap check
  -- (refusing a reversal would strand the window; the cap governs forward
  -- exposure at accept time).
  SELECT je.entry_type, je.reference_type, je.reference_id
  INTO v_entry_type, v_ref_type, v_ref_id
  FROM ledger.journal_entries je
  WHERE je.entry_id = NEW.entry_id;

  IF v_entry_type = 'payment_reversed' AND v_ref_type = 'SETTLEMENT'
     AND EXISTS (
       SELECT 1 FROM ledger.settlement_windows sw
       WHERE sw.window_id = v_ref_id
         AND sw.status IN ('CUTOFF_REACHED','SETTLEMENT_FAILED')
     ) THEN
    UPDATE ledger.position_accounts
    SET net_position_minor = net_position_minor - NEW.amount_minor,
        debit_count        = debit_count + 1,
        last_movement_at   = clock_timestamp()
    WHERE participant_id = v_participant_id
      AND settlement_window_id = v_ref_id;
    GET DIAGNOSTICS v_rows = ROW_COUNT;
    IF v_rows = 0 THEN
      RAISE EXCEPTION 'NDC_UNWIND_NO_POSITION_ROW: participant % has no position in window %',
        v_participant_id, v_ref_id
        USING ERRCODE = 'P0001';
    END IF;
    RETURN NEW;
  END IF;

  -- D-19 guard (spec/19): suspended participants refused mid-window. Fires
  -- only when a participants row EXISTS (N19-4; cross-package ref is
  -- app-enforced per E20 — pre-PSO flows carry no participants rows).
  SELECT p.kyb_status INTO v_kyb_status
  FROM public.participants p
  WHERE p.participant_id = v_participant_id;

  IF FOUND AND v_kyb_status <> 'ACTIVE' THEN
    SELECT pa.*
    INTO v_posn_row
    FROM ledger.position_accounts pa
    JOIN ledger.settlement_windows sw ON pa.settlement_window_id = sw.window_id
    WHERE pa.participant_id = v_participant_id
      AND sw.status = 'ACCUMULATING_POSITIONS'
    ORDER BY pa.created_at DESC
    LIMIT 1;

    INSERT INTO ledger.ndc_breach_log (
      breach_id, participant_id, settlement_window_id, position_account_id,
      attempted_debit_minor, net_position_before_minor, net_debit_cap_minor,
      headroom_before_minor, blocked_at, blocked_posting_entry_id
    ) VALUES (
      'ndc_' || left(encode(sha256(
        (NEW.posting_id || 'PARTICIPANT_NOT_ACTIVE' || clock_timestamp()::text)::bytea
      ), 'hex'), 24),
      v_participant_id,
      COALESCE(v_posn_row.settlement_window_id, 'NONE'),
      COALESCE(v_posn_row.position_account_id, 'NONE'),
      NEW.amount_minor,
      COALESCE(v_posn_row.net_position_minor, 0),
      COALESCE(v_posn_row.net_debit_cap_minor, 0),
      COALESCE(v_posn_row.net_debit_cap_minor, 0)
        - GREATEST(0, -COALESCE(v_posn_row.net_position_minor, 0)),
      clock_timestamp(),
      NEW.entry_id
    );
    RAISE EXCEPTION 'NDC_PARTICIPANT_NOT_ACTIVE: %', v_participant_id
      USING ERRCODE = 'P0001';
  END IF;

  -- ---- from here: byte-identical decision logic to 0012 ----
  SELECT pa.*
  INTO v_posn_row
  FROM ledger.position_accounts pa
  JOIN ledger.settlement_windows sw ON pa.settlement_window_id = sw.window_id
  WHERE pa.participant_id = v_participant_id
    AND sw.status = 'ACCUMULATING_POSITIONS'
  ORDER BY pa.created_at DESC
  LIMIT 1;

  IF NOT FOUND THEN
    INSERT INTO ledger.ndc_breach_log (
      breach_id, participant_id, settlement_window_id, position_account_id,
      attempted_debit_minor, net_position_before_minor, net_debit_cap_minor,
      headroom_before_minor, blocked_at, blocked_posting_entry_id
    ) VALUES (
      'ndc_' || left(encode(sha256(
        (NEW.posting_id || 'NO_OPEN_WINDOW' || clock_timestamp()::text)::bytea
      ), 'hex'), 24),
      v_participant_id, 'NONE', 'NONE',
      NEW.amount_minor, 0, 0, 0, clock_timestamp(), NEW.entry_id
    );
    RAISE EXCEPTION 'NDC_NO_OPEN_WINDOW: No ACCUMULATING_POSITIONS window for participant %',
      v_participant_id
      USING ERRCODE = 'P0001';
  END IF;

  SELECT cap_amount_minor
  INTO v_active_cap
  FROM ledger.net_debit_caps
  WHERE participant_id = v_participant_id
    AND superseded_at IS NULL
  LIMIT 1;

  IF NOT FOUND THEN
    RAISE EXCEPTION 'NDC_NOT_CONFIGURED: No active Net Debit Cap for participant %',
      v_participant_id
      USING ERRCODE = 'P0001';
  END IF;

  v_new_net_position := v_posn_row.net_position_minor - NEW.amount_minor;
  v_headroom := v_active_cap - GREATEST(0, -v_new_net_position);

  IF v_headroom < 0 THEN
    INSERT INTO ledger.ndc_breach_log (
      breach_id, participant_id, settlement_window_id, position_account_id,
      attempted_debit_minor, net_position_before_minor, net_debit_cap_minor,
      headroom_before_minor, blocked_at, blocked_posting_entry_id
    ) VALUES (
      'ndc_' || left(encode(sha256(
        (NEW.posting_id || v_posn_row.position_account_id
         || clock_timestamp()::text)::bytea
      ), 'hex'), 24),
      v_participant_id,
      v_posn_row.settlement_window_id,
      v_posn_row.position_account_id,
      NEW.amount_minor,
      v_posn_row.net_position_minor,
      v_active_cap,
      v_active_cap - GREATEST(0, -v_posn_row.net_position_minor),
      clock_timestamp(),
      NEW.entry_id
    );
    RAISE EXCEPTION
      'NDC_BREACH: Participant % net debit would be % paisa, exceeding cap % paisa (headroom %)',
      v_participant_id,
      -v_new_net_position,
      v_active_cap,
      v_headroom
      USING ERRCODE = 'P0001';
  END IF;

  UPDATE ledger.position_accounts
  SET net_position_minor = v_new_net_position,
      debit_count        = debit_count + 1,
      last_movement_at   = clock_timestamp()
  WHERE position_account_id = v_posn_row.position_account_id;

  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_enforce_net_debit_cap ON ledger.postings;
CREATE TRIGGER trg_enforce_net_debit_cap
  BEFORE INSERT ON ledger.postings
  FOR EACH ROW EXECUTE FUNCTION ledger.enforce_net_debit_cap();

-- Companion: CREDIT postings — same guard + reversal path (N19-4/04).
CREATE OR REPLACE FUNCTION ledger.update_position_on_credit()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ledger, pg_temp
AS $$
DECLARE
  v_account_subtype TEXT;
  v_participant_id  TEXT;
  v_kyb_status      TEXT;
  v_entry_type      TEXT;
  v_ref_type        TEXT;
  v_ref_id          TEXT;
  v_rows            INTEGER;
BEGIN
  IF NEW.side <> 'CREDIT' THEN RETURN NEW; END IF;

  SELECT a.account_subtype, a.owner_id
  INTO v_account_subtype, v_participant_id
  FROM ledger.accounts a WHERE a.account_id = NEW.account_id;

  IF v_account_subtype IS DISTINCT FROM 'PARTICIPANT_POSITION' THEN RETURN NEW; END IF;

  PERFORM pg_advisory_xact_lock(hashtext('NDC:' || v_participant_id));

  SELECT je.entry_type, je.reference_type, je.reference_id
  INTO v_entry_type, v_ref_type, v_ref_id
  FROM ledger.journal_entries je
  WHERE je.entry_id = NEW.entry_id;

  IF v_entry_type = 'payment_reversed' AND v_ref_type = 'SETTLEMENT'
     AND EXISTS (
       SELECT 1 FROM ledger.settlement_windows sw
       WHERE sw.window_id = v_ref_id
         AND sw.status IN ('CUTOFF_REACHED','SETTLEMENT_FAILED')
     ) THEN
    UPDATE ledger.position_accounts
    SET net_position_minor = net_position_minor + NEW.amount_minor,
        credit_count       = credit_count + 1,
        last_movement_at   = clock_timestamp()
    WHERE participant_id = v_participant_id
      AND settlement_window_id = v_ref_id;
    GET DIAGNOSTICS v_rows = ROW_COUNT;
    IF v_rows = 0 THEN
      RAISE EXCEPTION 'NDC_UNWIND_NO_POSITION_ROW: participant % has no position in window %',
        v_participant_id, v_ref_id
        USING ERRCODE = 'P0001';
    END IF;
    RETURN NEW;
  END IF;

  SELECT p.kyb_status INTO v_kyb_status
  FROM public.participants p
  WHERE p.participant_id = v_participant_id;

  IF FOUND AND v_kyb_status <> 'ACTIVE' THEN
    RAISE EXCEPTION 'NDC_PARTICIPANT_NOT_ACTIVE: %', v_participant_id
      USING ERRCODE = 'P0001';
  END IF;

  UPDATE ledger.position_accounts pa
  SET net_position_minor = pa.net_position_minor + NEW.amount_minor,
      credit_count       = pa.credit_count + 1,
      last_movement_at   = clock_timestamp()
  FROM ledger.settlement_windows sw
  WHERE pa.participant_id = v_participant_id
    AND pa.settlement_window_id = sw.window_id
    AND sw.status = 'ACCUMULATING_POSITIONS';

  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_update_position_on_credit ON ledger.postings;
CREATE TRIGGER trg_update_position_on_credit
  BEFORE INSERT ON ledger.postings
  FOR EACH ROW EXECUTE FUNCTION ledger.update_position_on_credit();

-- ============================================================
-- Grants + append-only posture
-- ============================================================
GRANT SELECT, INSERT, UPDATE ON ledger.netting_runs        TO ledger_app_role;
GRANT SELECT, INSERT         ON ledger.netting_obligations TO ledger_app_role;
GRANT SELECT ON ledger.netting_runs, ledger.netting_obligations TO ledger_ro;

-- Obligations are append-only for the app role (results are immutable; an
-- unwind writes a NEW run, never edits the old one).
ALTER TABLE ledger.netting_obligations ENABLE ROW LEVEL SECURITY;
DO $$
BEGIN
    CREATE POLICY nobl_append_only ON ledger.netting_obligations FOR ALL TO ledger_app_role
        USING (TRUE) WITH CHECK (TRUE);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;
DO $$
BEGIN
    CREATE POLICY nobl_no_update ON ledger.netting_obligations AS RESTRICTIVE FOR UPDATE
        TO ledger_app_role USING (FALSE);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;
DO $$
BEGIN
    CREATE POLICY nobl_no_delete ON ledger.netting_obligations AS RESTRICTIVE FOR DELETE
        TO ledger_app_role USING (FALSE);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;
