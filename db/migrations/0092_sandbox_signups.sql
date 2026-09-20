-- 0092_sandbox_signups.sql — self-serve public-sandbox registrations
-- (spec/16 §Data model, LR-4; DDL verbatim from the spec).
--
-- SANDBOX_PUBLIC deployments only: the table lives in the sandbox database,
-- which is a separate deployment/DB/OpenBao namespace (spec/16 §API F inv 1).
--
-- email_hash is sha256(lowercased email); the raw email travels only through
-- the (sandbox) notification dispatch path and is never persisted here. The
-- OTP digest is NOT a column — it lives in a separate short-lived vault
-- (deployment: Redis), mirroring the spec/01 operator OTP posture.
--
-- merchant_id / api_key_id are cross-package references (spec/08 / spec/01);
-- integrity is application-enforced (E20 posture, same as 0080/0094).

CREATE TABLE sandbox_signups (
  sandbox_signup_id TEXT PRIMARY KEY,          -- sbxs_<sha256_canonical({email_hash, created_at})[:24]>
  email_hash        TEXT NOT NULL,             -- sha256(lowercased email); raw email only in the (sandbox) notification path
  display_name      TEXT NOT NULL,
  state             TEXT NOT NULL DEFAULT 'CREATED'
    CHECK (state IN ('CREATED','VERIFIED','PROVISIONED','EXPIRED','REVOKED')),
  otp_attempts      SMALLINT NOT NULL DEFAULT 0,
  merchant_id       TEXT,                      -- set at PROVISIONED
  api_key_id        TEXT,                      -- akey_<…>, set at PROVISIONED
  revoke_reason     TEXT,
  created_at        TIMESTAMPTZ NOT NULL,
  provisioned_at    TIMESTAMPTZ,
  schema_version    SMALLINT NOT NULL DEFAULT 1
);

CREATE INDEX idx_sbxs_email ON sandbox_signups (email_hash, created_at DESC);
