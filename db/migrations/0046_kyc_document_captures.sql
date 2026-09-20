-- 0046_kyc_document_captures.sql — spec/09 DDL kyc_document_captures.
-- The OCR confidence ratio is NUMERIC(5,4) — a ratio, never money.

CREATE TABLE kyc_document_captures (
    capture_id              TEXT        PRIMARY KEY,
    -- make_id('kycdoc', {kyc_record_id, captured_at}) (kernel additive prefix, K-2)
    kyc_record_id           TEXT        NOT NULL REFERENCES kyc_records (kyc_record_id),
    captured_at             TIMESTAMPTZ NOT NULL,
    ocr_confidence_score    NUMERIC(5,4) NOT NULL,
    raw_payload_hash        TEXT        NOT NULL,
    raw_payload_object_key  TEXT        NOT NULL,
    normalised_summary_hash TEXT        NOT NULL,
    schema_version          SMALLINT    NOT NULL DEFAULT 1
);

CREATE INDEX idx_kyc_captures_record ON kyc_document_captures (kyc_record_id);
