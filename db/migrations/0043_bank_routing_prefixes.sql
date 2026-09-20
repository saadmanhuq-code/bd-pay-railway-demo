-- 0043_bank_routing_prefixes.sql — spec/08 DDL §Section 8 (static reference
-- data; seeded from BB-published routing codes by a follow-up data
-- migration once the operator supplies the gazetted list — the spec marks
-- the seed itself as a Sprint-0 operations item, not engineering data).

CREATE TABLE bank_routing_prefixes (
    routing_prefix CHAR(3)     PRIMARY KEY,   -- first 3 digits of the 9-digit routing number
    bank_name      TEXT        NOT NULL,
    bank_code      TEXT        NOT NULL,
    is_active      BOOLEAN     NOT NULL DEFAULT TRUE,
    created_at     TIMESTAMPTZ NOT NULL
);
