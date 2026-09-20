"""Route authorization policy — the spec/01 RBAC matrix as data.

Refusal-first (spec/01 §Authorization model): any (credential kind, resource,
method) combination not present in this table is DENIED with
``403 authorization / permission_denied``. There is no default-allow path.

Per entry:

- ``merchant_scope`` — scope a merchant API key needs (``"*"`` = any valid
  key, e.g. "own merchant record" reads); ``None`` = merchant keys denied.
- ``customer_scopes`` — any-of JWT scopes; ``None`` = customer tokens denied.
- ``operator_roles`` — any-of roles (``SUPER_ADMIN`` always passes);
  ``None`` = operator sessions denied.
- ``public`` — unauthenticated callers allowed (IP rate limit still applies;
  if credentials ARE presented they are fully verified and authorized).
- ``idempotency_required`` — mutating routes that demand ``Idempotency-Key``.
  Token-issuing and inbound-webhook POSTs are exempt (errata G-6).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

__all__ = ["RoutePolicy", "build_policies", "match_policy"]


@dataclass(frozen=True)
class RoutePolicy:
    name: str
    method: str
    pattern: re.Pattern[str] = field(repr=False)
    route_group: str = "payment_read"
    public: bool = False
    merchant_scope: str | None = None
    customer_scopes: tuple[str, ...] | None = None
    operator_roles: tuple[str, ...] | None = None
    idempotency_required: bool = False
    idempotent_redact_fields: tuple[str, ...] = ()
    ip_limit_per_minute: int | None = None  # None -> RateLimiter default for the bucket
    skip_auth: bool = False  # route consumes its own credentials (TOTP step)


def _p(path_template: str) -> re.Pattern[str]:
    """Compile a path template; ``{name}`` segments match one path segment."""
    pattern = re.sub(r"\{[a-z_]+\}", r"[^/]+", path_template)
    return re.compile(f"^{pattern}$")


_READ_ROLES = ("PAYMENT_OPS", "FINANCE", "READ_ONLY", "BB_INSPECTOR_READ_ONLY")


def build_policies() -> tuple[RoutePolicy, ...]:
    """The gateway's full route surface (spec/01 §API surface subset built here)."""
    return (
        # -- payment intents ------------------------------------------------
        RoutePolicy(
            name="payment_intents.create",
            method="POST",
            pattern=_p("/v1/payment-intents"),
            route_group="payment_write",
            merchant_scope="payment:write",
            idempotency_required=True,
        ),
        RoutePolicy(
            name="payment_intents.list",
            method="GET",
            pattern=_p("/v1/payment-intents"),
            route_group="payment_read",
            merchant_scope="payment:read",
            customer_scopes=("payment:read",),
            operator_roles=_READ_ROLES,
        ),
        RoutePolicy(
            name="payment_intents.get",
            method="GET",
            pattern=_p("/v1/payment-intents/{payment_intent_id}"),
            route_group="payment_read",
            merchant_scope="payment:read",
            customer_scopes=("payment:read",),
            operator_roles=_READ_ROLES,
        ),
        RoutePolicy(
            name="payment_intents.confirm",
            method="POST",
            pattern=_p("/v1/payment-intents/{payment_intent_id}/confirm"),
            route_group="payment_write",
            merchant_scope="payment:write",
            customer_scopes=("payment:write",),
            idempotency_required=True,
        ),
        RoutePolicy(
            name="payment_intents.capture",
            method="POST",
            pattern=_p("/v1/payment-intents/{payment_intent_id}/capture"),
            route_group="payment_write",
            merchant_scope="payment:write",
            operator_roles=("PAYMENT_OPS",),
            idempotency_required=True,
        ),
        RoutePolicy(
            name="payment_intents.cancel",
            method="POST",
            pattern=_p("/v1/payment-intents/{payment_intent_id}/cancel"),
            route_group="payment_write",
            merchant_scope="payment:write",
            operator_roles=("PAYMENT_OPS",),
            idempotency_required=True,
        ),
        RoutePolicy(
            name="payment_intents.attempts",
            method="GET",
            pattern=_p("/v1/payment-intents/{payment_intent_id}/attempts"),
            route_group="payment_read",
            merchant_scope="payment:read",
            operator_roles=_READ_ROLES,
        ),
        # -- refunds ----------------------------------------------------------
        RoutePolicy(
            name="refunds.create",
            method="POST",
            pattern=_p("/v1/refunds"),
            route_group="refund_write",
            merchant_scope="refund:write",
            operator_roles=("PAYMENT_OPS",),
            idempotency_required=True,
        ),
        RoutePolicy(
            name="refunds.list",
            method="GET",
            pattern=_p("/v1/refunds"),
            route_group="payment_read",
            merchant_scope="refund:read",
            operator_roles=_READ_ROLES,
        ),
        RoutePolicy(
            name="refunds.get",
            method="GET",
            pattern=_p("/v1/refunds/{refund_id}"),
            route_group="payment_read",
            merchant_scope="refund:read",
            operator_roles=_READ_ROLES,
        ),
        # -- merchants --------------------------------------------------------
        RoutePolicy(
            name="merchants.create",
            method="POST",
            pattern=_p("/v1/merchants"),
            route_group="admin",
            operator_roles=("MERCHANT_ADMIN",),
            idempotency_required=True,
        ),
        RoutePolicy(
            name="merchants.list",
            method="GET",
            pattern=_p("/v1/merchants"),
            route_group="admin",
            operator_roles=("MERCHANT_ADMIN",),
        ),
        RoutePolicy(
            name="merchants.get",
            method="GET",
            pattern=_p("/v1/merchants/{merchant_id}"),
            route_group="payment_read",
            merchant_scope="*",  # own record only; ownership enforced in the route
            operator_roles=("MERCHANT_ADMIN", "PAYMENT_OPS", "READ_ONLY"),
        ),
        RoutePolicy(
            name="diner.merchants.list",
            method="GET",
            pattern=_p("/v1/diner/merchants"),
            route_group="payment_read",
            customer_scopes=("payment:read",),
        ),
        # -- customers / eKYC ---------------------------------------------------
        RoutePolicy(
            name="customers.create",
            method="POST",
            pattern=_p("/v1/customers"),
            route_group="payment_write",
            public=True,  # public onboarding, IP rate-limited (spec/01 §G)
            merchant_scope="customer:write",
            idempotency_required=True,
        ),
        RoutePolicy(
            name="customers.get",
            method="GET",
            pattern=_p("/v1/customers/{customer_id}"),
            route_group="payment_read",
            merchant_scope="customer:read",
            customer_scopes=("customer:read",),
            operator_roles=("KYC_REVIEWER", "READ_ONLY"),
        ),
        RoutePolicy(
            name="customers.balance",
            method="GET",
            pattern=_p("/v1/customers/{customer_id}/balance"),
            route_group="payment_read",
            merchant_scope="customer:read",
            customer_scopes=("wallet:read",),
        ),
        # -- payment links (spec/16 LR-4 §C — merchant surface) ------------------
        RoutePolicy(
            name="payment_links.create",
            method="POST",
            pattern=_p("/v1/payment-links"),
            route_group="payment_write",
            merchant_scope="payment:write",
            idempotency_required=True,
        ),
        RoutePolicy(
            name="payment_links.list",
            method="GET",
            pattern=_p("/v1/payment-links"),
            route_group="payment_read",
            merchant_scope="payment:read",
        ),
        RoutePolicy(
            name="payment_links.get",
            method="GET",
            pattern=_p("/v1/payment-links/{plink_id}"),
            route_group="payment_read",
            merchant_scope="payment:read",
        ),
        RoutePolicy(
            name="payment_links.cancel",
            method="POST",
            pattern=_p("/v1/payment-links/{plink_id}/cancel"),
            route_group="payment_write",
            merchant_scope="payment:write",
            operator_roles=("PAYMENT_OPS",),  # FSM cancel trigger: merchant OR operator
            idempotency_required=True,
        ),
        # -- payment links (spec/16 LR-4 §C — public checkout, rate-limited) -----
        RoutePolicy(
            name="public.payment_links.get",
            method="GET",
            pattern=_p("/v1/public/payment-links/{public_code}"),
            route_group="public",
            public=True,
            ip_limit_per_minute=60,
            skip_auth=True,
        ),
        RoutePolicy(
            name="public.payment_links.create_intent",
            method="POST",
            pattern=_p("/v1/public/payment-links/{public_code}/intents"),
            route_group="public",
            public=True,
            # server-generated idempotency from public_code + payer inputs
            # (spec/16 §C) — no Idempotency-Key header demanded of payers.
            ip_limit_per_minute=20,
            skip_auth=True,
        ),
        # -- public surfaces (spec/16 LR-4 §E — unauthenticated, cached reads) ---
        RoutePolicy(
            name="public.status",
            method="GET",
            pattern=_p("/v1/public/status"),
            route_group="public",
            public=True,
            ip_limit_per_minute=120,
            skip_auth=True,
        ),
        RoutePolicy(
            name="public.certification_matrix",
            method="GET",
            pattern=_p("/v1/public/certification-matrix"),
            route_group="public",
            public=True,
            ip_limit_per_minute=120,
            skip_auth=True,
        ),
        RoutePolicy(
            name="public.badge",
            method="GET",
            pattern=_p("/v1/public/badges/{connector_id}.svg"),
            route_group="public",
            public=True,
            ip_limit_per_minute=120,
            skip_auth=True,
        ),
        # -- sandbox signup (spec/16 LR-4 §F — SANDBOX_PUBLIC deployments only;
        #    IP-hash buckets per the spec/01 BDA pattern: limiter default) ------
        RoutePolicy(
            name="sandbox.signups.create",
            method="POST",
            pattern=_p("/v1/sandbox/signups"),
            route_group="public",
            public=True,
            skip_auth=True,
        ),
        RoutePolicy(
            name="sandbox.signups.verify",
            method="POST",
            pattern=_p("/v1/sandbox/signups/{sbxs_id}/verify"),
            route_group="public",
            public=True,
            skip_auth=True,
        ),
        RoutePolicy(
            name="sandbox.signups.get",
            method="GET",
            pattern=_p("/v1/sandbox/signups/{sbxs_id}"),
            route_group="public",
            public=True,
            skip_auth=True,
        ),
        RoutePolicy(
            name="sandbox.demo.fullflow_seed",
            method="POST",
            pattern=_p("/v1/sandbox/demo/fullflow-seed"),
            route_group="public",
            public=True,
            skip_auth=True,
        ),
        RoutePolicy(
            name="sandbox.demo.diner_pay",
            method="POST",
            pattern=_p("/v1/sandbox/demo/diner-pay"),
            route_group="public",
            public=True,
            skip_auth=True,
            idempotency_required=True,
            ip_limit_per_minute=20,
        ),
        RoutePolicy(
            name="sandbox.demo.fullflow",
            method="GET",
            pattern=_p("/v1/sandbox/demo/fullflow"),
            route_group="payment_read",
            merchant_scope="payment:read",
            customer_scopes=("payment:read",),
            operator_roles=_READ_ROLES,
        ),
        # -- operator console native read models ---------------------------------
        RoutePolicy(
            name="ops.dashboard.tcsa",
            method="GET",
            pattern=_p("/v1/dashboards/tcsa"),
            route_group="admin",
            operator_roles=_READ_ROLES,
        ),
        RoutePolicy(
            name="ops.dashboard.settlement",
            method="GET",
            pattern=_p("/v1/dashboards/settlement"),
            route_group="admin",
            operator_roles=_READ_ROLES,
        ),
        RoutePolicy(
            name="ops.dashboard.connectors",
            method="GET",
            pattern=_p("/v1/dashboards/connectors"),
            route_group="admin",
            operator_roles=_READ_ROLES,
        ),
        RoutePolicy(
            name="ops.dashboard.aml",
            method="GET",
            pattern=_p("/v1/dashboards/aml"),
            route_group="admin",
            operator_roles=_READ_ROLES,
        ),
        RoutePolicy(
            name="ops.dashboard.sla",
            method="GET",
            pattern=_p("/v1/dashboards/sla"),
            route_group="admin",
            operator_roles=_READ_ROLES,
        ),
        RoutePolicy(
            name="ops.ledger.journal_entries",
            method="GET",
            pattern=_p("/v1/ledger/journal-entries"),
            route_group="admin",
            operator_roles=_READ_ROLES,
        ),
        RoutePolicy(
            name="ops.ledger.chain_entries",
            method="GET",
            pattern=_p("/v1/ledger/chain-entries"),
            route_group="admin",
            operator_roles=_READ_ROLES,
        ),
        RoutePolicy(
            name="ops.ledger.chain_verify_status",
            method="GET",
            pattern=_p("/v1/ledger/chain-verify-status"),
            route_group="admin",
            operator_roles=_READ_ROLES,
        ),
        # -- developer-portal merchant read surface (spec/15 §Developer portal
        #    — merchant API-key catalogue + merchant dashboard, read-only).
        #    Merchant-key only: the catalogue/dashboard are the merchant's own;
        #    operator/customer credentials are denied (no cross-merchant leak).
        RoutePolicy(
            name="api_keys.list",
            method="GET",
            pattern=_p("/v1/api-keys"),
            route_group="payment_read",
            merchant_scope="apikey:read",
        ),
        # -- developer-portal merchant management surface (spec/01 §B) ----------
        # Create / rotate / revoke the merchant's OWN API keys. Merchant-key
        # only (no operator_roles / customer_scopes): the policy table denies
        # operator + customer principals; the handlers additionally refuse any
        # non-merchant_key principal. ``apikey:write`` is required — a key
        # scoped only to apikey:read / payment:* is denied (403). Merchant
        # scoping (own merchant_id only) is enforced in the service layer.
        RoutePolicy(
            name="api_keys.create",
            method="POST",
            pattern=_p("/v1/api-keys"),
            route_group="payment_write",
            merchant_scope="apikey:write",
            idempotency_required=True,
            idempotent_redact_fields=("secret",),
        ),
        RoutePolicy(
            name="api_keys.rotate",
            method="POST",
            pattern=_p("/v1/api-keys/{api_key_id}/rotate"),
            route_group="payment_write",
            merchant_scope="apikey:write",
            idempotency_required=True,
            idempotent_redact_fields=("new_secret",),
        ),
        RoutePolicy(
            name="api_keys.revoke",
            method="POST",
            pattern=_p("/v1/api-keys/{api_key_id}/revoke"),
            route_group="payment_write",
            merchant_scope="apikey:write",
            idempotency_required=True,
        ),
        RoutePolicy(
            name="dashboards.merchant",
            method="GET",
            pattern=_p("/v1/dashboards/merchant"),
            route_group="payment_read",
            merchant_scope="payment:read",
        ),
        # -- Bangla QR (spec/13) — acquirer issuance + lifecycle ------------------
        RoutePolicy(
            name="qr.issue_static",
            method="POST",
            pattern=_p("/v1/merchants/{merchant_id}/qr-codes"),
            route_group="payment_write",
            merchant_scope="qr:write",
            idempotency_required=True,
        ),
        RoutePolicy(
            name="qr.list_for_merchant",
            method="GET",
            pattern=_p("/v1/merchants/{merchant_id}/qr-codes"),
            route_group="payment_read",
            merchant_scope="qr:read",
            operator_roles=_READ_ROLES,
        ),
        RoutePolicy(
            name="qr.issue_dynamic",
            method="POST",
            pattern=_p("/v1/qr-codes/dynamic"),
            route_group="payment_write",
            merchant_scope="qr:write",
            idempotency_required=True,
        ),
        # render routes BEFORE the metadata route: `{merchant_qr_id}` matches
        # one segment, so /render.png needs its own (more specific) entries.
        RoutePolicy(
            name="qr.render",
            method="GET",
            pattern=re.compile(
                r"^/v1/qr-codes/[^/]+/(render\.png|render\.svg|kit\.pdf)$"
            ),
            route_group="payment_read",
            merchant_scope="qr:read",
            operator_roles=_READ_ROLES,
        ),
        RoutePolicy(
            name="qr.get",
            method="GET",
            pattern=_p("/v1/qr-codes/{merchant_qr_id}"),
            route_group="payment_read",
            merchant_scope="qr:read",
            operator_roles=_READ_ROLES,
        ),
        # spec/13: lifecycle scope `qr:write` (merchant) OR `ops:write`
        # (operator) — operator side maps to PAYMENT_OPS per the E-S16-15
        # role-mapping precedent (closed role catalogue, no scope concept).
        RoutePolicy(
            name="qr.suspend",
            method="POST",
            pattern=_p("/v1/qr-codes/{merchant_qr_id}/suspend"),
            route_group="payment_write",
            merchant_scope="qr:write",
            operator_roles=("PAYMENT_OPS",),
            idempotency_required=True,
        ),
        RoutePolicy(
            name="qr.reactivate",
            method="POST",
            pattern=_p("/v1/qr-codes/{merchant_qr_id}/reactivate"),
            route_group="payment_write",
            merchant_scope="qr:write",
            operator_roles=("PAYMENT_OPS",),
            idempotency_required=True,
        ),
        RoutePolicy(
            name="qr.revoke",
            method="POST",
            pattern=_p("/v1/qr-codes/{merchant_qr_id}/revoke"),
            route_group="payment_write",
            merchant_scope="qr:write",
            operator_roles=("PAYMENT_OPS",),
            idempotency_required=True,
        ),
        # -- Bangla QR (spec/13) — issuer scan-to-pay (customer JWT) -------------
        RoutePolicy(
            name="qr.resolve",
            method="POST",
            pattern=_p("/v1/qr/resolve"),
            route_group="payment_write",
            customer_scopes=("payment:write",),
            # every scan is recorded append-only; re-resolves are safe and a
            # scanner app cannot be asked for an Idempotency-Key (G-6 class)
        ),
        RoutePolicy(
            name="qr.pay",
            method="POST",
            pattern=_p("/v1/qr/resolutions/{resolution_id}/pay"),
            route_group="payment_write",
            customer_scopes=("payment:write",),
            idempotency_required=True,
        ),
        # -- Bangla QR (spec/13) — operator surface -------------------------------
        RoutePolicy(
            name="qr.interop_tests.list",
            method="GET",
            pattern=_p("/v1/qr/interop-tests"),
            route_group="admin",
            operator_roles=_READ_ROLES,
        ),
        RoutePolicy(
            name="qr.interop_tests.run",
            method="POST",
            pattern=_p("/v1/qr/interop-tests/run"),
            route_group="admin",
            operator_roles=("PAYMENT_OPS",),  # spec qr:admin -> ops role mapping
            idempotency_required=True,
        ),
        RoutePolicy(
            name="qr.dashboard",
            method="GET",
            pattern=_p("/v1/qr/dashboard"),
            route_group="admin",
            operator_roles=_READ_ROLES,
        ),
        # -- outbound merchant webhooks (spec/01 §I subset + spec/16 LR-5 §D) ---
        RoutePolicy(
            name="webhook_endpoints.create",
            method="POST",
            pattern=_p("/v1/webhook-endpoints"),
            route_group="payment_write",
            merchant_scope="webhook:write",
            idempotency_required=True,
            idempotent_redact_fields=("signing_secret",),
        ),
        RoutePolicy(
            name="webhook_endpoints.list",
            method="GET",
            pattern=_p("/v1/webhook-endpoints"),
            route_group="payment_read",
            merchant_scope="webhook:read",
            operator_roles=_READ_ROLES,
        ),
        RoutePolicy(
            name="webhook_endpoints.rotate_secret",
            method="POST",
            pattern=_p("/v1/webhook-endpoints/{webhook_id}/rotate-secret"),
            route_group="payment_write",
            merchant_scope="webhook:write",
            idempotency_required=True,
            idempotent_redact_fields=("signing_secret",),
        ),
        RoutePolicy(
            name="webhook_deliveries.list",
            method="GET",
            pattern=_p("/v1/webhook-deliveries"),
            route_group="payment_read",
            merchant_scope="webhook:read",
            operator_roles=_READ_ROLES,
        ),
        # -- dossier exports (spec/16 §API A; operator-only) ---------------------
        # Scope mapping (errata E-S16-15): spec/16 names auth scopes
        # `compliance:export` / `compliance:read`; the built operator model is
        # the spec/01 closed role catalogue. compliance:export -> CAMLCO;
        # compliance:read -> CAMLCO + BB_INSPECTOR_READ_ONLY + READ_ONLY.
        RoutePolicy(
            name="dossier_exports.create",
            method="POST",
            pattern=_p("/v1/exports/dossiers"),
            route_group="admin",
            operator_roles=("CAMLCO",),
            idempotency_required=True,
        ),
        RoutePolicy(
            name="dossier_exports.list",
            method="GET",
            pattern=_p("/v1/exports/dossiers"),
            route_group="admin",
            operator_roles=("CAMLCO", "BB_INSPECTOR_READ_ONLY", "READ_ONLY"),
        ),
        RoutePolicy(
            name="dossier_exports.download",
            method="GET",
            pattern=_p("/v1/exports/dossiers/{dossier_export_id}/download"),
            route_group="admin",
            operator_roles=("CAMLCO",),
        ),
        RoutePolicy(
            name="dossier_exports.get",
            method="GET",
            pattern=_p("/v1/exports/dossiers/{dossier_export_id}"),
            route_group="admin",
            operator_roles=("CAMLCO", "BB_INSPECTOR_READ_ONLY", "READ_ONLY"),
        ),
        # -- operator auth (the TOTP step itself) -------------------------------
        RoutePolicy(
            name="auth.operator_totp_verify",
            method="POST",
            pattern=_p("/v1/auth/operator/totp/verify"),
            route_group="auth",
            public=True,  # the password token + TOTP inside the body ARE the auth
            ip_limit_per_minute=60,
            skip_auth=True,
        ),
        # -- inbound connector webhooks -----------------------------------------
        RoutePolicy(
            name="webhooks.inbound",
            method="POST",
            pattern=_p("/v1/webhooks/{connector_id}"),
            route_group="webhook_inbound",
            public=True,  # the connector signature IS the auth (fail-closed)
            ip_limit_per_minute=600,
            skip_auth=True,
        ),
        # -- health / metrics ----------------------------------------------------
        RoutePolicy(
            name="system.health",
            method="GET",
            pattern=re.compile(r"^/(v1/health|healthz)$"),
            route_group="system",
            public=True,
            ip_limit_per_minute=600,
            skip_auth=True,
        ),
        RoutePolicy(
            name="system.ready",
            method="GET",
            pattern=re.compile(r"^/(v1/ready|readyz)$"),
            route_group="system",
            public=True,
            ip_limit_per_minute=600,
            skip_auth=True,
        ),
        RoutePolicy(
            name="system.metrics",
            method="GET",
            pattern=_p("/metrics"),
            route_group="system",
            public=True,
            ip_limit_per_minute=600,
            skip_auth=True,
        ),
        RoutePolicy(
            name="system.openapi",
            method="GET",
            pattern=_p("/v1/openapi.json"),
            route_group="system",
            public=True,
            ip_limit_per_minute=600,
            skip_auth=True,
        ),
    )


def match_policy(
    policies: tuple[RoutePolicy, ...], method: str, path: str
) -> RoutePolicy | None:
    """First policy whose method+pattern match; ``None`` = unknown surface."""
    for policy in policies:
        if policy.method == method.upper() and policy.pattern.match(path):
            return policy
    return None
