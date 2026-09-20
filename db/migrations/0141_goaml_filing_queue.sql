-- Durable goAML filing queue insertion-order sequence.
--
-- The goaml_filings table is created by migration 0052. The in-memory queue
-- preserves enqueue order with a list; this sequence gives the Postgres store
-- the same deterministic ordering for rows() and latest_for().

ALTER TABLE goaml_filings
    ADD COLUMN IF NOT EXISTS seq BIGSERIAL;

CREATE INDEX IF NOT EXISTS idx_gfil_seq
    ON goaml_filings (seq);

CREATE INDEX IF NOT EXISTS idx_gfil_report_ref_seq
    ON goaml_filings (report_ref, seq DESC);
