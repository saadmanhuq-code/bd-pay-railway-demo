-- 0085_gateway_operator_directory.sql — durable operator first-factor directory
-- (spec/15 IAM boundary, plugged into the gateway's spec/01 operator 2FA FSM).
--
-- Migration 0081 intentionally stored only TOTP enrollments, sessions, and
-- lockout counters, with a comment that operators(operator_id) is owned by
-- spec/15 IAM. The Python composition root had no external IAM adapter or
-- configuration path, so PG mode paired durable 2FA state with a volatile,
-- never-seeded InMemoryOperatorDirectory. This table is the local durable IAM
-- directory until an external IAM provider is explicitly wired.

CREATE TYPE operator_directory_status AS ENUM ('ACTIVE', 'DISABLED', 'DELETED');

CREATE TABLE operator_directory_entries (
    operator_id          TEXT PRIMARY KEY,
    password_token_hash TEXT NOT NULL,              -- sha256(password token); raw never stored
    roles               operator_role[] NOT NULL,
    status              operator_directory_status NOT NULL DEFAULT 'ACTIVE',
    created_at          TIMESTAMPTZ NOT NULL,
    updated_at          TIMESTAMPTZ NOT NULL,
    disabled_at         TIMESTAMPTZ,
    schema_version      SMALLINT NOT NULL DEFAULT 1,
    CONSTRAINT operator_directory_hash_sha256_hex CHECK (
        password_token_hash ~ '^[0-9a-f]{64}$'
    ),
    CONSTRAINT operator_directory_roles_nonempty CHECK (cardinality(roles) > 0),
    CONSTRAINT operator_directory_disabled_at_terminal CHECK (
        disabled_at IS NULL OR status IN ('DISABLED', 'DELETED')
    )
);

CREATE INDEX idx_operator_directory_active ON operator_directory_entries (operator_id)
    WHERE status = 'ACTIVE';
