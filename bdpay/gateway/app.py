"""FastAPI edge application — middleware spine + /v1 routes (spec/01).

Request pipeline (one pass, refusal-first at every step):

1. request id (content-addressed + monotonic counter — no UUIDs)
2. Idempotency-Key presence on mutating routes (before auth, errata G-1: 400)
3. authentication (merchant bearer / HMAC-signed / customer JWT / operator
   session) and the RBAC policy table (errata: deny-by-default)
4. rate limiting — per-IP bucket then per-principal bucket (ported
   fixed-window core; spec/01 tier table; shadow + fail-open supported)
5. idempotency reserve / replay / conflict (422 on mismatch, errata G-2)
6. route handler via ports; response capture for idempotent replay
7. envelope + ``X-BDPay-Api-Version: 1`` + ``X-BDPay-Request-Id`` headers,
   PII-redacted request log line, metrics

Every non-2xx body is exactly the spec/00 §4 envelope, with two documented
probe exemptions (``/v1/ready`` 503 body, errata G-8).
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, ValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.cors import CORSMiddleware

from bdpay.gateway.apikeys import ApiKeyRecord, ApiKeyService
from bdpay.gateway.config import GatewaySettings
from bdpay.gateway.credentials import KEY_PREFIX_LIVE, KEY_PREFIX_TEST, sha256_hex
from bdpay.gateway.gw_errors import IdempotencyParamMismatchError
from bdpay.gateway.ids_ext import RequestIdGenerator
from bdpay.gateway.jwt_hs256 import JwtError, decode_jwt
from bdpay.gateway.links import PaymentLinkService
from bdpay.gateway.operators import OperatorAuthService
from bdpay.gateway.ops_read import (
    GatewaySlaRecorder,
    aml_dashboard,
    connectors_dashboard,
    ledger_chain_entries,
    ledger_chain_verify_status,
    ledger_journal_entries,
    settlement_dashboard,
    sla_dashboard,
    tcsa_dashboard,
)
from bdpay.gateway.otp_sessions import OtpSessionStore
from bdpay.gateway.pagination import build_page, parse_pagination
from bdpay.gateway.policy import RoutePolicy, build_policies, match_policy
from bdpay.gateway.ports import (
    CustomerOnboardingPort,
    DossierExportPort,
    MerchantOnboardingPort,
    PaymentIntentPort,
    QrPort,
    RefundPort,
    WebhookHandlerRegistry,
    WebhookSinkPort,
)
from bdpay.gateway.principals import Principal
from bdpay.gateway.public_surfaces import PublicSurfaceService
from bdpay.gateway.ratelimit import RateLimiter, RateLimitResult, client_ip_from_headers
from bdpay.gateway.sandbox import SandboxSignupService
from bdpay.gateway.schemas import (
    CancelRequest,
    CaptureRequest,
    ConfirmRequest,
    CustomerCreate,
    DinerPayRequest,
    MerchantCreate,
    OperatorTotpVerifyRequest,
    PaymentIntentCreate,
    RefundCreate,
    redact_metadata,
)
from bdpay.gateway.webhooks_out import (
    WebhookDeliveryService,
    WebhookEndpointService,
)
from bdpay.platform.canonical import CanonicalError, sha256_canonical
from bdpay.platform.clock import Clock, SteppingClock
from bdpay.platform.errors import (
    AuthenticationError,
    AuthorizationError,
    BDPayError,
    ConflictError,
    InternalError,
    InvalidRequestError,
    NotFoundError,
    RateLimitError,
    RequestTooLargeError,
)
from bdpay.platform.interfaces import IdempotencyPort
from bdpay.platform.observability import MetricsRegistry, configure_logging

__all__ = ["GatewayDependencies", "create_app"]

API_VERSION_HEADER = "X-BDPay-Api-Version"
REQUEST_ID_HEADER = "X-BDPay-Request-Id"
IDEMPOTENCY_HEADER = "Idempotency-Key"
SECURITY_HEADERS = {
    "Strict-Transport-Security": "max-age=63072000; includeSubDomains",
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'; base-uri 'none'",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
}

_AUTHED_IP_LIMIT_PER_MINUTE = 1200
_MUTATING_METHODS = ("POST", "PUT", "PATCH")


def rfc3339(at: datetime) -> str:
    """RFC3339 UTC with millisecond precision and ``Z`` suffix (E12 form)."""
    return at.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _as_datetime(value: object) -> datetime | None:
    """Best-effort coercion of an intent/refund ``created_at`` to a datetime.

    The kernel ports return either a tz-aware ``datetime`` or an RFC3339 string
    (fakes seed ISO strings); ``None`` / anything unparsable yields ``None`` so
    the merchant dashboard renders "—" rather than crashing the read.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


@dataclass
class GatewayDependencies:
    """Everything the edge needs, injected at composition time."""

    settings: GatewaySettings
    clock: Clock
    api_keys: ApiKeyService
    operators: OperatorAuthService
    otp_sessions: OtpSessionStore
    idempotency: IdempotencyPort
    rate_limiter: RateLimiter
    payment_intents: PaymentIntentPort
    refunds: RefundPort
    merchants: MerchantOnboardingPort
    customers: CustomerOnboardingPort
    webhook_registry: WebhookHandlerRegistry
    webhook_sink: WebhookSinkPort
    # BP-G4d: optional CERT WebhookPipeline (None => legacy verify+sink).
    webhook_pipeline: object | None = None
    metrics: MetricsRegistry = field(default_factory=MetricsRegistry)
    readiness_checks: Mapping[str, Callable[[], bool]] = field(default_factory=dict)
    jwt_revocation: Callable[[str], bool] | None = None
    log_stream: object | None = None
    policies: tuple[RoutePolicy, ...] = field(default_factory=build_policies)
    # spec/16 LR-5: outbound merchant webhooks (registration + §D rotation).
    webhook_endpoints: WebhookEndpointService | None = None
    webhook_deliveries: WebhookDeliveryService | None = None
    # spec/16 LR-1/LR-2: operator-only dossier exports (compliance-owned service).
    dossier_exports: DossierExportPort | None = None
    # spec/16 LR-4 merchant wedge: payment links (§C), public surfaces (§E),
    # sandbox signup (§F — wired ONLY in SANDBOX_PUBLIC deployments).
    payment_links: PaymentLinkService | None = None
    public_surfaces: PublicSurfaceService | None = None
    sandbox_signups: SandboxSignupService | None = None
    sandbox_demo_seed_enabled: bool = False
    # spec/13: Bangla QR (acquirer issuance + issuer scan-to-pay + operator).
    qr: QrPort | None = None
    # Operator console read models. These are optional for harnesses and older
    # composition paths; production build_services wires the real stores below.
    ledger: object | None = None
    tcsa_store: object | None = None
    settlement_store: object | None = None
    connector_registry: object | None = None
    aml_alert_store: object | None = None
    str_store: object | None = None
    ctr_store: object | None = None
    sanctions_store: object | None = None
    goaml_connector: object | None = None
    sla_recorder: GatewaySlaRecorder = field(default_factory=GatewaySlaRecorder)


def _envelope_response(
    error: BDPayError, request_id: str, headers: Mapping[str, str] | None = None
) -> JSONResponse:
    merged = dict(headers) if headers else {}
    # spec/16 envelope amendment: 503 capacity refusals carry Retry-After.
    retry_after = getattr(error, "retry_after_seconds", None)
    if isinstance(retry_after, int) and not isinstance(retry_after, bool):
        merged.setdefault("Retry-After", str(retry_after))
    return JSONResponse(
        status_code=error.http_status,
        content=error.to_envelope(request_id),
        headers=merged or None,
    )


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "req_unassigned")


def _principal(request: Request) -> Principal | None:
    return getattr(request.state, "principal", None)


def _require_principal(request: Request) -> Principal:
    principal = _principal(request)
    if principal is None:  # policy table prevents this; refuse anyway
        raise AuthenticationError("credentials required", code="missing_authorization")
    return principal


async def _json_body(request: Request) -> dict:
    raw = await request.body()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise InvalidRequestError(
            "request body must be a JSON object", code="invalid_json"
        ) from exc
    if not isinstance(parsed, dict):
        raise InvalidRequestError("request body must be a JSON object", code="invalid_json")
    return parsed


def _redact_json_fields(value: object, fields: tuple[str, ...]) -> dict:
    field_set = set(fields)

    def redact(node: object) -> object:
        if isinstance(node, Mapping):
            return {
                str(key): redact(child)
                for key, child in node.items()
                if str(key) not in field_set
            }
        if isinstance(node, list):
            return [redact(child) for child in node]
        return node

    redacted = redact(value)
    return redacted if isinstance(redacted, dict) else {}


def _validate[ModelT: BaseModel](model_cls: type[ModelT], data: dict) -> ModelT:
    try:
        return model_cls.model_validate(data)
    except ValidationError as exc:
        # field paths + expectation text only — input values never echo back
        parts = []
        for err in exc.errors(include_input=False, include_url=False):
            loc = ".".join(str(piece) for piece in err["loc"]) or "body"
            parts.append(f"{loc}: {err['msg']}")
        raise InvalidRequestError(
            "; ".join(parts[:5]) or "request validation failed", code="validation_failed"
        ) from exc


class GatewayMiddleware(BaseHTTPMiddleware):
    """The single edge pipeline: auth, rate limit, idempotency, envelope."""

    def __init__(self, app, deps: GatewayDependencies) -> None:  # type: ignore[no-untyped-def]
        super().__init__(app)
        self._deps = deps
        self._request_ids = RequestIdGenerator(seed=deps.settings.request_id_seed)
        self._log = logging.getLogger("bdpay.gateway")

    async def dispatch(self, request: Request, call_next) -> Response:  # type: ignore[no-untyped-def]
        deps = self._deps
        started_ns = time.perf_counter_ns()
        request_id = self._request_ids.next_id(request.method, request.url.path)
        request.state.request_id = request_id
        rate_headers: dict[str, str] = {}
        try:
            response = await self._process(request, call_next, rate_headers)
        except BDPayError as exc:
            response = _envelope_response(exc, request_id)
        except Exception:  # noqa: BLE001 - the edge never leaks a traceback
            self._log.exception("unhandled error on %s %s", request.method, request.url.path)
            response = _envelope_response(
                InternalError("an internal error occurred", code="internal_error"), request_id
            )
        for name, value in rate_headers.items():
            response.headers.setdefault(name, value)
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        response.headers[API_VERSION_HEADER] = "1"
        response.headers[REQUEST_ID_HEADER] = request_id
        self._log.info(
            "%s %s -> %d request_id=%s",
            request.method,
            request.url.path,
            response.status_code,
            request_id,
        )
        deps.metrics.increment(
            "gateway_requests_total",
            labels={"method": request.method, "status": str(response.status_code)},
        )
        route_group = getattr(request.state, "route_group", None)
        if isinstance(route_group, str) and route_group:
            elapsed_ms = max(0, (time.perf_counter_ns() - started_ns) // 1_000_000)
            deps.sla_recorder.record(route_group, elapsed_ms)
        return response

    # -- pipeline ------------------------------------------------------------

    async def _process(
        self, request: Request, call_next, rate_headers: dict[str, str]
    ) -> Response:  # type: ignore[no-untyped-def]
        deps = self._deps
        self._install_body_size_guard(request)
        policy = match_policy(deps.policies, request.method, request.url.path)
        if policy is None:
            return await call_next(request)  # enveloped 404/405 by the handlers
        request.state.route_group = policy.route_group

        client_ip = client_ip_from_headers(request.headers) or (
            request.client.host if request.client else "unknown"
        )
        ip_hash = sha256_hex(client_ip)
        request.state.client_ip_hash = ip_hash

        # Idempotency-Key presence — before auth (spec/01 failure modes; G-1: 400)
        idem_key = request.headers.get(IDEMPOTENCY_HEADER)
        if policy.idempotency_required and not idem_key:
            raise InvalidRequestError(
                "Mutating requests require an Idempotency-Key header.",
                code="idempotency_key_required",
            )

        principal: Principal | None = None
        if not policy.skip_auth:
            principal = await self._authenticate(request, policy, client_ip)
        self._authorize(policy, principal)
        request.state.principal = principal

        self._enforce_rate_limits(policy, principal, ip_hash, rate_headers)

        if policy.idempotency_required and request.method in _MUTATING_METHODS:
            return await self._with_idempotency(
                request, call_next, idem_key or "", principal, policy
            )
        return await call_next(request)

    def _install_body_size_guard(self, request: Request) -> None:
        limit = self._deps.settings.max_request_body_bytes
        raw_length = request.headers.get("content-length")
        if raw_length:
            try:
                content_length = int(raw_length, 10)
            except ValueError as exc:
                raise InvalidRequestError(
                    "Content-Length must be an integer", code="invalid_content_length"
                ) from exc
            if content_length > limit:
                raise RequestTooLargeError(
                    "request body exceeds the configured size limit",
                    code="request_body_too_large",
                )

        original_receive = request._receive  # type: ignore[attr-defined]
        bytes_seen = 0

        async def limited_receive():  # type: ignore[no-untyped-def]
            nonlocal bytes_seen
            message = await original_receive()
            if message.get("type") == "http.request":
                chunk = message.get("body", b"") or b""
                bytes_seen += len(chunk)
                if bytes_seen > limit:
                    raise RequestTooLargeError(
                        "request body exceeds the configured size limit",
                        code="request_body_too_large",
                    )
            return message

        request._receive = limited_receive  # type: ignore[attr-defined]

    # -- authentication --------------------------------------------------------

    async def _authenticate(
        self, request: Request, policy: RoutePolicy, client_ip: str
    ) -> Principal | None:
        deps = self._deps
        headers = request.headers
        if "X-BDPay-Key-Id" in headers:
            body = await request.body()
            return deps.api_keys.authenticate_hmac(
                key_id=headers.get("X-BDPay-Key-Id", ""),
                signature=headers.get("X-BDPay-Signature", ""),
                timestamp=headers.get("X-BDPay-Timestamp", ""),
                method=request.method,
                path=request.url.path,
                body=body,
                client_ip=client_ip,
            )
        authorization = headers.get("Authorization")
        if authorization is None:
            if policy.public:
                return None
            raise AuthenticationError(
                "credentials required", code="missing_authorization"
            )
        scheme, _, token = authorization.partition(" ")
        token = token.strip()
        if scheme.lower() != "bearer" or not token:
            raise AuthenticationError(
                "Authorization must carry a Bearer credential", code="missing_authorization"
            )
        if token.startswith((KEY_PREFIX_LIVE, KEY_PREFIX_TEST)):
            return deps.api_keys.authenticate_bearer(token, client_ip=client_ip)
        if token.count(".") == 2:
            return self._authenticate_customer(token)
        return deps.operators.authenticate_session(token)

    def _authenticate_customer(self, token: str) -> Principal:
        deps = self._deps
        secrets = [deps.settings.jwt_secret]
        if deps.settings.jwt_previous_secret:
            secrets.append(deps.settings.jwt_previous_secret)
        try:
            claims = decode_jwt(token, secrets, now=deps.clock.now())
        except JwtError as exc:
            code = "token_expired" if exc.reason == "token_expired" else "token_invalid"
            raise AuthenticationError("customer token rejected", code=code) from exc
        jti = str(claims.get("jti", ""))
        if deps.jwt_revocation is not None and deps.jwt_revocation(jti):
            raise AuthenticationError("customer token revoked", code="token_revoked")
        subject = str(claims.get("sub", ""))
        if not subject.startswith("cust_"):
            raise AuthenticationError("customer token rejected", code="token_invalid")
        scope = claims.get("scope", [])
        return Principal(
            kind="customer",
            principal_id=subject,
            customer_id=subject,
            scopes=frozenset(str(item) for item in scope),  # type: ignore[union-attr]
            kyc_tier=str(claims["kyc_tier"]) if "kyc_tier" in claims else None,
        )

    # -- authorization (refusal-first RBAC) --------------------------------------

    @staticmethod
    def _authorize(policy: RoutePolicy, principal: Principal | None) -> None:
        if principal is None:
            if policy.public or policy.skip_auth:
                return
            raise AuthenticationError("credentials required", code="missing_authorization")
        if principal.kind == "merchant_key":
            allowed = policy.merchant_scope is not None and (
                policy.merchant_scope == "*" or policy.merchant_scope in principal.scopes
            )
        elif principal.kind == "customer":
            allowed = policy.customer_scopes is not None and any(
                scope in principal.scopes for scope in policy.customer_scopes
            )
        else:
            allowed = policy.operator_roles is not None and (
                "SUPER_ADMIN" in principal.roles
                or any(role in principal.roles for role in policy.operator_roles)
            )
        if not allowed:
            raise AuthorizationError(
                "this credential may not perform this operation", code="permission_denied"
            )

    # -- rate limiting -------------------------------------------------------------

    def _enforce_rate_limits(
        self,
        policy: RoutePolicy,
        principal: Principal | None,
        ip_hash: str,
        rate_headers: dict[str, str],
    ) -> None:
        deps = self._deps
        if policy.route_group == "system":
            return
        ip_limit = policy.ip_limit_per_minute
        if ip_limit is None and principal is not None:
            ip_limit = _AUTHED_IP_LIMIT_PER_MINUTE
        results: list[tuple[RateLimitResult, int]] = [
            deps.rate_limiter.check_ip(
                ip_hash=ip_hash, route_group=policy.route_group, limit=ip_limit
            )
        ]
        if principal is not None:
            results.append(
                deps.rate_limiter.check_principal(
                    rate_key=principal.rate_key,
                    route_group=policy.route_group,
                    principal_class=RateLimiter.principal_class(
                        principal.kind, principal.key_env
                    ),
                )
            )
        primary, primary_limit = results[-1]
        rate_headers["X-RateLimit-Limit"] = str(primary_limit)
        rate_headers["X-RateLimit-Remaining"] = str(primary.remaining)
        rate_headers["X-RateLimit-Reset"] = str(primary.reset_at_ms // 1000)
        if any(result.degraded for result, _ in results):
            rate_headers["X-RateLimit-Mode"] = "degraded"
        blocked = next((result for result, _ in results if not result.allowed), None)
        if blocked is not None:
            now_s = int(deps.clock.now().timestamp())
            retry_after = max(1, blocked.reset_at_ms // 1000 - now_s)
            rate_headers["Retry-After"] = str(retry_after)
            raise RateLimitError(
                f"Rate limit exceeded for this endpoint. Retry after {retry_after} seconds.",
                code="rate_limit_exceeded",
            )

    # -- idempotency ----------------------------------------------------------------

    @staticmethod
    def _idempotency_scope_id(principal: Principal | None) -> str | None:
        """Stable principal identity used to scope idempotency reservations.

        Merchant keys keep their existing key_id scope; customer JWTs and
        operator sessions use their user-level identity so that two different
        principals cannot cross-replay the same Idempotency-Key on shared
        paths (e.g. /v1/refunds, /v1/customers, /v1/exports/dossiers).
        """
        if principal is None:
            return None
        if principal.kind == "merchant_key":
            return principal.principal_id
        if principal.kind == "customer":
            return principal.customer_id
        if principal.kind == "operator":
            return principal.operator_id
        return None

    async def _with_idempotency(
        self,
        request: Request,
        call_next,
        idem_key: str,
        principal: Principal | None,
        policy: RoutePolicy,
    ) -> Response:  # type: ignore[no-untyped-def]
        deps = self._deps
        body = await _json_body(request)
        try:
            body_hash = sha256_canonical(body)
        except CanonicalError as exc:
            raise InvalidRequestError(
                "request body is not canonically serializable "
                "(floats are rejected; money is integer paisa)",
                code="invalid_request_body",
            ) from exc
        key_id = (
            principal.principal_id
            if principal is not None and principal.kind == "merchant_key"
            else None
        )
        scope_id = self._idempotency_scope_id(principal)
        customer_id = principal.customer_id if principal is not None else None
        path = request.url.path
        reservation = deps.idempotency.reserve(
            idempotency_key=idem_key,
            key_id=key_id,
            scope_id=scope_id,
            customer_id=customer_id,
            http_method=request.method,
            request_path=path,
            request_body_hash=body_hash,
            now=deps.clock.now(),
        )
        if reservation.outcome == "IN_FLIGHT":
            raise ConflictError(
                "A request with this Idempotency-Key is still processing.",
                code="idempotency_processing",
            )
        if reservation.outcome == "CONFLICT":
            raise IdempotencyParamMismatchError(
                "The Idempotency-Key was used with different request parameters."
            )
        if reservation.outcome == "REPLAY":
            if reservation.response_body is None:
                raise ConflictError(
                    "the stored response exceeds the replay size cap",
                    code="idempotency_large_response",
                )
            replay_body = dict(reservation.response_body)
            replay_status = reservation.response_status or 200
            # Only successful responses ever carried the shown-once secret;
            # stored error envelopes replay verbatim (no marker bolted onto
            # the {error} shape).
            if policy.idempotent_redact_fields and 200 <= replay_status < 300:
                replay_body["secret_already_shown"] = True
            return JSONResponse(
                status_code=replay_status,
                content=replay_body,
                headers={"X-BDPay-Idempotent-Replay": "true"},
            )

        release = getattr(deps.idempotency, "release", None)
        try:
            response = await call_next(request)
        except Exception:
            if release is not None:  # 5xx path: keep retries possible (errata G-5)
                release(
                    idempotency_key=idem_key, key_id=key_id, scope_id=scope_id, request_path=path
                )
            raise
        raw = await self._drain_body(response)
        if response.status_code >= 500 and release is not None:
            release(
                idempotency_key=idem_key, key_id=key_id, scope_id=scope_id, request_path=path
            )
        else:
            try:
                stored: dict = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                stored = {}
            if policy.idempotent_redact_fields:
                stored = _redact_json_fields(stored, policy.idempotent_redact_fields)
            deps.idempotency.complete(
                idempotency_key=idem_key,
                key_id=key_id,
                scope_id=scope_id,
                request_path=path,
                response_status=response.status_code,
                response_body=stored,
                now=deps.clock.now(),
            )
        return Response(
            content=raw,
            status_code=response.status_code,
            headers=dict(response.headers),
            media_type=response.media_type,
        )

    @staticmethod
    async def _drain_body(response: Response) -> bytes:
        iterator = getattr(response, "body_iterator", None)
        if iterator is None:
            return bytes(response.body)
        chunks = [chunk async for chunk in iterator]
        return b"".join(chunks)


def create_app(deps: GatewayDependencies) -> FastAPI:
    """Compose the gateway FastAPI application from injected dependencies."""
    configure_logging(
        service="gateway",
        logger=logging.getLogger("bdpay.gateway"),
        stream=deps.log_stream,
    )
    app = FastAPI(
        title="BD-PAY Gateway",
        version="1",
        openapi_url="/v1/openapi.json",
        docs_url=None,
        redoc_url=None,
    )
    app.state.gateway_clock = deps.clock
    app.state.gateway_settings = deps.settings

    # -- error envelope exactness (spec/00 §4) ---------------------------------

    @app.exception_handler(BDPayError)
    async def _bdpay_error(request: Request, exc: BDPayError) -> JSONResponse:
        return _envelope_response(exc, _request_id(request))

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        if exc.status_code == 404:
            mapped: BDPayError = NotFoundError(
                "the requested route does not exist", code="route_not_found"
            )
        elif exc.status_code == 405:
            # refusal-first matrix: unknown (credential, resource, method) -> 403
            mapped = AuthorizationError(
                "method not permitted on this resource", code="method_not_allowed"
            )
        else:
            mapped = InternalError("an internal error occurred", code="internal_error")
        return _envelope_response(mapped, _request_id(request))

    @app.exception_handler(RequestValidationError)
    async def _validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return _envelope_response(
            InvalidRequestError("request validation failed", code="validation_failed"),
            _request_id(request),
        )

    # -- payment intents ----------------------------------------------------------

    async def create_payment_intent(request: Request) -> Response:
        principal = _require_principal(request)
        body = _validate(PaymentIntentCreate, await _json_body(request))
        if principal.kind != "merchant_key" or body.merchant_id != principal.merchant_id:
            raise AuthorizationError(
                "merchant_id does not match the authenticated key",
                code="merchant_id_mismatch",
            )
        metadata, stripped = redact_metadata(body.metadata)
        payload = body.model_dump()
        payload["metadata"] = metadata
        result = deps.payment_intents.create_intent(
            payload,
            merchant_id=principal.merchant_id,
            idempotency_key=request.headers.get(IDEMPOTENCY_HEADER, ""),
            clock=deps.clock,
        )
        headers = {"W-PII-Stripped": "1"} if stripped else None
        return JSONResponse(dict(result), status_code=201, headers=headers)

    def _load_intent(payment_intent_id: str, principal: Principal) -> dict:
        intent = deps.payment_intents.get_intent(payment_intent_id)
        if intent is None:
            raise NotFoundError("payment intent not found", code="payment_intent_not_found")
        intent = dict(intent)
        if principal.kind == "merchant_key" and intent.get("merchant_id") != principal.merchant_id:
            raise AuthorizationError(
                "payment intent belongs to a different merchant", code="merchant_id_mismatch"
            )
        if principal.kind == "customer" and intent.get("customer_id") != principal.customer_id:
            raise AuthorizationError(
                "payment intent belongs to a different customer", code="customer_id_mismatch"
            )
        return intent

    async def get_payment_intent(request: Request) -> Response:
        principal = _require_principal(request)
        intent = _load_intent(str(request.path_params["payment_intent_id"]), principal)
        return JSONResponse(intent)

    async def list_payment_intents(request: Request) -> Response:
        principal = _require_principal(request)
        limit, cursor = parse_pagination(request.query_params)
        params = request.query_params
        if principal.kind == "customer":
            # Customer-scoped listing: enforce that the customer only sees their
            # own intents (money-path isolation; overrides any caller-supplied
            # customer_id filter).
            filters = {
                name: params[name]
                for name in ("status", "created_after", "created_before")
                if name in params
            }
            items, next_cursor = deps.payment_intents.list_intents_for_customer(
                customer_id=principal.customer_id or "",
                filters=filters,
                limit=limit,
                cursor=cursor,
            )
            return JSONResponse(build_page(items, next_cursor))
        if principal.kind == "merchant_key":
            requested = params.get("merchant_id")
            if requested and requested != principal.merchant_id:
                raise AuthorizationError(
                    "merchant_id does not match the authenticated key",
                    code="merchant_id_mismatch",
                )
            merchant_id: str | None = principal.merchant_id
        else:
            merchant_id = params.get("merchant_id") or None
        filters = {
            name: params[name]
            for name in ("status", "customer_id", "created_after", "created_before")
            if name in params
        }
        items, next_cursor = deps.payment_intents.list_intents(
            merchant_id=merchant_id, filters=filters, limit=limit, cursor=cursor
        )
        return JSONResponse(build_page(items, next_cursor))

    async def confirm_payment_intent(request: Request) -> Response:
        principal = _require_principal(request)
        payment_intent_id = str(request.path_params["payment_intent_id"])
        body = _validate(ConfirmRequest, await _json_body(request))
        intent = _load_intent(payment_intent_id, principal)
        if principal.kind == "customer":
            amount = int(intent.get("amount_minor", 0))  # type: ignore[arg-type]
            if amount >= deps.settings.customer_confirm_2fa_threshold_minor:
                token = body.otp_session_token
                verified = token is not None and deps.otp_sessions.verify(
                    token,
                    customer_id=principal.customer_id or "",
                    now=deps.clock.now(),
                )
                if not verified:
                    raise AuthenticationError(
                        "OTP verification is required for this amount (BB ICT 2FA rule)",
                        code="otp_required",
                    )
        result = await _confirm_intent_via_port(
            payment_intent_id, body.model_dump(), actor=principal.kind
        )
        return JSONResponse(dict(result))

    async def capture_payment_intent(request: Request) -> Response:
        principal = _require_principal(request)
        payment_intent_id = str(request.path_params["payment_intent_id"])
        body = _validate(CaptureRequest, await _json_body(request))
        if principal.kind != "operator":
            _load_intent(payment_intent_id, principal)
        result = deps.payment_intents.capture_intent(
            payment_intent_id, amount_minor=body.amount_minor, clock=deps.clock
        )
        return JSONResponse(dict(result))

    async def cancel_payment_intent(request: Request) -> Response:
        principal = _require_principal(request)
        payment_intent_id = str(request.path_params["payment_intent_id"])
        body = _validate(CancelRequest, await _json_body(request))
        if principal.kind != "operator":
            _load_intent(payment_intent_id, principal)
        result = deps.payment_intents.cancel_intent(
            payment_intent_id,
            cancellation_reason=body.cancellation_reason,
            clock=deps.clock,
        )
        return JSONResponse(dict(result))

    async def list_payment_attempts(request: Request) -> Response:
        principal = _require_principal(request)
        payment_intent_id = str(request.path_params["payment_intent_id"])
        _load_intent(payment_intent_id, principal)
        limit, cursor = parse_pagination(request.query_params)
        items, next_cursor = deps.payment_intents.list_attempts(
            payment_intent_id, limit=limit, cursor=cursor
        )
        return JSONResponse(build_page(items, next_cursor))

    # -- refunds ---------------------------------------------------------------------

    async def create_refund(request: Request) -> Response:
        principal = _require_principal(request)
        body = _validate(RefundCreate, await _json_body(request))
        if principal.kind == "merchant_key":
            _load_intent(body.payment_intent_id, principal)
        if (
            principal.kind == "operator"
            and body.amount_minor >= deps.settings.operator_refund_2fa_threshold_minor
        ):
            verified_at = principal.totp_verified_at
            fresh = verified_at is not None and (
                (deps.clock.now() - verified_at).total_seconds()
                <= deps.settings.operator_refund_totp_freshness_seconds
            )
            if not fresh:
                raise AuthenticationError(
                    "operator TOTP re-verification is required for high-value refunds",
                    code="totp_reverification_required",
                )
        payload = body.model_dump()
        if payload.get("reason") == "requested_by_customer":
            payload["reason"] = "customer_request"
        result = deps.refunds.create_refund(
            payload,
            merchant_id=principal.merchant_id if principal.kind == "merchant_key" else None,
            idempotency_key=request.headers.get(IDEMPOTENCY_HEADER, ""),
            clock=deps.clock,
        )
        return JSONResponse(dict(result), status_code=201)

    async def get_refund(request: Request) -> Response:
        principal = _require_principal(request)
        refund = deps.refunds.get_refund(str(request.path_params["refund_id"]))
        if refund is None:
            raise NotFoundError("refund not found", code="refund_not_found")
        refund = dict(refund)
        if principal.kind == "merchant_key" and refund.get("merchant_id") != principal.merchant_id:
            raise AuthorizationError(
                "refund belongs to a different merchant", code="merchant_id_mismatch"
            )
        return JSONResponse(refund)

    async def list_refunds(request: Request) -> Response:
        principal = _require_principal(request)
        limit, cursor = parse_pagination(request.query_params)
        params = request.query_params
        merchant_id = (
            principal.merchant_id
            if principal.kind == "merchant_key"
            else params.get("merchant_id") or None
        )
        filters = {
            name: params[name]
            for name in ("payment_intent_id", "status")
            if name in params
        }
        items, next_cursor = deps.refunds.list_refunds(
            merchant_id=merchant_id, filters=filters, limit=limit, cursor=cursor
        )
        return JSONResponse(build_page(items, next_cursor))

    # -- merchants ----------------------------------------------------------------------

    async def create_merchant(request: Request) -> Response:
        _require_principal(request)
        body = _validate(MerchantCreate, await _json_body(request))
        result = deps.merchants.submit_application(
            body.model_dump(),
            idempotency_key=request.headers.get(IDEMPOTENCY_HEADER, ""),
            clock=deps.clock,
        )
        return JSONResponse(dict(result), status_code=202)

    async def get_merchant(request: Request) -> Response:
        principal = _require_principal(request)
        merchant_id = str(request.path_params["merchant_id"])
        if principal.kind == "merchant_key" and merchant_id != principal.merchant_id:
            raise AuthorizationError(
                "API keys may only read their own merchant record",
                code="merchant_id_mismatch",
            )
        merchant = deps.merchants.get_merchant(merchant_id)
        if merchant is None:
            raise NotFoundError("merchant not found", code="merchant_not_found")
        return JSONResponse(dict(merchant))

    async def list_merchants(request: Request) -> Response:
        _require_principal(request)
        limit, cursor = parse_pagination(request.query_params)
        params = request.query_params
        filters = {name: params[name] for name in ("status", "mcc") if name in params}
        items, next_cursor = deps.merchants.list_merchants(
            filters=filters, limit=limit, cursor=cursor
        )
        return JSONResponse(build_page(items, next_cursor))

    async def list_diner_merchants(request: Request) -> Response:
        _require_principal(request)
        limit, cursor = parse_pagination(request.query_params)
        params = request.query_params
        filters = {name: params[name] for name in ("area", "q") if name in params}
        items, next_cursor = deps.merchants.list_diner_merchants(
            filters=filters, limit=limit, cursor=cursor
        )
        return JSONResponse(build_page(items, next_cursor))

    # -- customers / eKYC -----------------------------------------------------------------

    async def create_customer(request: Request) -> Response:
        principal = _principal(request)
        body = _validate(CustomerCreate, await _json_body(request))
        if principal is not None and principal.kind == "merchant_key":
            if body.merchant_id and body.merchant_id != principal.merchant_id:
                raise AuthorizationError(
                    "merchant_id does not match the authenticated key",
                    code="merchant_id_mismatch",
                )
            merchant_id: str | None = principal.merchant_id
        else:
            merchant_id = body.merchant_id
        result = deps.customers.submit_customer(
            body.model_dump(),
            merchant_id=merchant_id,
            idempotency_key=request.headers.get(IDEMPOTENCY_HEADER, ""),
            clock=deps.clock,
        )
        return JSONResponse(dict(result), status_code=202)

    def _load_customer(customer_id: str, principal: Principal) -> dict:
        record = deps.customers.get_customer(customer_id)
        if record is None:
            raise NotFoundError("customer not found", code="customer_not_found")
        record = dict(record)
        if principal.kind == "customer" and customer_id != principal.customer_id:
            raise AuthorizationError(
                "customers may only read their own record", code="customer_id_mismatch"
            )
        if principal.kind == "merchant_key" and record.get("merchant_id") != principal.merchant_id:
            raise AuthorizationError(
                "customer belongs to a different merchant", code="merchant_id_mismatch"
            )
        return record

    async def get_customer(request: Request) -> Response:
        principal = _require_principal(request)
        record = _load_customer(str(request.path_params["customer_id"]), principal)
        return JSONResponse(record)

    async def get_customer_balance(request: Request) -> Response:
        principal = _require_principal(request)
        customer_id = str(request.path_params["customer_id"])
        _load_customer(customer_id, principal)
        balance = deps.customers.get_balance(customer_id)
        if balance is None:
            raise NotFoundError("customer not found", code="customer_not_found")
        return JSONResponse(dict(balance))

    # -- operator TOTP step -----------------------------------------------------------------

    async def operator_totp_verify(request: Request) -> Response:
        body = _validate(OperatorTotpVerifyRequest, await _json_body(request))
        authorization = request.headers.get("Authorization", "")
        scheme, _, password_token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not password_token.strip():
            raise AuthenticationError(
                "the pre-2FA operator token must be presented as a Bearer credential",
                code="missing_authorization",
            )
        token, session = deps.operators.verify_totp(
            operator_id=body.operator_id,
            totp_code=body.totp_code,
            password_token=password_token.strip(),
            client_ip=getattr(request.state, "client_ip_hash", ""),
            user_agent=request.headers.get("User-Agent", ""),
        )
        return JSONResponse(
            {
                "session_token": token,
                "operator_id": session.operator_id,
                "roles": list(session.roles),
                "expires_at": rfc3339(session.expires_at),
            }
        )

    # -- outbound merchant webhooks (spec/01 §I subset + spec/16 LR-5 §D) ----------------------

    def _require_webhook_services() -> tuple[WebhookEndpointService, WebhookDeliveryService]:
        if deps.webhook_endpoints is None or deps.webhook_deliveries is None:
            raise InternalError(
                "the outbound webhook surface is not wired in this deployment",
                code="webhook_surface_unavailable",
            )
        return deps.webhook_endpoints, deps.webhook_deliveries

    def _endpoint_view(record, *, secret: str | None = None) -> dict:  # type: ignore[no-untyped-def]
        view: dict = {
            "webhook_id": record.webhook_id,
            "url": record.url,
            "enabled_events": list(record.enabled_events),
            "status": record.status,
            "description": record.description,
            "created_at": rfc3339(record.created_at),
            "secret_rotation_grace_until": (
                rfc3339(record.secret_rotation_grace_until)
                if record.secret_rotation_grace_until is not None
                else None
            ),
        }
        if secret is not None:  # shown exactly once at creation/rotation
            view["signing_secret"] = secret
        return view

    async def create_webhook_endpoint(request: Request) -> Response:
        principal = _require_principal(request)
        endpoints, _deliveries = _require_webhook_services()
        body = await _json_body(request)
        if body.get("merchant_id") and body["merchant_id"] != principal.merchant_id:
            raise AuthorizationError(
                "merchant_id does not match the authenticated key",
                code="merchant_id_mismatch",
            )
        url = body.get("url")
        enabled_events = body.get("enabled_events")
        if not isinstance(url, str) or not isinstance(enabled_events, list):
            raise InvalidRequestError(
                "url (string) and enabled_events (list) are required",
                code="validation_failed",
            )
        created = endpoints.register(
            merchant_id=principal.merchant_id or "",
            url=url,
            enabled_events=tuple(str(item) for item in enabled_events),
            description=(
                str(body["description"]) if body.get("description") is not None else None
            ),
        )
        return JSONResponse(
            _endpoint_view(created.record, secret=created.signing_secret), status_code=201
        )

    async def list_webhook_endpoints(request: Request) -> Response:
        principal = _require_principal(request)
        endpoints, _deliveries = _require_webhook_services()
        records = endpoints.list_for_merchant(principal.merchant_id or "")
        # No signing_secret field on list reads (spec/01 §I).
        return JSONResponse(build_page([_endpoint_view(r) for r in records], None))

    async def rotate_webhook_secret(request: Request) -> Response:
        principal = _require_principal(request)
        endpoints, _deliveries = _require_webhook_services()
        webhook_id = str(request.path_params["webhook_id"])
        rotated = endpoints.rotate_secret(
            webhook_id, merchant_id=principal.merchant_id or ""
        )
        return JSONResponse(
            _endpoint_view(rotated.record, secret=rotated.signing_secret)
        )

    async def list_webhook_deliveries(request: Request) -> Response:
        principal = _require_principal(request)
        _endpoints, deliveries = _require_webhook_services()
        limit, cursor = parse_pagination(request.query_params)
        params = request.query_params
        items, next_cursor = deliveries.list_deliveries(
            merchant_id=principal.merchant_id or "",
            webhook_id=params.get("webhook_id") or None,
            status=params.get("status") or None,
            limit=limit,
            cursor=cursor,
        )
        return JSONResponse(build_page(items, next_cursor))

    # -- dossier exports (spec/16 §API A; operator-only, compliance-owned) ----------------------

    def _require_dossier_service() -> DossierExportPort:
        if deps.dossier_exports is None:
            raise InternalError(
                "the dossier export surface is not wired in this deployment",
                code="dossier_surface_unavailable",
            )
        return deps.dossier_exports

    def _dossier_view(record) -> dict:  # type: ignore[no-untyped-def]
        view: dict = {
            "dossier_export_id": record.dossier_export_id,
            "dossier_type": record.dossier_type,
            "state": record.state,
            "input_snapshot": dict(record.input_snapshot),
            "dossier_hash": record.dossier_hash,
            "fail_reason": record.fail_reason,
            "requested_by": record.requested_by,
            "created_at": rfc3339(record.created_at),
            "completed_at": (
                rfc3339(record.completed_at) if record.completed_at is not None else None
            ),
        }
        # Manifest is exposed once COMPLETED (spec/16 §A: "manifest (once COMPLETED)").
        view["manifest"] = (
            dict(record.manifest)
            if record.state == "COMPLETED" and record.manifest is not None
            else None
        )
        return view

    async def create_dossier_export(request: Request) -> Response:
        principal = _require_principal(request)
        service = _require_dossier_service()
        body = await _json_body(request)
        dossier_type = body.get("dossier_type")
        input_snapshot = body.get("input_snapshot")
        if not isinstance(dossier_type, str) or not isinstance(input_snapshot, dict):
            raise InvalidRequestError(
                "dossier_type (string) and input_snapshot (object) are required",
                code="validation_failed",
            )
        record = service.queue(
            dossier_type=dossier_type,
            input_snapshot=input_snapshot,
            # requested_by is the OPERATOR identity (the session id would not
            # survive an audit trail review across re-logins).
            requested_by=principal.operator_id or principal.principal_id,
        )
        # 202: assembly is asynchronous; identical type+snapshot returns the
        # existing export (content-addressed idempotency, spec/16 §A).
        return JSONResponse(
            {
                "dossier_export_id": record.dossier_export_id,  # type: ignore[attr-defined]
                "state": record.state,  # type: ignore[attr-defined]
            },
            status_code=202,
        )

    async def list_dossier_exports(request: Request) -> Response:
        _require_principal(request)
        service = _require_dossier_service()
        limit, cursor = parse_pagination(request.query_params)
        records, next_cursor = service.list(limit=limit, cursor=cursor)
        return JSONResponse(build_page([_dossier_view(r) for r in records], next_cursor))

    async def get_dossier_export(request: Request) -> Response:
        _require_principal(request)
        service = _require_dossier_service()
        record = service.get(str(request.path_params["dossier_export_id"]))
        if record is None:
            raise NotFoundError("dossier export not found", code="dossier_export_not_found")
        return JSONResponse(_dossier_view(record))

    async def download_dossier_export(request: Request) -> Response:
        _require_principal(request)
        service = _require_dossier_service()
        dossier_export_id = str(request.path_params["dossier_export_id"])
        zip_bytes, content_sha256 = service.download(dossier_export_id)
        # spec/15 deterministic file+hash contract: X-BDPay-Content-Sha256 +
        # byte-identical re-download (the service returns the sealed object).
        return Response(
            content=zip_bytes,
            media_type="application/zip",
            headers={
                "X-BDPay-Content-Sha256": content_sha256,
                "Content-Disposition": (
                    f'attachment; filename="{dossier_export_id}.zip"'
                ),
            },
        )

    # -- payment links (spec/16 LR-4 §C) ---------------------------------------------------------

    def _require_payment_links() -> PaymentLinkService:
        if deps.payment_links is None:
            raise InternalError(
                "the payment-link surface is not wired in this deployment",
                code="payment_link_surface_unavailable",
            )
        return deps.payment_links

    async def create_payment_link(request: Request) -> Response:
        principal = _require_principal(request)
        service = _require_payment_links()
        body = await _json_body(request)
        result = service.create(
            principal.merchant_id or "",
            body,
            request.headers.get(IDEMPOTENCY_HEADER, ""),
            deps.clock,
        )
        return JSONResponse(result, status_code=201)

    async def list_payment_links(request: Request) -> Response:
        principal = _require_principal(request)
        service = _require_payment_links()
        limit, cursor = parse_pagination(request.query_params)
        page = service.list(
            merchant_id=principal.merchant_id or "", limit=limit, cursor=cursor
        )
        return JSONResponse(page)

    async def get_payment_link(request: Request) -> Response:
        principal = _require_principal(request)
        service = _require_payment_links()
        view = service.get(
            str(request.path_params["plink_id"]),
            merchant_id=principal.merchant_id or "",
        )
        return JSONResponse(view)

    async def cancel_payment_link(request: Request) -> Response:
        principal = _require_principal(request)
        service = _require_payment_links()
        # Operators cancel without merchant scoping (FSM cancel trigger:
        # "merchant payment:write OR operator"); merchants only their own.
        merchant_id = (
            principal.merchant_id if principal.kind == "merchant_key" else None
        )
        view = service.cancel(
            str(request.path_params["plink_id"]),
            principal.principal_id,
            deps.clock,
            merchant_id=merchant_id,
        )
        return JSONResponse(view)

    async def get_public_payment_link(request: Request) -> Response:
        # PII-free public view; non-ACTIVE links return their state so the
        # hosted page renders a terminal view — never an error envelope.
        service = _require_payment_links()
        view = service.get_by_public_code(str(request.path_params["public_code"]))
        return JSONResponse(view)

    async def create_public_checkout_intent(request: Request) -> Response:
        # Idempotency is server-generated from public_code + payer inputs
        # (spec/16 §C) — payers never present an Idempotency-Key header.
        service = _require_payment_links()
        payer_inputs = await _json_body(request)
        intent = service.create_checkout_intent(
            str(request.path_params["public_code"]), payer_inputs, deps.clock
        )
        # spec/16 §C: hosted checkout renders a dynamic Bangla QR bound to the
        # intent (spec/13 TLV from the real engine, never a front-end mock).
        if deps.qr is not None:
            intent_id = intent.get("payment_intent_id")
            if isinstance(intent_id, str):
                try:
                    dyn = deps.qr.issue_dynamic(intent_id, ttl_seconds=300)
                except ConflictError:
                    # scan-only soft-launch / merchant method gate: the
                    # checkout proceeds without a QR (R2 posture).
                    pass
                else:
                    intent = dict(intent)
                    intent["qr_payload"] = dyn.payload  # type: ignore[attr-defined]
                    intent["qr_payload_hash"] = dyn.payload_hash  # type: ignore[attr-defined]
                    intent["qr_expires_at"] = rfc3339(dyn.expires_at)  # type: ignore[attr-defined]
        return JSONResponse(intent, status_code=201)

    # -- public surfaces (spec/16 LR-4 §E — cached, PII-free reads) ------------------------------

    def _require_public_surfaces() -> PublicSurfaceService:
        if deps.public_surfaces is None:
            raise InternalError(
                "the public status surface is not wired in this deployment",
                code="public_surface_unavailable",
            )
        return deps.public_surfaces

    async def public_status(request: Request) -> Response:
        return JSONResponse(
            _require_public_surfaces().status(),
            headers={"Cache-Control": "public, max-age=60"},
        )

    async def public_certification_matrix(request: Request) -> Response:
        return JSONResponse(
            _require_public_surfaces().certification_matrix(),
            headers={"Cache-Control": "public, max-age=60"},
        )

    async def public_badge(request: Request) -> Response:
        result = _require_public_surfaces().badge_svg(
            str(request.path_params["connector_id"])
        )
        if result is None:  # spec/16 §E: 404 for unknown connector_id
            raise NotFoundError(
                "no badge exists for this connector", code="unknown_connector"
            )
        svg, content_sha256 = result
        return Response(
            content=svg,
            media_type="image/svg+xml",
            headers={
                "Cache-Control": "public, max-age=300",
                "X-BDPay-Content-Sha256": content_sha256,
            },
        )

    # -- sandbox signup (spec/16 LR-4 §F; SANDBOX_PUBLIC deployments only) -----------------------

    def _require_sandbox() -> SandboxSignupService:
        if deps.sandbox_signups is None:
            # Outside SANDBOX_PUBLIC the surface does not exist at all —
            # indistinguishable from an unknown route (deployment boundary).
            raise NotFoundError(
                "the requested route does not exist", code="route_not_found"
            )
        return deps.sandbox_signups

    async def create_sandbox_signup(request: Request) -> Response:
        service = _require_sandbox()
        body = await _json_body(request)
        email = body.get("email")
        display_name = body.get("display_name")
        if not isinstance(email, str) or not isinstance(display_name, str):
            raise InvalidRequestError(
                "email (string) and display_name (string) are required",
                code="validation_failed",
            )
        return JSONResponse(
            service.signup(email, display_name, deps.clock), status_code=201
        )

    async def verify_sandbox_signup(request: Request) -> Response:
        service = _require_sandbox()
        body = await _json_body(request)
        otp = body.get("otp")
        if not isinstance(otp, str):
            raise InvalidRequestError(
                "otp (string) is required", code="validation_failed"
            )
        return JSONResponse(
            service.verify(str(request.path_params["sbxs_id"]), otp, deps.clock)
        )

    async def get_sandbox_signup(request: Request) -> Response:
        service = _require_sandbox()
        token = request.headers.get("X-Sandbox-Signup-Token", "")
        return JSONResponse(
            service.get(str(request.path_params["sbxs_id"]), token)
        )

    def _require_sandbox_demo_seed() -> SandboxSignupService:
        if not deps.sandbox_demo_seed_enabled or deps.sandbox_signups is None:
            raise NotFoundError(
                "the requested route does not exist", code="route_not_found"
            )
        return deps.sandbox_signups

    def _positive_minor(value: object, *, default: int, field_name: str) -> int:
        if value is None:
            return default
        if isinstance(value, bool):
            raise InvalidRequestError(
                f"{field_name} must be an integer paisa amount",
                code="validation_failed",
            )
        if isinstance(value, int):
            amount = value
        elif isinstance(value, str) and value.strip().isdigit():
            amount = int(value.strip(), 10)
        else:
            raise InvalidRequestError(
                f"{field_name} must be an integer paisa amount",
                code="validation_failed",
            )
        if amount <= 0:
            raise InvalidRequestError(
                f"{field_name} must be > 0", code="validation_failed"
            )
        return amount

    def _sandbox_demo_otp(service: SandboxSignupService, sandbox_signup_id: str) -> str:
        notifications = getattr(service, "_notifications", None)
        store = getattr(notifications, "_store", None)
        rows = getattr(store, "_rows", None)
        if not isinstance(rows, dict):
            raise InternalError(
                "sandbox demo seed requires the in-memory notification store",
                code="sandbox_demo_seed_unavailable",
            )
        for dispatch in rows.values():
            if (
                getattr(dispatch, "recipient_id", None) == sandbox_signup_id
                and getattr(dispatch, "template_id", None) == "sandbox_signup_otp"
            ):
                template_vars = getattr(dispatch, "template_vars", {})
                otp = template_vars.get("otp_code") if isinstance(template_vars, dict) else None
                if isinstance(otp, str) and otp:
                    return otp
        raise InternalError(
            "sandbox signup OTP was not queued in memory",
            code="sandbox_demo_seed_unavailable",
        )

    def _activate_demo_customer(record: object) -> object:
        if not isinstance(record, dict):
            return record
        updated = dict(record)
        updated["status"] = "ACTIVE"
        updated["kyc_tier"] = "SIMPLIFIED"
        rows = getattr(deps.customers, "_customers", None)
        customer_id = updated.get("customer_id")
        if isinstance(rows, dict) and isinstance(customer_id, str):
            rows[customer_id] = updated
        return updated

    async def _confirm_intent_via_port(
        payment_intent_id: str, payload: dict, *, actor: str, clock: Clock | None = None
    ) -> object:
        operation_clock = clock or deps.clock
        confirm_async = getattr(deps.payment_intents, "confirm_intent_async", None)
        if callable(confirm_async):
            return await confirm_async(
                payment_intent_id, payload, actor=actor, clock=operation_clock
            )
        return deps.payment_intents.confirm_intent(
            payment_intent_id, payload, actor=actor, clock=operation_clock
        )

    def _ledger_seed_proof(merchant_id: str, intent: dict, refund: dict) -> dict:
        orchestrator = getattr(deps.payment_intents, "_orch", None)
        ledger_port = getattr(orchestrator, "_ledger", None)
        ledger = getattr(ledger_port, "_svc", None)
        if ledger is None:
            return {"available": False}
        from bdpay.ledger.account_ids import merchant_settlement_id

        attempt_page, _ = deps.payment_intents.list_attempts(
            intent["payment_intent_id"], limit=10, cursor=None
        )
        attempts = list(attempt_page)
        charged_attempts = [
            item for item in attempts if item.get("status") in ("CHARGED", "AUTHORIZED")
        ]
        attempt_id = (
            charged_attempts[-1].get("attempt_id")
            if charged_attempts
            else (attempts[-1].get("attempt_id") if attempts else None)
        )

        def entries(reference_id: str | None, entry_types: tuple[str, ...]) -> list[str]:
            if not reference_id:
                return []
            return [
                row.entry_id
                for entry_type in entry_types
                for row in ledger.list_journal_entries_by_reference_and_type(
                    reference_id, entry_type
                )
            ]

        payment_reference_id = intent["payment_intent_id"]
        settlement_account = merchant_settlement_id(merchant_id)
        trial_balance = ledger.trial_balance()
        return {
            "available": True,
            "attempt_id": attempt_id,
            "capture_reference_id": payment_reference_id,
            "payment_captured_entries": entries(
                attempt_id, ("payment_captured",)
            )
            + entries(payment_reference_id, ("hold_captured", "payment_captured")),
            "fee_collected_entries": entries(attempt_id, ("fee_collected",))
            + entries(payment_reference_id, ("fee_collected",)),
            "refund_settled_entries": entries(
                refund.get("refund_id"), ("refund_settled",)
            ),
            "merchant_settlement_account": settlement_account,
            "merchant_settlement_balance_minor": ledger.get_balance(settlement_account),
            "trial_balance": {
                "total_debit_minor": trial_balance.total_debit_minor,
                "total_credit_minor": trial_balance.total_credit_minor,
                "balanced": trial_balance.balanced,
            },
        }

    def _require_sandbox_demo_read() -> None:
        if not deps.sandbox_demo_seed_enabled:
            raise NotFoundError(
                "the requested route does not exist", code="route_not_found"
            )

    def _json_ready(value: object) -> object:
        if isinstance(value, datetime):
            return rfc3339(value)
        if isinstance(value, Mapping):
            return {str(key): _json_ready(item) for key, item in value.items()}
        if isinstance(value, list | tuple):
            return [_json_ready(item) for item in value]
        enum_value = getattr(value, "value", None)
        if enum_value is not None and not isinstance(value, str | int | float | bool):
            return _json_ready(enum_value)
        return value

    def _demo_attr(row: object, name: str, default: object = None) -> object:
        if isinstance(row, Mapping):
            return row.get(name, default)
        return getattr(row, name, default)

    def _demo_ledger_service() -> object | None:
        orchestrator = getattr(deps.payment_intents, "_orch", None)
        ledger_port = getattr(orchestrator, "_ledger", None)
        return getattr(ledger_port, "_svc", None)

    def _ledger_entry_chain_view(ledger: object, entry_id: str) -> dict | None:
        store = getattr(ledger, "store", None)
        iter_chain = getattr(store, "iter_chain", None)
        if not callable(iter_chain):
            return None
        try:
            rows = list(iter_chain("MONEY"))
        except TypeError:
            rows = list(iter_chain())
        for row in rows:
            payload_pointer = str(_demo_attr(row, "payload_pointer", ""))
            if entry_id not in payload_pointer:
                continue
            return {
                "chain_id": _demo_attr(row, "chain_id"),
                "sequence": _demo_attr(row, "sequence"),
                "entry_hash": _demo_attr(row, "entry_hash"),
                "previous_hash": _demo_attr(row, "previous_hash"),
                "payload_pointer": payload_pointer,
                "created_at": _demo_attr(row, "created_at"),
            }
        return None

    def _ledger_entry_view(ledger: object, row: object) -> dict:
        entry_id = str(_demo_attr(row, "entry_id", ""))
        get_postings = getattr(ledger, "get_postings_for_entry", None)
        postings = list(get_postings(entry_id)) if callable(get_postings) else []
        return {
            "entry_id": entry_id,
            "entry_index": _demo_attr(row, "entry_index"),
            "reference_id": _demo_attr(row, "reference_id"),
            "reference_type": _demo_attr(row, "reference_type"),
            "entry_type": _demo_attr(row, "entry_type"),
            "description": _demo_attr(row, "description"),
            "produced_by": _demo_attr(row, "produced_by"),
            "produced_at": _demo_attr(row, "produced_at"),
            "idempotency_key": _demo_attr(row, "idempotency_key"),
            "schema_version": _demo_attr(row, "schema_version"),
            "postings": [
                {
                    "posting_id": _demo_attr(posting, "posting_id"),
                    "account_id": _demo_attr(posting, "account_id"),
                    "side": _demo_attr(posting, "side"),
                    "amount_minor": _demo_attr(posting, "amount_minor"),
                    "currency": _demo_attr(posting, "currency"),
                    "produced_at": _demo_attr(posting, "produced_at"),
                    "schema_version": _demo_attr(posting, "schema_version"),
                }
                for posting in postings
            ],
            "chain": _ledger_entry_chain_view(ledger, entry_id),
        }

    def _demo_ledger_view(intent: dict, attempts: list[dict], refunds: list[dict]) -> dict:
        ledger = _demo_ledger_service()
        if ledger is None:
            return {"available": False, "entries": []}

        def entries(reference_id: object, entry_types: tuple[str, ...]) -> list[object]:
            if not isinstance(reference_id, str) or not reference_id:
                return []
            list_entries = getattr(ledger, "list_journal_entries_by_reference_and_type", None)
            if not callable(list_entries):
                return []
            rows: list[object] = []
            for entry_type in entry_types:
                rows.extend(list(list_entries(reference_id, entry_type)))
            return rows

        entry_rows: list[object] = []
        payment_reference_id = intent.get("payment_intent_id")
        entry_rows.extend(
            entries(
                payment_reference_id,
                ("hold_captured", "payment_captured", "fee_collected"),
            )
        )
        for attempt in attempts:
            entry_rows.extend(
                entries(
                    attempt.get("attempt_id") or attempt.get("payment_attempt_id"),
                    ("hold_captured", "payment_captured", "fee_collected"),
                )
            )
        for refund in refunds:
            entry_rows.extend(entries(refund.get("refund_id"), ("refund_settled",)))

        seen: set[str] = set()
        rendered: list[dict] = []
        for row in entry_rows:
            entry_id = str(_demo_attr(row, "entry_id", ""))
            if not entry_id or entry_id in seen:
                continue
            seen.add(entry_id)
            rendered.append(_ledger_entry_view(ledger, row))

        trial_balance = None
        if callable(getattr(ledger, "trial_balance", None)):
            balance = ledger.trial_balance()
            trial_balance = {
                "total_debit_minor": _demo_attr(balance, "total_debit_minor"),
                "total_credit_minor": _demo_attr(balance, "total_credit_minor"),
                "balanced": _demo_attr(balance, "balanced"),
            }
        chain_verify = None
        if callable(getattr(ledger, "verify_chain", None)):
            result = ledger.verify_chain()
            chain_verify = {
                "ok": _demo_attr(result, "ok"),
                "checked_entries": _demo_attr(result, "checked_entries"),
                "broken_at": _demo_attr(result, "broken_at"),
            }
        return {
            "available": True,
            "entries": rendered,
            "trial_balance": trial_balance,
            "chain_verify": chain_verify,
        }

    def _demo_flow_status(intent: dict, refunds: list[dict]) -> str:
        amount_minor = int(intent.get("amount_minor") or 0)
        refund_statuses = {
            "REFUND_INITIATED",
            "REFUND_SETTLED",
            "SETTLED",
            "SUCCEEDED",
            "REFUNDED",
        }
        refunded_minor = sum(
            int(refund.get("amount_minor") or 0)
            for refund in refunds
            if str(refund.get("status") or "").upper() in refund_statuses
        )
        if amount_minor > 0 and 0 < refunded_minor < amount_minor:
            return "PARTIALLY_REFUNDED"
        if amount_minor > 0 and refunded_minor >= amount_minor:
            return "REFUNDED"
        return str(intent.get("status") or "UNKNOWN")

    async def get_tcsa_dashboard(request: Request) -> Response:
        _require_principal(request)
        return JSONResponse(tcsa_dashboard(deps))

    async def get_settlement_dashboard(request: Request) -> Response:
        _require_principal(request)
        return JSONResponse(settlement_dashboard(deps))

    async def get_connectors_dashboard(request: Request) -> Response:
        _require_principal(request)
        return JSONResponse(connectors_dashboard(deps))

    async def get_aml_dashboard(request: Request) -> Response:
        _require_principal(request)
        return JSONResponse(aml_dashboard(deps))

    async def get_sla_dashboard(request: Request) -> Response:
        _require_principal(request)
        return JSONResponse(sla_dashboard(deps))

    async def list_ledger_journal_entries(request: Request) -> Response:
        _require_principal(request)
        limit, cursor = parse_pagination(request.query_params)
        return JSONResponse(ledger_journal_entries(deps, limit=limit, cursor=cursor))

    async def list_ledger_chain_entries(request: Request) -> Response:
        _require_principal(request)
        limit, cursor = parse_pagination(request.query_params)
        return JSONResponse(ledger_chain_entries(deps, limit=limit, cursor=cursor))

    async def get_ledger_chain_verify_status(request: Request) -> Response:
        _require_principal(request)
        return JSONResponse(ledger_chain_verify_status(deps))

    async def sandbox_demo_fullflow(request: Request) -> Response:
        _require_sandbox_demo_read()
        principal = _require_principal(request)
        params = request.query_params
        merchant_id: str | None
        if principal.kind == "merchant_key":
            merchant_id = principal.merchant_id
        elif principal.kind == "customer":
            merchant_id = None
        else:
            merchant_id = params.get("merchant_id") or None

        items, _ = deps.payment_intents.list_intents(
            merchant_id=merchant_id,
            filters={},
            limit=100,
            cursor=None,
        )
        customer_id = principal.customer_id if principal.kind == "customer" else None
        candidates: list[dict] = []
        for item in items:
            intent = dict(item)
            metadata = intent.get("metadata") if isinstance(intent.get("metadata"), dict) else {}
            if not metadata.get("demo_seed_id"):
                continue
            if customer_id is not None and intent.get("customer_id") != customer_id:
                continue
            candidates.append(intent)
        if not candidates:
            raise NotFoundError(
                "sandbox demo full-flow seed not found",
                code="sandbox_demo_flow_not_found",
            )

        def sort_value(intent: dict) -> str:
            value = (
                intent.get("updated_at")
                or intent.get("created_at")
                or intent.get("payment_intent_id")
                or ""
            )
            return str(_json_ready(value))

        intent = sorted(candidates, key=sort_value)[-1]
        flow_merchant_id = str(intent.get("merchant_id") or "")
        payment_intent_id = str(intent.get("payment_intent_id") or "")
        attempts_page, _ = deps.payment_intents.list_attempts(
            payment_intent_id, limit=20, cursor=None
        )
        attempts = [dict(item) for item in attempts_page]
        refunds_page, _ = deps.refunds.list_refunds(
            merchant_id=flow_merchant_id or merchant_id,
            filters={"payment_intent_id": payment_intent_id},
            limit=20,
            cursor=None,
        )
        refunds = [
            dict(item)
            for item in refunds_page
            if item.get("payment_intent_id") == payment_intent_id
        ]
        refunded_minor = sum(int(refund.get("amount_minor") or 0) for refund in refunds)
        customer = None
        intent_customer_id = intent.get("customer_id")
        if isinstance(intent_customer_id, str) and intent_customer_id:
            customer = deps.customers.get_customer(intent_customer_id)
        merchant = deps.merchants.get_merchant(flow_merchant_id) if flow_merchant_id else None
        metadata = intent.get("metadata") if isinstance(intent.get("metadata"), dict) else {}
        content = {
            "seed_id": metadata.get("demo_seed_id"),
            "mode": "memory_api_seed",
            "auth": {
                "kind": principal.kind,
                "principal_id": principal.principal_id,
                "merchant_id": principal.merchant_id,
                "operator_id": principal.operator_id,
                "customer_id": principal.customer_id,
            },
            "summary": {
                "status": _demo_flow_status(intent, refunds),
                "amount_minor": intent.get("amount_minor"),
                "refunded_minor": refunded_minor,
                "currency": intent.get("currency"),
                "as_of": rfc3339(deps.clock.now()),
            },
            "merchant": merchant
            or {
                "merchant_id": flow_merchant_id,
                "status": "UNKNOWN",
            },
            "customer": customer,
            "intent": intent,
            "attempts": build_page(attempts, None),
            "refunds": build_page(refunds, None),
            "ledger": _demo_ledger_view(intent, attempts, refunds),
        }
        return JSONResponse(_json_ready(content))

    # -- developer-portal merchant read surface (spec/15 §Developer portal) --------
    #
    # Real, merchant-scoped, read-only endpoints the developer-portal renders
    # from (instead of the demo fullflow shim). Both refuse non-merchant
    # principals by policy (the catalogue/dashboard are the merchant's own;
    # operator/customer credentials are denied to prevent cross-merchant
    # leakage) and scope strictly to ``principal.merchant_id``.

    def _api_key_view(record: ApiKeyRecord) -> dict:  # type: ignore[no-untyped-def]
        # NEVER carry secret_hash / hmac_key_enc / the raw secret over this
        # boundary: the developer-portal renders the catalogue (id, label,
        # scopes, status, timestamps) and recovers a lost secret only via the
        # rotate flow (spec/01 §B). key_prefix is the public "bdpk_live_ /
        # bdpk_test_" clip the portal renders as a non-secret handle.
        env = "live" if record.key_prefix == KEY_PREFIX_LIVE else "test"
        return {
            "key_id": record.key_id,
            "env": env,
            "key_prefix": record.key_prefix,
            "key_name": record.key_name,
            "scopes": list(record.scopes),
            "last_used_at": rfc3339(record.last_used_at) if record.last_used_at else None,
            "created_at": rfc3339(record.created_at) if record.created_at else None,
            "expires_at": rfc3339(record.expires_at) if record.expires_at else None,
            "status": record.status,
        }

    async def list_api_keys(request: Request) -> Response:
        principal = _require_principal(request)
        # Belt-and-suspenders: the policy table already denies operator +
        # customer principals (no operator_roles / customer_scopes); refuse a
        # non-merchant-key principal explicitly rather than enumerate another
        # merchant's catalogue off an empty merchant_id.
        if principal.kind != "merchant_key":
            raise AuthorizationError(
                "this surface is merchant-key only", code="permission_denied"
            )
        records = deps.api_keys.list_for_merchant(principal.merchant_id or "")
        return JSONResponse(build_page([_api_key_view(r) for r in records], None))

    def _require_merchant_key(request: Request) -> Principal:  # type: ignore[no-untyped-def]
        principal = _require_principal(request)
        if principal.kind != "merchant_key":
            raise AuthorizationError(
                "this surface is merchant-key only", code="permission_denied"
            )
        return principal

    def _parse_expires_at(raw: object) -> datetime | None:  # type: ignore[no-untyped-def]
        if raw is None:
            return None
        if not isinstance(raw, str) or not raw.strip():
            raise InvalidRequestError(
                "expires_at must be an RFC3339 string", code="validation_failed"
            )
        try:
            return datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise InvalidRequestError(
                "expires_at must be an RFC3339 string", code="validation_failed"
            ) from exc

    async def create_api_key(request: Request) -> Response:
        principal = _require_merchant_key(request)
        body = await _json_body(request)
        # Defense-in-depth: never let a caller name another merchant's id; the
        # key is always minted under the authenticated principal's merchant_id.
        if body.get("merchant_id") and body["merchant_id"] != principal.merchant_id:
            raise AuthorizationError(
                "merchant_id does not match the authenticated key",
                code="merchant_id_mismatch",
            )
        key_name = body.get("key_name")
        scopes = body.get("scopes")
        env = body.get("env", "live")
        if not isinstance(key_name, str) or not key_name.strip():
            raise InvalidRequestError(
                "key_name (non-empty string) is required", code="validation_failed"
            )
        if not isinstance(scopes, list) or not scopes:
            raise InvalidRequestError(
                "scopes (non-empty list) is required", code="validation_failed"
            )
        if not all(isinstance(s, str) for s in scopes):
            raise InvalidRequestError(
                "scopes must be a list of strings", code="validation_failed"
            )
        if not isinstance(env, str) or env not in ("live", "test"):
            raise InvalidRequestError(
                "env must be live or test", code="invalid_key_env"
            )
        created = deps.api_keys.create_key(
            merchant_id=principal.merchant_id or "",
            key_name=key_name.strip(),
            scopes=tuple(scopes),
            env=env,
            expires_at=_parse_expires_at(body.get("expires_at")),
            entitlement=principal.scopes,
        )
        view = _api_key_view(created.record)
        view["secret"] = created.secret  # shown EXACTLY ONCE; never on reads
        return JSONResponse(view, status_code=201)

    async def rotate_api_key(request: Request) -> Response:
        principal = _require_merchant_key(request)
        api_key_id = str(request.path_params["api_key_id"])
        rotated = deps.api_keys.rotate_secret(
            api_key_id, merchant_id=principal.merchant_id or ""
        )
        view = _api_key_view(rotated.record)
        # New secret shown EXACTLY ONCE; old secret stops working immediately
        # (in-place rotation, no grace window) -> old_secret_expires_at is null.
        view["new_secret"] = rotated.secret
        view["old_secret_expires_at"] = None
        return JSONResponse(view)

    async def revoke_api_key(request: Request) -> Response:
        principal = _require_merchant_key(request)
        api_key_id = str(request.path_params["api_key_id"])
        updated = deps.api_keys.revoke(
            api_key_id, merchant_id=principal.merchant_id or ""
        )
        return JSONResponse(_api_key_view(updated))

    def _attempt_method(payment_intent_id: str) -> str | None:  # type: ignore[no-untyped-def]
        try:
            page, _ = deps.payment_intents.list_attempts(
                payment_intent_id, limit=1, cursor=None
            )
        except Exception:  # noqa: BLE001 - the dashboard never crashes on a
            # missing attempts port; it renders "—" for the method column.
            return None
        if not page:
            return None
        method = page[0].get("method") if isinstance(page[0], Mapping) else None
        return str(method) if method else None

    def _intent_summary_view(intent: Mapping) -> dict:  # type: ignore[no-untyped-def]
        return {
            "payment_intent_id": intent.get("payment_intent_id"),
            "env": None,  # filled by the dashboard view (principal key_env)
            "amount_minor": str(intent.get("amount_minor") or 0),
            "currency": intent.get("currency") or "BDT",
            "method": intent.get("method"),
            "status": intent.get("status"),
            "created_at": rfc3339(_as_datetime(intent.get("created_at")))
            if intent.get("created_at")
            else None,
        }

    async def merchant_dashboard(request: Request) -> Response:
        principal = _require_principal(request)
        if principal.kind != "merchant_key":
            raise AuthorizationError(
                "this surface is merchant-key only", code="permission_denied"
            )
        merchant_id = principal.merchant_id or ""
        env = principal.key_env or ("live" if principal.kind == "merchant_key" else "test")
        portal_env = "sandbox" if env == "test" else "live"
        now = deps.clock.now()
        items, _ = deps.payment_intents.list_intents(
            merchant_id=merchant_id, filters={}, limit=200, cursor=None
        )
        # Volume / counts are computed from the merchant's own intents in the
        # real store (capped at the most recent 200 for the read surface).
        # Integer paisa only — no floats (conventions §6).
        day_ms = 86_400_000
        week_ms = 7 * day_ms
        month_ms = 30 * day_ms
        now_ms = int(now.timestamp() * 1000)

        def _intent_ts(intent: Mapping) -> int | None:  # type: ignore[no-untyped-def]
            dt = _as_datetime(intent.get("created_at"))
            return None if dt is None else int(dt.timestamp() * 1000)

        volume_today = 0
        volume_7d = 0
        volume_30d = 0
        count_today = 0
        terminal = 0
        succeeded = 0
        for intent in items:
            amount = int(intent.get("amount_minor") or 0)
            ts = _intent_ts(intent)
            if ts is not None:
                if now_ms - ts <= day_ms:
                    volume_today += amount
                    count_today += 1
                if now_ms - ts <= week_ms:
                    volume_7d += amount
                if now_ms - ts <= month_ms:
                    volume_30d += amount
            status = str(intent.get("status") or "")
            if status in ("SUCCEEDED", "FAILED", "CANCELLED", "PARTIALLY_REFUNDED"):
                terminal += 1
                if status == "SUCCEEDED":
                    succeeded += 1
        success_rate_bps = (succeeded * 10_000 // terminal) if terminal else 0
        # Recent intents, newest first (the store returns creation order).
        recent = [
            _intent_summary_view(intent)
            for intent in reversed(items)
        ][:10]
        for row in recent:
            row["env"] = portal_env
            if not row.get("method"):
                row["method"] = _attempt_method(row["payment_intent_id"])
        # Settlement summary: real outstanding (settled-grade succeeded volume
        # minus settled refunds) — honest "no released settlement" rather than a
        # fabricated schedule. The portal renders the gaps as "—".
        refunds_page, _ = deps.refunds.list_refunds(
            merchant_id=merchant_id, filters={}, limit=200, cursor=None
        )
        settled_refunded = sum(
            int(r.get("amount_minor") or 0)
            for r in refunds_page
            if str(r.get("status") or "").upper()
            in ("REFUND_SETTLED", "SETTLED", "SUCCEEDED")
        )
        pending_minor = volume_30d - settled_refunded
        body = {
            "as_of": rfc3339(now),
            "env": portal_env,
            "volume_today_minor": str(volume_today),
            "volume_7d_minor": str(volume_7d),
            "volume_30d_minor": str(volume_30d),
            "count_today": count_today,
            "success_rate_bps": success_rate_bps,
            "settlement": {
                "pending_minor": str(max(0, pending_minor)),
                "next_release_at": "",
                "last_settled_minor": "0",
                "last_settled_at": "",
            },
            "recent_intents": recent,
        }
        return JSONResponse(_json_ready(body))

    async def sandbox_demo_fullflow_seed(request: Request) -> Response:
        service = _require_sandbox_demo_seed()
        body = await _json_body(request)
        seed_id = _request_id(request)
        seed_clock = SteppingClock(deps.clock.now(), step_seconds=1)
        seed_hash = sha256_hex(seed_id)[:12]
        display_name = str(body.get("display_name") or "BDPay Demo Merchant")
        email = str(body.get("email") or f"merchant+{seed_hash}@example.test")
        method = str(body.get("method") or "NPSB_IBFT")
        amount_minor = _positive_minor(
            body.get("amount_minor"), default=125_000, field_name="amount_minor"
        )
        refund_amount_minor = _positive_minor(
            body.get("refund_amount_minor"),
            default=50_000,
            field_name="refund_amount_minor",
        )
        if refund_amount_minor > amount_minor:
            raise InvalidRequestError(
                "refund_amount_minor must not exceed amount_minor",
                code="validation_failed",
            )

        signup = service.signup(email, display_name, seed_clock)
        otp = _sandbox_demo_otp(service, signup["sandbox_signup_id"])
        orchestrator = getattr(deps.payment_intents, "_orch", None)
        ledger_port = getattr(orchestrator, "_ledger", None)
        clock_targets = [getattr(service, "_onboarding", None), getattr(ledger_port, "_svc", None)]
        original_clocks = []
        for target in clock_targets:
            if target is None or not hasattr(target, "_clock"):
                continue
            original_clocks.append((target, target._clock))
        for target, _ in original_clocks:
            target._clock = seed_clock
        try:
            provisioned = service.verify(signup["sandbox_signup_id"], otp, seed_clock)
        finally:
            for target, original_clock in original_clocks:
                target._clock = original_clock
        merchant_id = provisioned["merchant_id"]
        default_api_key_id = provisioned["api_key_id"]
        developer_portal_key = service._key_minter.mint(
            merchant_id=merchant_id,
            key_name="Developer portal demo key",
            scopes=("apikey:read", "payment:read"),
            env="test",
        )
        api_key_secret = developer_portal_key.secret

        customer = deps.customers.submit_customer(
            {
                "phone": "01700000000",
                "name_en": "Demo Customer",
                "nid": "0000000000",
                "dob": "1990-01-01",
            },
            merchant_id=merchant_id,
            idempotency_key=f"{seed_id}:customer",
            clock=seed_clock,
        )
        customer = _activate_demo_customer(customer)
        customer_id = customer.get("customer_id") if isinstance(customer, dict) else None

        intent_payload = {
            "merchant_id": merchant_id,
            "amount_minor": amount_minor,
            "currency": "BDT",
            "payment_method_types": [method],
            "method": method,
            "capture_method": "manual",
            "customer_id": customer_id,
            "metadata": {"demo_seed_id": seed_id},
            "description": "Sandbox demo full-flow seed",
        }
        intent = deps.payment_intents.create_intent(
            intent_payload,
            merchant_id=merchant_id,
            idempotency_key=f"{seed_id}:intent",
            clock=seed_clock,
        )
        confirmed = await _confirm_intent_via_port(
            intent["payment_intent_id"],
            {"payment_method": {"type": method}},
            actor="merchant_key",
            clock=seed_clock,
        )
        latest = deps.payment_intents.get_intent(intent["payment_intent_id"]) or confirmed
        captured = deps.payment_intents.capture_intent(
            intent["payment_intent_id"], amount_minor=amount_minor, clock=seed_clock
        )
        refund_created = deps.refunds.create_refund(
            {
                "payment_intent_id": intent["payment_intent_id"],
                "amount_minor": refund_amount_minor,
                "currency": "BDT",
                "reason": "customer_request",
                "metadata": {"demo_seed_id": seed_id},
            },
            merchant_id=merchant_id,
            idempotency_key=f"{seed_id}:refund",
            clock=seed_clock,
        )

        refund_settled = refund_created
        orchestrator = getattr(deps.payment_intents, "_orch", None)
        process_refund = getattr(orchestrator, "process_refund", None)
        if callable(process_refund):
            await process_refund(refund_created["refund_id"])
            refund_settled = deps.refunds.get_refund(refund_created["refund_id"]) or refund_created

        attempts, _ = deps.payment_intents.list_attempts(
            intent["payment_intent_id"], limit=10, cursor=None
        )
        refund_list, _ = deps.refunds.list_refunds(
            merchant_id=merchant_id,
            filters={"payment_intent_id": intent["payment_intent_id"]},
            limit=10,
            cursor=None,
        )

        return JSONResponse(
            {
                "seed_id": seed_id,
                "mode": "memory_api_seed",
                "sandbox_signup": {
                    "sandbox_signup_id": signup["sandbox_signup_id"],
                    "state": "PROVISIONED",
                },
                "merchant": {
                    "merchant_id": merchant_id,
                    "display_name": display_name,
                    "api_key_id": developer_portal_key.record.key_id,
                    "api_key_secret": api_key_secret,
                    "api_key_scopes": list(developer_portal_key.record.scopes),
                    "default_payment_api_key_id": default_api_key_id,
                    "sandbox_base_url": provisioned["sandbox_base_url"],
                    "docs_url": provisioned["docs_url"],
                },
                "customer": customer,
                "flow": {
                    "intent_created": intent,
                    "intent_confirmed": latest,
                    "intent_captured": captured,
                    "refund_created": refund_created,
                    "refund_settled": refund_settled,
                    "attempts": attempts,
                    "refunds": refund_list,
                },
                "ledger": _ledger_seed_proof(merchant_id, captured, refund_settled),
            },
            status_code=201,
        )

    async def sandbox_demo_diner_pay(request: Request) -> Response:
        """SANDBOX-only one-tap diner demo payment (spec/16 LR-4 §F demo family).

        Reuses the SAME orchestrator path as ``sandbox_demo_fullflow_seed``:
        create_intent → confirm → capture against the simulator connector. The
        created intent is a real intent with the supplied ``customer_id``; it
        appears in the customer-scoped GET /v1/payment-intents listing.

        Fail-closed: this route is registered unconditionally, but the
        ``_require_sandbox_demo_seed`` guard returns 404 when the deployment is
        not in SANDBOX_PUBLIC + memory:// + simulator posture.
        """
        _require_sandbox_demo_seed()
        body = _validate(DinerPayRequest, await _json_body(request))
        merchant_id = body.merchant_id
        customer_id = body.customer_id
        amount_minor = body.amount_minor
        method = body.payment_method
        offer_id = body.offer_id

        merchant = deps.merchants.get_merchant(merchant_id)
        if merchant is None or merchant.get("status") != "ACTIVE":
            raise InvalidRequestError(
                "merchant is not available", code="merchant_not_active"
            )
        customer = deps.customers.get_customer(customer_id)
        if customer is None or customer.get("status") not in ("ACTIVE", "SIMPLIFIED"):
            raise InvalidRequestError(
                "customer is not available", code="customer_not_active"
            )

        idem_key = request.headers.get(IDEMPOTENCY_HEADER, "")
        metadata: dict[str, str] = {"demo_diner_pay": "true"}
        if offer_id:
            metadata["offer_id"] = offer_id

        intent_payload = {
            "merchant_id": merchant_id,
            "amount_minor": amount_minor,
            "currency": "BDT",
            "payment_method_types": [method],
            "method": method,
            "capture_method": "manual",
            "customer_id": customer_id,
            "metadata": metadata,
            "description": "Sandbox demo diner pay",
        }
        intent = deps.payment_intents.create_intent(
            intent_payload,
            merchant_id=merchant_id,
            idempotency_key=idem_key,
            clock=deps.clock,
        )
        await _confirm_intent_via_port(
            intent["payment_intent_id"],
            {"payment_method": {"type": method}},
            actor="merchant_key",
            clock=deps.clock,
        )
        captured = deps.payment_intents.capture_intent(
            intent["payment_intent_id"], amount_minor=amount_minor, clock=deps.clock
        )
        return JSONResponse(
            {
                "payment_intent_id": captured["payment_intent_id"],
                "status": captured["status"],
                "amount_minor": captured["amount_minor"],
                "customer_id": customer_id,
                "merchant_id": merchant_id,
            },
            status_code=201,
        )

    # -- Bangla QR (spec/13) ----------------------------------------------------------------

    def _require_qr() -> QrPort:
        if deps.qr is None:
            raise InternalError(
                "the Bangla QR surface is not wired in this deployment",
                code="qr_surface_unavailable",
            )
        return deps.qr

    def _qr_render_urls(merchant_qr_id: str) -> dict:
        base = f"/v1/qr-codes/{merchant_qr_id}"
        return {
            "png": f"{base}/render.png",
            "svg": f"{base}/render.svg",
            "kit_pdf": f"{base}/kit.pdf",
        }

    def _qr_view(row) -> dict:  # type: ignore[no-untyped-def]
        return {
            "merchant_qr_id": row.merchant_qr_id,
            "merchant_id": row.merchant_id,
            "state": row.state.value,
            "label": row.label,
            "store_id": row.store_id,
            "terminal_id": row.terminal_id,
            "suspend_reason": row.suspend_reason,
            "created_at": rfc3339(row.created_at),
            "updated_at": rfc3339(row.updated_at),
            "render_urls": _qr_render_urls(row.merchant_qr_id),
        }

    def _check_qr_ownership(request: Request, owner_merchant_id: str) -> None:
        """Merchant keys see only their own QRs (404 — no existence leak)."""
        principal = _require_principal(request)
        if principal.kind == "merchant_key" and principal.merchant_id != owner_merchant_id:
            raise NotFoundError("merchant qr not found", code="qr_not_found")

    async def issue_static_qr(request: Request) -> Response:
        principal = _require_principal(request)
        service = _require_qr()
        merchant_id = str(request.path_params["merchant_id"])
        if principal.kind == "merchant_key" and principal.merchant_id != merchant_id:
            raise AuthorizationError(
                "merchant keys may only issue QR codes for their own merchant",
                code="permission_denied",
            )
        body = await _json_body(request)
        label = body.get("label")
        if not isinstance(label, str) or not label:
            raise InvalidRequestError(
                "label (non-empty string) is required", code="validation_failed"
            )
        store_id = body.get("store_id")
        terminal_id = body.get("terminal_id")
        for name, value in (("store_id", store_id), ("terminal_id", terminal_id)):
            if value is not None and not isinstance(value, str):
                raise InvalidRequestError(
                    f"{name} must be a string when present", code="validation_failed"
                )
        result = service.issue_static(
            merchant_id, label=label, store_id=store_id, terminal_id=terminal_id
        )
        return JSONResponse(
            {
                "merchant_qr_id": result.merchant_qr_id,  # type: ignore[attr-defined]
                "state": result.state,  # type: ignore[attr-defined]
                "payload": result.payload,  # type: ignore[attr-defined]
                "payload_hash": result.payload_hash,  # type: ignore[attr-defined]
                "static_limit_minor": result.static_limit_minor,  # type: ignore[attr-defined]
                "render_urls": _qr_render_urls(result.merchant_qr_id),  # type: ignore[attr-defined]
            },
            status_code=201,
        )

    async def list_merchant_qrs(request: Request) -> Response:
        principal = _require_principal(request)
        service = _require_qr()
        merchant_id = str(request.path_params["merchant_id"])
        if principal.kind == "merchant_key" and principal.merchant_id != merchant_id:
            raise AuthorizationError(
                "merchant keys may only list their own QR codes",
                code="permission_denied",
            )
        rows = service.list_merchant_qrs(merchant_id)
        return JSONResponse({"data": [_qr_view(r) for r in rows]})

    async def issue_dynamic_qr(request: Request) -> Response:
        principal = _require_principal(request)
        service = _require_qr()
        body = await _json_body(request)
        payment_intent_id = body.get("payment_intent_id")
        if not isinstance(payment_intent_id, str) or not payment_intent_id:
            raise InvalidRequestError(
                "payment_intent_id (string) is required", code="validation_failed"
            )
        ttl_seconds = body.get("ttl_seconds", 300)
        if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int):
            raise InvalidRequestError(
                "ttl_seconds must be an integer", code="validation_failed"
            )
        # Ownership: the intent must belong to the calling merchant.
        if principal.kind == "merchant_key":
            intent = deps.payment_intents.get_intent(payment_intent_id)
            if intent is None or intent.get("merchant_id") != principal.merchant_id:
                raise NotFoundError(
                    "payment intent not found", code="payment_intent_not_found"
                )
        result = service.issue_dynamic(payment_intent_id, ttl_seconds=ttl_seconds)
        return JSONResponse(
            {
                "payload_id": result.payload_id,  # type: ignore[attr-defined]
                "payment_intent_id": result.payment_intent_id,  # type: ignore[attr-defined]
                "payload": result.payload,  # type: ignore[attr-defined]
                "payload_hash": result.payload_hash,  # type: ignore[attr-defined]
                "amount_minor": result.amount_minor,  # type: ignore[attr-defined]
                "expires_at": rfc3339(result.expires_at),  # type: ignore[attr-defined]
            },
            status_code=201,
        )

    async def get_merchant_qr(request: Request) -> Response:
        service = _require_qr()
        row = service.get_merchant_qr(str(request.path_params["merchant_qr_id"]))
        _check_qr_ownership(request, row.merchant_id)  # type: ignore[attr-defined]
        return JSONResponse(_qr_view(row))

    def _render_qr_asset(request: Request, kind: str) -> Response:
        service = _require_qr()
        merchant_qr_id = str(request.path_params["merchant_qr_id"])
        row = service.get_merchant_qr(merchant_qr_id)
        _check_qr_ownership(request, row.merchant_id)  # type: ignore[attr-defined]
        asset = service.render_asset(merchant_qr_id, kind)
        # spec/13 render contract: pure function of the payload string —
        # byte-identical across calls; X-BDPay-Content-Sha256 on every render.
        return Response(
            content=asset.content,  # type: ignore[attr-defined]
            media_type=asset.media_type,  # type: ignore[attr-defined]
            headers={
                "Cache-Control": "public, max-age=300",
                "X-BDPay-Content-Sha256": asset.content_sha256,  # type: ignore[attr-defined]
            },
        )

    async def render_qr_png(request: Request) -> Response:
        return _render_qr_asset(request, "png")

    async def render_qr_svg(request: Request) -> Response:
        return _render_qr_asset(request, "svg")

    async def render_qr_kit_pdf(request: Request) -> Response:
        return _render_qr_asset(request, "kit_pdf")

    async def _qr_lifecycle(request: Request, trigger: str) -> Response:
        service = _require_qr()
        merchant_qr_id = str(request.path_params["merchant_qr_id"])
        row = service.get_merchant_qr(merchant_qr_id)
        _check_qr_ownership(request, row.merchant_id)  # type: ignore[attr-defined]
        body = await _json_body(request)
        reason = body.get("reason")
        if reason is not None and not isinstance(reason, str):
            raise InvalidRequestError(
                "reason must be a string when present", code="validation_failed"
            )
        if trigger == "suspend":
            updated = service.suspend(merchant_qr_id, reason=reason or "merchant_request")
        elif trigger == "reactivate":
            updated = service.reactivate(merchant_qr_id)
        else:
            updated = service.revoke(merchant_qr_id, reason=reason or "merchant_request")
        return JSONResponse(_qr_view(updated))

    async def suspend_merchant_qr(request: Request) -> Response:
        return await _qr_lifecycle(request, "suspend")

    async def reactivate_merchant_qr(request: Request) -> Response:
        return await _qr_lifecycle(request, "reactivate")

    async def revoke_merchant_qr(request: Request) -> Response:
        return await _qr_lifecycle(request, "revoke")

    async def resolve_qr(request: Request) -> Response:
        principal = _require_principal(request)
        service = _require_qr()
        body = await _json_body(request)
        payload = body.get("payload")
        if not isinstance(payload, str) or not payload:
            raise InvalidRequestError(
                "payload (the scanned string) is required", code="validation_failed"
            )
        result = service.resolve(
            payload, customer_ref=principal.customer_id or principal.principal_id
        )
        return JSONResponse(
            {
                "resolution_id": result.resolution_id,  # type: ignore[attr-defined]
                "qr_type": result.qr_type,  # type: ignore[attr-defined]
                "on_us": result.on_us,  # type: ignore[attr-defined]
                "merchant_name": result.merchant_name,  # type: ignore[attr-defined]
                "merchant_name_alt": result.merchant_name_alt,  # type: ignore[attr-defined]
                "merchant_city": result.merchant_city,  # type: ignore[attr-defined]
                "mcc": result.mcc,  # type: ignore[attr-defined]
                "acquirer": {
                    "scheme": result.acquirer_scheme,  # type: ignore[attr-defined]
                    "acquirer_id": result.acquirer_id,  # type: ignore[attr-defined]
                    "merchant_pan_ref": result.merchant_pan_ref,  # type: ignore[attr-defined]
                },
                "amount_minor": result.amount_minor,  # type: ignore[attr-defined]
                "tip_indicator": result.tip_indicator,  # type: ignore[attr-defined]
                "static_limit_minor": result.static_limit_minor,  # type: ignore[attr-defined]
                "fee_quote": {
                    "fee_minor": result.fee_quote.fee_minor,  # type: ignore[attr-defined]
                    "payer_pays": result.fee_quote.payer_pays,  # type: ignore[attr-defined]
                    "disclosure": result.fee_quote.disclosure,  # type: ignore[attr-defined]
                },
                "expires_at": rfc3339(result.expires_at),  # type: ignore[attr-defined]
            }
        )

    async def pay_qr_resolution(request: Request) -> Response:
        _require_principal(request)
        service = _require_qr()
        body = await _json_body(request)
        amount_minor = body.get("amount_minor")
        if amount_minor is not None and (
            isinstance(amount_minor, bool) or not isinstance(amount_minor, int)
        ):
            raise InvalidRequestError(
                "amount_minor must be an integer (paisa) when present",
                code="validation_failed",
            )
        record = service.pay(
            str(request.path_params["resolution_id"]), amount_minor=amount_minor
        )
        # spec/13: returns the standard spec/02 intent envelope — re-read the
        # full wire view from the kernel port (the qr port record is thin).
        intent_id = record.payment_intent_id  # type: ignore[attr-defined]
        full = deps.payment_intents.get_intent(intent_id)
        if full is not None:
            return JSONResponse(dict(full), status_code=201)
        return JSONResponse(
            {
                "payment_intent_id": intent_id,
                "state": record.state,  # type: ignore[attr-defined]
                "amount_minor": record.amount_minor,  # type: ignore[attr-defined]
                "currency": record.currency,  # type: ignore[attr-defined]
                "method": record.method,  # type: ignore[attr-defined]
            },
            status_code=201,
        )

    async def list_qr_interop_tests(request: Request) -> Response:
        _require_principal(request)
        service = _require_qr()
        counterparty = request.query_params.get("counterparty") or None
        cases = service.list_interop_cases(counterparty)
        return JSONResponse(
            {
                "data": [
                    {
                        "test_case_id": case.test_case_id,
                        "name": case.name,
                        "version": case.version,
                        "direction": case.direction.value,
                        "counterparty_class": case.counterparty_class,
                        "last_result": case.last_result,
                        "last_run_at": (
                            rfc3339(case.last_run_at)
                            if case.last_run_at is not None
                            else None
                        ),
                    }
                    for case in cases
                ]
            }
        )

    async def run_qr_interop_tests(request: Request) -> Response:
        _require_principal(request)
        service = _require_qr()
        body = await _json_body(request)
        counterparty = body.get("counterparty_class")
        if counterparty is not None and not isinstance(counterparty, str):
            raise InvalidRequestError(
                "counterparty_class must be a string when present",
                code="validation_failed",
            )
        result = service.run_interop(counterparty_class=counterparty)
        return JSONResponse(
            {
                "pass_count": result.pass_count,  # type: ignore[attr-defined]
                "fail_count": result.fail_count,  # type: ignore[attr-defined]
                "counterparty_class": result.counterparty_class,  # type: ignore[attr-defined]
            }
        )

    async def qr_dashboard(request: Request) -> Response:
        _require_principal(request)
        service = _require_qr()
        stats = service.dashboard()
        return JSONResponse(
            {
                "static_qr_count": stats.static_qr_count,  # type: ignore[attr-defined]
                "payload_count": stats.payload_count,  # type: ignore[attr-defined]
                "scan_count": stats.scan_count,  # type: ignore[attr-defined]
                "scan_valid_count": stats.scan_valid_count,  # type: ignore[attr-defined]
                "failure_breakdown": dict(stats.failure_breakdown),  # type: ignore[attr-defined]
            }
        )

    # -- inbound connector webhooks (fail-closed) ----------------------------------------------

    async def inbound_webhook(request: Request) -> Response:
        connector_id = str(request.path_params["connector_id"])
        raw = await request.body()
        headers = dict(request.headers)
        pipeline = getattr(deps, "webhook_pipeline", None)
        if pipeline is not None:
            # CERT path: persist-before-verify, DUPLICATE-terminal, no signature
            # oracle. Dual-write to webhook_sink happens inside the kernel handoff.
            from bdpay.connectors.registry import UnknownConnectorError
            from bdpay.gateway.webhook_pipeline_bridge import bind_webhook_connector_id

            source_ip = request.client.host if request.client is not None else "0.0.0.0"
            token = bind_webhook_connector_id(connector_id)
            try:
                try:
                    outcome = await pipeline.ingest(
                        connector_id, headers, raw, source_ip=source_ip
                    )
                except UnknownConnectorError as exc:
                    raise NotFoundError(
                        "no webhook handler is registered for this connector",
                        code="unknown_connector",
                    ) from exc
            finally:
                bind_webhook_connector_id(None)
                del token
            return JSONResponse(outcome.response)

        handler = deps.webhook_registry.get(connector_id)
        if handler is None:
            raise NotFoundError(
                "no webhook handler is registered for this connector",
                code="unknown_connector",
            )
        try:
            verified = handler.verify_signature(headers, raw)
        except Exception:  # noqa: BLE001 - unverifiable means refused
            verified = False
        if not verified:
            raise AuthenticationError(
                "webhook signature verification failed", code="webhook_signature_invalid"
            )
        try:
            result = handler.parse_event(headers, raw)
        except Exception as exc:
            raise InvalidRequestError(
                "webhook payload could not be parsed", code="webhook_payload_invalid"
            ) from exc
        deps.webhook_sink.deliver(connector_id, result)
        return JSONResponse({"received": True})

    # -- system -----------------------------------------------------------------------------

    async def root(request: Request) -> Response:
        # Honest service card for a bare GET / (previously an unrouted 404).
        # Informational only: no secrets, no config beyond mode/env labels.
        cfg = deps.settings
        simulated = cfg.connector_mode != "live"
        body: dict[str, object] = {
            "service": "BD Pay gateway",
            "mode": cfg.connector_mode,
            "environment": cfg.deployment_env or None,
            "simulated_money": simulated,
            "notice": (
                "Simulator / sandbox — no real money moves."
                if simulated
                else "Live connector mode."
            ),
            "api_version": "v1",
            "commit": cfg.build_commit or None,
            "links": {
                "health": "/healthz",
                "ready": "/v1/ready",
                "openapi": "/v1/openapi.json",
                "developer_portal": cfg.portal_url or None,
            },
            "timestamp": rfc3339(deps.clock.now()),
        }
        return JSONResponse(body)

    async def health(request: Request) -> Response:
        # runtime_sha/deploy_id: the health running-sha contract the portfolio
        # freshness probe reconciles against the default-branch HEAD.
        return JSONResponse(
            {
                "status": "ok",
                "timestamp": rfc3339(deps.clock.now()),
                "runtime_sha": deps.settings.runtime_sha,
                "deploy_id": deps.settings.deploy_id,
            }
        )

    async def ready(request: Request) -> Response:
        checks: dict[str, str] = {}
        all_ok = True
        for name, probe in deps.readiness_checks.items():
            try:
                ok = bool(probe())
            except Exception:  # noqa: BLE001 - a throwing probe is a failing probe
                ok = False
            checks[name] = "ok" if ok else "degraded"
            all_ok = all_ok and ok
        # probe body shape per spec/01 §L — exempt from the envelope (errata G-8)
        if all_ok:
            return JSONResponse({"status": "ready", "checks": checks})
        return JSONResponse({"status": "not_ready", "checks": checks}, status_code=503)

    async def metrics(request: Request) -> Response:
        return PlainTextResponse(deps.metrics.export_text())

    # -- registration ---------------------------------------------------------------------------

    app.add_api_route("/v1/payment-intents", create_payment_intent, methods=["POST"])
    app.add_api_route("/v1/payment-intents", list_payment_intents, methods=["GET"])
    app.add_api_route(
        "/v1/payment-intents/{payment_intent_id}", get_payment_intent, methods=["GET"]
    )
    app.add_api_route(
        "/v1/payment-intents/{payment_intent_id}/confirm",
        confirm_payment_intent,
        methods=["POST"],
    )
    app.add_api_route(
        "/v1/payment-intents/{payment_intent_id}/capture",
        capture_payment_intent,
        methods=["POST"],
    )
    app.add_api_route(
        "/v1/payment-intents/{payment_intent_id}/cancel",
        cancel_payment_intent,
        methods=["POST"],
    )
    app.add_api_route(
        "/v1/payment-intents/{payment_intent_id}/attempts",
        list_payment_attempts,
        methods=["GET"],
    )
    app.add_api_route("/v1/refunds", create_refund, methods=["POST"])
    app.add_api_route("/v1/refunds", list_refunds, methods=["GET"])
    app.add_api_route("/v1/refunds/{refund_id}", get_refund, methods=["GET"])
    app.add_api_route("/v1/merchants", create_merchant, methods=["POST"])
    app.add_api_route("/v1/merchants", list_merchants, methods=["GET"])
    app.add_api_route("/v1/merchants/{merchant_id}", get_merchant, methods=["GET"])
    app.add_api_route("/v1/diner/merchants", list_diner_merchants, methods=["GET"])
    app.add_api_route("/v1/customers", create_customer, methods=["POST"])
    app.add_api_route("/v1/customers/{customer_id}", get_customer, methods=["GET"])
    app.add_api_route(
        "/v1/customers/{customer_id}/balance", get_customer_balance, methods=["GET"]
    )
    app.add_api_route(
        "/v1/auth/operator/totp/verify", operator_totp_verify, methods=["POST"]
    )
    app.add_api_route("/v1/webhook-endpoints", create_webhook_endpoint, methods=["POST"])
    app.add_api_route("/v1/webhook-endpoints", list_webhook_endpoints, methods=["GET"])
    app.add_api_route(
        "/v1/webhook-endpoints/{webhook_id}/rotate-secret",
        rotate_webhook_secret,
        methods=["POST"],
    )
    app.add_api_route("/v1/webhook-deliveries", list_webhook_deliveries, methods=["GET"])
    app.add_api_route("/v1/exports/dossiers", create_dossier_export, methods=["POST"])
    app.add_api_route("/v1/exports/dossiers", list_dossier_exports, methods=["GET"])
    app.add_api_route(
        "/v1/exports/dossiers/{dossier_export_id}", get_dossier_export, methods=["GET"]
    )
    app.add_api_route(
        "/v1/exports/dossiers/{dossier_export_id}/download",
        download_dossier_export,
        methods=["GET"],
    )
    app.add_api_route("/v1/payment-links", create_payment_link, methods=["POST"])
    app.add_api_route("/v1/payment-links", list_payment_links, methods=["GET"])
    app.add_api_route("/v1/payment-links/{plink_id}", get_payment_link, methods=["GET"])
    app.add_api_route(
        "/v1/payment-links/{plink_id}/cancel", cancel_payment_link, methods=["POST"]
    )
    app.add_api_route(
        "/v1/public/payment-links/{public_code}", get_public_payment_link, methods=["GET"]
    )
    app.add_api_route(
        "/v1/public/payment-links/{public_code}/intents",
        create_public_checkout_intent,
        methods=["POST"],
    )
    app.add_api_route("/v1/public/status", public_status, methods=["GET"])
    app.add_api_route(
        "/v1/public/certification-matrix", public_certification_matrix, methods=["GET"]
    )
    app.add_api_route(
        "/v1/public/badges/{connector_id}.svg", public_badge, methods=["GET"]
    )
    app.add_api_route("/v1/dashboards/tcsa", get_tcsa_dashboard, methods=["GET"])
    app.add_api_route(
        "/v1/dashboards/settlement", get_settlement_dashboard, methods=["GET"]
    )
    app.add_api_route(
        "/v1/dashboards/connectors", get_connectors_dashboard, methods=["GET"]
    )
    app.add_api_route("/v1/dashboards/aml", get_aml_dashboard, methods=["GET"])
    app.add_api_route("/v1/dashboards/sla", get_sla_dashboard, methods=["GET"])
    app.add_api_route(
        "/v1/ledger/journal-entries", list_ledger_journal_entries, methods=["GET"]
    )
    app.add_api_route(
        "/v1/ledger/chain-entries", list_ledger_chain_entries, methods=["GET"]
    )
    app.add_api_route(
        "/v1/ledger/chain-verify-status",
        get_ledger_chain_verify_status,
        methods=["GET"],
    )
    app.add_api_route("/v1/sandbox/signups", create_sandbox_signup, methods=["POST"])
    app.add_api_route(
        "/v1/sandbox/signups/{sbxs_id}/verify", verify_sandbox_signup, methods=["POST"]
    )
    app.add_api_route(
        "/v1/sandbox/signups/{sbxs_id}", get_sandbox_signup, methods=["GET"]
    )
    app.add_api_route(
        "/v1/sandbox/demo/fullflow-seed",
        sandbox_demo_fullflow_seed,
        methods=["POST"],
    )
    app.add_api_route(
        "/v1/sandbox/demo/diner-pay",
        sandbox_demo_diner_pay,
        methods=["POST"],
    )
    app.add_api_route(
        "/v1/sandbox/demo/fullflow",
        sandbox_demo_fullflow,
        methods=["GET"],
    )
    # Developer-portal merchant read + management surface (spec/15 §Developer
    # portal + spec/01 §B): real merchant-scoped endpoints the portal renders
    # from. The catalogue read is GET; create/rotate/revoke are mutating
    # merchant-key-only routes (apikey:write). Secret material crosses the
    # boundary EXACTLY ONCE, on create/rotate only.
    app.add_api_route("/v1/api-keys", list_api_keys, methods=["GET"])
    app.add_api_route("/v1/api-keys", create_api_key, methods=["POST"])
    app.add_api_route(
        "/v1/api-keys/{api_key_id}/rotate", rotate_api_key, methods=["POST"]
    )
    app.add_api_route(
        "/v1/api-keys/{api_key_id}/revoke", revoke_api_key, methods=["POST"]
    )
    app.add_api_route(
        "/v1/dashboards/merchant", merchant_dashboard, methods=["GET"]
    )
    # Bangla QR (spec/13). Order matters: the literal /dynamic and the render
    # suffix paths register before the bare {merchant_qr_id} matcher.
    app.add_api_route(
        "/v1/merchants/{merchant_id}/qr-codes", issue_static_qr, methods=["POST"]
    )
    app.add_api_route(
        "/v1/merchants/{merchant_id}/qr-codes", list_merchant_qrs, methods=["GET"]
    )
    app.add_api_route("/v1/qr-codes/dynamic", issue_dynamic_qr, methods=["POST"])
    app.add_api_route(
        "/v1/qr-codes/{merchant_qr_id}/render.png", render_qr_png, methods=["GET"]
    )
    app.add_api_route(
        "/v1/qr-codes/{merchant_qr_id}/render.svg", render_qr_svg, methods=["GET"]
    )
    app.add_api_route(
        "/v1/qr-codes/{merchant_qr_id}/kit.pdf", render_qr_kit_pdf, methods=["GET"]
    )
    app.add_api_route(
        "/v1/qr-codes/{merchant_qr_id}", get_merchant_qr, methods=["GET"]
    )
    app.add_api_route(
        "/v1/qr-codes/{merchant_qr_id}/suspend", suspend_merchant_qr, methods=["POST"]
    )
    app.add_api_route(
        "/v1/qr-codes/{merchant_qr_id}/reactivate",
        reactivate_merchant_qr,
        methods=["POST"],
    )
    app.add_api_route(
        "/v1/qr-codes/{merchant_qr_id}/revoke", revoke_merchant_qr, methods=["POST"]
    )
    app.add_api_route("/v1/qr/resolve", resolve_qr, methods=["POST"])
    app.add_api_route(
        "/v1/qr/resolutions/{resolution_id}/pay", pay_qr_resolution, methods=["POST"]
    )
    app.add_api_route("/v1/qr/interop-tests", list_qr_interop_tests, methods=["GET"])
    app.add_api_route(
        "/v1/qr/interop-tests/run", run_qr_interop_tests, methods=["POST"]
    )
    app.add_api_route("/v1/qr/dashboard", qr_dashboard, methods=["GET"])
    app.add_api_route("/v1/webhooks/{connector_id}", inbound_webhook, methods=["POST"])
    app.add_api_route("/", root, methods=["GET"])
    app.add_api_route("/v1/health", health, methods=["GET"])
    app.add_api_route("/healthz", health, methods=["GET"])
    app.add_api_route("/v1/ready", ready, methods=["GET"])
    app.add_api_route("/readyz", ready, methods=["GET"])
    app.add_api_route("/metrics", metrics, methods=["GET"])

    if deps.settings.cors_allowed_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(deps.settings.cors_allowed_origins),
            allow_credentials=True,
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
            allow_headers=[
                "Authorization",
                "Content-Type",
                IDEMPOTENCY_HEADER,
                "X-BDPay-Timestamp",
                "X-BDPay-Signature",
            ],
        )
    app.add_middleware(GatewayMiddleware, deps=deps)
    return app
