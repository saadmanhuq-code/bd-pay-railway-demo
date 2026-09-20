-- 0036_kyb_documents.sql — spec/08 DDL §Section 2 (DOCUMENT INTAKE),
-- including the immutability trigger on content_hash/storage_pointer.

CREATE TYPE document_review_status_enum AS ENUM (
    'PENDING',
    'OCR_COMPLETE',
    'ACCEPTED',
    'REJECTED',
    'SUPERSEDED'
);

CREATE TYPE ocr_status_enum AS ENUM ('PENDING', 'IN_PROGRESS', 'COMPLETE', 'FAILED', 'SKIPPED');

CREATE TABLE kyb_documents (
    document_id              TEXT PRIMARY KEY,
    -- make_id('doc', {kyb_record_id, document_type, content_hash})

    kyb_record_id            TEXT NOT NULL REFERENCES kyb_records (kyb_record_id),
    document_type            TEXT NOT NULL,

    content_hash             TEXT NOT NULL,      -- server-computed sha256; client hash untrusted
    storage_pointer          TEXT NOT NULL,      -- write-once object-store pointer

    file_size_bytes          INTEGER,
    mime_type                TEXT,
    original_filename_hash   TEXT,

    ocr_status               ocr_status_enum NOT NULL DEFAULT 'PENDING',
    ocr_extracted_fields     JSONB,
    ocr_raw_hash             TEXT,
    ocr_attempted_at         TIMESTAMPTZ,
    ocr_completed_at         TIMESTAMPTZ,

    review_status            document_review_status_enum NOT NULL DEFAULT 'PENDING',
    reviewed_by              TEXT,
    reviewed_at              TIMESTAMPTZ,
    review_notes             TEXT,

    ubo_node_id              TEXT,

    uploaded_at              TIMESTAMPTZ NOT NULL,
    uploaded_by              TEXT NOT NULL,
    schema_version           SMALLINT NOT NULL DEFAULT 1,

    CONSTRAINT kyb_docs_content_hash_type_unique
        UNIQUE (kyb_record_id, document_type, content_hash)
);

CREATE INDEX idx_kyb_docs_record      ON kyb_documents (kyb_record_id);
CREATE INDEX idx_kyb_docs_type_status ON kyb_documents (kyb_record_id, document_type,
                                                        review_status);
CREATE INDEX idx_kyb_docs_ubo         ON kyb_documents (ubo_node_id)
    WHERE ubo_node_id IS NOT NULL;

-- Integrity guard: content_hash and storage_pointer are immutable after write.
CREATE OR REPLACE FUNCTION kyb_documents_immutable_cols()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.content_hash != NEW.content_hash OR OLD.storage_pointer != NEW.storage_pointer THEN
        RAISE EXCEPTION 'kyb_documents: content_hash and storage_pointer are immutable';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER kyb_documents_immutable_trigger
    BEFORE UPDATE ON kyb_documents
    FOR EACH ROW EXECUTE FUNCTION kyb_documents_immutable_cols();
