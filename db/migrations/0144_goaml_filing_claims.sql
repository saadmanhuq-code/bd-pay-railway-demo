-- Durable goAML submission claims for HA app topology.
--
-- The production compose starts more than one app process. A plain due-row
-- SELECT lets both schedulers submit the same filing before either advances
-- state. SUBMITTING is a short lease state claimed with FOR UPDATE SKIP LOCKED
-- before the external goAML POST.

ALTER TABLE goaml_filings
    DROP CONSTRAINT IF EXISTS goaml_filings_state_check;

ALTER TABLE goaml_filings
    ADD CONSTRAINT goaml_filings_state_check
    CHECK (
        state IN (
            'QUEUED',
            'SUBMITTING',
            'SUBMITTED',
            'ACK_PENDING',
            'ACKED',
            'REJECTED_BY_PORTAL',
            'MANUAL_ESCALATED'
        )
    );

CREATE INDEX IF NOT EXISTS idx_gfil_claim_due
    ON goaml_filings (state, next_retry_at, seq)
    WHERE state IN ('QUEUED', 'SUBMITTING');
