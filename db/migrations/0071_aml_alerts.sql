-- 0071_aml_alerts.sql — spec/06 Data model: aml_alerts.
-- Retention: 5 years (MLPA 2012 minimum); 12 years in PSO mode (config-gated).

CREATE TABLE aml_alerts (
    alert_id                TEXT PRIMARY KEY,        -- aml_<sha256_canonical[:24]>
    subject_type            TEXT NOT NULL CHECK (subject_type IN ('Customer','Merchant')),
    subject_id              TEXT NOT NULL,           -- cust_* or mrch_* (cross-schema ref, no FK)
    rule_id                 TEXT NOT NULL,           -- must exist in active rule_pack.rules_json
    rule_pack_id            TEXT NOT NULL REFERENCES rule_packs(pack_id),
    status                  TEXT NOT NULL
                            CHECK (status IN ('RAISED','TRIAGING','IN_CASE','STR_FILED','CLOSED')),
    risk_tier               TEXT NOT NULL CHECK (risk_tier IN ('HIGH','MEDIUM','LOW')),
    triggered_amount_minor  BIGINT,                  -- paisa; nullable (velocity-only rules)
    currency                CHAR(3) NOT NULL DEFAULT 'BDT',
    contributing_payment_ids TEXT[] NOT NULL DEFAULT '{}',
    notes                   TEXT,
    assigned_to             TEXT,                    -- operator_id
    raised_at               TIMESTAMPTZ NOT NULL,
    triaged_at              TIMESTAMPTZ,
    case_opened_at          TIMESTAMPTZ,
    str_report_id           TEXT,                    -- str_* set when STR filed
    closed_at               TIMESTAMPTZ,
    close_reason            TEXT CHECK (close_reason IN ('FALSE_POSITIVE','RESOLVED','BELOW_THRESHOLD','OTHER')
                                        OR close_reason IS NULL),
    freeze_applied          BOOLEAN NOT NULL DEFAULT FALSE,
    escalation_count        SMALLINT NOT NULL DEFAULT 0,
    idempotency_key         TEXT UNIQUE NOT NULL,    -- make_id('aml', {rule_id, subject_id, window_start})
    schema_version          SMALLINT NOT NULL DEFAULT 1
);

CREATE INDEX idx_aml_alerts_status ON aml_alerts (status);
CREATE INDEX idx_aml_alerts_subject ON aml_alerts (subject_type, subject_id);
CREATE INDEX idx_aml_alerts_raised_at ON aml_alerts (raised_at DESC);
CREATE INDEX idx_aml_alerts_str ON aml_alerts (str_report_id) WHERE str_report_id IS NOT NULL;
