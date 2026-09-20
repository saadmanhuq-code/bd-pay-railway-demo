"""HTTP transport for the vault sub-process (spec/14 §API surface — 12 endpoints).

Plane A (public hosted-fields intake), Plane B (internal, channel-authed +
allowlisted, Idempotency-Key on every mutating endpoint), Plane C
(maintenance). Every response uses the conventions §4 envelope on errors and
carries ``X-BDPay-Api-Version: 1``. All error messages are PII-free by the
platform error hierarchy (redaction applied at construction).
"""

from __future__ import annotations

import hashlib
import inspect
import json
from collections.abc import Awaitable, Callable
from datetime import datetime

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from bdpay.platform.canonical import sha256_canonical
from bdpay.platform.errors import (
    BDPayError,
    IdempotencyConflictError,
    InvalidRequestError,
)
from bdpay.vault.channel import (
    CALLER_HEADER,
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    SharedSecretChannelAuth,
    authorization_denied,
)
from bdpay.vault.records import IdempotencyRecord, KeyKind
from bdpay.vault.service import VaultService, rfc3339
from bdpay.vault.wiring import VaultRuntime

__all__ = ["API_VERSION_HEADER", "create_app"]

API_VERSION_HEADER = ("X-BDPay-Api-Version", "1")
_LOCAL_HOSTS = frozenset({"127.0.0.1", "::1"})


def _jsonable(value: object) -> object:
    if isinstance(value, datetime):
        return rfc3339(value)
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonable(item) for item in value]
    return value


def _parse_json(body: bytes) -> dict:
    if not body:
        return {}
    try:
        parsed = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise InvalidRequestError("request body must be JSON", code="body_not_json") from exc
    if not isinstance(parsed, dict):
        raise InvalidRequestError("request body must be a JSON object", code="body_not_json")
    return parsed


def create_app(runtime: VaultRuntime) -> FastAPI:
    service: VaultService = runtime.service
    channel: SharedSecretChannelAuth = runtime.channel
    clock = runtime.clock
    app = FastAPI(title="bd-pay-vault", docs_url=None, redoc_url=None, openapi_url=None)
    request_counter = {"n": 0}

    def request_id() -> str:
        request_counter["n"] += 1
        return "req_" + sha256_canonical({"n": request_counter["n"], "at": clock.now()})[:24]

    @app.middleware("http")
    async def version_header(request: Request, call_next):
        response = await call_next(request)
        response.headers[API_VERSION_HEADER[0]] = API_VERSION_HEADER[1]
        return response

    @app.exception_handler(BDPayError)
    async def envelope_handler(_request: Request, exc: BDPayError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.http_status,
            content=exc.to_envelope(request_id()),
            headers={API_VERSION_HEADER[0]: API_VERSION_HEADER[1]},
        )

    async def authed(request: Request, operation: str | None) -> tuple[str, bytes]:
        """Channel auth (shared secret, errata LB7) + default-deny allowlist."""
        body = await request.body()
        caller = channel.verify(
            caller=request.headers.get(CALLER_HEADER),
            method=request.method,
            path=request.url.path,
            body=body,
            ts=request.headers.get(TIMESTAMP_HEADER),
            signature=request.headers.get(SIGNATURE_HEADER),
        )
        if operation is not None:
            service.authorize(caller, operation)
        return caller, body

    async def idempotent(
        request: Request,
        caller: str,
        body: bytes,
        status_code: int,
        produce: Callable[[], object | Awaitable[object]],
    ) -> JSONResponse:
        """Plane-B mutating endpoints: durable Idempotency-Key in vault_db
        (24 h), replay returns the stored response, mismatch is a conflict."""
        key = request.headers.get("Idempotency-Key")
        if not key:
            raise InvalidRequestError(
                "Idempotency-Key header is required", code="idempotency_key_required"
            )
        request_hash = sha256_canonical(
            {
                "body_sha256": hashlib.sha256(body).hexdigest(),
                "method": request.method,
                "path": request.url.path,
            }
        )
        existing = service.idempotency.get(key, caller)
        if existing is not None:
            if existing.request_hash != request_hash:
                raise IdempotencyConflictError(
                    "Idempotency-Key replayed with different parameters",
                    code="idempotency_conflict",
                )
            return JSONResponse(
                status_code=existing.response_status,
                content=existing.response_body,
                headers={"X-BDPay-Idempotent-Replay": "true"},
            )
        result = produce()
        if inspect.isawaitable(result):
            result = await result
        payload = _jsonable(result)
        service.idempotency.save(
            IdempotencyRecord(
                idempotency_key=key,
                caller_san=caller,
                request_hash=request_hash,
                response_status=status_code,
                response_body=payload,  # type: ignore[arg-type]
                created_at=clock.now(),
            )
        )
        return JSONResponse(status_code=status_code, content=payload)

    # ---------------------------------------------------------------- Plane A

    @app.post("/v1/card-intake/{intake_session_id}")
    async def card_intake(intake_session_id: str, request: Request) -> JSONResponse:
        fields = _parse_json(await request.body())
        client_ip = request.client.host if request.client else "unknown"
        result = service.card_intake(intake_session_id, fields, client_ip=client_ip)
        return JSONResponse(status_code=200, content=_jsonable(result))

    # ---------------------------------------------------------------- Plane B

    @app.post("/vault/v1/card-intake-sessions")
    async def create_intake_session(request: Request) -> JSONResponse:
        caller, body = await authed(request, "intake_session.create")
        payload = _parse_json(body)

        def produce() -> dict:
            return service.create_intake_session(
                caller_san=caller,
                merchant_id=str(payload.get("merchant_id", "")),
                purpose=str(payload.get("purpose", "")),
                payment_intent_id=payload.get("payment_intent_id"),
                customer_id=payload.get("customer_id"),
                allowed_origins=payload.get("allowed_origins"),
                cvv_required=bool(payload.get("cvv_required", True)),
            )

        return await idempotent(request, caller, body, 201, produce)

    @app.post("/vault/v1/tokens")
    async def import_token(request: Request) -> JSONResponse:
        caller, body = await authed(request, "token.import")
        payload = _parse_json(body)
        return await idempotent(
            request, caller, body, 201,
            lambda: service.import_token(caller_san=caller, body=payload),
        )

    @app.get("/vault/v1/tokens/{token}")
    async def token_metadata(token: str, request: Request) -> JSONResponse:
        caller, _body = await authed(request, "token.read_metadata")
        return JSONResponse(
            status_code=200,
            content=_jsonable(service.token_metadata(caller_san=caller, token=token)),
        )

    @app.post("/vault/v1/tokens/{token}/retire")
    async def retire_token(token: str, request: Request) -> JSONResponse:
        caller, body = await authed(request, "token.retire")
        payload = _parse_json(body)
        return await idempotent(
            request,
            caller,
            body,
            200,
            lambda: service.retire_token(
                caller_san=caller,
                token=token,
                reason=str(payload.get("reason", "")),
                approval_request_id=payload.get("approval_request_id"),
            ),
        )

    @app.post("/vault/v1/detokenize-egress")
    async def detokenize_egress(request: Request) -> JSONResponse:
        caller, body = await authed(request, None)  # guard 1 runs inside the service
        payload = _parse_json(body)
        return await idempotent(
            request,
            caller,
            body,
            200,
            lambda: service.detokenize_egress(caller_san=caller, request=payload),
        )

    # ----------------------------------------------------- Plane B: key admin

    @app.post("/vault/v1/keys/ceremonies")
    async def open_ceremony(request: Request) -> JSONResponse:
        caller, body = await authed(request, "key.ceremony.create")
        payload = _parse_json(body)

        def produce() -> dict:
            try:
                kind = KeyKind(str(payload.get("kind", "")))
            except ValueError as exc:
                raise InvalidRequestError("unknown key kind", code="key_kind_invalid") from exc
            record = service.keys.open_ceremony(
                kind=kind,
                target_key_name=str(payload.get("target_key_name", "")),
                reason=str(payload.get("reason", "")),
                custodian_ids=list(payload.get("custodian_ids") or []),
                quorum=int(payload.get("quorum", 0)),
                approval_request_id=str(payload.get("approval_request_id", "")),
                actor=caller,
            )
            return {
                "ceremony_id": record.vcer_id,
                "status": record.status.value,
                "ttl_expires_at": rfc3339(record.ttl_expires_at),
            }

        return await idempotent(request, caller, body, 201, produce)

    @app.post("/vault/v1/keys/ceremonies/{ceremony_id}/custodian-share")
    async def custodian_share(ceremony_id: str, request: Request) -> JSONResponse:
        caller, body = await authed(request, "key.ceremony.share")
        payload = _parse_json(body)

        def produce() -> dict:
            record = service.keys.attest_share(
                ceremony_id=ceremony_id,
                custodian_id=str(payload.get("custodian_id", "")),
                share_kcv=str(payload.get("share_kcv", "")),
                attestation=str(payload.get("attestation", "")),
                operator_identity=str(payload.get("operator_identity", "")),
            )
            return {
                "ceremony_id": record.vcer_id,
                "status": record.status.value,
                "shares": len(record.shares),
                "quorum": record.quorum,
            }

        return await idempotent(request, caller, body, 200, produce)

    @app.post("/vault/v1/keys/{key_id}/rotate")
    async def rotate_key(key_id: str, request: Request) -> JSONResponse:
        caller, body = await authed(request, "key.rotate")
        payload = _parse_json(body)
        return await idempotent(
            request,
            caller,
            body,
            202,
            lambda: service.keys.rotate_key(
                key_id,
                ceremony_id=str(payload.get("ceremony_id", "")),
                approval_request_id=str(payload.get("approval_request_id", "")),
                actor=caller,
            ),
        )

    @app.get("/vault/v1/keys")
    async def list_keys(request: Request) -> JSONResponse:
        await authed(request, "key.read_metadata")
        return JSONResponse(status_code=200, content={"data": _jsonable(service.keys.list_keys())})

    @app.post("/vault/v1/keys/{key_id}/compromise")
    async def compromise_key(key_id: str, request: Request) -> JSONResponse:
        caller, body = await authed(request, "key.compromise")
        payload = _parse_json(body)
        return await idempotent(
            request,
            caller,
            body,
            200,
            lambda: service.keys.declare_compromise(
                key_id,
                approval_request_id=str(payload.get("approval_request_id", "")),
                evidence_ref=str(payload.get("evidence_ref", "")),
                declared_by=str(payload.get("declared_by", caller)),
            ),
        )

    # ----------------------------------------------------------------- health

    @app.get("/vault/v1/health")
    async def health(request: Request) -> JSONResponse:
        client_host = request.client.host if request.client else ""
        if client_host not in _LOCAL_HOSTS:
            body = await request.body()
            caller = channel.verify(
                caller=request.headers.get(CALLER_HEADER),
                method=request.method,
                path=request.url.path,
                body=body,
                ts=request.headers.get(TIMESTAMP_HEADER),
                signature=request.headers.get(SIGNATURE_HEADER),
            )
            if not service.known_caller(caller):
                raise authorization_denied("health.read")
        return JSONResponse(status_code=200, content=_jsonable(service.health()))

    # ---------------------------------------------------------------- Plane C

    @app.post("/vault/v1/maintenance/sweep")
    async def maintenance_sweep(request: Request) -> JSONResponse:
        caller, _body = await authed(request, "maintenance.sweep")
        return JSONResponse(
            status_code=200, content=_jsonable(service.maintenance_sweep(actor=caller))
        )

    return app
