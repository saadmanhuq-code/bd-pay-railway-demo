-- Gateway customer profile bridge.
--
-- Canonical identity/KYC rows live in customers + kyc_records. The gateway
-- customer read surface also needs merchant ownership and balance display data
-- for authorization and public API responses, so this table holds the gateway
-- profile keyed by customer_id.

CREATE TABLE IF NOT EXISTS customer_profiles (
    customer_id TEXT PRIMARY KEY REFERENCES customers(customer_id),
    merchant_id TEXT,
    status TEXT NOT NULL,
    kyc_record_id TEXT REFERENCES kyc_records(kyc_record_id),
    balance_minor BIGINT NOT NULL DEFAULT 0,
    currency TEXT NOT NULL DEFAULT 'BDT',
    balance_cap_minor BIGINT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    schema_version SMALLINT NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_customer_profiles_merchant
    ON customer_profiles (merchant_id);
