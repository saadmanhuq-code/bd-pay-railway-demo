-- 0081_gateway_operator_auth.sql — operator sessions + TOTP enrollment
-- (spec/01 §Data model OPERATOR SESSIONS; FSM 2; BB ICT §9.2 / §6.2.6).
--
-- Errata G-9 additions over the spec/01 column set:
--   * operator_sessions.totp_verified_at — the refund 2FA gate (spec/01 §E)
--     requires "TOTP verified within the last 900 seconds"; without this
--     column the fact is unrecorded and the gate cannot be enforced.
--   * operator_auth_failures — the 3-failures-in-30-min lockout FSM needs
--     durable counters; spec/01 describes the FSM but declares no store.
--
-- operators(operator_id) is owned by spec/15 IAM; cross-package integrity is
-- application-enforced.

CREATE TYPE operator_role AS ENUM (
    'SUPER_ADMIN', 'MERCHANT_ADMIN', 'PAYMENT_OPS', 'KYC_REVIEWER',
    'CAMLCO', 'FINANCE', 'TREASURY', 'NOTIFICATION_ADMIN', 'IAM_ADMIN',
    'READ_ONLY', 'BB_INSPECTOR_READ_ONLY'
);

CREATE TYPE operator_session_status AS ENUM ('ACTIVE', 'TERMINATED', 'LOCKED');

CREATE TABLE operator_sessions (
    session_id          TEXT PRIMARY KEY,            -- osess_<sha256_canonical[:24]>
    operator_id         TEXT NOT NULL,               -- spec/15 operators (app-enforced)
    jti                 TEXT NOT NULL UNIQUE,        -- sha256(session token)
    roles               operator_role[] NOT NULL,
    ip_hash             TEXT NOT NULL,               -- sha256(ip); raw IP never stored
    user_agent_hash     TEXT NOT NULL,               -- sha256(user agent)
    status              operator_session_status NOT NULL DEFAULT 'ACTIVE',
    created_at          TIMESTAMPTZ NOT NULL,
    expires_at          TIMESTAMPTZ NOT NULL,        -- created_at + 8h
    last_activity_at    TIMESTAMPTZ NOT NULL,
    totp_verified_at    TIMESTAMPTZ NOT NULL,        -- errata G-9: 2FA freshness gates
    terminated_at       TIMESTAMPTZ,
    termination_reason  TEXT,
    schema_version      SMALLINT NOT NULL DEFAULT 1
);

CREATE INDEX idx_osess_operator ON operator_sessions (operator_id) WHERE status = 'ACTIVE';
CREATE INDEX idx_osess_expires ON operator_sessions (expires_at) WHERE status = 'ACTIVE';

CREATE TABLE operator_totp_enrollments (
    operator_id         TEXT PRIMARY KEY,
    totp_secret_enc     TEXT NOT NULL,               -- AES-256-GCM sealed base32 secret
    backup_code_hashes  TEXT[] NOT NULL,             -- sha256 of each 10-digit backup code
    enrolled_at         TIMESTAMPTZ NOT NULL,
    last_used_at        TIMESTAMPTZ,
    schema_version      SMALLINT NOT NULL DEFAULT 1
);

CREATE TABLE operator_auth_failures (
    operator_id         TEXT PRIMARY KEY,
    failed_attempts     SMALLINT NOT NULL DEFAULT 0,
    window_started_at   TIMESTAMPTZ,
    locked_until        TIMESTAMPTZ,
    schema_version      SMALLINT NOT NULL DEFAULT 1
);
