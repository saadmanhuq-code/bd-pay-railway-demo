-- 0100_participant_onboarding_v2.sql — spec/19 PSO-1 participant onboarding v2.
--
-- Additive amendments to the spec/08-owned participants table (D-19-1 states +
-- spec/19 columns) plus the two new PSO-1 tables this spec owns:
-- participant_documents and participant_mous. Migration number per spec/19
-- D-19-5: the directed 0090-0094 range was already occupied by spec/16 lanes,
-- so spec/19 claims 0100-0104 (errata P19-1).
--
-- Conventions: spec/00 §3 content-addressed IDs (E18 shim until prefix
-- fold-in), §7 TIMESTAMPTZ UTC from the injected clock (never NOW()).

-- ===========================================================================
-- participants amendments (owning spec 08 stays authoritative; D-19-1)
-- ===========================================================================
ALTER TABLE participants DROP CONSTRAINT participants_kyb_status_check;
ALTER TABLE participants ADD CONSTRAINT participants_kyb_status_check CHECK (kyb_status IN (
  'APPLICATION_SUBMITTED',
  'DOCUMENT_VERIFICATION_PENDING',                -- new (D-19-1)
  'LICENSE_VERIFICATION_PENDING',                 -- legacy, retained
  'SETTLEMENT_AGREEMENT_PENDING',                 -- legacy, retained
  'MOU_SIGNED', 'CONFORMANCE_TESTING', 'CONFORMANCE_PASSED',  -- new
  'NDC_ASSIGNMENT_PENDING',
  'ACTIVE', 'SUSPENDED', 'REJECTED', 'TERMINATED'
));
ALTER TABLE participants ADD COLUMN mou_id TEXT;                       -- pmou_<...>
ALTER TABLE participants ADD COLUMN conformance_passed_at TIMESTAMPTZ;
ALTER TABLE participants ADD COLUMN suspended_reason TEXT;

-- ===========================================================================
-- participant_documents (spec/19 §Data model, verbatim DDL)
-- ===========================================================================
CREATE TABLE participant_documents (
  document_id        TEXT PRIMARY KEY,            -- pdoc_<sha256_canonical({participant_id, document_class, content_sha256})[:24]>
  participant_id     TEXT NOT NULL REFERENCES participants(participant_id),
  document_class     TEXT NOT NULL CHECK (document_class IN
    ('BB_LICENSE','BOARD_RESOLUTION','SETTLEMENT_ACCOUNT_DETAILS','TECHNICAL_CONTACT','COMPLIANCE_CONTACT')),
  storage_pointer    TEXT NOT NULL,               -- object store; PII inside the object only, never in this row
  content_sha256     TEXT NOT NULL,
  metadata           JSONB NOT NULL DEFAULT '{}'::jsonb,  -- PII-screened at intake (reject, not redact)
  review_status      TEXT NOT NULL DEFAULT 'PENDING'
    CHECK (review_status IN ('PENDING','VERIFIED','REJECTED','SUPERSEDED')),
  reject_reason      TEXT,
  uploaded_by        TEXT NOT NULL,
  verified_by        TEXT,
  uploaded_at        TIMESTAMPTZ NOT NULL,
  verified_at        TIMESTAMPTZ,
  schema_version     SMALLINT NOT NULL DEFAULT 1,
  CONSTRAINT pdoc_verifier_differs CHECK (verified_by IS NULL OR verified_by <> uploaded_by),
  CONSTRAINT pdoc_verified_complete CHECK (
    review_status <> 'VERIFIED' OR (verified_by IS NOT NULL AND verified_at IS NOT NULL))
);
-- one live (non-superseded) document per class per participant:
CREATE UNIQUE INDEX uidx_pdoc_live_class ON participant_documents (participant_id, document_class)
  WHERE review_status IN ('PENDING','VERIFIED');
CREATE INDEX idx_pdoc_participant ON participant_documents (participant_id, uploaded_at DESC);

-- ===========================================================================
-- participant_mous (spec/19 §Data model, verbatim DDL + immutability trigger)
-- ===========================================================================
CREATE TABLE participant_mous (
  mou_id                    TEXT PRIMARY KEY,     -- pmou_<sha256_canonical({participant_id, mou_template_version, executed_date})[:24]>
  participant_id            TEXT NOT NULL REFERENCES participants(participant_id),
  mou_template_version      TEXT NOT NULL,        -- e.g. 'founding-v1' (tracked artifact, spec/19 §PSO-5)
  executed_date             DATE NOT NULL,
  signed_document_pointer   TEXT NOT NULL,
  signed_document_sha256    TEXT NOT NULL,
  counterparty_signatory    JSONB NOT NULL,       -- {name, title}; PII-screened
  -- The three founding flags are structurally immutable TRUE in v1 (the MOU IS
  -- non-binding, contingent, zero-capital by definition; a binding instrument is
  -- a different document class recorded at activation, not a flag flip):
  non_binding               BOOLEAN NOT NULL DEFAULT TRUE  CHECK (non_binding),
  contingent_on_license     BOOLEAN NOT NULL DEFAULT TRUE  CHECK (contingent_on_license),
  zero_capital_commitment   BOOLEAN NOT NULL DEFAULT TRUE  CHECK (zero_capital_commitment),
  founding_fee_terms_ref    TEXT,                 -- placeholder; NULL until principal sets terms (spec/19 Open Question 2)
  recorded_by               TEXT NOT NULL,
  recorded_at               TIMESTAMPTZ NOT NULL,
  schema_version            SMALLINT NOT NULL DEFAULT 1,
  UNIQUE (participant_id, mou_template_version)
);

-- The MOU record is the founding instrument: append-only at the database
-- layer (the spec/19 data-model preamble denies the app role UPDATE/DELETE on
-- append-only tables; kyb_documents immutability-trigger precedent).
CREATE OR REPLACE FUNCTION participant_mous_immutable() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'participant_mous rows are immutable (founding instrument; append-only)';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER participant_mous_immutable_trigger
    BEFORE UPDATE OR DELETE ON participant_mous
    FOR EACH ROW EXECUTE FUNCTION participant_mous_immutable();
