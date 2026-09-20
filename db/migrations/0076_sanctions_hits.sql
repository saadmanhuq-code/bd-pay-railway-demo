-- 0076_sanctions_hits.sql — spec/07 Data model: sanctions_hits (retention
-- indefinite), screening_whitelist (false-positive suppression), rescreen_runs.

CREATE TABLE sanctions_hits (
    sanc_id           TEXT PRIMARY KEY,           -- sanc_<sha256(canonical_json({screening_id, entry_id}))[:24]>
    screening_id      TEXT NOT NULL REFERENCES sanctions_screenings(screening_id),
    entry_id          TEXT NOT NULL REFERENCES sanctions_list_entries(entry_id),
    entry_content_hash TEXT NOT NULL,             -- denormalized for whitelist anchoring
    list_code         TEXT NOT NULL,
    list_version_id   TEXT NOT NULL,
    subject_type      TEXT NOT NULL,
    subject_id        TEXT,
    payment_intent_id TEXT,
    status            TEXT NOT NULL CHECK (status IN
                      ('DETECTED_FROZEN','UNDER_REVIEW','CONFIRMED','CLEARED_FALSE_POSITIVE')),
    score             NUMERIC(5,4) NOT NULL,
    matched_alias     TEXT NOT NULL,
    freeze_applied    BOOLEAN NOT NULL DEFAULT TRUE,
    freeze_released_at TIMESTAMPTZ,
    aml_alert_id      TEXT,                       -- aml_* (spec/06) set on CONFIRMED
    str_report_id     TEXT,                       -- str_* (spec/06) set on CONFIRMED
    approval_request_id TEXT,                     -- appr_* (four-eyes clearance)
    reviewed_by       TEXT,
    confirmed_by      TEXT,                       -- CAMLCO operator_id
    rationale_pointer TEXT,                       -- object store: review/clearance rationale
    detected_at       TIMESTAMPTZ NOT NULL,
    review_started_at TIMESTAMPTZ,
    resolved_at       TIMESTAMPTZ,
    escalation_count  SMALLINT NOT NULL DEFAULT 0,
    schema_version    SMALLINT NOT NULL DEFAULT 1
);
CREATE INDEX idx_sanc_status ON sanctions_hits (status);
CREATE INDEX idx_sanc_subject ON sanctions_hits (subject_type, subject_id);

CREATE TABLE screening_whitelist (
    wl_id             TEXT PRIMARY KEY,           -- wl_<sha256_canonical[:24]>
    subject_type      TEXT NOT NULL,
    subject_id        TEXT NOT NULL,
    entry_content_hash TEXT NOT NULL,             -- anchored to ENTRY CONTENT, not entry_id:
                                                  -- any change to the entry breaks suppression
    list_code         TEXT NOT NULL,
    source_sanc_id    TEXT NOT NULL REFERENCES sanctions_hits(sanc_id),
    approval_request_id TEXT NOT NULL,            -- appr_* — four-eyes mandatory
    approved_by       TEXT NOT NULL,              -- CAMLCO
    rationale_pointer TEXT NOT NULL,
    created_at        TIMESTAMPTZ NOT NULL,
    expires_at        TIMESTAMPTZ NOT NULL,       -- max 24 months; periodic re-review forced
    revoked_at        TIMESTAMPTZ,
    schema_version    SMALLINT NOT NULL DEFAULT 1,
    UNIQUE (subject_type, subject_id, entry_content_hash)
);
CREATE INDEX idx_wl_lookup ON screening_whitelist (subject_type, subject_id)
    WHERE revoked_at IS NULL;

CREATE TABLE rescreen_runs (
    rrun_id           TEXT PRIMARY KEY,           -- rrun_<sha256_canonical[:24]>
    trigger           TEXT NOT NULL CHECK (trigger IN
                      ('LIST_UPDATE_DELTA','LIST_UPDATE_FULL','ALGO_VERSION_CHANGE','PERIODIC_FULL','MANUAL')),
    list_version_ids  TEXT[] NOT NULL,
    scope             TEXT NOT NULL CHECK (scope IN ('DELTA_ENTRIES_ONLY','FULL_BOOK_FULL_LIST')),
    status            TEXT NOT NULL CHECK (status IN ('QUEUED','RUNNING','COMPLETED','FAILED')),
    subjects_total    INTEGER NOT NULL DEFAULT 0,
    subjects_done     INTEGER NOT NULL DEFAULT 0,
    hits_raised       INTEGER NOT NULL DEFAULT 0,
    potentials_raised INTEGER NOT NULL DEFAULT 0,
    batch_size        INTEGER NOT NULL DEFAULT 5000,
    last_batch_no     INTEGER NOT NULL DEFAULT 0, -- resume offset (idempotent restart)
    started_at        TIMESTAMPTZ,
    deadline_at       TIMESTAMPTZ NOT NULL,       -- performance bound; breach pages on-call
    completed_at      TIMESTAMPTZ,
    error_detail_hash TEXT,
    scheduled_at      TIMESTAMPTZ NOT NULL,
    schema_version    SMALLINT NOT NULL DEFAULT 1
);
CREATE INDEX idx_rrun_status ON rescreen_runs (status) WHERE status IN ('QUEUED','RUNNING');
