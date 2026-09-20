-- 0075_sanctions_screenings.sql — spec/07 Data model: sanctions_screenings
-- (per-screen audit; EVERY screen recorded, including CLEAR).
--
-- Errata E-C4 (E1-class defect): spec/07 declares screening_id TEXT PRIMARY
-- KEY together with PARTITION BY RANGE (screened_at); Postgres rejects a
-- unique constraint that omits the partition key. Ships UNPARTITIONED with
-- the true primary key (E1 precedent); retention by indexed deletes on
-- screened_at until an explicit partitioning migration.

CREATE TABLE sanctions_screenings (
    screening_id      TEXT PRIMARY KEY,           -- scrn_<sha256_canonical[:24]>
    subject_type      TEXT NOT NULL CHECK (subject_type IN ('Customer','Merchant','Participant','UBO','EXTERNAL_PARTY')),
    subject_id        TEXT,                       -- nullable for EXTERNAL_PARTY
    payment_intent_id TEXT,                       -- pi_* when context=PAYMENT
    context           TEXT NOT NULL CHECK (context IN ('ONBOARDING','PAYMENT','RESCREEN','MANUAL','PERIODIC')),
    names_screened    TEXT[] NOT NULL,            -- raw inputs (PII; excluded from log emission)
    names_norm        TEXT[] NOT NULL,            -- canonical normalized forms
    identifiers_hash  TEXT NOT NULL,              -- sha256(canonical_json(identifiers))
    list_version_ids  TEXT[] NOT NULL,            -- the EXACT active versions used
    algorithm_version TEXT NOT NULL,              -- 'match_algo_v1'
    translit_version  TEXT NOT NULL,              -- content hash of the translit table
    thresholds_version TEXT NOT NULL,             -- YAML thresholds artifact version
    decision          TEXT NOT NULL CHECK (decision IN ('CLEAR','POTENTIAL_MATCH','HIT')),
    top_score         NUMERIC(5,4),               -- similarity score (NOT money; NUMERIC permitted)
    match_count       SMALLINT NOT NULL DEFAULT 0,
    result_hash       TEXT NOT NULL,              -- determinism anchor
    result_pointer    TEXT NOT NULL,              -- object store key: full ordered match array
    sanctions_hit_id  TEXT,                       -- sanc_* when decision=HIT
    rescreen_run_id   TEXT,                       -- rrun_* when context=RESCREEN/PERIODIC
    duration_ms       INTEGER NOT NULL,
    screened_at       TIMESTAMPTZ NOT NULL,
    schema_version    SMALLINT NOT NULL DEFAULT 1
);
CREATE INDEX idx_scrn_subject ON sanctions_screenings (subject_type, subject_id, screened_at DESC);
CREATE INDEX idx_scrn_decision ON sanctions_screenings (decision, screened_at DESC);
CREATE INDEX idx_scrn_payment ON sanctions_screenings (payment_intent_id) WHERE payment_intent_id IS NOT NULL;
CREATE INDEX idx_scrn_screened_at ON sanctions_screenings (screened_at);
