-- 0074_sanctions_lists.sql — spec/07 Data model: sanctions_lists (registry),
-- sanctions_list_versions (immutable, append-only), sanctions_list_entries.

CREATE TABLE sanctions_lists (
    list_code       TEXT PRIMARY KEY
                    CHECK (list_code IN ('UN_1267','UN_1373','BFIU_DOMESTIC')),
    description     TEXT NOT NULL,
    list_priority   SMALLINT NOT NULL,            -- 1=UN_1267, 2=UN_1373, 3=BFIU_DOMESTIC
    source_kind     TEXT NOT NULL CHECK (source_kind IN ('CONNECTOR','MANUAL_UPLOAD','BOTH')),
    schema_version  SMALLINT NOT NULL DEFAULT 1
);
INSERT INTO sanctions_lists VALUES
 ('UN_1267','UN Security Council Consolidated List (ISIL/Al-Qaida/Taliban regimes)',1,'CONNECTOR',1),
 ('UN_1373','UNSCR 1373 designations as implemented in Bangladesh (ATA 2009)',2,'BOTH',1),
 ('BFIU_DOMESTIC','BFIU domestic designation/suspension list',3,'MANUAL_UPLOAD',1);

CREATE TABLE sanctions_list_versions (
    list_version_id   TEXT PRIMARY KEY,           -- slist_<sha256_canonical[:24]>
    list_code         TEXT NOT NULL REFERENCES sanctions_lists(list_code),
    source_artifact_sha256 TEXT NOT NULL,         -- raw feed file in object store, content-addressed
    source_artifact_pointer TEXT NOT NULL,        -- object store key
    entry_count       INTEGER NOT NULL CHECK (entry_count >= 0),
    added_count       INTEGER NOT NULL DEFAULT 0,
    changed_count     INTEGER NOT NULL DEFAULT 0,
    removed_count     INTEGER NOT NULL DEFAULT 0,
    status            TEXT NOT NULL CHECK (status IN ('ACTIVE','SUPERSEDED','REJECTED')),
    ingested_by       TEXT NOT NULL,              -- operator_id or 'sanctions_feed_v1'
    connector_result_id TEXT,                     -- cres_* when source_kind=CONNECTOR
    ingested_at       TIMESTAMPTZ NOT NULL,
    superseded_at     TIMESTAMPTZ,
    schema_version    SMALLINT NOT NULL DEFAULT 1,
    UNIQUE (list_code, source_artifact_sha256)    -- idempotent ingestion
);
CREATE UNIQUE INDEX idx_slv_active ON sanctions_list_versions (list_code) WHERE status='ACTIVE';

CREATE TABLE sanctions_list_entries (
    entry_id          TEXT PRIMARY KEY,           -- sent_<sha256_canonical[:24]>
    list_version_id   TEXT NOT NULL REFERENCES sanctions_list_versions(list_version_id),
    list_code         TEXT NOT NULL REFERENCES sanctions_lists(list_code),
    entry_reference   TEXT NOT NULL,              -- e.g. UN permanent ref 'QDi.063'; BFIU serial
    entry_content_hash TEXT NOT NULL,             -- whitelist anchoring + change detection
    entity_kind       TEXT NOT NULL CHECK (entity_kind IN ('INDIVIDUAL','ENTITY','VESSEL','AIRCRAFT')),
    primary_name      TEXT NOT NULL,              -- as published
    primary_name_norm TEXT NOT NULL,              -- canonical normalized at ingestion
    aliases           TEXT[] NOT NULL DEFAULT '{}',
    aliases_norm      TEXT[] NOT NULL DEFAULT '{}',
    dob               DATE,
    dob_year_only     SMALLINT,
    nationalities     TEXT[] NOT NULL DEFAULT '{}', -- ISO 3166-1 alpha-2
    identifiers       JSONB NOT NULL DEFAULT '{}',  -- {"passport":[...],"nid":[...],"other":[...]}
    dossier_pointer   TEXT NOT NULL,              -- object store key for full published entry text
    designated_at     DATE,
    schema_version    SMALLINT NOT NULL DEFAULT 1
);
CREATE INDEX idx_sle_version ON sanctions_list_entries (list_version_id);
CREATE INDEX idx_sle_content ON sanctions_list_entries (entry_content_hash);
