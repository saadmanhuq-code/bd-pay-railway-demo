-- 0031_customers.sql — canonical customers table (spec/00 §2; errata K-14).
--
-- spec/09 references customers.customer_id ("owned by the onboarding
-- service") but no spec ships the DDL. Authored minimal: identity fields the
-- specs read (spec/09 name-change detection compares full_name_en /
-- full_name_bn) plus the corporate flag the NPSB limit classes need
-- (spec/02 LimitEnforcer individual vs corporate caps). No raw NID, phone,
-- or email column exists here — identity data lives hashed/encrypted in
-- kyc_records.

CREATE TABLE customers (
    customer_id    TEXT        PRIMARY KEY,   -- cust_<sha256[:24]>
    full_name_en   TEXT        NOT NULL,
    full_name_bn   TEXT,                      -- NFC-normalized Bangla name
    is_corporate   BOOLEAN     NOT NULL DEFAULT FALSE,
    created_at     TIMESTAMPTZ NOT NULL,
    updated_at     TIMESTAMPTZ NOT NULL,
    schema_version SMALLINT    NOT NULL DEFAULT 1
);
