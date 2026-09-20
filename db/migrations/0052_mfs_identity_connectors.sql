-- 0052_mfs_identity_connectors.sql — spec/12 additions to schema connectors
-- (+ notifications). Lane-B migration range 0050-0099; 0050/0051 already used.
--
-- IDs are content-addressed (conventions §3 + spec/12 additive prefixes
-- mtok/gfil/sfeed/ntfm/ntpl per the LB2 pattern); timestamps TIMESTAMPTZ UTC
-- (conventions §7); hashes are sha256_canonical per errata E12; amounts never
-- appear here (paisa->decimal happens only at the goAML render boundary).
--
-- Errata note (E1 precedent applied, same as 0050): spec/12 declares
-- veridyn_check_requests / notification_messages as PARTITION BY RANGE while
-- giving each a TEXT PRIMARY KEY that does not include the partition column —
-- invalid in PostgreSQL. notification_messages ships unpartitioned here with
-- the retention window enforced operationally. veridyn_check_requests is NOT
-- created: the veridyn_p2_compliance_v1 connector is outside the lane-B brief
-- scope for this group (see LANE-B-BRIEF scope table; spec/12 §H).

CREATE TABLE mfs_token_states (
  token_state_id  TEXT PRIMARY KEY,            -- mtok_<sha256(connector_id+mode)[:24]>
  connector_id    TEXT NOT NULL REFERENCES connector_registry(connector_id),
  mode            TEXT NOT NULL,               -- SANDBOX|PRODUCTION
  state           TEXT NOT NULL DEFAULT 'NO_TOKEN'
    CHECK (state IN ('NO_TOKEN','GRANTED','REFRESHING','EXPIRED','GRANT_FAILED')),
  token_hash      TEXT,                        -- sha256 of current id_token (token itself ONLY in Redis, encrypted)
  granted_at      TIMESTAMPTZ,
  refresh_expires_at TIMESTAMPTZ,              -- 28d for bKash
  consecutive_failures SMALLINT NOT NULL DEFAULT 0,
  updated_at      TIMESTAMPTZ NOT NULL,
  schema_version  SMALLINT NOT NULL DEFAULT 1,
  UNIQUE (connector_id, mode)
);

CREATE TABLE goaml_filings (                    -- append-only attempt chain
  filing_id       TEXT PRIMARY KEY,             -- gfil_<sha256(report_ref+attempt_no)[:24]>
  report_type     TEXT NOT NULL CHECK (report_type IN ('STR','CTR')),
  report_ref      TEXT NOT NULL,                -- str_<...> | ctr_<...> (spec/06)
  attempt_no      INTEGER NOT NULL DEFAULT 1,
  state           TEXT NOT NULL DEFAULT 'QUEUED'
    CHECK (state IN ('QUEUED','SUBMITTED','ACK_PENDING','ACKED','REJECTED_BY_PORTAL','MANUAL_ESCALATED')),
  xml_payload_hash TEXT NOT NULL,               -- sha256(rendered goAML XML); raw in object store
  xml_payload_pointer TEXT NOT NULL,
  goaml_submission_ref TEXT,
  portal_reject_reason_hash TEXT,
  retry_count     INTEGER NOT NULL DEFAULT 0,
  next_retry_at   TIMESTAMPTZ,
  submitted_at    TIMESTAMPTZ,
  acked_at        TIMESTAMPTZ,
  created_at      TIMESTAMPTZ NOT NULL,
  updated_at      TIMESTAMPTZ NOT NULL,
  schema_version  SMALLINT NOT NULL DEFAULT 1,
  UNIQUE (report_ref, attempt_no)
);
CREATE INDEX idx_gfil_due ON goaml_filings (next_retry_at) WHERE state = 'QUEUED';

CREATE TABLE sanctions_feed_versions (
  feed_version_id TEXT PRIMARY KEY,             -- sfeed_<sha256(source+content_hash)[:24]>
  source          TEXT NOT NULL CHECK (source IN ('UN_CONSOLIDATED','BFIU_DOMESTIC','VENDOR')),
  state           TEXT NOT NULL DEFAULT 'FETCHED'
    CHECK (state IN ('FETCHED','VALIDATED','HANDED_OFF','INGESTED','REJECTED','STALE_ALARMED')),
  content_hash    TEXT NOT NULL,
  raw_pointer     TEXT NOT NULL,                -- object store
  entry_count     INTEGER,
  previous_entry_count INTEGER,
  unchanged       BOOLEAN NOT NULL DEFAULT FALSE,
  spec07_list_version_id TEXT,                  -- slist_<...> once ingested
  fetched_at      TIMESTAMPTZ NOT NULL,
  ingested_at     TIMESTAMPTZ,
  schema_version  SMALLINT NOT NULL DEFAULT 1,
  UNIQUE (source, content_hash)
);

CREATE TABLE notification_templates (
  template_id     TEXT PRIMARY KEY,             -- ntpl_<sha256(name+version)[:24]>
  name            TEXT NOT NULL,                -- 'otp_payment', 'kyc_approved', ...
  version         INTEGER NOT NULL DEFAULT 1,
  channel         TEXT NOT NULL CHECK (channel IN ('SMS','EMAIL')),
  body_en         TEXT NOT NULL,                -- {render variables}; PII slots whitelisted per template
  body_bn         TEXT NOT NULL,                -- Bengali body (UCS-2 segment costs reviewed at authoring)
  subject_en      TEXT,                         -- email only
  subject_bn      TEXT,
  fallback_channel TEXT CHECK (fallback_channel IN ('SMS','EMAIL','NONE')),
  active          BOOLEAN NOT NULL DEFAULT TRUE,
  created_by      TEXT NOT NULL,
  created_at      TIMESTAMPTZ NOT NULL,
  schema_version  SMALLINT NOT NULL DEFAULT 1,
  UNIQUE (name, version)
);

CREATE TABLE notification_messages (            -- append-only; state via FSM function
  message_id      TEXT PRIMARY KEY,             -- ntfm_<sha256(event_id+recipient_hash+template)[:24]>
  event_id        TEXT NOT NULL,                -- obx_<...> that triggered it (idempotency leg 1)
  template_id     TEXT NOT NULL REFERENCES notification_templates(template_id),
  channel         TEXT NOT NULL,
  locale          TEXT NOT NULL CHECK (locale IN ('bn','en')),
  recipient_hash  TEXT NOT NULL,                -- sha256(msisdn|email); raw recipient in vault-scoped lookup only
  state           TEXT NOT NULL DEFAULT 'QUEUED'
    CHECK (state IN ('QUEUED','RENDERED','SENT','DELIVERED','FAILED','SUPPRESSED')),
  encoding        TEXT CHECK (encoding IN ('GSM7','UCS2')),     -- SMS only
  segment_count   SMALLINT,
  body_hash       TEXT,
  body_pointer    TEXT,                         -- object store; PII-bearing body never in DB/logs
  provider_message_id TEXT,
  failure_code    TEXT,
  queued_at       TIMESTAMPTZ NOT NULL,
  sent_at         TIMESTAMPTZ,
  delivered_at    TIMESTAMPTZ,
  schema_version  SMALLINT NOT NULL DEFAULT 1,
  UNIQUE (event_id, recipient_hash, template_id)               -- idempotency, DB-enforced
);

-- Deploy-rehearsal fix (2026-06-13, fresh-database apply on VM3): the REVOKE
-- below references bdpay_connector_rt, but no earlier migration creates it —
-- 0050 documents the role's privilege fence in comments and defers creation,
-- so this file could never apply on a fresh database (the migration-runner
-- failed closed here with `role "bdpay_connector_rt" does not exist`; the
-- file's transaction rolled back whole, so it was never recorded as applied).
-- Guarded creation matches the established role pattern in 0010/0060
-- (NOLOGIN, idempotent on re-run).
DO $$
BEGIN
    CREATE ROLE bdpay_connector_rt NOLOGIN;
EXCEPTION WHEN duplicate_object THEN
    NULL;
END $$;

REVOKE UPDATE, DELETE ON goaml_filings, notification_messages
  FROM bdpay_connector_rt;   -- state advances via SECURITY DEFINER FSM fns + audit row
