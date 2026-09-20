-- 0070_signing_and_rule_packs.sql — spec/06 Data model: signing_key_registry
-- + rule_packs (Ed25519-signed, versioned monitoring rule artifacts).
--
-- Errata E-C4 (SPEC_ERRATA-LANE-A-compliance.md): spec/06 declares
--   CONSTRAINT one_active_pack UNIQUE NULLS NOT DISTINCT (status, activated_at)
-- with a comment saying "enforced in-app: only one ACTIVE". That constraint
-- does not enforce one-active (two ACTIVE rows with different activated_at
-- both pass). The partial unique index below enforces the spec's stated
-- intent directly.

CREATE TABLE signing_key_registry (
    key_id              TEXT PRIMARY KEY,            -- signer_<name>_v<version>
    signer_id           TEXT NOT NULL,
    algorithm           TEXT NOT NULL DEFAULT 'Ed25519',
    scope               TEXT NOT NULL DEFAULT 'pack_signing',
    status              TEXT NOT NULL CHECK (status IN ('active','revoked')),
    public_key_b64      TEXT,                        -- 32-byte Ed25519 public key, base64
    registry_version    INTEGER NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL,
    revoked_at          TIMESTAMPTZ,
    revocation_reason   TEXT,
    record_hash         TEXT NOT NULL,               -- sha256(canonical_json(row_without_timestamps))
    schema_version      SMALLINT NOT NULL DEFAULT 1
);

CREATE UNIQUE INDEX idx_skr_active_signer ON signing_key_registry (signer_id)
    WHERE status = 'active';

CREATE TABLE rule_packs (
    pack_id             TEXT PRIMARY KEY,            -- pack_<sha256(canonical_json(manifest_core))[:24]> (E-C2)
    pack_version        TEXT NOT NULL,               -- semver string e.g. "1.2.0"
    domain              TEXT NOT NULL DEFAULT 'Bangladesh PSP/PSO AML',
    jurisdiction_scope  CHAR(2) NOT NULL DEFAULT 'BD',
    status              TEXT NOT NULL
                        CHECK (status IN ('ACTIVE','SUPERSEDED','REJECTED')),
    rule_count          INTEGER NOT NULL CHECK (rule_count > 0),
    signer_id           TEXT NOT NULL,               -- active entry in signing_key_registry
    signed_pack_content_hash TEXT NOT NULL,          -- sha256 hex of canonical pack rules
    signature_hex       TEXT NOT NULL,               -- Ed25519 signature hex (128 chars)
    pack_manifest_hash  TEXT NOT NULL,
    rules_json          JSONB NOT NULL,              -- full rules object; validated at load
    activated_at        TIMESTAMPTZ NOT NULL,
    superseded_at       TIMESTAMPTZ,
    activation_notes    TEXT,
    activated_by        TEXT NOT NULL,               -- operator_id; PII-redacted to role
    schema_version      SMALLINT NOT NULL DEFAULT 1
);

-- One ACTIVE pack at any time (errata E-C4 resolution).
CREATE UNIQUE INDEX idx_rule_packs_one_active ON rule_packs ((TRUE))
    WHERE status = 'ACTIVE';
CREATE INDEX idx_rule_packs_active ON rule_packs (status) WHERE status = 'ACTIVE';
