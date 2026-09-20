-- 0060_vault.sql — schema vault (spec/14-tokenization-vault-pci.md §Data model,
-- verbatim-adapted; lane-B migration range 0050-0099, vault sub-range 0060-0069).
--
-- TARGET DATABASE: the dedicated `vault_db` (Postgres 16), NOT the platform
-- database (spec/14 topology: no other role has CONNECT; the platform `bdpay`
-- role cannot see vault_db; cross-database queries/FDW/logical replication
-- between the two are prohibited). This file ships with the lane-B migration
-- set for review/versioning; the vault deployment pipeline applies it to
-- vault_db under the `vault_svc` role.
--
-- IDs are content-addressed (conventions §3 + errata LB6 additive prefixes
-- tok/epan/vkey/vcus/vcer/vint/vaud/vobx/vrun); timestamps TIMESTAMPTZ UTC
-- (conventions §7); hashes are sha256_canonical per errata E12. There is NO
-- money in this schema: no amount_minor, no currency, no NUMERIC/DECIMAL
-- (conventions §6 satisfied vacuously and deliberately). There is NO column
-- for SAD anywhere: CVV/track/PIN-block columns are a build-breaking
-- violation (spec/14 SAD-store section).
--
-- Errata notes applied:
--   LB8: card_tokens.last_seen_at added (spec text references it in intake
--        step 4 but the spec DDL omits it).
--   detokenization_audit ships partitioned BY RANGE (created_at) with a
--   DEFAULT partition so inserts succeed before the monthly partition job
--   exists; PK (id, created_at) includes the partition key (E1-compatible).

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'vault_svc') THEN
        CREATE ROLE vault_svc NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'vault_app') THEN
        CREATE ROLE vault_app NOLOGIN;
    END IF;
END $$;

CREATE SCHEMA IF NOT EXISTS vault AUTHORIZATION vault_svc;

CREATE TYPE vault.token_status AS ENUM
  ('PENDING_FIRST_AUTH','ACTIVE','SUSPENDED','RETIRED','EXPIRED');
CREATE TYPE vault.intake_session_status AS ENUM
  ('CREATED','CONSUMED','EXPIRED','VOIDED');
CREATE TYPE vault.key_kind AS ENUM
  ('LMK','KEK','DEK','PEPPER','PII_KEY','EGRESS_HMAC');
CREATE TYPE vault.key_status AS ENUM
  ('PENDING_CEREMONY','ACTIVE','DEPRECATED','COMPROMISED','DESTROYED');
CREATE TYPE vault.ceremony_status AS ENUM
  ('DRAFT','SHARES_PENDING','QUORUM_REACHED','EXECUTED','ABORTED');
CREATE TYPE vault.detok_decision AS ENUM ('ALLOWED','DENIED');
CREATE TYPE vault.reencryption_status AS ENUM
  ('PENDING','RUNNING','COMPLETED','FAILED','CANCELLED');

-- ---------------------------------------------------------------------
-- 1. vault_keys — key hierarchy metadata. NEVER plaintext key material.
--    rotation_due_at is the rotation-schedule record (crypto periods:
--    DEK 90d, KEK 1y, LMK 2y, EGRESS_HMAC 90d, PII_KEY 1y; PEPPER NULL =
--    compromise-only).
-- ---------------------------------------------------------------------
CREATE TABLE vault.vault_keys (
    vkey_id              TEXT PRIMARY KEY CHECK (vkey_id LIKE 'vkey_%'),
    kind                 vault.key_kind NOT NULL,
    generation           INTEGER NOT NULL CHECK (generation >= 1),
    status               vault.key_status NOT NULL DEFAULT 'PENDING_CEREMONY',
    kcv                  CHAR(6) NOT NULL,
    wrapped_key_material TEXT,
    openbao_key_name     TEXT,
    openbao_key_version  INTEGER,
    ceremony_id          TEXT,
    created_at           TIMESTAMPTZ NOT NULL,
    activated_at         TIMESTAMPTZ,
    deprecated_at        TIMESTAMPTZ,
    destroyed_at         TIMESTAMPTZ,
    destroy_receipt_hash TEXT,
    rotation_due_at      TIMESTAMPTZ,
    CONSTRAINT lmk_never_exported CHECK (kind <> 'LMK' OR (wrapped_key_material IS NULL AND openbao_key_name IS NULL)),
    CONSTRAINT wrapped_kinds      CHECK (kind NOT IN ('DEK','PEPPER') OR wrapped_key_material IS NOT NULL),
    CONSTRAINT openbao_kinds      CHECK (kind NOT IN ('KEK','PII_KEY','EGRESS_HMAC') OR openbao_key_name IS NOT NULL),
    UNIQUE (kind, openbao_key_name, generation)
);
CREATE UNIQUE INDEX one_active_per_kind_name
    ON vault.vault_keys (kind, COALESCE(openbao_key_name,'')) WHERE status = 'ACTIVE';

-- ---------------------------------------------------------------------
-- 2. key_custodians
-- ---------------------------------------------------------------------
CREATE TABLE vault.key_custodians (
    vcus_id            TEXT PRIMARY KEY CHECK (vcus_id LIKE 'vcus_%'),
    person_name        TEXT NOT NULL,
    operator_identity  TEXT NOT NULL UNIQUE,
    reporting_line     TEXT NOT NULL,
    status             TEXT NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE','REVOKED')),
    appointed_at       TIMESTAMPTZ NOT NULL,
    revoked_at         TIMESTAMPTZ,
    appointment_approval_id TEXT NOT NULL
);

-- ---------------------------------------------------------------------
-- 3. key_ceremonies + 4. ceremony_shares (attestations only — split
--    knowledge: share/component material NEVER transits or rests here)
-- ---------------------------------------------------------------------
CREATE TABLE vault.key_ceremonies (
    vcer_id             TEXT PRIMARY KEY CHECK (vcer_id LIKE 'vcer_%'),
    kind                vault.key_kind NOT NULL,
    target_key_name     TEXT NOT NULL,
    reason              TEXT NOT NULL CHECK (reason IN ('initial','scheduled_rotation','compromise','custodian_change')),
    quorum              INTEGER NOT NULL CHECK (quorum >= 2),
    custodian_ids       TEXT[] NOT NULL,
    status              vault.ceremony_status NOT NULL DEFAULT 'DRAFT',
    approval_request_id TEXT NOT NULL,
    opened_at           TIMESTAMPTZ NOT NULL,
    quorum_at           TIMESTAMPTZ,
    executed_at         TIMESTAMPTZ,
    aborted_at          TIMESTAMPTZ,
    ttl_expires_at      TIMESTAMPTZ NOT NULL,
    evidence_pack_ref   TEXT
);
CREATE TABLE vault.ceremony_shares (
    ceremony_id      TEXT NOT NULL REFERENCES vault.key_ceremonies(vcer_id),
    custodian_id     TEXT NOT NULL REFERENCES vault.key_custodians(vcus_id),
    share_kcv        CHAR(6) NOT NULL,
    attestation_hash TEXT NOT NULL,
    attested_at      TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (ceremony_id, custodian_id)
);
ALTER TABLE vault.vault_keys
    ADD CONSTRAINT vault_keys_ceremony_fk
    FOREIGN KEY (ceremony_id) REFERENCES vault.key_ceremonies(vcer_id);

-- ---------------------------------------------------------------------
-- 5. card_tokens — metadata reads expose BIN(<=8)+last4 only (Req 3.4.1).
--    Cardholder name is CHD: stored only as DEK-encrypted ciphertext.
-- ---------------------------------------------------------------------
CREATE TABLE vault.card_tokens (
    token              TEXT PRIMARY KEY CHECK (token LIKE 'tok_%'),
    pan_hmac           TEXT NOT NULL,             -- HMAC-SHA256(PEPPER, PAN); NEVER unkeyed sha256
    pepper_kid         TEXT NOT NULL REFERENCES vault.vault_keys(vkey_id),
    bin                VARCHAR(8) NOT NULL,
    last4              CHAR(4) NOT NULL,
    scheme             TEXT NOT NULL CHECK (scheme IN ('VISA','MASTERCARD','AMEX','JCB','UNIONPAY','UNKNOWN')),
    expiry_month       CHAR(2) NOT NULL,
    expiry_year        CHAR(4) NOT NULL,
    holder_name_ct     BYTEA,
    holder_name_nonce  BYTEA CHECK (octet_length(holder_name_nonce) = 12),
    holder_name_dek    TEXT REFERENCES vault.vault_keys(vkey_id),
    status             vault.token_status NOT NULL DEFAULT 'PENDING_FIRST_AUTH',
    origin             TEXT NOT NULL CHECK (origin IN ('intake','import')),
    merchant_id        TEXT NOT NULL,             -- mrch_<id> cross-ref; no FK across DBs
    customer_id        TEXT,                      -- cust_<id> for save_card tokens
    created_at         TIMESTAMPTZ NOT NULL,
    first_used_at      TIMESTAMPTZ,
    last_used_at       TIMESTAMPTZ,
    last_seen_at       TIMESTAMPTZ,               -- additive (errata LB8)
    terminal_at        TIMESTAMPTZ,
    retire_reason      TEXT,
    UNIQUE (pan_hmac, pepper_kid)
);
CREATE INDEX card_tokens_sweep ON vault.card_tokens (expiry_year, expiry_month)
    WHERE status NOT IN ('RETIRED','EXPIRED');

-- ---------------------------------------------------------------------
-- 6. encrypted_pans — exactly one live ciphertext per token; AAD binds
--    ciphertext to token; row HARD-DELETED at destroy_after.
-- ---------------------------------------------------------------------
CREATE TABLE vault.encrypted_pans (
    epan_id        TEXT PRIMARY KEY CHECK (epan_id LIKE 'epan_%'),
    token          TEXT NOT NULL UNIQUE REFERENCES vault.card_tokens(token),
    dek_key_id     TEXT NOT NULL REFERENCES vault.vault_keys(vkey_id),
    nonce          BYTEA NOT NULL CHECK (octet_length(nonce) = 12),   -- HSM/os.urandom; NEVER deterministic
    pan_ciphertext BYTEA NOT NULL,
    aad            TEXT NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL,
    reencrypted_at TIMESTAMPTZ,
    destroy_after  TIMESTAMPTZ
);
CREATE INDEX encrypted_pans_by_dek ON vault.encrypted_pans (dek_key_id);
CREATE INDEX encrypted_pans_destroy ON vault.encrypted_pans (destroy_after) WHERE destroy_after IS NOT NULL;

-- ---------------------------------------------------------------------
-- 7. card_intake_sessions
-- ---------------------------------------------------------------------
CREATE TABLE vault.card_intake_sessions (
    vint_id           TEXT PRIMARY KEY CHECK (vint_id LIKE 'vint_%'),
    merchant_id       TEXT NOT NULL,
    purpose           TEXT NOT NULL CHECK (purpose IN ('payment','save_card')),
    payment_intent_id TEXT,
    customer_id       TEXT,
    allowed_origins   JSONB NOT NULL,
    cvv_required      BOOLEAN NOT NULL DEFAULT TRUE,
    status            vault.intake_session_status NOT NULL DEFAULT 'CREATED',
    failed_attempts   INTEGER NOT NULL DEFAULT 0 CHECK (failed_attempts <= 6),
    token             TEXT REFERENCES vault.card_tokens(token),
    created_at        TIMESTAMPTZ NOT NULL,
    expires_at        TIMESTAMPTZ NOT NULL,
    terminal_at       TIMESTAMPTZ,
    CONSTRAINT purpose_binding CHECK (
        (purpose = 'payment'   AND payment_intent_id IS NOT NULL) OR
        (purpose = 'save_card' AND customer_id IS NOT NULL))
);
CREATE INDEX intake_sessions_sweep ON vault.card_intake_sessions (expires_at) WHERE status = 'CREATED';

-- ---------------------------------------------------------------------
-- 8. detokenization_audit — one row per egress ATTEMPT (allowed or denied).
--    Monthly partitions (DEFAULT partition ships so inserts never fail
--    before the partition job runs), 12-month hot, then WORM archive.
-- ---------------------------------------------------------------------
CREATE SEQUENCE vault.detok_audit_id_seq;
CREATE TABLE vault.detokenization_audit (
    id                 BIGINT NOT NULL DEFAULT nextval('vault.detok_audit_id_seq'),
    instruction_id     TEXT NOT NULL,
    connector_ref      TEXT NOT NULL,
    token              TEXT NOT NULL,
    caller_san         TEXT NOT NULL,
    decision           vault.detok_decision NOT NULL,
    denial_code        TEXT,
    grant_hash         TEXT NOT NULL,
    acquirer_host      TEXT,
    sad_consumed       BOOLEAN NOT NULL DEFAULT FALSE,
    egress_at          TIMESTAMPTZ,
    responded_at       TIMESTAMPTZ,
    acquirer_status    INTEGER,
    raw_response_hash  TEXT,
    stored_response    JSONB,
    replay_expires_at  TIMESTAMPTZ,
    created_at         TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (id, created_at)
) PARTITION BY RANGE (created_at);
CREATE TABLE vault.detokenization_audit_default
    PARTITION OF vault.detokenization_audit DEFAULT;
CREATE UNIQUE INDEX detok_idempotency ON vault.detokenization_audit (connector_ref, created_at)
    WHERE decision = 'ALLOWED';
CREATE INDEX detok_by_token ON vault.detokenization_audit (token, created_at);
CREATE INDEX detok_by_instruction ON vault.detokenization_audit (instruction_id, created_at);
ALTER TABLE vault.detokenization_audit ENABLE ROW LEVEL SECURITY;
CREATE POLICY detok_rw ON vault.detokenization_audit FOR ALL TO vault_app
    USING (TRUE) WITH CHECK (TRUE);
CREATE POLICY detok_no_delete ON vault.detokenization_audit AS RESTRICTIVE
    FOR DELETE TO vault_app USING (FALSE);
CREATE FUNCTION vault.detok_guard_update() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF (NEW.instruction_id, NEW.connector_ref, NEW.token, NEW.decision, NEW.grant_hash,
        NEW.raw_response_hash, NEW.created_at)
       IS DISTINCT FROM
       (OLD.instruction_id, OLD.connector_ref, OLD.token, OLD.decision, OLD.grant_hash,
        OLD.raw_response_hash, OLD.created_at)
       OR NEW.stored_response IS NOT NULL THEN
        RAISE EXCEPTION 'detokenization_audit: only nulling stored_response is permitted';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER detok_update_guard BEFORE UPDATE ON vault.detokenization_audit
    FOR EACH ROW EXECUTE FUNCTION vault.detok_guard_update();

-- ---------------------------------------------------------------------
-- 9. vault_audit_chain — hash-chained, append-only CDE audit. Preimage:
--    canonical_json({prev_hash, chain_index, entry_type, payload_hash,
--    actor, produced_at}). Payload is PII-free by filter.
-- ---------------------------------------------------------------------
CREATE TABLE vault.vault_audit_chain (
    vaud_id      TEXT NOT NULL CHECK (vaud_id LIKE 'vaud_%'),
    chain_index  BIGINT PRIMARY KEY,
    prev_hash    TEXT NOT NULL,
    entry_type   TEXT NOT NULL,
    payload      JSONB NOT NULL,
    payload_hash TEXT NOT NULL,
    actor        TEXT NOT NULL,
    produced_at  TIMESTAMPTZ NOT NULL,
    entry_hash   TEXT NOT NULL UNIQUE
);
ALTER TABLE vault.vault_audit_chain ENABLE ROW LEVEL SECURITY;
CREATE POLICY vaud_insert ON vault.vault_audit_chain FOR ALL TO vault_app
    USING (TRUE) WITH CHECK (TRUE);
CREATE POLICY vaud_no_update ON vault.vault_audit_chain AS RESTRICTIVE
    FOR UPDATE TO vault_app USING (FALSE);
CREATE POLICY vaud_no_delete ON vault.vault_audit_chain AS RESTRICTIVE
    FOR DELETE TO vault_app USING (FALSE);

-- ---------------------------------------------------------------------
-- 10. vault_outbox — sole event egress (conventions §5)
-- ---------------------------------------------------------------------
CREATE TABLE vault.vault_outbox (
    vobx_id      TEXT PRIMARY KEY CHECK (vobx_id LIKE 'vobx_%'),
    event_type   TEXT NOT NULL,
    subject_type TEXT NOT NULL,
    subject_id   TEXT NOT NULL,
    payload      JSONB NOT NULL,                  -- PAN-free by schema (bin/last4 max)
    occurred_at  TIMESTAMPTZ NOT NULL,
    published_at TIMESTAMPTZ,
    attempts     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX vault_outbox_unpublished ON vault.vault_outbox (occurred_at) WHERE published_at IS NULL;

-- ---------------------------------------------------------------------
-- 11. vault_api_allowlist — caller identity -> operations (default deny).
--     Mutations require four-eyes ApprovalRequest and are audited.
-- ---------------------------------------------------------------------
CREATE TABLE vault.vault_api_allowlist (
    caller_san          TEXT PRIMARY KEY,
    operations          TEXT[] NOT NULL,
    approval_request_id TEXT NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL,
    revoked_at          TIMESTAMPTZ
);

-- ---------------------------------------------------------------------
-- 12. token_aliases — pepper-rotation dual-token migration map
-- ---------------------------------------------------------------------
CREATE TABLE vault.token_aliases (
    old_token      TEXT PRIMARY KEY REFERENCES vault.card_tokens(token),
    new_token      TEXT NOT NULL REFERENCES vault.card_tokens(token),
    old_pepper_kid TEXT NOT NULL REFERENCES vault.vault_keys(vkey_id),
    new_pepper_kid TEXT NOT NULL REFERENCES vault.vault_keys(vkey_id),
    migrated_at    TIMESTAMPTZ NOT NULL
);

-- ---------------------------------------------------------------------
-- 13. reencryption_runs — DEK rotation + PII-key rotation evidence
-- ---------------------------------------------------------------------
CREATE TABLE vault.reencryption_runs (
    vrun_id      TEXT PRIMARY KEY CHECK (vrun_id LIKE 'vrun_%'),
    scope        TEXT NOT NULL,
    old_key_id   TEXT NOT NULL REFERENCES vault.vault_keys(vkey_id),
    new_key_id   TEXT NOT NULL REFERENCES vault.vault_keys(vkey_id),
    ceremony_id  TEXT REFERENCES vault.key_ceremonies(vcer_id),
    status       vault.reencryption_status NOT NULL DEFAULT 'PENDING',
    total_rows   BIGINT,
    done_rows    BIGINT NOT NULL DEFAULT 0,
    cursor_ref   TEXT,
    rate_cap_rps INTEGER NOT NULL DEFAULT 50,
    started_at   TIMESTAMPTZ,
    deadline_at  TIMESTAMPTZ NOT NULL,
    completed_at TIMESTAMPTZ,
    stats        JSONB NOT NULL DEFAULT '{}'::jsonb
);

-- ---------------------------------------------------------------------
-- 14. vault_idempotency_keys — Plane-B mutating endpoints (durable in
--     vault_db; Redis is out-of-CDE and never used by the vault; 24h sweep)
-- ---------------------------------------------------------------------
CREATE TABLE vault.vault_idempotency_keys (
    idempotency_key TEXT NOT NULL,
    caller_san      TEXT NOT NULL,
    request_hash    TEXT NOT NULL,
    response_status INTEGER NOT NULL,
    response_body   JSONB NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (idempotency_key, caller_san)
);
CREATE INDEX vault_idem_sweep ON vault.vault_idempotency_keys (created_at);

-- Role grants (runtime role = vault_app; owner/DDL = vault_svc):
GRANT USAGE ON SCHEMA vault TO vault_app;
GRANT INSERT, SELECT ON ALL TABLES IN SCHEMA vault TO vault_app;
GRANT UPDATE ON vault.card_tokens, vault.card_intake_sessions, vault.vault_keys,
    vault.key_ceremonies, vault.vault_outbox, vault.reencryption_runs,
    vault.encrypted_pans, vault.vault_api_allowlist, vault.detokenization_audit TO vault_app;
GRANT DELETE ON vault.encrypted_pans, vault.vault_idempotency_keys TO vault_app;
GRANT USAGE ON SEQUENCE vault.detok_audit_id_seq TO vault_app;
