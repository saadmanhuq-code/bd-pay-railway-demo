-- 0094_payment_links.sql — payment links + single-use checkout claims
-- (spec/16 §Data model, LR-4; DDL verbatim from the spec).
-- Renumbered from the VM push's 0091 (errata E-S16-12): 0091 is the reserved
-- tiered-MDR seed (0091_fee_rules_tiering.sql, spec/19 D-19-5); migration
-- numbers are unique and append-only — next free after 0092/0093 was 0094.
--
-- merchants(merchant_id) is a cross-package reference (spec/08); integrity is
-- application-enforced (E20 posture, same as api_keys.merchant_id in 0080).
--
-- Single-use concurrency backstop: at most one non-terminal intent per link
-- is enforced by the partial-unique guard on the claims side — the
-- link-checkout path inserts (link_id) into payment_link_claims with
-- UNIQUE(link_id) WHERE released = FALSE; the claim is released when the
-- intent reaches a terminal failure state.

CREATE TABLE payment_links (
  link_id           TEXT PRIMARY KEY,          -- plink_<sha256_canonical({merchant_id, idempotency_key})[:24]>
  merchant_id       TEXT NOT NULL,             -- application-enforced ref to merchants (E20 posture)
  public_code       TEXT NOT NULL UNIQUE,      -- base32(sha256(link_id+merchant_id+created_at))[:16]
  amount_minor      BIGINT,                    -- NULL = payer-entered
  amount_min_minor  BIGINT,                    -- bounds when amount_minor IS NULL
  amount_max_minor  BIGINT,
  currency          CHAR(3) NOT NULL DEFAULT 'BDT',
  description       TEXT NOT NULL,             -- PII-rejected at intake
  single_use        BOOLEAN NOT NULL DEFAULT TRUE,
  state             TEXT NOT NULL DEFAULT 'ACTIVE'
    CHECK (state IN ('ACTIVE','PAID','EXPIRED','CANCELLED')),
  payment_intent_id TEXT,                      -- set at PAID
  expires_at        TIMESTAMPTZ NOT NULL,
  created_at        TIMESTAMPTZ NOT NULL,
  terminal_at       TIMESTAMPTZ,
  schema_version    SMALLINT NOT NULL DEFAULT 1,
  CONSTRAINT plink_amount_shape CHECK (
    (amount_minor IS NOT NULL AND amount_min_minor IS NULL AND amount_max_minor IS NULL)
    OR (amount_minor IS NULL AND amount_min_minor IS NOT NULL AND amount_max_minor IS NOT NULL
        AND amount_min_minor > 0 AND amount_min_minor <= amount_max_minor)),
  CONSTRAINT plink_amount_positive CHECK (amount_minor IS NULL OR amount_minor > 0)
);

CREATE INDEX idx_plink_merchant ON payment_links (merchant_id, created_at DESC);

CREATE TABLE payment_link_claims (
  link_id           TEXT NOT NULL REFERENCES payment_links(link_id),
  payment_intent_id TEXT NOT NULL,
  released          BOOLEAN NOT NULL DEFAULT FALSE,
  claimed_at        TIMESTAMPTZ NOT NULL,
  PRIMARY KEY (link_id, payment_intent_id)
);

CREATE UNIQUE INDEX uidx_plink_one_open_claim ON payment_link_claims (link_id) WHERE released = FALSE;
