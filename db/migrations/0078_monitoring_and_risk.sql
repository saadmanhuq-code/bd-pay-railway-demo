-- 0078_monitoring_and_risk.sql — spec/06 Data model: monitoring_rule_events
-- (the audit trail of what the monitoring engine saw) + the risk-score tables.
--
-- Errata E-C4 (E1-class defect): spec/06 declares event_id TEXT PRIMARY KEY
-- together with PARTITION BY RANGE (evaluated_at); ships UNPARTITIONED with
-- the true primary key (E1 precedent). The 12-year PSO retention runs as
-- indexed deletes/archival on evaluated_at until an explicit partitioning
-- migration project.

CREATE TABLE monitoring_rule_events (
    event_id            TEXT PRIMARY KEY,            -- mrev_<sha256_canonical[:24]>
    payment_intent_id   TEXT NOT NULL,               -- pi_* reference
    rule_id             TEXT NOT NULL,
    rule_pack_id        TEXT NOT NULL,
    subject_type        TEXT NOT NULL,
    subject_id          TEXT NOT NULL,
    evaluation_result   TEXT NOT NULL CHECK (evaluation_result IN ('ALERT_RAISED','NEAR_MISS','PASSED')),
    score_delta         SMALLINT,
    alert_id            TEXT,                        -- set if evaluation_result = 'ALERT_RAISED'
    evaluated_at        TIMESTAMPTZ NOT NULL,
    schema_version      SMALLINT NOT NULL DEFAULT 1
);
CREATE INDEX idx_mrev_subject ON monitoring_rule_events (subject_type, subject_id, evaluated_at DESC);
CREATE INDEX idx_mrev_rule ON monitoring_rule_events (rule_id, evaluated_at DESC);
CREATE INDEX idx_mrev_evaluated_at ON monitoring_rule_events (evaluated_at);

CREATE TABLE customer_risk_scores (
    score_id            TEXT PRIMARY KEY,            -- cscore_<sha256_canonical[:24]>
    customer_id         TEXT NOT NULL,               -- cust_* reference
    risk_tier           TEXT NOT NULL CHECK (risk_tier IN ('HIGH','MEDIUM','LOW')),
    risk_score          SMALLINT NOT NULL CHECK (risk_score BETWEEN 0 AND 100),
    score_basis         TEXT[] NOT NULL,             -- contributing factor IDs
    thresholds_version  TEXT NOT NULL,               -- thresholds YAML version at scoring time
    computed_at         TIMESTAMPTZ NOT NULL,
    next_review_at      TIMESTAMPTZ NOT NULL,        -- HIGH=1yr, MEDIUM=2yr, LOW=5yr
    superseded_at       TIMESTAMPTZ,                 -- null if current
    schema_version      SMALLINT NOT NULL DEFAULT 1
);
CREATE UNIQUE INDEX idx_crs_current ON customer_risk_scores (customer_id)
    WHERE superseded_at IS NULL;
CREATE INDEX idx_crs_review_due ON customer_risk_scores (next_review_at)
    WHERE superseded_at IS NULL;

CREATE TABLE merchant_risk_scores (
    score_id            TEXT PRIMARY KEY,            -- mscore_<sha256_canonical[:24]>
    merchant_id         TEXT NOT NULL,               -- mrch_* reference
    risk_tier           TEXT NOT NULL CHECK (risk_tier IN ('HIGH','MEDIUM','LOW')),
    risk_score          SMALLINT NOT NULL CHECK (risk_score BETWEEN 0 AND 100),
    score_basis         TEXT[] NOT NULL,
    thresholds_version  TEXT NOT NULL,
    computed_at         TIMESTAMPTZ NOT NULL,
    next_review_at      TIMESTAMPTZ NOT NULL,
    superseded_at       TIMESTAMPTZ,
    schema_version      SMALLINT NOT NULL DEFAULT 1
);
CREATE UNIQUE INDEX idx_mrs_current ON merchant_risk_scores (merchant_id)
    WHERE superseded_at IS NULL;
CREATE INDEX idx_mrs_review_due ON merchant_risk_scores (next_review_at)
    WHERE superseded_at IS NULL;
