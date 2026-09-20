-- 0149_gateway_value_extension_scopes.sql — additive gateway API scopes.
--
-- The Python API scope catalogue already exposes QR (spec/13) and offers
-- (spec/18). Postgres api_key_scopes is backed by the closed api_scope enum,
-- so PG-mode key issuance must carry the same additive values.

ALTER TYPE api_scope ADD VALUE IF NOT EXISTS 'qr:write';
ALTER TYPE api_scope ADD VALUE IF NOT EXISTS 'qr:read';
ALTER TYPE api_scope ADD VALUE IF NOT EXISTS 'offers:write';
ALTER TYPE api_scope ADD VALUE IF NOT EXISTS 'offers:read';
