-- 0044_kyb_api_key_issuances.sql — API-key issuance record at merchant
-- activation (spec/08 activation flow; build brief).
--
-- The gateway lane (spec/01) owns the live api_keys table; this kernel
-- table is the onboarding-side issuance evidence: WHICH key was minted for
-- WHICH merchant, WHEN, by WHOM, with the secret stored only as a sha256
-- hash — the raw secret is returned to the caller exactly once and never
-- persisted anywhere.

CREATE TABLE kyb_api_key_issuances (
    issuance_id    TEXT        PRIMARY KEY,   -- apik_<sha256[:24]> (kernel additive prefix, K-2)
    merchant_id    TEXT        NOT NULL REFERENCES merchants (merchant_id),
    key_id         TEXT        NOT NULL,
    secret_hash    TEXT        NOT NULL CHECK (char_length(secret_hash) = 64),
    issued_at      TIMESTAMPTZ NOT NULL,
    issued_by      TEXT        NOT NULL,
    schema_version SMALLINT    NOT NULL DEFAULT 1
);

CREATE INDEX idx_apik_merchant ON kyb_api_key_issuances (merchant_id);

-- Append-only issuance evidence.
CREATE OR REPLACE FUNCTION kyb_api_key_issuances_append_only()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'kyb_api_key_issuances is append-only';
END;
$$;

CREATE TRIGGER kyb_api_key_issuances_no_update
    BEFORE UPDATE OR DELETE ON kyb_api_key_issuances
    FOR EACH ROW EXECUTE FUNCTION kyb_api_key_issuances_append_only();
