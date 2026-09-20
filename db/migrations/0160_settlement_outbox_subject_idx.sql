-- Settlement event reconciliation queries outbox rows by event type + subject.
-- Without this index the scheduler's resume_confirmations() scan becomes an
-- unbounded sequential read as the outbox grows.
CREATE INDEX IF NOT EXISTS outbox_event_type_subject_idx
    ON ledger.outbox (event_type, subject_id);
