-- 0040_kyb_refresh_schedule.sql — spec/08 DDL §Section 6 (KYB REFRESH SCHEDULE).

CREATE TABLE kyb_refresh_schedule (
    refresh_id              TEXT PRIMARY KEY,
    -- make_id('kybr', {kyb_record_id, due_at})

    kyb_record_id           TEXT NOT NULL REFERENCES kyb_records (kyb_record_id),
    merchant_id             TEXT NOT NULL,

    due_at                  TIMESTAMPTZ NOT NULL,
    refresh_interval_months SMALLINT NOT NULL,
    risk_tier_at_schedule   risk_tier_enum NOT NULL,

    status                  TEXT NOT NULL DEFAULT 'SCHEDULED'
        CHECK (status IN ('SCHEDULED', 'NOTIFIED', 'IN_PROGRESS', 'COMPLETE', 'OVERDUE')),

    notified_at             TIMESTAMPTZ,
    started_at              TIMESTAMPTZ,
    completed_at            TIMESTAMPTZ,

    created_at              TIMESTAMPTZ NOT NULL,
    schema_version          SMALLINT NOT NULL DEFAULT 1
);

CREATE INDEX idx_kyb_refresh_due ON kyb_refresh_schedule (due_at)
    WHERE status IN ('SCHEDULED', 'NOTIFIED');
