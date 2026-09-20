-- 0015_bb_calendar.sql — Bangladesh Bank working-day calendar (spec/04).
-- Seeded/updated via the ops-console, never hardcoded into scheduling logic;
-- the platform BangladeshBankCalendar takes the holiday set as injectable
-- config and ops reconcile this table against the BB gazetted circular.

CREATE TABLE IF NOT EXISTS ledger.bb_calendar (
    calendar_date       DATE PRIMARY KEY,
    is_working_day      BOOLEAN NOT NULL,
    day_type            TEXT NOT NULL,
    holiday_name        TEXT,
    beftn_session_count SMALLINT NOT NULL DEFAULT 0,
    notes               TEXT,
    updated_at          TIMESTAMPTZ NOT NULL,
    updated_by          TEXT NOT NULL,

    CONSTRAINT bbcal_day_type_check CHECK (
        day_type IN ('WORKING','WEEKEND','PUBLIC_HOLIDAY','BB_CLOSURE')
    ),
    CONSTRAINT bbcal_session_count_check CHECK (
        beftn_session_count >= 0 AND beftn_session_count <= 3
    )
);

CREATE INDEX IF NOT EXISTS bb_calendar_working_idx
    ON ledger.bb_calendar (calendar_date, is_working_day);

GRANT SELECT, INSERT, UPDATE ON ledger.bb_calendar TO ledger_app_role;
GRANT SELECT ON ledger.bb_calendar TO ledger_ro;
