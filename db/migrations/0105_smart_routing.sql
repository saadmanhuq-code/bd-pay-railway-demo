-- 0105_smart_routing.sql — spec/17 VX2 Smart MFS routing
--
-- Scope: routing_policies + routing_decisions only.  The success-rate and
-- health inputs remain the canonical spec/10 connector_results and
-- connector_health_samples tables; fee inputs remain ledger.fee_rules.
-- Timestamps are supplied by the application clock.

CREATE TABLE routing_policies (
    policy_id           TEXT PRIMARY KEY CHECK (policy_id LIKE 'rpol_%'),
    method              TEXT NOT NULL,
    version             BIGINT NOT NULL CHECK (version >= 1),
    w_success_bps       BIGINT NOT NULL CHECK (w_success_bps BETWEEN 0 AND 10000),
    w_health_bps        BIGINT NOT NULL CHECK (w_health_bps BETWEEN 0 AND 10000),
    w_fee_bps           BIGINT NOT NULL CHECK (w_fee_bps BETWEEN 0 AND 10000),
    w_latency_bps       BIGINT NOT NULL CHECK (w_latency_bps BETWEEN 0 AND 10000),
    window_hours        BIGINT NOT NULL DEFAULT 24 CHECK (window_hours BETWEEN 1 AND 168),
    status              TEXT NOT NULL CHECK (status IN ('ACTIVE','SUPERSEDED')),
    approval_request_id TEXT NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL,
    schema_version      SMALLINT NOT NULL DEFAULT 1,
    CONSTRAINT rpol_weights_sum
        CHECK (w_success_bps + w_health_bps + w_fee_bps + w_latency_bps = 10000)
);

CREATE UNIQUE INDEX rpol_method_version_uidx
    ON routing_policies (method, version);

CREATE UNIQUE INDEX rpol_one_active_per_method
    ON routing_policies (method)
    WHERE status = 'ACTIVE';

CREATE TABLE routing_decisions (
    decision_id           TEXT PRIMARY KEY CHECK (decision_id LIKE 'rdec_%'),
    payment_attempt_id    TEXT NOT NULL,
    policy_id             TEXT NOT NULL REFERENCES routing_policies(policy_id),
    candidates            JSONB NOT NULL,
    selected_connector_id TEXT NOT NULL,
    inputs_hash           TEXT NOT NULL CHECK (char_length(inputs_hash) = 64),
    decided_at            TIMESTAMPTZ NOT NULL,
    fallback              BOOLEAN NOT NULL DEFAULT FALSE,
    fallback_reason       TEXT,
    schema_version        SMALLINT NOT NULL DEFAULT 1,
    CONSTRAINT rdec_fallback_reason_check
        CHECK ((fallback = FALSE AND fallback_reason IS NULL)
               OR (fallback = TRUE AND fallback_reason IS NOT NULL))
);

CREATE INDEX rdec_attempt_idx
    ON routing_decisions (payment_attempt_id);

CREATE INDEX rdec_policy_idx
    ON routing_decisions (policy_id, decided_at DESC);
