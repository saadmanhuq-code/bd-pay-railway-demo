-- 0072_str_reports.sql — spec/06 Data model: str_reports.
-- STR is suspicion-based at ANY amount: this table has NO amount column and
-- no threshold check by design (cut-last rule #1).
--
-- Additive columns beyond the spec DDL (errata E-C6): filing_due_at tracks
-- the 24h filing clock from the CAMLCO decision (build brief, binding);
-- contributing_payment_ids carries the transaction set into the goAML
-- payload without re-reading the alert at filing time.

CREATE TABLE str_reports (
    str_id                      TEXT PRIMARY KEY,    -- str_<sha256_canonical[:24]>
    alert_id                    TEXT NOT NULL REFERENCES aml_alerts(alert_id),
    subject_type                TEXT NOT NULL CHECK (subject_type IN ('Customer','Merchant')),
    subject_id                  TEXT NOT NULL,
    status                      TEXT NOT NULL
                                CHECK (status IN ('PENDING_CAMLCO_REVIEW','CAMLCO_APPROVED','FILING_PENDING',
                                                   'FILING_SUBMITTED','FILING_CONFIRMED','FILING_FAILED','WITHDRAWN')),
    camlco_id                   TEXT NOT NULL,       -- operator_id; PII-redacted
    camlco_approved_at          TIMESTAMPTZ,
    filing_due_at               TIMESTAMPTZ,         -- camlco_approved_at + 24h (E-C6)
    suspicion_narrative_hash    TEXT NOT NULL,       -- sha256; raw narrative in object store
    suspicion_narrative_pointer TEXT NOT NULL,       -- object store key; never plaintext here
    requires_bfiu_escalation    BOOLEAN NOT NULL DEFAULT FALSE,
    bfiu_manual_escalation_required BOOLEAN NOT NULL DEFAULT FALSE,
    goaml_payload_hash          TEXT,
    goaml_payload_pointer       TEXT,
    goaml_ref                   TEXT,
    filing_submitted_at         TIMESTAMPTZ,
    filing_confirmed_at         TIMESTAMPTZ,
    filing_error_detail_hash    TEXT,
    retry_count                 SMALLINT NOT NULL DEFAULT 0,
    retry_scheduled_at          TIMESTAMPTZ,
    withdrawn_at                TIMESTAMPTZ,
    withdrawal_reason           TEXT,
    contributing_payment_ids    TEXT[] NOT NULL DEFAULT '{}',  -- (E-C6)
    created_at                  TIMESTAMPTZ NOT NULL,
    schema_version              SMALLINT NOT NULL DEFAULT 1
);

CREATE INDEX idx_str_reports_status ON str_reports (status);
CREATE INDEX idx_str_reports_subject ON str_reports (subject_type, subject_id);
CREATE INDEX idx_str_reports_alert ON str_reports (alert_id);
CREATE INDEX idx_str_reports_pending_retry ON str_reports (retry_scheduled_at)
    WHERE status IN ('FILING_FAILED', 'FILING_PENDING') AND bfiu_manual_escalation_required = FALSE;
