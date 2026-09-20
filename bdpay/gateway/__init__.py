"""BD-PAY gateway — the single inbound HTTP edge (spec/01).

Owns authentication (merchant API key bearer + HMAC-signed, customer JWT,
operator token + TOTP), refusal-first authorization, the idempotency
middleware, rate limiting, PII-safe request/response logging, the binding
spec/00 §4 error envelope, and the /v1 route surface. All business logic is
reached through ports (``bdpay.gateway.ports`` + ``bdpay.platform.interfaces``)
— the gateway never imports kernel/ledger/compliance modules directly.
"""

from bdpay.gateway.app import GatewayDependencies, create_app

__all__ = ["GatewayDependencies", "create_app"]
