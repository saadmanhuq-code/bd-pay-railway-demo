-- Durable customer OTP sessions for gateway high-value confirms.
--
-- Tokens are stored as SHA-256 digests so the raw OTP session token is never
-- persisted. The used flag enforces single-use verification across gateway
-- workers, and attempt_count gives audit/rate-limit evidence for misses.

CREATE TABLE IF NOT EXISTS otp_sessions (
    token_hash TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL,
    issued_at TIMESTAMPTZ NOT NULL,
    used BOOLEAN NOT NULL DEFAULT FALSE,
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    schema_version SMALLINT NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_otp_sessions_issued_at
    ON otp_sessions (issued_at);

CREATE INDEX IF NOT EXISTS idx_otp_sessions_customer
    ON otp_sessions (customer_id);
