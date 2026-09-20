-- 0077_pep_records.sql — spec/07 Data model: pep_records.
-- Retention: 6 yr after close (KYC-anchored).

CREATE TABLE pep_records (
    pep_id            TEXT PRIMARY KEY,           -- pep_<sha256_canonical[:24]>
    subject_type      TEXT NOT NULL CHECK (subject_type IN ('Customer','Merchant','UBO','Participant')),
    subject_id        TEXT NOT NULL,
    pep_category      TEXT NOT NULL CHECK (pep_category IN
                      ('DOMESTIC_PEP','FOREIGN_PEP','INTERNATIONAL_ORG_PEP','RCA')),
    position_held     TEXT NOT NULL,
    source            TEXT NOT NULL CHECK (source IN ('SELF_DECLARED','SCREENING_VENDOR','MANUAL_RESEARCH','BFIU_NOTICE')),
    status            TEXT NOT NULL CHECK (status IN ('ACTIVE','CLOSED')),
    edd_completed_at  TIMESTAMPTZ,                -- EDD must complete before subject activation
    camlco_approved_by TEXT,                      -- CAMLCO approval to establish/continue
    camlco_approved_at TIMESTAMPTZ,
    next_review_at    TIMESTAMPTZ NOT NULL,       -- annual (PEPs are always HIGH risk)
    closed_at         TIMESTAMPTZ,
    close_approval_request_id TEXT,               -- appr_* four-eyes
    created_at        TIMESTAMPTZ NOT NULL,
    schema_version    SMALLINT NOT NULL DEFAULT 1
);
CREATE INDEX idx_pep_subject ON pep_records (subject_type, subject_id) WHERE status='ACTIVE';
CREATE INDEX idx_pep_review ON pep_records (next_review_at) WHERE status='ACTIVE';
