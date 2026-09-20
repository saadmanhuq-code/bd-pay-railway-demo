-- 0150_gateway_subscription_scopes.sql — additive subscription API scopes.

ALTER TYPE api_scope ADD VALUE IF NOT EXISTS 'subscriptions:write';
ALTER TYPE api_scope ADD VALUE IF NOT EXISTS 'subscriptions:read';
