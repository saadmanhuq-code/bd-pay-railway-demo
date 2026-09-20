-- Durable subject-control state for compliance freezes / halts.
--
-- Compliance FSMs apply and release these controls. Kernel preflight reads the
-- active rows before allowing money movement.

CREATE TABLE IF NOT EXISTS subject_controls (
    subject_type TEXT NOT NULL CHECK (subject_type <> ''),
    subject_id TEXT NOT NULL CHECK (subject_id <> ''),
    control_type TEXT NOT NULL CHECK (
        control_type IN ('freeze_subject', 'halt_onboarding', 'freeze_payouts')
    ),
    reason TEXT NOT NULL CHECK (reason <> ''),
    reference_id TEXT NOT NULL CHECK (reference_id <> ''),
    applied_at TIMESTAMPTZ NOT NULL,
    released_at TIMESTAMPTZ,
    release_reason TEXT,
    release_reference_id TEXT,
    schema_version SMALLINT NOT NULL DEFAULT 1,
    PRIMARY KEY (subject_type, subject_id, control_type),
    CHECK (released_at IS NULL OR released_at >= applied_at)
);

CREATE INDEX IF NOT EXISTS idx_subject_controls_active_subject
    ON subject_controls (subject_type, subject_id)
    WHERE released_at IS NULL;

CREATE TABLE IF NOT EXISTS subject_control_events (
    subject_type TEXT NOT NULL CHECK (subject_type <> ''),
    subject_id TEXT NOT NULL CHECK (subject_id <> ''),
    control_type TEXT NOT NULL CHECK (
        control_type IN ('freeze_subject', 'halt_onboarding', 'freeze_payouts')
    ),
    action TEXT NOT NULL CHECK (action IN ('APPLIED', 'RELEASED')),
    reason TEXT NOT NULL CHECK (reason <> ''),
    reference_id TEXT NOT NULL CHECK (reference_id <> ''),
    occurred_at TIMESTAMPTZ NOT NULL,
    schema_version SMALLINT NOT NULL DEFAULT 1,
    PRIMARY KEY (
        subject_type,
        subject_id,
        control_type,
        action,
        reference_id,
        occurred_at
    )
);

CREATE INDEX IF NOT EXISTS idx_subject_control_events_subject
    ON subject_control_events (subject_type, subject_id, occurred_at);
