-- 0012_pso_position.sql — PSO-mode position accounts, Net Debit Caps,
-- settlement windows, and the DB-layer NDC enforcement trigger (spec/05).
--
-- Errata applied:
--  * LA-L4: spec/05 places these tables in a 'bdpay' schema, but the NDC
--    trigger joins ledger.postings/ledger.accounts — the tables live in the
--    ledger schema so the trigger composes (one schema, one search_path).
--  * The application-side mirror of enforce_net_debit_cap() is
--    bdpay/ledger/position.py::PositionService (identical decision logic,
--    unit-tested); this trigger is the authoritative safety net.

-- ============================================================
-- SETTLEMENT WINDOWS (PSO DNS sessions)
-- ============================================================
DO $$
BEGIN
    CREATE TYPE ledger.settlement_window_status AS ENUM (
        'WINDOW_OPEN',
        'ACCUMULATING_POSITIONS',
        'CUTOFF_REACHED',
        'NETTING_CALCULATED',
        'WINDOW_BLOCKED',
        'SETTLEMENT_INSTRUCTED',
        'SETTLEMENT_CONFIRMED',
        'RECONCILIATION_RUNNING',
        'RECONCILIATION_EXCEPTION',
        'WINDOW_CLOSED',
        'SETTLEMENT_FAILED'
    );
EXCEPTION WHEN duplicate_object THEN
    NULL;
END $$;

CREATE TABLE IF NOT EXISTS ledger.settlement_windows (
    window_id               TEXT PRIMARY KEY
                              CHECK (window_id LIKE 'swin_%'),
    dns_session             TEXT NOT NULL,
    window_date             DATE NOT NULL,
    status                  ledger.settlement_window_status NOT NULL DEFAULT 'WINDOW_OPEN',
    opened_at               TIMESTAMPTZ NOT NULL,
    cutoff_at               TIMESTAMPTZ,
    netting_completed_at    TIMESTAMPTZ,
    instructed_at           TIMESTAMPTZ,
    confirmed_at            TIMESTAMPTZ,
    closed_at               TIMESTAMPTZ,
    net_settlement_amount_minor BIGINT,
    currency                CHAR(3) NOT NULL DEFAULT 'BDT',
    block_reason            TEXT,
    blocked_at              TIMESTAMPTZ,
    unblocked_at            TIMESTAMPTZ,
    shortfall_override      BOOLEAN NOT NULL DEFAULT FALSE,
    shortfall_override_approval_id TEXT,
    settlement_attempt_number INTEGER NOT NULL DEFAULT 1,
    settlement_batch_id     TEXT,  -- FK -> settlement_batches (0011; app-enforced)
    schema_version          SMALLINT NOT NULL DEFAULT 1,

    UNIQUE (window_date, dns_session)
);
CREATE INDEX IF NOT EXISTS swin_status_idx
    ON ledger.settlement_windows (status, window_date DESC);

-- ============================================================
-- POSITION ACCOUNTS — one per (participant, window).
-- ============================================================
CREATE TABLE IF NOT EXISTS ledger.position_accounts (
    position_account_id     TEXT PRIMARY KEY
                              CHECK (position_account_id LIKE 'posn_%'),
    participant_id          TEXT NOT NULL,  -- FK -> participants (spec/08; app-enforced)
    settlement_window_id    TEXT NOT NULL REFERENCES ledger.settlement_windows(window_id),
    net_position_minor      BIGINT NOT NULL DEFAULT 0,
    currency                CHAR(3) NOT NULL DEFAULT 'BDT',
    net_debit_cap_minor     BIGINT NOT NULL,
    debit_count             INTEGER NOT NULL DEFAULT 0,
    credit_count            INTEGER NOT NULL DEFAULT 0,
    last_movement_at        TIMESTAMPTZ,
    window_state            TEXT NOT NULL,
    created_at              TIMESTAMPTZ NOT NULL,
    schema_version          SMALLINT NOT NULL DEFAULT 1,

    UNIQUE (participant_id, settlement_window_id)
);
CREATE INDEX IF NOT EXISTS posn_window_idx
    ON ledger.position_accounts (settlement_window_id, participant_id);
CREATE INDEX IF NOT EXISTS posn_participant_idx
    ON ledger.position_accounts (participant_id, created_at DESC);

-- ============================================================
-- NET DEBIT CAPS — one ACTIVE per participant; superseded rows retained.
-- ============================================================
CREATE TABLE IF NOT EXISTS ledger.net_debit_caps (
    ndc_id                  TEXT PRIMARY KEY
                              CHECK (ndc_id LIKE 'ndc_%'),
    participant_id          TEXT NOT NULL,
    cap_amount_minor        BIGINT NOT NULL
                              CHECK (cap_amount_minor > 0),
    currency                CHAR(3) NOT NULL DEFAULT 'BDT',
    effective_from          TIMESTAMPTZ NOT NULL,
    superseded_at           TIMESTAMPTZ,
    approved_by             TEXT NOT NULL,
    approval_request_id     TEXT NOT NULL,
    rationale               TEXT NOT NULL
                              CHECK (char_length(rationale) <= 1000),
    created_at              TIMESTAMPTZ NOT NULL,
    schema_version          SMALLINT NOT NULL DEFAULT 1
);
CREATE UNIQUE INDEX IF NOT EXISTS ndc_active_per_participant
    ON ledger.net_debit_caps (participant_id)
    WHERE superseded_at IS NULL;
CREATE INDEX IF NOT EXISTS ndc_participant_idx
    ON ledger.net_debit_caps (participant_id, effective_from DESC);

-- ============================================================
-- NDC BREACH LOG — append-only forensic table.
-- ============================================================
CREATE TABLE IF NOT EXISTS ledger.ndc_breach_log (
    breach_id               TEXT PRIMARY KEY
                              CHECK (breach_id LIKE 'ndc_%'),
    participant_id          TEXT NOT NULL,
    settlement_window_id    TEXT NOT NULL,
    position_account_id     TEXT NOT NULL,
    attempted_debit_minor   BIGINT NOT NULL CHECK (attempted_debit_minor > 0),
    net_position_before_minor BIGINT NOT NULL,
    net_debit_cap_minor     BIGINT NOT NULL,
    headroom_before_minor   BIGINT NOT NULL,
    blocked_at              TIMESTAMPTZ NOT NULL,
    blocked_posting_entry_id TEXT,
    source_payment_id       TEXT,
    schema_version          SMALLINT NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS ndcb_participant_idx
    ON ledger.ndc_breach_log (participant_id, blocked_at DESC);
CREATE INDEX IF NOT EXISTS ndcb_window_idx
    ON ledger.ndc_breach_log (settlement_window_id);

-- ============================================================
-- DB-LAYER NET DEBIT CAP ENFORCEMENT (the authoritative safety net).
-- Fires on DEBIT postings to PARTICIPANT_POSITION accounts; validates the
-- net position BEFORE the posting commits; raises on breach. Application
-- checks (PositionService) are advisory; this trigger cannot be bypassed.
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
BEGIN
  IF NEW.side <> 'DEBIT' THEN RETURN NEW; END IF;

  SELECT a.account_subtype, a.owner_id
  INTO v_account_subtype, v_participant_id
  FROM ledger.accounts a
  WHERE a.account_id = NEW.account_id;

  IF v_account_subtype IS DISTINCT FROM 'PARTICIPANT_POSITION' THEN RETURN NEW; END IF;

  -- Serialize per participant (prevents concurrent window races).
  PERFORM pg_advisory_xact_lock(hashtext('NDC:' || v_participant_id));

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

-- Companion: CREDIT postings to PARTICIPANT_POSITION update the position.
CREATE OR REPLACE FUNCTION ledger.update_position_on_credit()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ledger, pg_temp
AS $$
DECLARE
  v_account_subtype TEXT;
  v_participant_id  TEXT;
BEGIN
  IF NEW.side <> 'CREDIT' THEN RETURN NEW; END IF;

  SELECT a.account_subtype, a.owner_id
  INTO v_account_subtype, v_participant_id
  FROM ledger.accounts a WHERE a.account_id = NEW.account_id;

  IF v_account_subtype IS DISTINCT FROM 'PARTICIPANT_POSITION' THEN RETURN NEW; END IF;

  PERFORM pg_advisory_xact_lock(hashtext('NDC:' || v_participant_id));

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

GRANT SELECT, INSERT, UPDATE ON ledger.settlement_windows TO ledger_app_role;
GRANT SELECT, INSERT, UPDATE ON ledger.position_accounts  TO ledger_app_role;
GRANT SELECT, INSERT, UPDATE ON ledger.net_debit_caps     TO ledger_app_role;
GRANT SELECT, INSERT         ON ledger.ndc_breach_log     TO ledger_app_role;
GRANT SELECT ON ledger.settlement_windows, ledger.position_accounts,
    ledger.net_debit_caps, ledger.ndc_breach_log TO ledger_ro;

-- Breach log is append-only for the app role.
ALTER TABLE ledger.ndc_breach_log ENABLE ROW LEVEL SECURITY;
DO $$
BEGIN
    CREATE POLICY ndcb_append_only ON ledger.ndc_breach_log FOR ALL TO ledger_app_role
        USING (TRUE) WITH CHECK (TRUE);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;
DO $$
BEGIN
    CREATE POLICY ndcb_no_update ON ledger.ndc_breach_log AS RESTRICTIVE FOR UPDATE
        TO ledger_app_role USING (FALSE);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;
DO $$
BEGIN
    CREATE POLICY ndcb_no_delete ON ledger.ndc_breach_log AS RESTRICTIVE FOR DELETE
        TO ledger_app_role USING (FALSE);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;
