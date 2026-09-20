"""WebhookInbound ingestion pipeline (spec/10 §State Machines 3).

RECEIVED (raw persisted first, before verification) -> verify FAIL-CLOSED
(the only path out of RECEIVED other than REJECTED is a literal ``True`` from
the connector's ``verify_signature``; ``False``, any exception, or a missing
key are all REJECTED) -> content-hash dedupe over sha256(raw body bytes)
(DUPLICATE terminal) -> parse_event -> kernel handoff port. No matching
``connector_ref`` parks the row; a 5-minute retry sweep runs for <= 24h, then
REJECTED(reason=unmatched) with an ops alert. Response semantics: always
``{"received": true}`` once durably persisted — rejected signatures get no
oracle. Verify-then-persist callback lineage: Agentic-Transformation m12
ingestion (ADAPT PATTERN, re-implemented). Resolution pinned by test: a
``parse_event`` exception is also fail-closed -> REJECTED(reason=parse_error).

Bengali-locale rule: ``normalize_numeric_fields`` is applied by handlers to
numeric/date fields BEFORE parsing comparisons; the untouched raw bytes are
what the content hash and archive are computed over.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import timedelta

from bdpay.connectors.ids_ext import make_connector_id
from bdpay.connectors.ports import (
    AuditSink,
    EventSink,
    InMemoryAuditSink,
    InMemoryEventSink,
    InMemoryObjectStore,
    KernelHandoff,
    ObjectStore,
    raw_object_key,
)
from bdpay.connectors.registry import UnknownConnectorError
from bdpay.connectors.sdk import ConnectorResult, ConnectorWebhookHandler
from bdpay.connectors.stores import (
    ConnectorResultStore,
    InMemoryConnectorResultStore,
    InMemoryWebhookInboundStore,
    WebhookInboundRecord,
    WebhookInboundStore,
)
from bdpay.platform.bangla import normalize_bengali_digits
from bdpay.platform.canonical import sha256_canonical
from bdpay.platform.clock import Clock

__all__ = [
    "IngestOutcome",
    "PARK_SWEEP_INTERVAL_S",
    "PARK_TTL_S",
    "WebhookPipeline",
    "normalize_numeric_fields",
]

#: PARKED retry sweep cadence and ceiling (spec/10 WebhookInbound FSM).
PARK_SWEEP_INTERVAL_S = 300
PARK_TTL_S = 24 * 3600

_PIPELINE_ACTOR = "system:webhook-pipeline"

_ALLOWED = frozenset(
    {
        ("RECEIVED", "VERIFIED"),
        ("RECEIVED", "REJECTED"),
        ("VERIFIED", "PARSED"),
        ("VERIFIED", "DUPLICATE"),
        ("PARSED", "HANDED_OFF"),
        ("PARSED", "PARKED"),
        # parse_event exception is fail-closed (errata-resolution, pinned by test)
        ("PARSED", "REJECTED"),
        ("HANDED_OFF", "PROCESSED"),
        ("PARKED", "PARSED"),
        ("PARKED", "REJECTED"),
    }
)


def normalize_numeric_fields(payload: dict, fields: tuple[str, ...]) -> dict:
    """Return a copy with Bengali digits normalized in the named str fields."""
    normalized = dict(payload)
    for name in fields:
        value = normalized.get(name)
        if isinstance(value, str):
            normalized[name] = normalize_bengali_digits(value)
    return normalized


@dataclass(frozen=True)
class IngestOutcome:
    """Disposition of one delivery plus the constant edge response body."""

    inbound_id: str
    status: str
    response: dict


class WebhookPipeline:
    """The single ingestion path for ``POST /v1/connector-callbacks/{id}``."""

    def __init__(
        self,
        *,
        clock: Clock,
        kernel: KernelHandoff,
        handlers: dict[str, ConnectorWebhookHandler] | None = None,
        inbound_store: WebhookInboundStore | None = None,
        result_store: ConnectorResultStore | None = None,
        object_store: ObjectStore | None = None,
        audit: AuditSink | None = None,
        events: EventSink | None = None,
        mode_at_call: str = "SIMULATOR",
    ) -> None:
        self._clock = clock
        self._kernel = kernel
        self._handlers: dict[str, ConnectorWebhookHandler] = dict(handlers or {})
        self._inbound = (
            inbound_store if inbound_store is not None else InMemoryWebhookInboundStore()
        )
        self._results = result_store if result_store is not None else InMemoryConnectorResultStore()
        self._objects = object_store if object_store is not None else InMemoryObjectStore()
        self._audit = audit if audit is not None else InMemoryAuditSink()
        self._events = events if events is not None else InMemoryEventSink()
        self._mode_at_call = mode_at_call

    def register_handler(self, handler: ConnectorWebhookHandler) -> None:
        self._handlers[handler.connector_id] = handler

    @property
    def inbound_store(self) -> WebhookInboundStore:
        return self._inbound

    @property
    def result_store(self) -> ConnectorResultStore:
        return self._results

    # -- FSM advance (the only status writer) ---------------------------------

    def _advance(
        self, record: WebhookInboundRecord, to_status: str, *, reason: str | None = None
    ) -> None:
        if (record.status, to_status) not in _ALLOWED:
            raise ValueError(
                f"webhook FSM transition {record.status} -> {to_status} is DENIED (refusal-first)"
            )
        record.status = to_status
        if reason is not None:
            record.reject_reason = reason
        if to_status in {"PROCESSED", "REJECTED", "DUPLICATE"}:
            record.processed_at = self._clock.now()
        self._inbound.save(record)

    # -- verification (fail-closed by construction) ---------------------------

    @staticmethod
    def _verify(handler: ConnectorWebhookHandler, headers: dict, body: bytes) -> bool:
        try:
            return handler.verify_signature(dict(headers), body) is True
        except Exception:
            return False

    def _reject_delivery(self, record: WebhookInboundRecord, *, reason: str) -> IngestOutcome:
        """Reject THIS delivery without touching the stored row's status.

        Used for re-deliveries that fail verification: the original row keeps
        its (possibly terminal) FSM state; the unverifiable delivery is still
        audited and gets the constant no-oracle response.
        """
        now = self._clock.now()
        self._audit.record(
            "WEBHOOK_REJECTED",
            actor_id=_PIPELINE_ACTOR,
            occurred_at=now,
            detail={
                "inbound_id": record.inbound_id,
                "connector_id": record.connector_id,
                "reason": reason,
            },
        )
        self._events.emit(
            "webhook_inbound.rejected",
            {
                "inbound_id": record.inbound_id,
                "connector_id": record.connector_id,
                "reject_reason": reason,
                "source_ip_hash": record.source_ip_hash,
            },
        )
        return IngestOutcome(record.inbound_id, "REJECTED", {"received": True})

    # -- ingestion -------------------------------------------------------------

    async def ingest(
        self, connector_id: str, headers: dict, body: bytes, *, source_ip: str = "0.0.0.0"
    ) -> IngestOutcome:
        handler = self._handlers.get(connector_id)
        if handler is None:
            raise UnknownConnectorError(f"no webhook handler for connector {connector_id}")
        now = self._clock.now()
        content_hash = hashlib.sha256(body).hexdigest()
        raw_pointer = f"webhooks/{connector_id}/{content_hash}"
        # Persist raw FIRST — rejected callbacks stay forensically available.
        self._objects.put(raw_pointer, body)
        self._objects.put(f"{raw_pointer}.headers", sha256_canonical(dict(headers)).encode())
        record = WebhookInboundRecord(
            inbound_id=make_connector_id(
                "winb", {"connector_id": connector_id, "content_hash": content_hash}
            ),
            connector_id=connector_id,
            content_hash=content_hash,
            headers_hash=sha256_canonical(dict(headers)),
            raw_pointer=raw_pointer,
            source_ip_hash=hashlib.sha256(source_ip.encode("utf-8")).hexdigest(),
            received_at=now,
        )
        record, created = self._inbound.insert(record)
        verified = self._verify(handler, headers, body)
        if not created:
            # Re-delivery: verify first (fail-closed), then dedupe terminal.
            if not verified:
                return self._reject_delivery(record, reason="signature_unverifiable")
            self._audit.record(
                "WEBHOOK_DUPLICATE",
                actor_id=_PIPELINE_ACTOR,
                occurred_at=now,
                detail={"inbound_id": record.inbound_id, "connector_id": connector_id},
            )
            return IngestOutcome(record.inbound_id, "DUPLICATE", {"received": True})
        if not verified:
            self._advance(record, "REJECTED", reason="signature_unverifiable")
            self._audit.record(
                "WEBHOOK_REJECTED",
                actor_id=_PIPELINE_ACTOR,
                occurred_at=now,
                detail={
                    "inbound_id": record.inbound_id,
                    "connector_id": connector_id,
                    "reason": "signature_unverifiable",
                },
            )
            self._events.emit(
                "webhook_inbound.rejected",
                {
                    "inbound_id": record.inbound_id,
                    "connector_id": connector_id,
                    "reject_reason": "signature_unverifiable",
                    "source_ip_hash": record.source_ip_hash,
                },
            )
            return IngestOutcome(record.inbound_id, "REJECTED", {"received": True})
        self._advance(record, "VERIFIED")
        self._audit.record(
            "WEBHOOK_VERIFIED",
            actor_id=_PIPELINE_ACTOR,
            occurred_at=now,
            detail={"inbound_id": record.inbound_id, "connector_id": connector_id},
        )
        self._advance(record, "PARSED")
        try:
            result = handler.parse_event(dict(headers), body)
        except Exception:
            self._advance(record, "REJECTED", reason="parse_error")
            self._events.emit(
                "webhook_inbound.rejected",
                {
                    "inbound_id": record.inbound_id,
                    "connector_id": connector_id,
                    "reject_reason": "parse_error",
                    "source_ip_hash": record.source_ip_hash,
                },
            )
            return IngestOutcome(record.inbound_id, "REJECTED", {"received": True})
        record.parsed_connector_ref = result.connector_ref
        self._inbound.save(record)
        return await self._handoff(record, result)

    async def _handoff(
        self, record: WebhookInboundRecord, result: ConnectorResult
    ) -> IngestOutcome:
        accepted = await self._kernel.handle_connector_result(result)
        if not accepted:
            self._advance(record, "PARKED")
            self._audit.record(
                "WEBHOOK_PARKED",
                actor_id=_PIPELINE_ACTOR,
                occurred_at=self._clock.now(),
                detail={"inbound_id": record.inbound_id, "connector_id": record.connector_id},
            )
            self._events.emit(
                "webhook_inbound.parked",
                {"inbound_id": record.inbound_id, "connector_id": record.connector_id},
            )
            return IngestOutcome(record.inbound_id, "PARKED", {"received": True})
        pointer = raw_object_key(record.connector_id, result.raw_response_hash)
        if not self._objects.exists(pointer):
            self._objects.put(pointer, self._objects.get(record.raw_pointer))
        row = self._results.record(
            result,
            connector_id=record.connector_id,
            call_kind="WEBHOOK",
            attempt_no=self._results.next_attempt_no(
                record.connector_id, result.connector_ref, "WEBHOOK"
            ),
            mode_at_call=self._mode_at_call,
            raw_response_pointer=pointer,
            produced_at=self._clock.now(),
            webhook_inbound_id=record.inbound_id,
        )
        record.result_id = row.result_id
        self._events.emit(
            "connector_result.recorded",
            {
                "result_id": row.result_id,
                "connector_id": row.connector_id,
                "connector_ref": row.connector_ref,
                "call_kind": row.call_kind,
                "status": row.status,
                "error_code": row.error_code,
                "mode_at_call": row.mode_at_call,
            },
        )
        self._advance(record, "HANDED_OFF")
        self._advance(record, "PROCESSED")
        return IngestOutcome(record.inbound_id, "PROCESSED", {"received": True})

    # -- PARKED retry sweep ------------------------------------------------------

    async def sweep_parked(self) -> dict[str, str]:
        """Retry parked rows; expire those parked > 24h. Returns id -> status."""
        outcome: dict[str, str] = {}
        now = self._clock.now()
        for record in list(self._inbound.list_by_status("PARKED")):
            if record.received_at is not None and now - record.received_at >= timedelta(
                seconds=PARK_TTL_S
            ):
                self._advance(record, "REJECTED", reason="unmatched")
                self._audit.record(
                    "WEBHOOK_REJECTED",
                    actor_id=_PIPELINE_ACTOR,
                    occurred_at=now,
                    detail={
                        "inbound_id": record.inbound_id,
                        "connector_id": record.connector_id,
                        "reason": "unmatched",
                    },
                )
                self._events.emit(
                    "webhook_inbound.rejected",
                    {
                        "inbound_id": record.inbound_id,
                        "connector_id": record.connector_id,
                        "reject_reason": "unmatched",
                        "source_ip_hash": record.source_ip_hash,
                    },
                )
                outcome[record.inbound_id] = "REJECTED"
                continue
            handler = self._handlers.get(record.connector_id)
            if handler is None:
                outcome[record.inbound_id] = "PARKED"
                continue
            body = self._objects.get(record.raw_pointer)
            try:
                result = handler.parse_event({}, body)
            except Exception:
                outcome[record.inbound_id] = "PARKED"
                continue
            self._advance(record, "PARSED")
            handed = await self._handoff(record, result)
            outcome[record.inbound_id] = handed.status
        return outcome
