-- 0042_participant_license_verifications.sql — spec/08 DDL §Section 7.
--
-- K-18 (errata): the spec's idx_plv_expiry partial index predicate uses
-- CURRENT_DATE, which Postgres rejects in index predicates (functions in a
-- partial-index WHERE must be immutable). Ships as a plain b-tree index on
-- bb_license_expiry; the expiry sweep query supplies its own clock-injected
-- date bound.

CREATE TABLE participant_license_verifications (
    verification_id     TEXT PRIMARY KEY,
    -- make_id('plv', {participant_id, bb_license_number, verified_at})

    participant_id      TEXT NOT NULL REFERENCES participants (participant_id),

    bb_license_number   TEXT NOT NULL,
    bb_license_type     TEXT NOT NULL,
    bb_license_expiry   DATE NOT NULL,

    verification_method TEXT NOT NULL DEFAULT 'MANUAL_BB_REGISTRY'
        CHECK (verification_method IN ('MANUAL_BB_REGISTRY', 'AUTOMATED_API')),

    verified_by         TEXT NOT NULL,
    verified_at         TIMESTAMPTZ NOT NULL,
    verification_notes  TEXT,
    document_pointer    TEXT,

    expires_alert_sent_at TIMESTAMPTZ,

    schema_version      SMALLINT NOT NULL DEFAULT 1,
    created_at          TIMESTAMPTZ NOT NULL
);

CREATE INDEX idx_plv_participant ON participant_license_verifications (participant_id);
CREATE INDEX idx_plv_expiry      ON participant_license_verifications (bb_license_expiry);
