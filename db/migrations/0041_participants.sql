-- 0041_participants.sql — canonical participants table (PSO mode; spec/00 §2,
-- spec/08 §10 cross-reference; errata K-14: no spec ships the DDL, authored
-- with exactly the columns spec/08 writes).

CREATE TABLE participants (
    participant_id   TEXT        PRIMARY KEY,   -- part_<sha256[:24]>
    institution_name TEXT        NOT NULL,
    institution_type TEXT        NOT NULL
        CHECK (institution_type IN (
            'SCHEDULED_BANK', 'NBFI', 'LICENSED_PSO', 'LICENSED_PSP', 'MFS_OPERATOR'
        )),
    kyb_status       TEXT        NOT NULL DEFAULT 'APPLICATION_SUBMITTED'
        CHECK (kyb_status IN (
            'APPLICATION_SUBMITTED', 'LICENSE_VERIFICATION_PENDING',
            'SETTLEMENT_AGREEMENT_PENDING', 'NDC_ASSIGNMENT_PENDING',
            'ACTIVE', 'SUSPENDED', 'REJECTED', 'TERMINATED'
        )),
    bb_license_number TEXT,
    bb_license_type   TEXT,
    bb_license_expiry DATE,
    net_debit_cap_id  TEXT,                     -- ndc_<...>; set on NDC assignment
    activated_at      TIMESTAMPTZ,
    created_at        TIMESTAMPTZ NOT NULL,
    updated_at        TIMESTAMPTZ NOT NULL,
    schema_version    SMALLINT    NOT NULL DEFAULT 1
);

CREATE INDEX idx_participants_status ON participants (kyb_status);
