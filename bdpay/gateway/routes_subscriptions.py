"""Subscription routes (spec/17 VX3), kept separate for bounded wiring."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol, runtime_checkable

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from bdpay.gateway.app import IDEMPOTENCY_HEADER, _json_body, _require_principal
from bdpay.gateway.pagination import build_page, parse_pagination
from bdpay.gateway.policy import RoutePolicy
from bdpay.platform.bangla import normalize_bengali_digits
from bdpay.platform.clock import Clock
from bdpay.platform.errors import AuthorizationError, InvalidRequestError

__all__ = [
    "SUBSCRIPTION_WEBHOOK_EVENTS",
    "SubscriptionRouteDependencies",
    "add_subscription_routes",
    "subscription_route_policies",
]

SUBSCRIPTION_WEBHOOK_EVENTS: tuple[str, ...] = (
    "subscription.created",
    "subscription.paused",
    "subscription.resumed",
    "subscription.past_due",
    "subscription.reactivated",
    "subscription.cancelled",
    "subscription.completed",
    "subscription_cycle.invoiced",
    "subscription_cycle.paid",
    "subscription_cycle.failed",
    "subscription_cycle.skipped",
    "subscription_cycle.dunning_started",
    "mandate.activated",
    "mandate.suspended",
    "mandate.revoked",
    "mandate.expired",
)


@runtime_checkable
class SubscriptionPort(Protocol):
    def create(
        self,
        merchant_id: str,
        payload: Mapping[str, object],
        *,
        idempotency_key: str,
        clock: Clock,
    ) -> Mapping[str, object]: ...

    def get(self, subscription_id: str, *, merchant_id: str) -> Mapping[str, object]: ...

    def list(
        self, *, merchant_id: str, limit: int, cursor: str | None
    ) -> tuple[Sequence[Mapping[str, object]], str | None]: ...

    def pause(
        self, subscription_id: str, *, merchant_id: str, clock: Clock
    ) -> Mapping[str, object]: ...

    def resume(
        self, subscription_id: str, *, merchant_id: str, clock: Clock
    ) -> Mapping[str, object]: ...

    def cancel(
        self, subscription_id: str, *, merchant_id: str, clock: Clock
    ) -> Mapping[str, object]: ...

    def list_cycles(
        self, subscription_id: str, *, merchant_id: str, limit: int, cursor: str | None
    ) -> tuple[Sequence[Mapping[str, object]], str | None]: ...

    def create_mandate(
        self,
        payload: Mapping[str, object],
        *,
        merchant_id: str,
        idempotency_key: str,
        clock: Clock,
    ) -> Mapping[str, object]: ...


@dataclass(frozen=True)
class SubscriptionRouteDependencies:
    service: SubscriptionPort
    clock: Clock


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class SubscriptionCreate(_StrictModel):
    customer_ref: str = Field(min_length=1)
    plan_amount_minor: int
    currency: Literal["BDT"] = "BDT"
    interval: Literal["WEEKLY", "MONTHLY"]
    anchor_date: str = Field(min_length=10, max_length=10)
    collection: Literal["LINK", "MANDATE"] = "LINK"
    metadata: dict[str, str] = Field(default_factory=dict)

    @field_validator("plan_amount_minor", mode="before")
    @classmethod
    def _amount(cls, value: Any) -> int:
        if isinstance(value, bool):
            raise ValueError("plan_amount_minor must be integer paisa")
        if isinstance(value, int):
            amount = value
        elif isinstance(value, str):
            normalized = normalize_bengali_digits(value).strip()
            if not normalized.isdigit():
                raise ValueError("plan_amount_minor string must contain only digits")
            amount = int(normalized, 10)
        else:
            raise ValueError("plan_amount_minor must be integer paisa")
        if amount <= 0:
            raise ValueError("plan_amount_minor must be > 0")
        return amount


class MandateCreate(_StrictModel):
    subscription_id: str = Field(min_length=1)
    rail: Literal["CARD", "BKASH", "NAGAD"]
    instrument_ref: str = Field(min_length=1)


def subscription_route_policies() -> tuple[RoutePolicy, ...]:
    """RoutePolicy rows for callers that wire this bounded module."""
    from bdpay.gateway.policy import _p

    return (
        RoutePolicy(
            name="subscriptions.create",
            method="POST",
            pattern=_p("/v1/subscriptions"),
            route_group="payment_write",
            merchant_scope="subscriptions:write",
            idempotency_required=True,
        ),
        RoutePolicy(
            name="subscriptions.list",
            method="GET",
            pattern=_p("/v1/subscriptions"),
            route_group="payment_read",
            merchant_scope="subscriptions:read",
        ),
        RoutePolicy(
            name="subscriptions.get",
            method="GET",
            pattern=_p("/v1/subscriptions/{subscription_id}"),
            route_group="payment_read",
            merchant_scope="subscriptions:read",
        ),
        RoutePolicy(
            name="subscriptions.pause",
            method="POST",
            pattern=_p("/v1/subscriptions/{subscription_id}/pause"),
            route_group="payment_write",
            merchant_scope="subscriptions:write",
            idempotency_required=True,
        ),
        RoutePolicy(
            name="subscriptions.resume",
            method="POST",
            pattern=_p("/v1/subscriptions/{subscription_id}/resume"),
            route_group="payment_write",
            merchant_scope="subscriptions:write",
            idempotency_required=True,
        ),
        RoutePolicy(
            name="subscriptions.cancel",
            method="POST",
            pattern=_p("/v1/subscriptions/{subscription_id}/cancel"),
            route_group="payment_write",
            merchant_scope="subscriptions:write",
            idempotency_required=True,
        ),
        RoutePolicy(
            name="subscriptions.cycles",
            method="GET",
            pattern=_p("/v1/subscriptions/{subscription_id}/cycles"),
            route_group="payment_read",
            merchant_scope="subscriptions:read",
        ),
        RoutePolicy(
            name="mandates.create",
            method="POST",
            pattern=_p("/v1/mandates"),
            route_group="payment_write",
            merchant_scope="subscriptions:write",
            idempotency_required=True,
        ),
    )


def add_subscription_routes(app: FastAPI, deps: SubscriptionRouteDependencies) -> None:
    """Register the spec/17 route surface on an existing FastAPI app."""

    def _merchant_id(request: Request) -> str:
        principal = _require_principal(request)
        if principal.kind != "merchant_key" or principal.merchant_id is None:
            raise AuthorizationError(
                "subscriptions are a merchant API surface", code="permission_denied"
            )
        return principal.merchant_id

    async def create_subscription(request: Request) -> JSONResponse:
        body = _validate(SubscriptionCreate, await _json_body(request))
        result = deps.service.create(
            _merchant_id(request),
            body.model_dump(),
            idempotency_key=request.headers.get(IDEMPOTENCY_HEADER, ""),
            clock=deps.clock,
        )
        return JSONResponse(dict(result), status_code=201)

    async def list_subscriptions(request: Request) -> JSONResponse:
        limit, cursor = parse_pagination(request.query_params)
        items, next_cursor = deps.service.list(
            merchant_id=_merchant_id(request), limit=limit, cursor=cursor
        )
        return JSONResponse(build_page(items, next_cursor))

    async def get_subscription(request: Request) -> JSONResponse:
        result = deps.service.get(
            str(request.path_params["subscription_id"]),
            merchant_id=_merchant_id(request),
        )
        return JSONResponse(dict(result))

    async def pause_subscription(request: Request) -> JSONResponse:
        result = deps.service.pause(
            str(request.path_params["subscription_id"]),
            merchant_id=_merchant_id(request),
            clock=deps.clock,
        )
        return JSONResponse(dict(result))

    async def resume_subscription(request: Request) -> JSONResponse:
        result = deps.service.resume(
            str(request.path_params["subscription_id"]),
            merchant_id=_merchant_id(request),
            clock=deps.clock,
        )
        return JSONResponse(dict(result))

    async def cancel_subscription(request: Request) -> JSONResponse:
        result = deps.service.cancel(
            str(request.path_params["subscription_id"]),
            merchant_id=_merchant_id(request),
            clock=deps.clock,
        )
        return JSONResponse(dict(result))

    async def list_cycles(request: Request) -> JSONResponse:
        limit, cursor = parse_pagination(request.query_params)
        items, next_cursor = deps.service.list_cycles(
            str(request.path_params["subscription_id"]),
            merchant_id=_merchant_id(request),
            limit=limit,
            cursor=cursor,
        )
        return JSONResponse(build_page(items, next_cursor))

    async def create_mandate(request: Request) -> JSONResponse:
        body = _validate(MandateCreate, await _json_body(request))
        result = deps.service.create_mandate(
            body.model_dump(),
            merchant_id=_merchant_id(request),
            idempotency_key=request.headers.get(IDEMPOTENCY_HEADER, ""),
            clock=deps.clock,
        )
        return JSONResponse(dict(result), status_code=202)

    app.add_api_route("/v1/subscriptions", create_subscription, methods=["POST"])
    app.add_api_route("/v1/subscriptions", list_subscriptions, methods=["GET"])
    app.add_api_route("/v1/subscriptions/{subscription_id}", get_subscription, methods=["GET"])
    app.add_api_route(
        "/v1/subscriptions/{subscription_id}/pause", pause_subscription, methods=["POST"]
    )
    app.add_api_route(
        "/v1/subscriptions/{subscription_id}/resume", resume_subscription, methods=["POST"]
    )
    app.add_api_route(
        "/v1/subscriptions/{subscription_id}/cancel", cancel_subscription, methods=["POST"]
    )
    app.add_api_route(
        "/v1/subscriptions/{subscription_id}/cycles", list_cycles, methods=["GET"]
    )
    app.add_api_route("/v1/mandates", create_mandate, methods=["POST"])


def _validate[ModelT: BaseModel](model_cls: type[ModelT], data: dict) -> ModelT:
    try:
        return model_cls.model_validate(data)
    except ValidationError as exc:
        parts = []
        for err in exc.errors(include_input=False, include_url=False):
            loc = ".".join(str(piece) for piece in err["loc"]) or "body"
            parts.append(f"{loc}: {err['msg']}")
        raise InvalidRequestError(
            "; ".join(parts[:5]) or "request validation failed", code="validation_failed"
        ) from exc
