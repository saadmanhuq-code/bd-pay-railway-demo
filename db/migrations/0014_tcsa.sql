-- 0014_tcsa.sql — TCSA coverage snapshots + EOD certifications (spec/05).
--
-- Errata applied:
--  * LA-L1 (E1 class): tcsa_snapshots ships UNPARTITIONED — the spec's
--    PARTITION BY RANGE (snapshotted_at) with PRIMARY KEY (snapshot_id) is
--    impossible in Postgres 16.
--  * LA-L3: the balance subtype is the canonical 'SPONSOR_BANK_TCSA'
--    (spec/05's queries say 'TCSA', which is not in the spec/03 subtype
--    vocabulary); balances are normal-balance signed sums.

CREATE TABLE IF NOT EXISTS ledger.tcsa_snapshots (
    snapshot_id             TEXT PRIMARY KEY
                              CHECK (snapshot_id LIKE 'tcsa_%'),
    snapshotted_at          TIMESTAMPTZ NOT NULL,
    platform_mode           TEXT NOT NULL
                              CHECK (platform_mode IN ('PSP','PSO')),
    tcsa_balance_minor      BIGINT NOT NULL,
    currency                CHAR(3) NOT NULL DEFAULT 'BDT',
    outstanding_merchant_liability_minor  BIGINT NOT NULL,
    outstanding_customer_float_minor      BIGINT NOT NULL DEFAULT 0,
    total_outstanding_liability_minor     BIGINT GENERATED ALWAYS AS
        (outstanding_merchant_liability_minor + outstanding_customer_float_minor) STORED,
    coverage_surplus_minor  BIGINT GENERATED ALWAYS AS
        (tcsa_balance_minor
         - (outstanding_merchant_liability_minor + outstanding_customer_float_minor)) STORED,
    coverage_ratio_bps      BIGINT,   -- NULL when liability = 0
    status                  TEXT NOT NULL
                              CHECK (status IN ('OK','SHORTFALL','BLOCKED')),
    shortfall_minor         BIGINT NOT NULL DEFAULT 0
                              CHECK (shortfall_minor >= 0),
    shortfall_days_running  INTEGER NOT NULL DEFAULT 0
                              CHECK (shortfall_days_running >= 0),
    penalty_exposure_minor  BIGINT NOT NULL DEFAULT 0
                              CHECK (penalty_exposure_minor >= 0),
    payout_initiation_blocked   BOOLEAN NOT NULL DEFAULT FALSE,
    alert_dispatched            BOOLEAN NOT NULL DEFAULT FALSE,
    alert_dispatched_at         TIMESTAMPTZ,
    ledger_chain_index_at_snap  BIGINT NOT NULL,
    ledger_chain_hash_at_snap   TEXT NOT NULL,
    produced_by             TEXT NOT NULL DEFAULT 'tcsa-monitor@1',
    schema_version          SMALLINT NOT NULL DEFAULT 1,

    CONSTRAINT tcsa_shortfall_consistency
      CHECK (
        (status = 'OK'        AND shortfall_minor = 0) OR
        (status = 'SHORTFALL' AND shortfall_minor > 0) OR
        (status = 'BLOCKED'   AND shortfall_minor > 0)
      )
);

CREATE INDEX IF NOT EXISTS tcsa_snap_at_idx ON ledger.tcsa_snapshots (snapshotted_at DESC);
CREATE INDEX IF NOT EXISTS tcsa_snap_status_idx
    ON ledger.tcsa_snapshots (status, snapshotted_at DESC);

-- Append-only for the application role.
ALTER TABLE ledger.tcsa_snapshots ENABLE ROW LEVEL SECURITY;
DO $$
BEGIN
    CREATE POLICY tcsa_snapshots_append_only ON ledger.tcsa_snapshots FOR ALL
        TO ledger_app_role USING (TRUE) WITH CHECK (TRUE);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;
DO $$
BEGIN
    CREATE POLICY tcsa_snapshots_no_update ON ledger.tcsa_snapshots AS RESTRICTIVE FOR UPDATE
        TO ledger_app_role USING (FALSE);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;
DO $$
BEGIN
    CREATE POLICY tcsa_snapshots_no_delete ON ledger.tcsa_snapshots AS RESTRICTIVE FOR DELETE
        TO ledger_app_role USING (FALSE);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- EOD certification: one per BD calendar day; CAMLCO acknowledgement is the
-- only permitted update path (application-enforced column subset).
CREATE TABLE IF NOT EXISTS ledger.tcsa_eod_certifications (
    certification_id        TEXT PRIMARY KEY
                              CHECK (certification_id LIKE 'tcsa_%'),
    certification_date      DATE NOT NULL UNIQUE,
    is_working_day          BOOLEAN NOT NULL,
    status                  TEXT NOT NULL
                              CHECK (status IN ('OK','SHORTFALL','ACKNOWLEDGED')),
    supporting_snapshot_id  TEXT NOT NULL REFERENCES ledger.tcsa_snapshots(snapshot_id),
    tcsa_balance_minor      BIGINT NOT NULL,
    total_outstanding_liability_minor BIGINT NOT NULL,
    shortfall_minor         BIGINT NOT NULL DEFAULT 0,
    penalty_exposure_minor  BIGINT NOT NULL DEFAULT 0,
    shortfall_days_running  INTEGER NOT NULL DEFAULT 0,
    camlco_acknowledged     BOOLEAN NOT NULL DEFAULT FALSE,
    camlco_acknowledged_by  TEXT,
    camlco_acknowledged_at  TIMESTAMPTZ,
    acknowledgement_note    TEXT,
    personal_liability_flagged  BOOLEAN NOT NULL DEFAULT FALSE,
    personal_liability_flagged_at TIMESTAMPTZ,
    certified_at            TIMESTAMPTZ NOT NULL,
    produced_by             TEXT NOT NULL DEFAULT 'tcsa-monitor@1',
    schema_version          SMALLINT NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS tcsa_eod_date_idx
    ON ledger.tcsa_eod_certifications (certification_date DESC);

GRANT SELECT, INSERT         ON ledger.tcsa_snapshots          TO ledger_app_role;
GRANT SELECT, INSERT, UPDATE ON ledger.tcsa_eod_certifications TO ledger_app_role;
GRANT SELECT ON ledger.tcsa_snapshots, ledger.tcsa_eod_certifications TO ledger_ro;
