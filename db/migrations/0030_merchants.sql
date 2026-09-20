-- 0030_merchants.sql — canonical merchants table (spec/00 §2; spec/08 §10).
--
-- spec/08 cross-references "merchants table (defined by spec 02 kernel)" but
-- no spec ships its DDL (errata K-14): authored here with exactly the
-- columns the specs read/write — the spec/08 §10 mirror columns
-- (kyb_status, kyb_record_id, risk_tier, payout_blocked, mdr_basis_points,
-- settlement_cycle_days, activated_at) used by the spec/08 activation gates
-- the kernel re-checks on every payment initiation.

CREATE TABLE merchants (
    merchant_id           TEXT        PRIMARY KEY,   -- mrch_<sha256[:24]>
    legal_name            TEXT        NOT NULL,
    kyb_status            TEXT        NOT NULL DEFAULT 'APPLICATION_SUBMITTED'
        CHECK (kyb_status IN (
            'APPLICATION_SUBMITTED', 'DOCUMENTS_PENDING', 'KYB_IN_PROGRESS',
            'ENHANCED_DUE_DILIGENCE', 'PENDING_HUMAN_REVIEW', 'SANCTIONS_REVIEW',
            'RISK_ASSESSED', 'AGREEMENT_PENDING', 'ACTIVE', 'SUSPENDED',
            'KYB_REFRESH_PENDING', 'REJECTED', 'TERMINATED'
        )),
    kyb_record_id         TEXT,                      -- kyb_<...>; FK added app-side (record
                                                     -- and merchant insert in one txn)
    risk_tier             TEXT
        CHECK (risk_tier IN ('LOW', 'MEDIUM', 'HIGH') OR risk_tier IS NULL),
    payout_blocked        BOOLEAN     NOT NULL DEFAULT FALSE,
    mdr_basis_points      SMALLINT
        CHECK (mdr_basis_points BETWEEN 0 AND 10000 OR mdr_basis_points IS NULL),
    settlement_cycle_days SMALLINT
        CHECK (settlement_cycle_days BETWEEN 1 AND 7 OR settlement_cycle_days IS NULL),
    activated_at          TIMESTAMPTZ,
    created_at            TIMESTAMPTZ NOT NULL,
    updated_at            TIMESTAMPTZ NOT NULL,
    schema_version        SMALLINT    NOT NULL DEFAULT 1
);

CREATE INDEX idx_merchants_status ON merchants (kyb_status);
