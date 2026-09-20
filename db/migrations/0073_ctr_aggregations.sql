-- 0073_ctr_aggregations.sql — spec/06 Data model: ctr_aggregations.
--
-- Errata E-C4 (E1-class defect): spec/06 declares ctr_id TEXT PRIMARY KEY
-- together with PARTITION BY RANGE (aggregation_date). Postgres requires the
-- partition key inside every unique constraint, so that DDL cannot apply.
-- Resolution (E1 precedent): ship UNPARTITIONED with the true primary key
-- and UNIQUE (account_id, aggregation_date); partitioning is a later,
-- explicit migration project once volume demands it. Retention (5yr/12yr
-- PSO) runs as indexed deletes on aggregation_date until then.

CREATE TABLE ctr_aggregations (
    ctr_id                  TEXT PRIMARY KEY,        -- ctr_<sha256(canonical_json({account_id, aggregation_date}))[:24]>
    account_id              TEXT NOT NULL,           -- acct_* reference
    aggregation_date        DATE NOT NULL,
    total_cash_in_minor     BIGINT NOT NULL DEFAULT 0 CHECK (total_cash_in_minor >= 0),
    total_cash_out_minor    BIGINT NOT NULL DEFAULT 0 CHECK (total_cash_out_minor >= 0),
    currency                CHAR(3) NOT NULL DEFAULT 'BDT',
    threshold_minor         BIGINT NOT NULL DEFAULT 100000000,  -- BDT 10 lakh; config-gated
    threshold_crossed       BOOLEAN NOT NULL GENERATED ALWAYS AS
                            ((total_cash_in_minor + total_cash_out_minor) >= threshold_minor) STORED,
    contributing_txn_count  INTEGER NOT NULL DEFAULT 0,
    ctr_filed               BOOLEAN NOT NULL DEFAULT FALSE,
    ctr_filed_at            TIMESTAMPTZ,
    goaml_ref               TEXT,
    annual_batch_year       SMALLINT,                -- set when included in the annual BFIU report
    computed_at             TIMESTAMPTZ NOT NULL,
    schema_version          SMALLINT NOT NULL DEFAULT 1,
    UNIQUE (account_id, aggregation_date)
);

CREATE INDEX idx_ctr_agg_unfiled ON ctr_aggregations (aggregation_date)
    WHERE threshold_crossed = TRUE AND ctr_filed = FALSE;
CREATE INDEX idx_ctr_agg_account_date ON ctr_aggregations (account_id, aggregation_date DESC);
