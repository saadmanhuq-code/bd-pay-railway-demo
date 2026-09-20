-- 0050_connectors.sql — schema connectors (spec/10-connector-sdk-and-registry.md
-- §Data model, verbatim-adapted; lane-B migration range 0050-0099).
--
-- IDs are content-addressed (conventions §3 + errata LB2 additive prefixes
-- creg/cmode/chlth/cbrk/winb/simsc/certr); timestamps TIMESTAMPTZ UTC
-- (conventions §7); hashes are sha256_canonical per errata E12.
--
-- Errata note (E1 precedent applied): spec/10 declares
--   connector_health_samples / connector_results / webhook_inbound
-- as PARTITION BY RANGE while also declaring a TEXT PRIMARY KEY that does not
-- include the partition column — invalid in PostgreSQL. As with E1 (ledger
-- pilot), the tables ship unpartitioned here; the retention windows noted in
-- the spec are enforced operationally until a partition-keyed revision lands.

CREATE TABLE connector_registry (
  registration_id      TEXT PRIMARY KEY,                  -- creg_<sha256(connector_id)[:24]>
  connector_id         TEXT NOT NULL UNIQUE,              -- 'bkash_pgw_v2' ... (Part II(d) catalogue)
  display_name         TEXT NOT NULL,
  protocol             TEXT NOT NULL
    CHECK (protocol IN ('PAYMENT','IDENTITY','SANCTIONS','AML_FILING','SETTLEMENT_FILE','HSM','NOTIFICATION')),
  capabilities         TEXT[] NOT NULL,                   -- subset: submit,query_status,reverse,health_check,webhook,recon_file,batch
  supported_methods    TEXT[] NOT NULL DEFAULT '{}',      -- e.g. {NPSB_IBFT,BANGLA_QR}
  active_mode          TEXT NOT NULL DEFAULT 'MANUAL_LOCAL'
    CHECK (active_mode IN ('MANUAL_LOCAL','SIMULATOR','SANDBOX','PRODUCTION','DISABLED')),
  previous_mode        TEXT,                              -- restored on re-enable
  enabled              BOOLEAN NOT NULL DEFAULT TRUE,
  adapter_version      TEXT NOT NULL,                     -- semver of the adapter module
  sdk_version          SMALLINT NOT NULL DEFAULT 1,
  health_status        TEXT NOT NULL DEFAULT 'UNKNOWN'
    CHECK (health_status IN ('HEALTHY','DEGRADED','UNHEALTHY','UNKNOWN')),
  last_health_at       TIMESTAMPTZ,
  certification_status TEXT NOT NULL DEFAULT 'NEVER_RUN'
    CHECK (certification_status IN ('NEVER_RUN','PASSED','FAILED','STALE')),
  last_certified_at    TIMESTAMPTZ,
  config               JSONB NOT NULL DEFAULT '{}'::jsonb, -- validated against config_schema; NO secret values
  config_schema        JSONB NOT NULL,                     -- JSON Schema draft 2020-12 (spec/10)
  timeout_ms           INTEGER NOT NULL,                   -- hard timeout (spec/10 budget table)
  retry_budget         JSONB NOT NULL,                     -- {"submit_retries":0,"poll_attempts":3,"poll_interval_ms":10000,...}
  created_at           TIMESTAMPTZ NOT NULL,
  updated_at           TIMESTAMPTZ NOT NULL,
  schema_version       SMALLINT NOT NULL DEFAULT 1
);

CREATE TABLE connector_mode_changes (                      -- append-only
  mode_change_id   TEXT PRIMARY KEY,                       -- cmode_<sha256(connector_id+from+to+occurred_at)[:24]>
  connector_id     TEXT NOT NULL REFERENCES connector_registry(connector_id),
  from_mode        TEXT NOT NULL,
  to_mode          TEXT NOT NULL,
  reason           TEXT NOT NULL,
  actor_id         TEXT NOT NULL,                          -- operator or 'system:fail-closed-sweep'
  approval_request_id TEXT,                                -- appr_<...> required for -> PRODUCTION
  occurred_at      TIMESTAMPTZ NOT NULL,
  schema_version   SMALLINT NOT NULL DEFAULT 1
);
CREATE INDEX idx_mode_changes_connector ON connector_mode_changes (connector_id, occurred_at DESC);

CREATE TABLE connector_health_samples (                    -- append-only; 90-day retention (unpartitioned, see header)
  sample_id      TEXT PRIMARY KEY,                         -- chlth_<sha256(connector_id+sampled_at)[:24]>
  connector_id   TEXT NOT NULL REFERENCES connector_registry(connector_id),
  healthy        BOOLEAN NOT NULL,
  latency_ms     INTEGER,
  detail_code    TEXT,                                     -- e.g. 'tcp_timeout','auth_expired'
  sampled_at     TIMESTAMPTZ NOT NULL,
  schema_version SMALLINT NOT NULL DEFAULT 1
);
CREATE INDEX idx_health_samples_connector ON connector_health_samples (connector_id, sampled_at DESC);

CREATE TABLE circuit_breaker_states (
  breaker_id            TEXT PRIMARY KEY,                  -- cbrk_<sha256(connector_id)[:24]>
  connector_id          TEXT NOT NULL UNIQUE REFERENCES connector_registry(connector_id),
  state                 TEXT NOT NULL DEFAULT 'CLOSED'
    CHECK (state IN ('CLOSED','OPEN','HALF_OPEN')),
  consecutive_failures  INTEGER NOT NULL DEFAULT 0,
  window_started_at     TIMESTAMPTZ,
  opened_at             TIMESTAMPTZ,
  half_open_successes   INTEGER NOT NULL DEFAULT 0,
  half_open_probes      INTEGER NOT NULL DEFAULT 0,        -- additive (lane-B): probe-quota accounting
  failure_threshold     INTEGER NOT NULL DEFAULT 5,
  window_s              INTEGER NOT NULL DEFAULT 60,
  cooldown_s            INTEGER NOT NULL DEFAULT 30,
  probe_quota           INTEGER NOT NULL DEFAULT 3,
  updated_at            TIMESTAMPTZ NOT NULL,
  schema_version        SMALLINT NOT NULL DEFAULT 1
);
-- Redis mirrors {connector_id: state} with 5s TTL purely as a read-through cache;
-- on Redis miss/outage the Postgres row is read; on Postgres outage the breaker
-- REPORTS OPEN (fail-closed, dse-profit-engine semantics).

CREATE TABLE connector_results (                           -- append-only; the audit spine of every rail interaction
  result_id           TEXT PRIMARY KEY,                    -- cres_<sha256(connector_id+connector_ref+attempt_no+raw_response_hash)[:24]>
  instruction_id      TEXT NOT NULL,                       -- ins_<...> echoed (conventions §10)
  connector_ref       TEXT NOT NULL,                       -- idempotency key, echoed
  connector_id        TEXT NOT NULL REFERENCES connector_registry(connector_id),
  attempt_no          INTEGER NOT NULL DEFAULT 1,          -- runner attempt counter (submit=1; polls increment)
  call_kind           TEXT NOT NULL
    CHECK (call_kind IN ('SUBMIT','QUERY_STATUS','REVERSE','WEBHOOK','MANUAL','HEALTH','BATCH_SUBMIT','BATCH_STATUS')),
  status              TEXT NOT NULL
    CHECK (status IN ('success','pending','failed','rejected','timed_out','reversed')),  -- ConnectorStatus values
  rail_transaction_id TEXT,                                -- NPSB STAN/RRN, bKash trxID, ...
  responded_at        TIMESTAMPTZ,
  error_code          TEXT,                                -- canonical taxonomy (spec/11,12 map rail codes -> this)
  raw_response_hash   TEXT NOT NULL,                       -- sha256(canonical_json(raw wire response))
  raw_response_pointer TEXT NOT NULL,                      -- object-store key; 12-yr retention; investigator-only
  mode_at_call        TEXT NOT NULL,                       -- MANUAL_LOCAL|SIMULATOR|SANDBOX|PRODUCTION
  webhook_inbound_id  TEXT,                                -- winb_<...> when call_kind=WEBHOOK
  produced_at         TIMESTAMPTZ NOT NULL,
  schema_version      SMALLINT NOT NULL DEFAULT 1,
  UNIQUE (connector_id, connector_ref, call_kind, attempt_no)
);
CREATE INDEX idx_cres_ref ON connector_results (connector_ref, produced_at DESC);
CREATE INDEX idx_cres_rail_txn ON connector_results (rail_transaction_id) WHERE rail_transaction_id IS NOT NULL;

CREATE TABLE webhook_inbound (                             -- single row per delivery; status advances ONLY via FSM function
  inbound_id      TEXT PRIMARY KEY,                        -- winb_<sha256(connector_id+content_hash)[:24]>
  connector_id    TEXT NOT NULL REFERENCES connector_registry(connector_id),
  content_hash    TEXT NOT NULL,                           -- sha256(raw body bytes)  <- dedupe key
  headers_hash    TEXT NOT NULL,                           -- sha256(canonical_json(headers))
  raw_pointer     TEXT NOT NULL,                           -- object-store key (raw body + headers); 12-yr
  source_ip_hash  TEXT NOT NULL,                           -- sha256(ip); raw IP never stored
  status          TEXT NOT NULL DEFAULT 'RECEIVED'
    CHECK (status IN ('RECEIVED','VERIFIED','REJECTED','DUPLICATE','PARSED','HANDED_OFF','PROCESSED','PARKED')),
  reject_reason   TEXT,
  parsed_connector_ref TEXT,
  result_id       TEXT,                                    -- cres_<...> once parsed
  received_at     TIMESTAMPTZ NOT NULL,
  processed_at    TIMESTAMPTZ,
  schema_version  SMALLINT NOT NULL DEFAULT 1,
  UNIQUE (connector_id, content_hash)                      -- content-hash dedupe, DB-enforced
);
-- status transitions executed solely by connectors.webhook_fsm_advance() which also
-- writes the matching audit_events row in the same transaction; direct UPDATE revoked.

CREATE TABLE simulator_scenarios (
  scenario_id     TEXT PRIMARY KEY,                        -- simsc_<sha256(connector_id+name+version)[:24]>
  connector_id    TEXT NOT NULL REFERENCES connector_registry(connector_id),
  name            TEXT NOT NULL,                           -- 'success','pending_then_success',...
  version         INTEGER NOT NULL DEFAULT 1,
  match_rules     JSONB NOT NULL,                          -- spec/10 Simulator DSL (closed set)
  script          JSONB NOT NULL,                          -- ordered steps; closed step vocabulary
  enabled         BOOLEAN NOT NULL DEFAULT TRUE,
  created_by      TEXT NOT NULL,
  created_at      TIMESTAMPTZ NOT NULL,
  schema_version  SMALLINT NOT NULL DEFAULT 1,
  UNIQUE (connector_id, name, version)
);

CREATE TABLE certification_runs (                          -- append-only
  run_id          TEXT PRIMARY KEY,                        -- certr_<sha256(connector_id+adapter_version+started_at)[:24]>
  connector_id    TEXT NOT NULL REFERENCES connector_registry(connector_id),
  adapter_version TEXT NOT NULL,
  mode_under_test TEXT NOT NULL CHECK (mode_under_test IN ('SIMULATOR','SANDBOX')),
  status          TEXT NOT NULL DEFAULT 'QUEUED'
    CHECK (status IN ('QUEUED','RUNNING','PASSED','FAILED','ERRORED')),
  checks          JSONB NOT NULL DEFAULT '[]'::jsonb,      -- [{check_id, name, verdict, evidence_hash}]
  report_pointer  TEXT,                                    -- object-store key, deterministic file+hash report
  report_hash     TEXT,                                    -- sha256 of report file
  started_at      TIMESTAMPTZ,
  finished_at     TIMESTAMPTZ,
  triggered_by    TEXT NOT NULL,
  created_at      TIMESTAMPTZ NOT NULL,
  schema_version  SMALLINT NOT NULL DEFAULT 1
);

-- Privilege fencing (the no-business-DB-writes invariant, DB-enforced):
--   role bdpay_connector_rt:  SELECT on connector_registry, simulator_scenarios;
--                             INSERT on connector_results, connector_health_samples;
--                             NOTHING on any table outside schema 'connectors'.
--   Adapters in spec/11 and spec/12 run under bdpay_connector_rt.
--   role bdpay_connector_admin: registry/mode/cert tables; used by ConnectorRunner control plane.
--   Neither role has any grant on kernel/ledger/compliance schemas.
--   The spec's closing statement is applied once those schemas/roles exist:
--     REVOKE ALL ON ALL TABLES IN SCHEMA kernel, ledger, compliance, qr FROM bdpay_connector_rt;
--   (deferred here: lane-B builds connectors before kernel/ledger/compliance schemas land).
