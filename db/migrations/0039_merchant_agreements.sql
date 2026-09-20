-- 0039_merchant_agreements.sql — spec/08 DDL §Section 5 (MERCHANT AGREEMENTS).
--
-- K-17 (errata): the spec's agreements_single_active CHECK contains a
-- subquery, which Postgres rejects in CHECK constraints (the spec itself
-- calls it advisory). The one-active-version invariant ships as a partial
-- unique index over a constant expression — only one row may have
-- effective_to IS NULL.

CREATE TABLE merchant_agreements (
    agreement_version      TEXT PRIMARY KEY,    -- human-readable semver, e.g. 'v1.3'
    agreement_hash         TEXT NOT NULL,       -- sha256(canonical_json(agreement_text))
    agreement_text_pointer TEXT NOT NULL,       -- object-store pointer
    effective_from         DATE NOT NULL,
    effective_to           DATE,                -- NULL = currently published
    published_by           TEXT NOT NULL,
    published_at           TIMESTAMPTZ NOT NULL,
    legal_review_ref       TEXT
);

CREATE UNIQUE INDEX merchant_agreements_single_active
    ON merchant_agreements ((1)) WHERE effective_to IS NULL;

CREATE TABLE merchant_agreement_acceptances (
    acceptance_id     TEXT PRIMARY KEY,
    -- make_id('agrac', {merchant_id, agreement_version, accepted_at})

    merchant_id       TEXT NOT NULL REFERENCES merchants (merchant_id),
    agreement_version TEXT NOT NULL REFERENCES merchant_agreements (agreement_version),
    agreement_hash    TEXT NOT NULL,    -- denormalized hash at acceptance time; immutable

    signer_name       TEXT NOT NULL,
    signer_role       TEXT NOT NULL,
    signer_nid_last4  TEXT NOT NULL,

    sign_token_hash   TEXT NOT NULL,    -- sha256(sign_token); the token is ephemeral
    ip_address_hash   TEXT,             -- sha256(ip); raw IP never stored
    user_agent_hash   TEXT,

    accepted_at       TIMESTAMPTZ NOT NULL,
    schema_version    SMALLINT NOT NULL DEFAULT 1
);

CREATE INDEX idx_acceptances_merchant ON merchant_agreement_acceptances (merchant_id);

-- Append-only: acceptance rows can never be modified or removed.
CREATE OR REPLACE FUNCTION merchant_agreement_acceptances_append_only()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'merchant_agreement_acceptances is append-only';
END;
$$;

CREATE TRIGGER merchant_agreement_acceptances_no_update
    BEFORE UPDATE OR DELETE ON merchant_agreement_acceptances
    FOR EACH ROW EXECUTE FUNCTION merchant_agreement_acceptances_append_only();
