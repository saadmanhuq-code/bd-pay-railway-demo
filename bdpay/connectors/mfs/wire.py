"""Shared wire plumbing for the spec/12 MFS adapters.

One transport Protocol + the real httpx implementation (SANDBOX/PRODUCTION wire
code — fully written, activated purely by credentials/config), canonical raw
archiving (E12 hash discipline: ``raw_response_hash`` over the parsed wire
response, raw bytes archived content-addressed, never logged), and the
MANUAL_LOCAL contingency state (spec/10 §modes; two-eyes manual results).

JSON bodies are parsed with ``parse_float=str`` so a decimal amount on the wire
can never materialize as a Python float (spec 00 §6; canonical_json rejects
floats by construction).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

from bdpay.connectors.ports import InMemoryObjectStore, ObjectStore, raw_object_key
from bdpay.connectors.sdk import ConnectorResult, ConnectorStatus
from bdpay.platform.canonical import canonical_json, sha256_canonical
from bdpay.platform.clock import Clock
from bdpay.platform.errors import ConflictError, InvalidRequestError

__all__ = [
    "HttpxTransport",
    "ManualLocalState",
    "PinnedHttpxTransport",
    "RawArchive",
    "TwoEyesRequiredError",
    "WireResponse",
    "WireTransport",
    "parse_json_body",
    "rfc3339",
]


def rfc3339(dt: datetime) -> str:
    """Second-precision RFC3339 UTC string (the connector wire timestamp form)."""
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_json_body(body: bytes) -> dict:
    """Parse a JSON wire body; decimal literals arrive as strings, never floats."""
    parsed = json.loads(body.decode("utf-8"), parse_float=str)
    if not isinstance(parsed, dict):
        raise InvalidRequestError("wire body must be a JSON object", code="wire_body_invalid")
    return parsed


@dataclass(frozen=True, eq=False)
class WireResponse:
    """One HTTP exchange outcome as the adapters consume it.

    ``headers`` carries the response headers when a transport provides them
    (HTTP header names are case-insensitive — read through :meth:`header`).
    Adapters that predate the field keep working: it defaults to empty.
    """

    status_code: int
    body: bytes
    headers: dict = field(default_factory=dict)

    def header(self, name: str) -> str:
        """Case-insensitive response-header read; '' when absent."""
        target = name.lower()
        for key, value in self.headers.items():
            if str(key).lower() == target:
                return str(value)
        return ""

    def json(self) -> dict:
        return parse_json_body(self.body)


@runtime_checkable
class WireTransport(Protocol):
    """Minimal async HTTP port; the live implementation is :class:`HttpxTransport`."""

    async def request(
        self, method: str, url: str, *, headers: dict, content: bytes
    ) -> WireResponse: ...


class HttpxTransport:
    """Real HTTP transport over httpx — the SANDBOX/PRODUCTION wire path.

    TLS verification stays ON (BB ICT encryption-in-transit rule); the timeout
    is enforced again by the runner's hard timeout, this one bounds the socket.
    """

    def __init__(self, *, timeout_s: float = 30.0, verify: bool = True) -> None:
        import httpx

        self._client = httpx.AsyncClient(timeout=timeout_s, verify=verify)

    async def request(
        self, method: str, url: str, *, headers: dict, content: bytes
    ) -> WireResponse:
        response = await self._client.request(method, url, headers=headers, content=content)
        return WireResponse(
            status_code=response.status_code,
            body=response.content,
            headers=dict(response.headers),
        )

    async def aclose(self) -> None:
        await self._client.aclose()


class PinnedHttpxTransport:
    """:class:`WireTransport` for dynamic egress URLs — validated + pinned.

    Unlike :class:`HttpxTransport` (operator-fixed, config-bound endpoints),
    this transport is for connectors whose URLs are influenced by upstream
    responses (e.g. the sanctions feed's redirect following). Every request
    goes through :func:`bdpay.security.outbound_allowlist.pinned_request`:
    the destination is resolved once (off-loop, bounded), every resolved
    address is allowlist-validated, and the connection is pinned to the
    validated address with the original hostname kept for Host + TLS
    (SNI/certificate) verification. ``trust_env=False`` keeps ambient proxy
    configuration out of the path; redirects stay client-disabled (the
    connector follows them hop-by-hop, re-validating each Location).
    """

    def __init__(self, *, timeout_s: float = 30.0, verify: bool = True) -> None:
        import httpx

        self._client = httpx.AsyncClient(
            timeout=timeout_s, verify=verify, trust_env=False
        )

    async def request(
        self, method: str, url: str, *, headers: dict, content: bytes
    ) -> WireResponse:
        from bdpay.security.outbound_allowlist import pinned_request

        response = await pinned_request(
            method, url, headers=headers, content=content, client=self._client
        )
        return WireResponse(
            status_code=response.status_code,
            body=response.content,
            headers=dict(response.headers),
        )

    async def aclose(self) -> None:
        await self._client.aclose()


class RawArchive:
    """Content-addressed archive of raw wire responses (CERT-04 discipline)."""

    def __init__(self, connector_id: str, object_store: ObjectStore | None = None) -> None:
        self.connector_id = connector_id
        self._objects = object_store if object_store is not None else InMemoryObjectStore()

    @property
    def object_store(self) -> ObjectStore:
        return self._objects

    def archive(self, raw: dict) -> str:
        """Archive ``raw`` canonically; return its sha256 (the result hash)."""
        raw_hash = sha256_canonical(raw)
        self._objects.put(raw_object_key(self.connector_id, raw_hash), canonical_json(raw))
        return raw_hash


class TwoEyesRequiredError(ConflictError):
    """A MANUAL_LOCAL result needs an approved two-eyes approval_request_id."""

    default_code = "manual_result_requires_two_eyes"


class ManualLocalState:
    """MANUAL_LOCAL contingency (spec/10 §modes; spec/12 §C Rocket).

    ``submit()`` in MANUAL_LOCAL returns PENDING with no outbound I/O; the
    outcome is recorded by an operator (two-eyes ``ApprovalRequest``) and flows
    through the identical kernel handoff as a webhook. Idempotent per
    ``connector_ref``: the first recorded manual result wins.
    """

    def __init__(self, connector_id: str, *, clock: Clock, archive: RawArchive) -> None:
        self.connector_id = connector_id
        self._clock = clock
        self._archive = archive
        self._pending: dict[str, str] = {}  # connector_ref -> instruction_id
        self._recorded: dict[str, ConnectorResult] = {}

    def accept_submit(self, instruction_id: str, connector_ref: str) -> ConnectorResult:
        recorded = self._recorded.get(connector_ref)
        if recorded is not None:
            return recorded
        self._pending.setdefault(connector_ref, instruction_id)
        raw = {
            "connector_id": self.connector_id,
            "connector_ref": connector_ref,
            "instruction_id": instruction_id,
            "manual_local": True,
            "status": "pending",
            "responded_at": rfc3339(self._clock.now()),
        }
        return ConnectorResult(
            instruction_id=instruction_id,
            connector_ref=connector_ref,
            status=ConnectorStatus.PENDING,
            rail_transaction_id=None,
            responded_at=raw["responded_at"],
            error_code=None,
            raw_response_hash=self._archive.archive(raw),
        )

    def record_manual_result(
        self,
        connector_ref: str,
        *,
        status: ConnectorStatus,
        rail_transaction_id: str | None,
        approval_request_id: str | None,
        recorded_by: str,
        error_code: str | None = None,
    ) -> ConnectorResult:
        """Operator records the out-of-band rail outcome (two-eyes enforced)."""
        if not approval_request_id:
            raise TwoEyesRequiredError(
                "manual result without an approved approval_request_id is refused"
            )
        existing = self._recorded.get(connector_ref)
        if existing is not None:
            return existing  # first approved record wins; replay is a pure read
        instruction_id = self._pending.get(connector_ref, "")
        raw = {
            "connector_id": self.connector_id,
            "connector_ref": connector_ref,
            "instruction_id": instruction_id,
            "manual_local": True,
            "status": status.value,
            "rail_transaction_id": rail_transaction_id,
            "error_code": error_code,
            "approval_request_id": approval_request_id,
            "recorded_by": recorded_by,
            "responded_at": rfc3339(self._clock.now()),
        }
        result = ConnectorResult(
            instruction_id=instruction_id,
            connector_ref=connector_ref,
            status=status,
            rail_transaction_id=rail_transaction_id,
            responded_at=raw["responded_at"],
            error_code=error_code,
            raw_response_hash=self._archive.archive(raw),
        )
        self._recorded[connector_ref] = result
        return result

    def recorded(self, connector_ref: str) -> ConnectorResult | None:
        return self._recorded.get(connector_ref)
