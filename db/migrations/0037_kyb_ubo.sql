-- 0037_kyb_ubo.sql — spec/08 DDL §Section 3 (UBO GRAPH): nodes + edges.
-- Acyclicity is enforced application-side (DFS before insert); the DB
-- prevents duplicate edges and self-loops. NID never appears unhashed.

CREATE TYPE ubo_node_type_enum AS ENUM ('NATURAL_PERSON', 'LEGAL_ENTITY');

CREATE TYPE porichoy_status_enum AS ENUM (
    'NOT_REQUIRED',
    'PENDING',
    'VERIFIED',
    'MISMATCH',
    'UNAVAILABLE',
    'MANUALLY_VERIFIED'
);

CREATE TYPE ubo_sanctions_status_enum AS ENUM (
    'PENDING',
    'CLEAR',
    'HIT_UNREVIEWED',
    'HIT_REVIEWED',
    'CONFIRMED_HIT'
);

CREATE TABLE kyb_ubo_nodes (
    ubo_node_id              TEXT PRIMARY KEY,
    -- make_id('ubon', {kyb_record_id, node_type, identifier_hash}); the
    -- identifier_hash is sha256 of the NID/registration number — NEVER raw.

    kyb_record_id            TEXT NOT NULL REFERENCES kyb_records (kyb_record_id),
    node_type                ubo_node_type_enum NOT NULL,

    full_name_en             TEXT NOT NULL,
    full_name_bn             TEXT,

    nid_number_encrypted     BYTEA,
    nid_number_hash          TEXT,
    passport_number_encrypted BYTEA,

    dob                      DATE,
    nationality              CHAR(2) NOT NULL DEFAULT 'BD',

    is_ultimate_beneficial_owner BOOLEAN NOT NULL DEFAULT FALSE,
    ownership_percentage_direct  NUMERIC(5,2),
    -- display ratio only; NEVER money arithmetic (spec/00 §6 stays intact)
    role                     TEXT NOT NULL,

    porichoy_status          porichoy_status_enum NOT NULL DEFAULT 'PENDING',
    porichoy_ref             TEXT,
    porichoy_verified_at     TIMESTAMPTZ,
    manual_verify_by         TEXT,
    manual_verify_at         TIMESTAMPTZ,
    manual_verify_note       TEXT,

    sanctions_status         ubo_sanctions_status_enum NOT NULL DEFAULT 'PENDING',
    sanctions_screened_at    TIMESTAMPTZ,
    sanctions_hit_id         TEXT,

    created_at               TIMESTAMPTZ NOT NULL,
    created_by               TEXT NOT NULL,
    schema_version           SMALLINT NOT NULL DEFAULT 1
);

CREATE INDEX idx_ubo_nodes_kyb       ON kyb_ubo_nodes (kyb_record_id);
CREATE INDEX idx_ubo_nodes_sanctions ON kyb_ubo_nodes (sanctions_status)
    WHERE sanctions_status NOT IN ('CLEAR');
CREATE INDEX idx_ubo_nodes_porichoy  ON kyb_ubo_nodes (porichoy_status)
    WHERE porichoy_status IN ('PENDING', 'UNAVAILABLE');

CREATE TABLE kyb_ubo_edges (
    edge_id                  TEXT PRIMARY KEY,
    -- make_id('uboe', {parent_node_id, child_node_id})

    kyb_record_id            TEXT NOT NULL REFERENCES kyb_records (kyb_record_id),
    parent_node_id           TEXT NOT NULL REFERENCES kyb_ubo_nodes (ubo_node_id),
    child_node_id            TEXT NOT NULL REFERENCES kyb_ubo_nodes (ubo_node_id),
    ownership_percentage     NUMERIC(5,2),

    created_at               TIMESTAMPTZ NOT NULL,

    CONSTRAINT ubo_edges_no_self_loop CHECK (parent_node_id != child_node_id),
    CONSTRAINT ubo_edges_unique UNIQUE (parent_node_id, child_node_id)
);
