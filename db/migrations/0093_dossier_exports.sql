-- 0093_dossier_exports.sql — spec/16 Data model (LR-2): dossier_exports —
-- (renumbered from 0090: the parallel-lane fact_corrections migration took
-- 0090 on the same branch; errata E-S16-14 pins one-number-one-migration)
-- one assembled, hash-manifested evidence ZIP per content-addressed
-- (dossier_type, input_snapshot) pair. The input_snapshot is the determinism
-- anchor: re-assembly from an identical snapshot is byte-identical.
--
-- IDs per the E18-class shim (SPEC_ERRATA): dxp_<sha256_canonical(...)[:24]>.
-- App-role posture (spec/16 Data model note): terminal rows ('COMPLETED',
-- 'FAILED') are immutable; state advances only via the DossierExport FSM.

CREATE TABLE dossier_exports (
  dossier_export_id TEXT PRIMARY KEY,         -- dxp_<sha256_canonical({dossier_type, input_snapshot})[:24]>
  dossier_type      TEXT NOT NULL
    CHECK (dossier_type IN ('BB_PHASE2','SPONSOR_BANK_PACK','BFIU_EVIDENCE')),
  input_snapshot    JSONB NOT NULL,            -- §API A schema; the determinism anchor
  state             TEXT NOT NULL DEFAULT 'QUEUED'
    CHECK (state IN ('QUEUED','ASSEMBLING','COMPLETED','FAILED')),
  manifest          JSONB,                     -- members [{path, sha256, bytes}] once COMPLETED
  dossier_hash      TEXT,                      -- sha256(canonical_json(manifest.members))
  zip_pointer       TEXT,                      -- object-store key; 12-yr retention (it IS inspection evidence)
  fail_reason       TEXT,
  requested_by      TEXT NOT NULL,             -- operator_id
  created_at        TIMESTAMPTZ NOT NULL,
  completed_at      TIMESTAMPTZ,
  schema_version    SMALLINT NOT NULL DEFAULT 1
);
CREATE INDEX idx_dxp_state ON dossier_exports (state, created_at DESC);
