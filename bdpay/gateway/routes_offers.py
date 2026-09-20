"""Offer routes (spec/18 §API surface), kept separate for bounded wiring.

Registers the merchant offer-management surface and the consumer discovery
endpoint. The spec/18 EXTENSION to ``POST /v1/payment-intents`` (optional
``offer_id`` + ``gross_amount_minor``) is NOT registered here — that route
is owned by :mod:`bdpay.gateway.app`; the wiring agent plugs
:class:`bdpay.kernel.offers.seam.OfferPaymentSeam.create_intent_with_offer`
into the existing create handler (zero-offer invariant: with ``offer_id``
absent the handler path is byte-identical to today).

Auth posture (spec/18): merchant endpoints require a merchant API key scoped
to the caller's own ``merchant_id``; consumer discovery requires a customer
JWT and carries a dedicated rate bucket (abuse surface). Nothing here is
operator-only — the subsidy config does not travel through this surface.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
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
    "ELIGIBLE_BUCKET_LIMIT_PER_MINUTE",
    "OFFER_WEBHOOK_EVENTS",
    "OfferRouteDependencies",
    "add_offer_routes",
    "offer_route_policies",
]

#: spec/18 §Events produced — the offer-engine event catalogue, explicit.
OFFER_WEBHOOK_EVENTS: tuple[str, ...] = (
    "offer.activated",
    "offer.paused",
    "offer.resumed",
    "offer.exhausted",
    "offer.expired",
    "offer.archived",
    "offer_redemption.reserved",
    "offer_redemption.applied",
    "offer_redemption.released",
    "offer_redemption.reversed",
    "offer_redemption.settled",
)

#: Dedicated abuse bucket for the discovery endpoint (spec/18: rate-limited
#: per Part III gateway defaults PLUS a dedicated bucket).
ELIGIBLE_BUCKET_LIMIT_PER_MINUTE = 120


@runtime_checkable
class OfferEnginePort(Protocol):
    """The slice of :class:`bdpay.kernel.offers.service.OfferService` routes use."""

    def create_offer(
        self,
        merchant_id: str,
        payload: Mapping[str, object],
        *,
        idempotency_key: str,
        conn: Any | None = None,
    ) -> Mapping[str, object]: ...

    def activate_offer(
        self, offer_id: str, merchant_id: str, *, conn: Any | None = None
    ) -> Mapping[str, object]: ...

    def pause_offer(
        self, offer_id: str, merchant_id: str, *, conn: Any | None = None
    ) -> Mapping[str, object]: ...

    def resume_offer(
        self, offer_id: str, merchant_id: str, *, conn: Any | None = None
    ) -> Mapping[str, object]: ...

    def archive_offer(
        self, offer_id: str, merchant_id: str, *, conn: Any | None = None
    ) -> Mapping[str, object]: ...

    def get_offer(
        self, offer_id: str, merchant_id: str, *, conn: Any | None = None
    ) -> Mapping[str, object]: ...

    def list_offers(
        self,
        merchant_id: str,
        *,
        state: str | None = None,
        limit: int = 20,
        cursor: str | None = None,
        conn: Any | None = None,
    ) -> tuple[Sequence[Mapping[str, object]], str | None]: ...

    def eligible_offers(
        self,
        merchant_id: str,
        *,
        at: datetime | None = None,
        method: str | None = None,
        example_amount_minor: int | None = None,
        conn: Any | None = None,
    ) -> Sequence[Mapping[str, object]]: ...


@dataclass(frozen=True)
class OfferRouteDependencies:
    service: OfferEnginePort
    clock: Clock


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class OfferWindowBody(_StrictModel):
    days: list[str] = Field(min_length=1, max_length=7)
    start_local: str = Field(min_length=5, max_length=5)
    end_local: str = Field(min_length=5, max_length=5)


class OfferCreate(_StrictModel):
    """spec/18 POST /v1/offers body. Semantic validation (windows overlap,
    caps, merchant connector set, D2 BOGO refusal) lives in the kernel
    service — this model pins shape and integer-paisa typing only."""

    kind: Literal["PERCENT_OFF", "BOGO"]
    percent_bps: int | None = None
    bogo_config: dict[str, object] | None = None
    title: str = Field(min_length=1, max_length=200)
    title_bn: str = Field(min_length=1, max_length=200)
    windows: list[OfferWindowBody] = Field(min_length=1, max_length=7)
    valid_from: str = Field(min_length=1)
    valid_until: str = Field(min_length=1)
    min_spend_minor: int | None = None
    max_discount_minor: int | None = None
    cap_per_day: int | None = None
    cap_total: int | None = None
    cap_per_customer: int | None = None
    allowed_methods: list[str] = Field(min_length=1)
    cap_recredit_on_refund: bool = True

    @field_validator(
        "percent_bps",
        "min_spend_minor",
        "max_discount_minor",
        "cap_per_day",
        "cap_total",
        "cap_per_customer",
        mode="before",
    )
    @classmethod
    def _integer_only(cls, value: object) -> object:
        if value is None or isinstance(value, int) and not isinstance(value, bool):
            return value
        raise ValueError("must be an integer (paisa amounts are integer-only)")


def offer_route_policies() -> tuple[RoutePolicy, ...]:
    """RoutePolicy rows for callers that wire this bounded module."""
    from bdpay.gateway.policy import _p

    lifecycle = tuple(
        RoutePolicy(
            name=f"offers.{action}",
            method="POST",
            pattern=_p(f"/v1/offers/{{offer_id}}/{action}"),
            route_group="payment_write",
            merchant_scope="offers:write",
            idempotency_required=True,
        )
        for action in ("activate", "pause", "resume", "archive")
    )
    return (
        RoutePolicy(
            name="offers.create",
            method="POST",
            pattern=_p("/v1/offers"),
            route_group="payment_write",
            merchant_scope="offers:write",
            idempotency_required=True,
        ),
        *lifecycle,
        RoutePolicy(
            name="offers.list",
            method="GET",
            pattern=_p("/v1/offers"),
            route_group="payment_read",
            merchant_scope="offers:read",
        ),
        # NOTE: registered before offers.get so /v1/offers/eligible never
        # falls into the {offer_id} pattern.
        RoutePolicy(
            name="offers.eligible",
            method="GET",
            pattern=_p("/v1/offers/eligible"),
            route_group="payment_read",
            customer_scopes=("payment:read",),
            ip_limit_per_minute=ELIGIBLE_BUCKET_LIMIT_PER_MINUTE,
        ),
        RoutePolicy(
            name="offers.get",
            method="GET",
            pattern=_p("/v1/offers/{offer_id}"),
            route_group="payment_read",
            merchant_scope="offers:read",
        ),
    )


def add_offer_routes(app: FastAPI, deps: OfferRouteDependencies) -> None:
    """Register the spec/18 offer route surface on an existing FastAPI app."""

    def _merchant_id(request: Request) -> str:
        principal = _require_principal(request)
        if principal.kind != "merchant_key" or principal.merchant_id is None:
            raise AuthorizationError(
                "offer management is a merchant API surface", code="permission_denied"
            )
        return principal.merchant_id

    async def create_offer(request: Request) -> JSONResponse:
        body = _validate(OfferCreate, await _json_body(request))
        result = deps.service.create_offer(
            _merchant_id(request),
            body.model_dump(),
            idempotency_key=request.headers.get(IDEMPOTENCY_HEADER, ""),
        )
        return JSONResponse(dict(result), status_code=201)

    def _lifecycle(action: str):
        async def handler(request: Request) -> JSONResponse:
            offer_id = str(request.path_params["offer_id"])
            merchant_id = _merchant_id(request)
            method = getattr(deps.service, f"{action}_offer")
            return JSONResponse(dict(method(offer_id, merchant_id)))

        handler.__name__ = f"{action}_offer"
        return handler

    async def list_offers(request: Request) -> JSONResponse:
        limit, cursor = parse_pagination(request.query_params)
        state = request.query_params.get("state") or None
        items, next_cursor = deps.service.list_offers(
            _merchant_id(request), state=state, limit=limit, cursor=cursor
        )
        return JSONResponse(build_page(items, next_cursor))

    async def get_offer(request: Request) -> JSONResponse:
        result = deps.service.get_offer(
            str(request.path_params["offer_id"]), _merchant_id(request)
        )
        return JSONResponse(dict(result))

    async def eligible_offers(request: Request) -> JSONResponse:
        principal = _require_principal(request)
        if principal.kind != "customer":
            raise AuthorizationError(
                "offer discovery is a consumer (diner-app) surface",
                code="permission_denied",
            )
        params = request.query_params
        merchant_id = params.get("merchant_id") or ""
        if not merchant_id:
            raise InvalidRequestError(
                "merchant_id query parameter is required", code="validation_failed"
            )
        at_raw = params.get("at")
        at: datetime | None = None
        if at_raw:
            try:
                at = datetime.fromisoformat(at_raw.replace("Z", "+00:00"))
            except ValueError as exc:
                raise InvalidRequestError(
                    "at must be an RFC3339 timestamp", code="validation_failed"
                ) from exc
            if at.tzinfo is None:
                raise InvalidRequestError(
                    "at must carry a timezone", code="validation_failed"
                )
        example_raw = params.get("example_amount_minor")
        example: int | None = None
        if example_raw:
            normalized = normalize_bengali_digits(example_raw).strip()
            if not normalized.isdigit():
                raise InvalidRequestError(
                    "example_amount_minor must be integer paisa",
                    code="validation_failed",
                )
            example = int(normalized, 10)
        items = deps.service.eligible_offers(
            merchant_id,
            at=at,
            method=params.get("method") or None,
            example_amount_minor=example,
        )
        # PII-free by construction (spec/18: discovery excludes customer checks).
        return JSONResponse({"data": [dict(item) for item in items]})

    app.add_api_route("/v1/offers", create_offer, methods=["POST"])
    app.add_api_route("/v1/offers", list_offers, methods=["GET"])
    # eligible MUST register before the parameterized {offer_id} route.
    app.add_api_route("/v1/offers/eligible", eligible_offers, methods=["GET"])
    app.add_api_route("/v1/offers/{offer_id}", get_offer, methods=["GET"])
    for action in ("activate", "pause", "resume", "archive"):
        app.add_api_route(
            f"/v1/offers/{{offer_id}}/{action}", _lifecycle(action), methods=["POST"]
        )


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
