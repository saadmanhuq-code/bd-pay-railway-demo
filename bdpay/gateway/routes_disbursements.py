"""Spec/17 bulk disbursement routes.

Named launch consumers: Talent payroll and Khep payouts. This module is a
bounded installer so the route surface can be composed with the existing
gateway middleware, idempotency, and RBAC policy data without changing the
gateway spine.
"""

from __future__ import annotations

import hmac
import re
from datetime import date
from typing import Protocol, runtime_checkable

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

import bdpay.gateway.apikeys as apikeys
from bdpay.gateway.app import IDEMPOTENCY_HEADER, _json_body, _require_principal, _validate
from bdpay.gateway.jwt_hs256 import JwtError, decode_jwt
from bdpay.gateway.pagination import build_page, parse_pagination
from bdpay.gateway.policy import RoutePolicy
from bdpay.platform.errors import AuthenticationError, InternalError, NotFoundError

__all__ = [
    "DisbursementPort",
    "disbursement_route_policies",
    "install_disbursement_routes",
]

_DISBURSEMENT_SCOPES = frozenset({"disbursements:read", "disbursements:write"})
apikeys.API_SCOPES = frozenset((*apikeys.API_SCOPES, *_DISBURSEMENT_SCOPES))
_STEP_UP_HEADER = "X-BDPay-Step-Up"
_STEP_UP_PURPOSE = "disbursement_batch.submit"
_STEP_UP_REQUIRED_CLAIMS = (
    "sub",
    "iat",
    "exp",
    "jti",
    "purpose",
    "merchant_id",
    "batch_id",
    "idempotency_key",
)


def _p(path_template: str) -> re.Pattern[str]:
    pattern = re.sub(r"\{[a-z_]+\}", r"[^/]+", path_template)
    return re.compile(f"^{pattern}$")


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class DisbursementItemCreate(_StrictModel):
    beneficiary_ref: str = Field(min_length=1)
    beneficiary_name: str = Field(min_length=1)
    rail: str = Field(min_length=1)
    amount_minor: int | str
    purpose_code: str = Field(min_length=1)
    item_ref: str = Field(min_length=1)


class DisbursementBatchCreate(_StrictModel):
    client_batch_ref: str = Field(min_length=1)
    items: list[DisbursementItemCreate] = Field(min_length=1)


class DisbursementBatchDispatch(_StrictModel):
    session: str = Field(min_length=1)
    effective_date: date


def _raise_totp_required() -> None:
    raise AuthenticationError("maker TOTP verification is required", code="totp_required")


def _verify_submit_step_up(
    request: Request,
    *,
    merchant_id: str,
    maker_user_id: str,
    batch_id: str,
) -> None:
    token = (request.headers.get(_STEP_UP_HEADER) or "").strip()
    if not token:
        _raise_totp_required()
    settings = getattr(request.app.state, "gateway_settings", None)
    clock = getattr(request.app.state, "gateway_clock", None)
    if settings is None or clock is None:
        raise InternalError(
            "gateway step-up verifier is not wired",
            code="gateway_step_up_unavailable",
        )
    secrets = [settings.jwt_secret]
    if settings.jwt_previous_secret:
        secrets.append(settings.jwt_previous_secret)
    try:
        claims = decode_jwt(
            token,
            secrets,
            now=clock.now(),
            required_claims=_STEP_UP_REQUIRED_CLAIMS,
        )
    except JwtError as exc:
        raise AuthenticationError(
            "maker TOTP verification is required", code="totp_required"
        ) from exc

    expected = {
        "sub": maker_user_id,
        "purpose": _STEP_UP_PURPOSE,
        "merchant_id": merchant_id,
        "batch_id": batch_id,
        "idempotency_key": request.headers.get(IDEMPOTENCY_HEADER, ""),
    }
    for name, expected_value in expected.items():
        actual_value = str(claims.get(name, ""))
        if not hmac.compare_digest(actual_value, expected_value):
            _raise_totp_required()


@runtime_checkable
class DisbursementPort(Protocol):
    def create_disbursement_batch(
        self,
        payload,
        *,
        merchant_id: str,
        maker_user_id: str,
        idempotency_key: str,
        clock,
    ): ...

    def get_disbursement_batch(
        self, batch_id: str, *, merchant_id: str | None = None
    ): ...

    def list_disbursement_items(
        self,
        batch_id: str,
        *,
        status: str | None,
        limit: int,
        cursor: str | None,
        merchant_id: str | None = None,
    ): ...

    def submit_disbursement_batch(
        self,
        batch_id: str,
        *,
        merchant_id: str,
        maker_user_id: str,
        totp_verified: bool,
        clock,
    ): ...

    def cancel_disbursement_batch(
        self, batch_id: str, *, merchant_id: str, actor_id: str, clock
    ): ...

    async def dispatch_disbursement_batch(
        self,
        batch_id: str,
        *,
        operator_id: str,
        session: str,
        effective_date: date,
        clock,
    ): ...


def disbursement_route_policies() -> tuple[RoutePolicy, ...]:
    """Policy rows consumed by the existing gateway middleware."""
    return (
        RoutePolicy(
            name="disbursement_batches.create",
            method="POST",
            pattern=_p("/v1/disbursement-batches"),
            route_group="disbursement_write",
            merchant_scope="disbursements:write",
            idempotency_required=True,
        ),
        RoutePolicy(
            name="disbursement_batches.get",
            method="GET",
            pattern=_p("/v1/disbursement-batches/{batch_id}"),
            route_group="payment_read",
            merchant_scope="disbursements:read",
        ),
        RoutePolicy(
            name="disbursement_batches.items",
            method="GET",
            pattern=_p("/v1/disbursement-batches/{batch_id}/items"),
            route_group="payment_read",
            merchant_scope="disbursements:read",
        ),
        RoutePolicy(
            name="disbursement_batches.submit",
            method="POST",
            pattern=_p("/v1/disbursement-batches/{batch_id}/submit"),
            route_group="disbursement_write",
            merchant_scope="disbursements:write",
            idempotency_required=True,
        ),
        RoutePolicy(
            name="disbursement_batches.cancel",
            method="POST",
            pattern=_p("/v1/disbursement-batches/{batch_id}/cancel"),
            route_group="disbursement_write",
            merchant_scope="disbursements:write",
            idempotency_required=True,
        ),
        RoutePolicy(
            name="disbursement_batches.dispatch",
            method="POST",
            pattern=_p("/v1/disbursement-batches/{batch_id}/dispatch"),
            route_group="disbursement_write",
            operator_roles=frozenset({"PAYMENT_OPS", "FINANCE", "TREASURY"}),
            idempotency_required=True,
        ),
    )


def install_disbursement_routes(app: FastAPI) -> None:
    """Install the spec/17 disbursement route surface on an existing app."""

    def service(request: Request) -> DisbursementPort:
        port = getattr(request.app.state, "disbursements", None)
        if port is None:
            raise InternalError(
                "the disbursement surface is not wired in this deployment",
                code="disbursement_surface_unavailable",
            )
        return port

    @app.post("/v1/disbursement-batches")
    async def create_batch(request: Request):
        principal = _require_principal(request)
        body = _validate(DisbursementBatchCreate, await _json_body(request))
        maker_user_id = request.headers.get("X-BDPay-Merchant-User-Id") or principal.principal_id
        result = service(request).create_disbursement_batch(
            body.model_dump(),
            merchant_id=principal.merchant_id or "",
            maker_user_id=maker_user_id,
            idempotency_key=request.headers.get(IDEMPOTENCY_HEADER, ""),
            clock=request.app.state.gateway_clock,
        )
        return JSONResponse(dict(result), status_code=201)

    @app.get("/v1/disbursement-batches/{batch_id}")
    async def get_batch(batch_id: str, request: Request):
        principal = _require_principal(request)
        result = service(request).get_disbursement_batch(
            batch_id,
            merchant_id=principal.merchant_id or "",
        )
        if result is None:
            raise NotFoundError("disbursement batch not found", code="batch_not_found")
        return JSONResponse(dict(result))

    @app.get("/v1/disbursement-batches/{batch_id}/items")
    async def list_items(batch_id: str, request: Request):
        principal = _require_principal(request)
        limit, cursor = parse_pagination(request.query_params)
        status = request.query_params.get("status") or None
        items, next_cursor = service(request).list_disbursement_items(
            batch_id,
            status=status,
            limit=limit,
            cursor=cursor,
            merchant_id=principal.merchant_id or "",
        )
        return JSONResponse(build_page(items, next_cursor))

    @app.post("/v1/disbursement-batches/{batch_id}/submit")
    async def submit_batch(batch_id: str, request: Request):
        principal = _require_principal(request)
        merchant_id = principal.merchant_id or ""
        maker_user_id = request.headers.get("X-BDPay-Merchant-User-Id") or principal.principal_id
        _verify_submit_step_up(
            request,
            merchant_id=merchant_id,
            maker_user_id=maker_user_id,
            batch_id=batch_id,
        )
        result = service(request).submit_disbursement_batch(
            batch_id,
            merchant_id=merchant_id,
            maker_user_id=maker_user_id,
            totp_verified=True,
            clock=request.app.state.gateway_clock,
        )
        return JSONResponse(dict(result))

    @app.post("/v1/disbursement-batches/{batch_id}/cancel")
    async def cancel_batch(batch_id: str, request: Request):
        principal = _require_principal(request)
        actor_id = request.headers.get("X-BDPay-Merchant-User-Id") or principal.principal_id
        result = service(request).cancel_disbursement_batch(
            batch_id,
            merchant_id=principal.merchant_id or "",
            actor_id=actor_id,
            clock=request.app.state.gateway_clock,
        )
        return JSONResponse(dict(result))

    @app.post("/v1/disbursement-batches/{batch_id}/dispatch")
    async def dispatch_batch(batch_id: str, request: Request):
        principal = _require_principal(request)
        body = _validate(DisbursementBatchDispatch, await _json_body(request))
        operator_id = principal.operator_id or principal.principal_id
        result = await service(request).dispatch_disbursement_batch(
            batch_id,
            operator_id=operator_id,
            session=body.session,
            effective_date=body.effective_date,
            clock=request.app.state.gateway_clock,
        )
        return JSONResponse(dict(result))

    app.state.gateway_clock = getattr(app.state, "gateway_clock", None)
