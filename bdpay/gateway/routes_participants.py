"""Participant onboarding v2 routes — spec/19 §API A (PSO-1 extension).

The spec/08 routes (``POST /v1/participants``, ``…/net-debit-cap``,
``GET …/kyb``) remain where spec/08 put them; this module adds ONLY the
spec/19 §A surface as a self-contained :class:`fastapi.APIRouter` factory the
edge composition mounts (``app.include_router(build_participants_router(deps))``).

Binding gates, in order, before anything else:

1. **PLATFORM_MODE** — under ``PSP`` every route here returns
   ``403 authorization / platform_mode_pso_required``; no data is read or
   written before the check (spec/19 §Scope item 1).
2. **Operator auth + scope** — all routes are operator routes; scopes are
   ``onboarding:write`` / ``onboarding:approve`` / ``onboarding:read`` per
   the §A table. The authenticated principal arrives on
   ``request.state.principal`` (set by the gateway middleware spine).

Errors raise the platform ``BDPayError`` hierarchy; the app-level handlers
render the spec/00 §4 envelope exactly as for every other route.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from bdpay.gateway.pagination import build_page, parse_pagination
from bdpay.gateway.policy import RoutePolicy
from bdpay.gateway.principals import Principal
from bdpay.platform.config import Settings
from bdpay.platform.errors import (
    AuthenticationError,
    AuthorizationError,
    InvalidRequestError,
    NotFoundError,
)

__all__ = [
    "ParticipantRoutesDependencies",
    "build_participants_router",
    "participant_route_policies",
]

_SCOPE_WRITE = "onboarding:write"
_SCOPE_APPROVE = "onboarding:approve"
_SCOPE_READ = "onboarding:read"

_DIRECTIONS = ("OUTBOUND", "INBOUND")


def _p(path_template: str) -> re.Pattern[str]:
    pattern = re.sub(r"\{[a-z_]+\}", r"[^/]+", path_template)
    return re.compile(f"^{pattern}$")


def participant_route_policies() -> tuple[RoutePolicy, ...]:
    """Policy rows consumed by the assembled gateway middleware.

    Operator auth currently transports privileges in the session ``roles`` set;
    the route layer still enforces the spec/19 ``onboarding:*`` scopes. A
    ``SUPER_ADMIN`` operator can cross the middleware and route gate.
    """
    return (
        RoutePolicy(
            name="participants.documents.register",
            method="POST",
            pattern=_p("/v1/participants/{participant_id}/documents"),
            route_group="onboarding_write",
            operator_roles=(_SCOPE_WRITE,),
        ),
        RoutePolicy(
            name="participants.documents.verify",
            method="POST",
            pattern=_p("/v1/participants/{participant_id}/documents/{pdoc_id}/verify"),
            route_group="onboarding_write",
            operator_roles=(_SCOPE_APPROVE,),
        ),
        RoutePolicy(
            name="participants.documents.reject",
            method="POST",
            pattern=_p("/v1/participants/{participant_id}/documents/{pdoc_id}/reject"),
            route_group="onboarding_write",
            operator_roles=(_SCOPE_APPROVE,),
        ),
        RoutePolicy(
            name="participants.mou.record",
            method="POST",
            pattern=_p("/v1/participants/{participant_id}/mou"),
            route_group="onboarding_write",
            operator_roles=(_SCOPE_APPROVE,),
        ),
        RoutePolicy(
            name="participants.conformance_runs.queue",
            method="POST",
            pattern=_p("/v1/participants/{participant_id}/conformance-runs"),
            route_group="onboarding_write",
            operator_roles=(_SCOPE_WRITE,),
        ),
        RoutePolicy(
            name="participants.conformance_runs.list",
            method="GET",
            pattern=_p("/v1/participants/{participant_id}/conformance-runs"),
            route_group="onboarding_read",
            operator_roles=(_SCOPE_READ,),
        ),
        RoutePolicy(
            name="participants.conformance_runs.get",
            method="GET",
            pattern=_p("/v1/conformance-runs/{confr_id}"),
            route_group="onboarding_read",
            operator_roles=(_SCOPE_READ,),
        ),
        RoutePolicy(
            name="participants.activate",
            method="POST",
            pattern=_p("/v1/participants/{participant_id}/activate"),
            route_group="onboarding_write",
            operator_roles=(_SCOPE_APPROVE,),
        ),
    )


def _rfc3339(at: datetime | None) -> str | None:
    if at is None:
        return None
    return at.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _default_principal_provider(request: Request) -> Principal | None:
    return getattr(request.state, "principal", None)


@dataclass
class ParticipantRoutesDependencies:
    """Everything the §A surface needs, injected at composition time.

    ``service`` is the kernel ``ParticipantOnboardingService`` (typed loosely
    so the gateway stays a thin edge over the kernel port).
    """

    settings: Settings
    service: Any
    principal_provider: Callable[[Request], Principal | None] = _default_principal_provider


def build_participants_router(deps: ParticipantRoutesDependencies) -> APIRouter:
    """The spec/19 §API A router (operator-only, PSO-mode-gated)."""

    router = APIRouter()

    # -- gates (mode first; zero reads/writes before it) ----------------------

    def _gate(request: Request, scope: str) -> Principal:
        if deps.settings.platform_mode != "PSO":
            raise AuthorizationError(
                "this operation requires PLATFORM_MODE=PSO",
                code="platform_mode_pso_required",
            )
        principal = deps.principal_provider(request)
        if principal is None:
            raise AuthenticationError("credentials required", code="missing_authorization")
        if principal.kind != "operator":
            raise AuthorizationError(
                "participant onboarding routes are operator-only",
                code="operator_token_required",
            )
        if "SUPER_ADMIN" not in principal.roles and scope not in (
            principal.scopes | principal.roles
        ):
            raise AuthorizationError(
                f"operator lacks the {scope} scope", code="insufficient_scope"
            )
        return principal

    async def _json_body(request: Request) -> dict:
        raw = await request.body()
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
        except ValueError as exc:
            raise InvalidRequestError(
                "request body must be a JSON object", code="invalid_json"
            ) from exc
        if not isinstance(parsed, dict):
            raise InvalidRequestError(
                "request body must be a JSON object", code="invalid_json"
            )
        return parsed

    def _require_str(body: dict, field: str) -> str:
        value = body.get(field)
        if not isinstance(value, str) or not value:
            raise InvalidRequestError(
                f"{field} (non-empty string) is required", code="validation_failed"
            )
        return value

    # -- views ---------------------------------------------------------------

    def _document_view(doc: Any) -> dict:
        return {
            "document_id": doc.document_id,
            "participant_id": doc.participant_id,
            "document_class": doc.document_class,
            "content_sha256": doc.content_sha256,
            "review_status": doc.review_status,
            "reject_reason": doc.reject_reason,
            "uploaded_by": doc.uploaded_by,
            "verified_by": doc.verified_by,
            "uploaded_at": _rfc3339(doc.uploaded_at),
            "verified_at": _rfc3339(doc.verified_at),
        }

    def _run_view(run: Any) -> dict:
        return {
            "conformance_run_id": run.conformance_run_id,
            "participant_id": run.participant_id,
            "direction": run.direction,
            "suite_version": run.suite_version,
            "state": run.status,
            "checks": list(run.checks),
            "evidence_sha256": run.evidence_sha256,
            "report_hash": run.report_hash,
            "triggered_by": run.triggered_by,
            "created_at": _rfc3339(run.created_at),
            "started_at": _rfc3339(run.started_at),
            "finished_at": _rfc3339(run.finished_at),
        }

    # -- POST /v1/participants/{participant_id}/documents ---------------------

    async def register_document(request: Request) -> JSONResponse:
        principal = _gate(request, _SCOPE_WRITE)
        participant_id = str(request.path_params["participant_id"])
        body = await _json_body(request)
        metadata = body.get("metadata") or {}
        if not isinstance(metadata, dict):
            raise InvalidRequestError(
                "metadata must be an object", code="validation_failed"
            )
        document = deps.service.register_document(
            participant_id,
            document_class=_require_str(body, "document_class"),
            storage_pointer=_require_str(body, "storage_pointer"),
            content_sha256=_require_str(body, "content_sha256"),
            metadata=metadata,
            uploaded_by=principal.operator_id or principal.principal_id,
        )
        return JSONResponse(_document_view(document), status_code=201)

    # -- POST .../documents/{pdoc_id}/verify -----------------------------------

    async def verify_document(request: Request) -> JSONResponse:
        principal = _gate(request, _SCOPE_APPROVE)
        document = deps.service.verify_document(
            str(request.path_params["participant_id"]),
            str(request.path_params["pdoc_id"]),
            verified_by=principal.operator_id or principal.principal_id,
        )
        return JSONResponse(_document_view(document))

    # -- POST .../documents/{pdoc_id}/reject ------------------------------------

    async def reject_document(request: Request) -> JSONResponse:
        principal = _gate(request, _SCOPE_APPROVE)
        body = await _json_body(request)
        document = deps.service.reject_document(
            str(request.path_params["participant_id"]),
            str(request.path_params["pdoc_id"]),
            reason=_require_str(body, "reason"),
            actor_id=principal.operator_id or principal.principal_id,
        )
        return JSONResponse(_document_view(document))

    # -- POST /v1/participants/{participant_id}/mou ------------------------------

    async def record_mou(request: Request) -> JSONResponse:
        principal = _gate(request, _SCOPE_APPROVE)
        participant_id = str(request.path_params["participant_id"])
        body = await _json_body(request)
        signatory = body.get("counterparty_signatory")
        if not isinstance(signatory, dict):
            raise InvalidRequestError(
                "counterparty_signatory (object) is required", code="validation_failed"
            )
        fee_ref = body.get("founding_fee_terms_ref")
        if fee_ref is not None and not isinstance(fee_ref, str):
            raise InvalidRequestError(
                "founding_fee_terms_ref must be a string or null", code="validation_failed"
            )
        mou, record = deps.service.record_mou(
            participant_id,
            mou_template_version=_require_str(body, "mou_template_version"),
            executed_date=_require_str(body, "executed_date"),
            signed_document_pointer=_require_str(body, "signed_document_pointer"),
            signed_document_sha256=_require_str(body, "signed_document_sha256"),
            counterparty_signatory=signatory,
            founding_fee_terms_ref=fee_ref,
            recorded_by=principal.operator_id or principal.principal_id,
        )
        return JSONResponse(
            {
                "mou_id": mou.mou_id,
                "participant_id": participant_id,
                "state": record.kyb_status,
                "mou_template_version": mou.mou_template_version,
                "non_binding": mou.non_binding,
                "contingent_on_license": mou.contingent_on_license,
                "zero_capital_commitment": mou.zero_capital_commitment,
            },
            status_code=201,
        )

    # -- POST /v1/participants/{participant_id}/conformance-runs -----------------

    async def queue_conformance_run(request: Request) -> JSONResponse:
        principal = _gate(request, _SCOPE_WRITE)
        participant_id = str(request.path_params["participant_id"])
        body = await _json_body(request)
        direction = _require_str(body, "direction")
        if direction not in _DIRECTIONS:
            raise InvalidRequestError(
                f"direction must be one of {list(_DIRECTIONS)}", code="validation_failed"
            )
        evidence = body.get("evidence") or {}
        if not isinstance(evidence, dict):
            raise InvalidRequestError("evidence must be an object", code="validation_failed")
        run = deps.service.queue_conformance_run(
            participant_id,
            direction=direction,
            suite_version=body.get("suite_version"),
            evidence=evidence,
            triggered_by=principal.operator_id or principal.principal_id,
        )
        # 202: execution is asynchronous (runner pickup).
        return JSONResponse(
            {"conformance_run_id": run.conformance_run_id, "state": run.status},
            status_code=202,
        )

    # -- GET /v1/participants/{participant_id}/conformance-runs -------------------

    async def list_conformance_runs(request: Request) -> JSONResponse:
        _gate(request, _SCOPE_READ)
        participant_id = str(request.path_params["participant_id"])
        limit, cursor = parse_pagination(request.query_params)
        runs = sorted(
            deps.service.list_conformance_runs(participant_id),
            key=lambda r: (r.created_at, r.conformance_run_id),
        )
        start = 0
        if cursor:
            ids = [r.conformance_run_id for r in runs]
            if cursor not in ids:
                raise InvalidRequestError("unknown cursor", code="invalid_cursor")
            start = ids.index(cursor) + 1
        page = runs[start : start + limit]
        next_cursor = (
            page[-1].conformance_run_id if len(runs) > start + limit and page else None
        )
        return JSONResponse(build_page([_run_view(r) for r in page], next_cursor))

    # -- GET /v1/conformance-runs/{confr_id} ---------------------------------------

    async def get_conformance_run(request: Request) -> JSONResponse:
        _gate(request, _SCOPE_READ)
        run = deps.service.get_conformance_run(str(request.path_params["confr_id"]))
        if run is None:
            raise NotFoundError("conformance run not found", code="conformance_run_not_found")
        return JSONResponse(_run_view(run))

    # -- POST /v1/participants/{participant_id}/activate ----------------------------

    async def activate_participant(request: Request) -> JSONResponse:
        principal = _gate(request, _SCOPE_APPROVE)
        participant_id = str(request.path_params["participant_id"])
        body = await _json_body(request)
        pointer = body.get("settlement_agreement_pointer")
        if pointer is not None and not isinstance(pointer, str):
            raise InvalidRequestError(
                "settlement_agreement_pointer must be a string or null",
                code="validation_failed",
            )
        approval, record = deps.service.request_activation(
            participant_id,
            requested_by=principal.operator_id or principal.principal_id,
            settlement_agreement_pointer=pointer,
        )
        # 202: activation completes on second-operator approval (two-eyes).
        return JSONResponse(
            {
                "approval_request_id": approval.approval_request_id,
                "participant_id": participant_id,
                "state": record.kyb_status,
            },
            status_code=202,
        )

    router.add_api_route(
        "/v1/participants/{participant_id}/documents", register_document, methods=["POST"]
    )
    router.add_api_route(
        "/v1/participants/{participant_id}/documents/{pdoc_id}/verify",
        verify_document,
        methods=["POST"],
    )
    router.add_api_route(
        "/v1/participants/{participant_id}/documents/{pdoc_id}/reject",
        reject_document,
        methods=["POST"],
    )
    router.add_api_route(
        "/v1/participants/{participant_id}/mou", record_mou, methods=["POST"]
    )
    router.add_api_route(
        "/v1/participants/{participant_id}/conformance-runs",
        queue_conformance_run,
        methods=["POST"],
    )
    router.add_api_route(
        "/v1/participants/{participant_id}/conformance-runs",
        list_conformance_runs,
        methods=["GET"],
    )
    router.add_api_route(
        "/v1/conformance-runs/{confr_id}", get_conformance_run, methods=["GET"]
    )
    router.add_api_route(
        "/v1/participants/{participant_id}/activate",
        activate_participant,
        methods=["POST"],
    )
    return router
