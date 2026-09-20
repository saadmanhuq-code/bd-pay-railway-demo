-- 0051_qr.sql — schema qr (spec/13-qr-bangla-emvco.md §Data model, verbatim
-- where the spec gives DDL; lane-B migration range 0050-0099).
--
-- IDs are content-addressed (conventions §3 + spec/13 additive prefixes
-- mqr/qrp/qres/qtest); money is integer paisa BIGINT (conventions §6);
-- timestamps TIMESTAMPTZ UTC (conventions §7).

CREATE TABLE merchant_qrs (
  merchant_qr_id  TEXT PRIMARY KEY,             -- mqr_<sha256(...)[:24]>
  merchant_id     TEXT NOT NULL,                -- mrch_<...> (spec/08)
  state           TEXT NOT NULL DEFAULT 'DRAFT'
    CHECK (state IN ('DRAFT','ACTIVE','SUSPENDED','REVOKED')),
  label           TEXT NOT NULL,
  store_id        TEXT,
  terminal_id     TEXT,
  current_payload_id TEXT,                      -- qrp_<...>
  suspend_reason  TEXT,
  created_at      TIMESTAMPTZ NOT NULL,
  updated_at      TIMESTAMPTZ NOT NULL,
  schema_version  SMALLINT NOT NULL DEFAULT 1,
  UNIQUE (merchant_id, label)
);

CREATE TABLE qr_payloads (
  payload_id      TEXT PRIMARY KEY,             -- qrp_<sha256(payload_string)[:24]> (content-addressed)
  qr_type         TEXT NOT NULL CHECK (qr_type IN ('STATIC','DYNAMIC')),
  merchant_qr_id  TEXT REFERENCES merchant_qrs(merchant_qr_id),   -- static
  payment_intent_id TEXT,                       -- dynamic (pi_<...>, spec/02)
  state           TEXT NOT NULL DEFAULT 'ISSUED'                  -- dynamic only; static rows stay 'ISSUED'
    CHECK (state IN ('ISSUED','SCANNED','PAID','EXPIRED','CANCELLED')),
  payload_string  TEXT NOT NULL,                -- full EMVCo string incl CRC
  payload_hash    TEXT NOT NULL UNIQUE,         -- sha256_canonical(payload_string), errata E12
  amount_minor    BIGINT,                       -- dynamic only; NULL static (CHECK below)
  currency        CHAR(3) NOT NULL DEFAULT 'BDT',
  mcc             CHAR(4) NOT NULL,
  merchant_name   TEXT NOT NULL,                -- as encoded (ID 59, post-truncation)
  merchant_name_bn TEXT,                        -- ID 64 alternate (UTF-8 Bengali)
  merchant_city   TEXT NOT NULL,
  expires_at      TIMESTAMPTZ,                  -- dynamic only
  created_at      TIMESTAMPTZ NOT NULL,
  schema_version  SMALLINT NOT NULL DEFAULT 1,
  CHECK ((qr_type='STATIC' AND amount_minor IS NULL AND merchant_qr_id IS NOT NULL)
      OR (qr_type='DYNAMIC' AND amount_minor IS NOT NULL AND amount_minor > 0
          AND payment_intent_id IS NOT NULL AND expires_at IS NOT NULL))
);
CREATE UNIQUE INDEX idx_qrp_intent ON qr_payloads (payment_intent_id)
  WHERE qr_type = 'DYNAMIC';                    -- dynamic payload maps 1:1 to a PaymentIntent

CREATE TABLE qr_scan_resolutions (              -- append-only
  resolution_id   TEXT NOT NULL,                -- qres_<sha256(payload_hash+customer_ref+scanned_at)[:24]>
  payload_hash    TEXT NOT NULL,                -- ours OR foreign (off-us scans)
  payload_id      TEXT,                         -- qrp_<...> if ours
  on_us           BOOLEAN NOT NULL,
  customer_ref    TEXT NOT NULL,                -- cust_<...> hashed-at-rest per spec/09 policy
  qr_type         TEXT NOT NULL,
  validation_result TEXT NOT NULL CHECK (validation_result IN ('VALID','INVALID')),
  failure_code    TEXT,                         -- qr_invalid_crc | qr_wrong_currency | ...
  quoted_fee_minor BIGINT,
  payment_intent_id TEXT,                       -- set when /pay proceeds
  scanned_at      TIMESTAMPTZ NOT NULL,
  schema_version  SMALLINT NOT NULL DEFAULT 1,
  PRIMARY KEY (resolution_id, scanned_at)       -- partition key must be in the PK (errata E1 lesson)
) PARTITION BY RANGE (scanned_at);              -- monthly partitions; 12-yr retention
CREATE INDEX idx_qres_payload ON qr_scan_resolutions (payload_hash, scanned_at DESC);
-- Operational default partition so inserts never fail before the monthly
-- partition job (scheduler) creates the real ranges.
CREATE TABLE qr_scan_resolutions_default PARTITION OF qr_scan_resolutions DEFAULT;

CREATE TABLE qr_interop_test_cases (
  test_case_id    TEXT PRIMARY KEY,             -- qtest_<sha256(name+version)[:24]>
  name            TEXT NOT NULL,
  version         INTEGER NOT NULL DEFAULT 1,
  direction       TEXT NOT NULL CHECK (direction IN ('WE_ISSUE','WE_SCAN')),
  payload_string  TEXT NOT NULL,
  expected        JSONB NOT NULL,               -- {"valid": bool, "failure_code": ..., "fields": {...}}
  counterparty_class TEXT,                      -- 'BANK_APP','MFS_APP','PSP_APP','SIMULATOR'
  last_result     TEXT CHECK (last_result IN ('PASS','FAIL','UNRUN')),
  last_run_at     TIMESTAMPTZ,
  created_at      TIMESTAMPTZ NOT NULL,
  schema_version  SMALLINT NOT NULL DEFAULT 1,
  UNIQUE (name, version)
);

-- Grants: qr-service rt role INSERT/SELECT; UPDATE on merchant_qrs/qr_payloads state
-- only via qr_fsm_advance() SECURITY DEFINER (writes audit_events in-txn) —
-- applied in the deploy-role migration alongside the audit schema (lane A range).
