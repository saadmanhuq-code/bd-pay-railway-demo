-- 0048_kyc_limits.sql — spec/09 DDL kyc_monthly_totals + kyc_limit_configs
-- with the regulatory seed values (all integer paisa; BDT 1 = 100 paisa).
--
-- Seed config_ids are deterministic make_id('kyccfg', {tier, payment_type,
-- limit_scope, effective_from}) values precomputed with
-- bdpay.kernel.ids_ext (the spec's inline make_id(...) pseudo-calls cannot
-- execute in SQL; pinned by tests/kernel/test_migrations_kernel.py).
-- Timestamps are fixed literals, never NOW() (spec/00 §7).

CREATE TABLE kyc_monthly_totals (
    customer_id        TEXT        NOT NULL,
    year_month         CHAR(7)     NOT NULL,    -- 'YYYY-MM', UTC month boundary
    payment_type       TEXT        NOT NULL
        CHECK (payment_type IN ('CASH_IN', 'CASH_OUT', 'FUND_TRANSFER',
                                'ELECTRONIC_PAYMENT')),
    total_amount_minor BIGINT      NOT NULL DEFAULT 0 CHECK (total_amount_minor >= 0),
    transaction_count  INTEGER     NOT NULL DEFAULT 0 CHECK (transaction_count >= 0),
    last_updated_at    TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (customer_id, year_month, payment_type)
);

CREATE TABLE kyc_limit_configs (
    config_id              TEXT        PRIMARY KEY,
    tier                   TEXT        NOT NULL
        CHECK (tier IN ('SIMPLIFIED', 'REGULAR', 'SUSPENDED')),
    payment_type           TEXT        NOT NULL,
    limit_scope            TEXT        NOT NULL
        CHECK (limit_scope IN ('PER_TRANSACTION', 'MONTHLY', 'BALANCE')),
    limit_amount_minor     BIGINT      NOT NULL CHECK (limit_amount_minor >= 0),
    limit_amount_no_cap    BOOLEAN     NOT NULL DEFAULT FALSE,
    effective_from         TIMESTAMPTZ NOT NULL,
    effective_to           TIMESTAMPTZ,
    created_by_operator_id TEXT        NOT NULL,
    created_at             TIMESTAMPTZ NOT NULL,
    notes                  TEXT,
    schema_version         SMALLINT    NOT NULL DEFAULT 1
);

INSERT INTO kyc_limit_configs
    (config_id, tier, payment_type, limit_scope, limit_amount_minor,
     limit_amount_no_cap, effective_from, effective_to, created_by_operator_id,
     created_at, notes, schema_version)
VALUES
    -- SIMPLIFIED tier (BFIU Sep-2026 framework, research 03 s2.2 / s7.1)
    ('kyccfg_00d3a0af0ed8ff91ea30ea4a', 'SIMPLIFIED', 'CASH_IN', 'PER_TRANSACTION',
     10000000, FALSE, '2026-09-01T00:00:00Z', NULL, 'system',
     '2026-06-12T00:00:00Z', 'BB eKYC Sep-2026 s2.2', 1),
    ('kyccfg_b73c191aaed50445a2a6e9c9', 'SIMPLIFIED', 'CASH_IN', 'MONTHLY',
     30000000, FALSE, '2026-09-01T00:00:00Z', NULL, 'system',
     '2026-06-12T00:00:00Z', 'BB eKYC Sep-2026 s2.2', 1),
    ('kyccfg_b92b6ee39559ba2f3e4193e4', 'SIMPLIFIED', 'FUND_TRANSFER', 'PER_TRANSACTION',
     25000000, FALSE, '2026-09-01T00:00:00Z', NULL, 'system',
     '2026-06-12T00:00:00Z', 'BB eKYC Sep-2026 s2.2', 1),
    ('kyccfg_8a2b46d5458823862bc05217', 'SIMPLIFIED', 'FUND_TRANSFER', 'MONTHLY',
     50000000, FALSE, '2026-09-01T00:00:00Z', NULL, 'system',
     '2026-06-12T00:00:00Z', 'BB eKYC Sep-2026 s2.2', 1),
    ('kyccfg_0358846b9c02c9720254ab6c', 'SIMPLIFIED', 'ELECTRONIC_PAYMENT', 'PER_TRANSACTION',
     10000000, FALSE, '2026-09-01T00:00:00Z', NULL, 'system',
     '2026-06-12T00:00:00Z', 'BB eKYC Sep-2026 s2.2', 1),
    ('kyccfg_5b51a40e1ddc77bc9c696a33', 'SIMPLIFIED', 'ELECTRONIC_PAYMENT', 'MONTHLY',
     30000000, FALSE, '2026-09-01T00:00:00Z', NULL, 'system',
     '2026-06-12T00:00:00Z', 'BB eKYC Sep-2026 s2.2', 1),
    ('kyccfg_4c5105204bec40f48393cfec', 'SIMPLIFIED', 'BALANCE_CAP', 'BALANCE',
     40000000, FALSE, '2026-09-01T00:00:00Z', NULL, 'system',
     '2026-06-12T00:00:00Z', 'BB PSP e-wallet cap, research 03 s7.1', 1),
    -- REGULAR tier: balance cap only; published per-txn/monthly caps absent
    ('kyccfg_71076a1af4ad97d4107dc5c2', 'REGULAR', 'BALANCE_CAP', 'BALANCE',
     40000000, FALSE, '2026-09-01T00:00:00Z', NULL, 'system',
     '2026-06-12T00:00:00Z', 'BB PSP e-wallet cap, research 03 s7.1', 1),
    ('kyccfg_19763b27f70163f532bfb224', 'REGULAR', 'FUND_TRANSFER', 'PER_TRANSACTION',
     0, TRUE, '2026-09-01T00:00:00Z', NULL, 'system',
     '2026-06-12T00:00:00Z', 'No published per-txn cap; AML monitor governs', 1),
    ('kyccfg_e8630c0a411521f96ddaeb08', 'REGULAR', 'FUND_TRANSFER', 'MONTHLY',
     0, TRUE, '2026-09-01T00:00:00Z', NULL, 'system',
     '2026-06-12T00:00:00Z', 'No published monthly cap; AML monitor governs', 1)
ON CONFLICT (config_id) DO NOTHING;
