"""``goaml_reporter_v1`` — BFIU goAML portal filing adapter (spec/12 §F).

Implements the frozen ``sdk.AmlFilingConnector`` protocol with DURABLE queue
semantics: **a filing is NEVER dropped** (MLPA 2012 criminal exposure; spec/10
budget row ``never_drop``).

HONESTY NOTE (in-code, not aspirational): the connector accepts injected filing
and object stores, and the assembled app wires Postgres stores for both in PG
mode. The audit/event sinks still default to in-memory stores unless explicitly
injected. ``build_services`` wires the live/sandbox portal only from the
operator-owned connector deployment config; missing portal credentials still
fail closed at boot.

The GoamlFiling FSM (spec/12 §State Machines 2, refusal-first):

``QUEUED -> SUBMITTING -> SUBMITTED -> ACK_PENDING -> ACKED (terminal)`` with
``REJECTED_BY_PORTAL -> (regenerated payload) -> QUEUED`` and the operator
``MANUAL_ESCALATED (terminal — human files out-of-band; payload preserved)``.
Submit failures keep the row QUEUED with unbounded exponential backoff
(1m -> 2m -> 4m ... cap 1h); ``goaml_filing.retrying`` is emitted every 10th
attempt; the circuit breaker is INFORMATIONAL ONLY for this connector — the
queue keeps retrying regardless.

XML is rendered per the binding goAML 4.x element map and locally validated
against the XSD slot before queueing (the BFIU BD-profile XSD is an awaited
artifact — Open question 4 — so the default validator is structural and the
slot is hot-swappable registry config). Amounts cross paisa -> decimal-string
at this render boundary only. PII renders into the XML (object store) only —
never into ``goaml_filings`` rows.

``file_str``/``file_ctr`` return the durable filing reference immediately
(LB8): the portal submission ref propagates via the FSM/result handoff once
receipted — returning could otherwise block unboundedly on a portal outage,
which would contradict the queue design the same spec mandates.
"""

from __future__ import annotations

import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol, runtime_checkable

from bdpay.connectors.identity.ids12 import make_spec12_id
from bdpay.connectors.mfs.amounts import paisa_to_decimal_string
from bdpay.connectors.mfs.wire import rfc3339
from bdpay.connectors.ports import (
    AuditSink,
    EventSink,
    InMemoryAuditSink,
    InMemoryEventSink,
    InMemoryObjectStore,
    ObjectStore,
)
from bdpay.connectors.registry import ConnectorMode
from bdpay.platform.canonical import sha256_canonical
from bdpay.platform.clock import Clock
from bdpay.platform.errors import ConflictError, InvalidRequestError
from bdpay.platform.ids import make_id

__all__ = [
    "BACKOFF_CAP_S",
    "BACKOFF_INITIAL_S",
    "CLAIM_LEASE_S",
    "GoamlFilingRow",
    "GoamlFilingStore",
    "GoamlPayloadInvalidError",
    "GoamlReporterConnector",
    "HttpGoamlPortal",
    "InMemoryGoamlFilingStore",
    "PortalReply",
    "PortalTransport",
    "SimulatedGoamlPortal",
    "StructuralGoamlValidator",
    "TRANSMODE_BY_METHOD",
    "render_goaml_xml",
]

CONNECTOR_ID = "goaml_reporter_v1"

BACKOFF_INITIAL_S = 60
BACKOFF_CAP_S = 3600
CLAIM_LEASE_S = 900

#: goAML ``transmode_code`` map (spec/12 §F element table).
TRANSMODE_BY_METHOD = {
    "BKASH": "MO",
    "NAGAD": "MO",
    "ROCKET": "MO",
    "UPAY": "MO",
    "NPSB_IBFT": "CT",
    "BEFTN_CREDIT": "CT",
    "RTGS": "CT",
    "CARD": "CC",
    "CASH": "CD",
}

_STATES = frozenset(
    {
        "QUEUED",
        "SUBMITTING",
        "SUBMITTED",
        "ACK_PENDING",
        "ACKED",
        "REJECTED_BY_PORTAL",
        "MANUAL_ESCALATED",
    }
)
_ALLOWED = frozenset(
    {
        ("QUEUED", "SUBMITTING"),
        ("SUBMITTING", "SUBMITTED"),
        ("SUBMITTING", "QUEUED"),
        ("SUBMITTED", "ACK_PENDING"),
        ("ACK_PENDING", "ACKED"),
        ("ACK_PENDING", "REJECTED_BY_PORTAL"),
        ("QUEUED", "MANUAL_ESCALATED"),
    }
)

_SYSTEM_ACTOR = "system:goaml-reporter"


class GoamlPayloadInvalidError(InvalidRequestError):
    """Local XSD-slot validation refused the rendered XML (pre-queue)."""

    default_code = "goaml_payload_invalid"


class FilingTransitionDeniedError(ConflictError):
    default_code = "goaml_filing_transition_denied"


@dataclass
class GoamlFilingRow:
    """One ``goaml_filings`` row (append-only attempt chain)."""

    filing_id: str
    report_type: str
    report_ref: str
    attempt_no: int
    xml_payload_hash: str
    xml_payload_pointer: str
    state: str = "QUEUED"
    goaml_submission_ref: str | None = None
    portal_reject_reason_hash: str | None = None
    retry_count: int = 0
    next_retry_at: datetime | None = None
    submitted_at: datetime | None = None
    acked_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    schema_version: int = 1


@runtime_checkable
class GoamlFilingStore(Protocol):
    """Durable queue port for goAML filing rows."""

    durable: bool

    def enqueue(self, row: GoamlFilingRow) -> None: ...

    def save(self, row: GoamlFilingRow) -> None: ...

    def rows(self) -> list[GoamlFilingRow]: ...

    def latest_for(self, report_ref: str) -> GoamlFilingRow | None: ...

    def list_due(self, *, now: datetime) -> list[GoamlFilingRow]: ...

    def claim_due(
        self, *, now: datetime, lease_until: datetime, limit: int = 100
    ) -> list[GoamlFilingRow]: ...


class InMemoryGoamlFilingStore:
    """Deterministic in-memory goAML filing queue."""

    durable = False

    def __init__(self) -> None:
        self._rows: dict[str, GoamlFilingRow] = {}
        self._order: list[str] = []

    def enqueue(self, row: GoamlFilingRow) -> None:
        if row.filing_id in self._rows:
            raise ConflictError(
                f"goaml filing {row.filing_id!r} already exists",
                code="duplicate_filing",
            )
        self._rows[row.filing_id] = row
        self._order.append(row.filing_id)

    def save(self, row: GoamlFilingRow) -> None:
        if row.filing_id not in self._rows:
            raise ConflictError(
                f"goaml filing {row.filing_id!r} does not exist",
                code="filing_not_found",
            )
        self._rows[row.filing_id] = row

    def rows(self) -> list[GoamlFilingRow]:
        return [self._rows[k] for k in self._order]

    def latest_for(self, report_ref: str) -> GoamlFilingRow | None:
        for filing_id in reversed(self._order):
            if self._rows[filing_id].report_ref == report_ref:
                return self._rows[filing_id]
        return None

    def list_due(self, *, now: datetime) -> list[GoamlFilingRow]:
        return [
            row
            for row in self.rows()
            if row.state == "QUEUED"
            and (row.next_retry_at is None or row.next_retry_at <= now)
        ]

    def claim_due(
        self, *, now: datetime, lease_until: datetime, limit: int = 100
    ) -> list[GoamlFilingRow]:
        claimed: list[GoamlFilingRow] = []
        for row in self.rows():
            if len(claimed) >= limit:
                break
            due_queued = row.state == "QUEUED" and (
                row.next_retry_at is None or row.next_retry_at <= now
            )
            expired_claim = row.state == "SUBMITTING" and (
                row.next_retry_at is None or row.next_retry_at <= now
            )
            if not due_queued and not expired_claim:
                continue
            row.state = "SUBMITTING"
            row.next_retry_at = lease_until
            row.updated_at = now
            self.save(row)
            claimed.append(row)
        return claimed


# -- XML rendering (binding element map) -------------------------------------------


def render_goaml_xml(
    report_type: str,
    payload: dict,
    *,
    clock: Clock,
    rentity_id: str,
    reporting_person: dict | None = None,
) -> bytes:
    """Render the goAML 4.x report XML from the spec/06 handover payload."""
    if report_type not in {"STR", "CTR"}:
        raise GoamlPayloadInvalidError(f"unknown report_type {report_type!r}")
    if not payload.get("report_ref"):
        raise GoamlPayloadInvalidError("payload requires report_ref")
    report = ElementTree.Element("report")
    ElementTree.SubElement(report, "rentity_id").text = rentity_id
    ElementTree.SubElement(report, "submission_code").text = "E"
    ElementTree.SubElement(report, "report_code").text = report_type
    ElementTree.SubElement(report, "entity_reference").text = str(payload["report_ref"])
    ElementTree.SubElement(report, "submission_date").text = rfc3339(clock.now())
    person = ElementTree.SubElement(report, "reporting_person")
    for key, value in sorted((reporting_person or {}).items()):
        ElementTree.SubElement(person, key).text = str(value)
    for txn in payload.get("transactions", []):
        node = ElementTree.SubElement(report, "transaction")
        ElementTree.SubElement(node, "transactionnumber").text = str(txn["transaction_id"])
        ElementTree.SubElement(node, "date_transaction").text = str(txn["occurred_at"])
        ElementTree.SubElement(node, "transmode_code").text = TRANSMODE_BY_METHOD.get(
            str(txn.get("method", "CASH")), "CD"
        )
        # paisa -> decimal string at this render boundary only (binding).
        ElementTree.SubElement(node, "amount_local").text = paisa_to_decimal_string(
            int(txn["amount_minor"])
        )
        for party_key in ("t_from_my_client", "t_to"):
            party = txn.get(party_key)
            if party:
                party_node = ElementTree.SubElement(node, party_key)
                for key, value in sorted(party.items()):
                    ElementTree.SubElement(party_node, key).text = str(value)
    if report_type == "STR":
        if not payload.get("suspicion_narrative"):
            raise GoamlPayloadInvalidError("STR payload requires suspicion_narrative")
        ElementTree.SubElement(report, "reason").text = str(payload["suspicion_narrative"])
    else:
        totals = ElementTree.SubElement(report, "ctr_totals")
        ElementTree.SubElement(totals, "total_cash_in").text = paisa_to_decimal_string(
            int(payload.get("total_cash_in_minor", 0))
        )
        ElementTree.SubElement(totals, "total_cash_out").text = paisa_to_decimal_string(
            int(payload.get("total_cash_out_minor", 0))
        )
        ElementTree.SubElement(totals, "aggregation_date").text = str(
            payload.get("aggregation_date", "")
        )
    return ElementTree.tostring(report, encoding="utf-8", xml_declaration=True)


class StructuralGoamlValidator:
    """Default XSD-slot validator: hardened parse + required-element checks.

    The generic goAML 4.x XSD ships pinned and the BFIU BD profile is awaited
    (Open question 4); until it lands this validator enforces structure
    (well-formed XML via defusedxml — fail-closed — plus the mandatory
    elements of the binding map). Swap via registry config ``goaml_xsd_ref``.
    """

    REQUIRED = ("rentity_id", "submission_code", "report_code", "entity_reference")

    def validate(self, xml_bytes: bytes) -> None:
        from defusedxml.ElementTree import fromstring

        try:
            root = fromstring(xml_bytes.decode("utf-8"))
        except Exception as exc:
            raise GoamlPayloadInvalidError(f"goAML XML is not well-formed: {exc}") from exc
        for element in self.REQUIRED:
            node = root.find(element)
            if node is None or not (node.text or "").strip():
                raise GoamlPayloadInvalidError(f"goAML XML missing required element {element}")
        if root.findtext("report_code") not in {"STR", "CTR"}:
            raise GoamlPayloadInvalidError("report_code must be STR or CTR")


# -- Portal transport ------------------------------------------------------------------


@dataclass(frozen=True)
class PortalReply:
    status_code: int
    body: dict


@runtime_checkable
class PortalTransport(Protocol):
    """goAML portal port; live implementation is :class:`HttpGoamlPortal`."""

    async def submit_report(self, xml_bytes: bytes) -> PortalReply: ...

    async def poll_ack(self, submission_ref: str) -> PortalReply: ...


class HttpGoamlPortal:
    """Real goAML portal wire path (SANDBOX/PRODUCTION; httpx transport)."""

    def __init__(self, *, transport, credentials, config: dict) -> None:
        self._transport = transport
        self._credentials = credentials
        self._config = dict(config)

    def _headers(self) -> dict:
        session_ref = self._config.get(
            "session_ref", "openbao:secret/connectors/goaml/session_token"
        )
        return {
            "authorization": f"Bearer {self._credentials.resolve(session_ref)}",
            "content-type": "application/xml",
        }

    async def submit_report(self, xml_bytes: bytes) -> PortalReply:
        from bdpay.connectors.mfs.wire import parse_json_body

        base = str(self._config.get("base_url", "")).rstrip("/")
        response = await self._transport.request(
            "POST",
            f"{base}{self._config.get('upload_path', '/goaml/upload')}",
            headers=self._headers(),
            content=xml_bytes,
        )
        try:
            body = parse_json_body(response.body)
        except Exception:
            body = {}
        return PortalReply(status_code=response.status_code, body=body)

    async def poll_ack(self, submission_ref: str) -> PortalReply:
        from bdpay.connectors.mfs.wire import parse_json_body

        base = str(self._config.get("base_url", "")).rstrip("/")
        response = await self._transport.request(
            "GET",
            f"{base}{self._config.get('ack_path', '/goaml/ack')}/{submission_ref}",
            headers=self._headers(),
            content=b"",
        )
        try:
            body = parse_json_body(response.body)
        except Exception:
            body = {}
        return PortalReply(status_code=response.status_code, body=body)


class SimulatedGoamlPortal:
    """Deterministic SIMULATOR portal — scriptable failure/reject runs.

    ``fail_next(n)`` scripts n consecutive HTTP-500 submit failures (the
    ``portal_500_storm`` scenario); ``reject_refs`` lists entity_references
    the portal rejects at ack time (the ``xsd_reject_then_requeue`` scenario).
    Receipts are content-addressed from the XML hash — no randomness.
    """

    def __init__(self, *, reject_refs: frozenset[str] = frozenset()) -> None:
        self._fail_remaining = 0
        self._reject_refs = set(reject_refs)
        self.submissions: list[str] = []

    def fail_next(self, count: int) -> None:
        self._fail_remaining = count

    def clear_reject(self, entity_reference: str) -> None:
        self._reject_refs.discard(entity_reference)

    @staticmethod
    def _entity_reference(xml_bytes: bytes) -> str:
        from defusedxml.ElementTree import fromstring

        return fromstring(xml_bytes.decode("utf-8")).findtext("entity_reference") or ""

    async def submit_report(self, xml_bytes: bytes) -> PortalReply:
        if self._fail_remaining > 0:
            self._fail_remaining -= 1
            return PortalReply(status_code=500, body={"error": "portal_unavailable"})
        ref = "GOAML-" + sha256_canonical({"xml": xml_bytes.decode("utf-8")})[:12]
        self.submissions.append(self._entity_reference(xml_bytes))
        return PortalReply(status_code=200, body={"submission_ref": ref})

    async def poll_ack(self, submission_ref: str) -> PortalReply:
        for entity_reference in self.submissions:
            if entity_reference in self._reject_refs:
                return PortalReply(
                    status_code=200,
                    body={"status": "rejected", "reason": "schema_validation_failed"},
                )
        return PortalReply(status_code=200, body={"status": "accepted"})


# -- The connector -------------------------------------------------------------------------


class GoamlReporterConnector:
    """Frozen ``sdk.AmlFilingConnector`` implementation for ``goaml_reporter_v1``."""

    connector_id = CONNECTOR_ID

    def __init__(
        self,
        *,
        mode: ConnectorMode | str,
        clock: Clock,
        portal: PortalTransport | None = None,
        validator=None,
        object_store: ObjectStore | None = None,
        filing_store: GoamlFilingStore | None = None,
        audit: AuditSink | None = None,
        events: EventSink | None = None,
        config: dict | None = None,
        max_str_retry_for_escalation: int = 100,
    ) -> None:
        self._mode = mode if isinstance(mode, ConnectorMode) else ConnectorMode(str(mode))
        self._clock = clock
        config = dict(config or {})
        if portal is not None:
            self._portal = portal
        elif self._mode is ConnectorMode.SIMULATOR:
            self._portal = SimulatedGoamlPortal()
        elif self._mode is ConnectorMode.DISABLED:
            # DISABLED refuses in ``_file`` BEFORE any row is enqueued, so the
            # submit sweep never dereferences the portal — None is safe here.
            self._portal = None
        else:
            # FAIL CLOSED (MLPA 2012 criminal exposure): a credentialed/live
            # mode (SANDBOX / PRODUCTION / MANUAL_LOCAL) with no portal would
            # store ``None`` and then NoneType-fail inside ``process_due`` — the
            # broad ``except`` there would swallow it and a CTR/STR would be
            # SILENTLY not filed.  Refuse to construct such a connector at all;
            # the caller must wire a real :class:`PortalTransport` (BFIU goAML
            # portal + credentials are an external operator boundary).
            raise InvalidRequestError(
                f"goaml_reporter_v1 in {self._mode.value} mode requires a configured "
                "portal (PortalTransport) with BFIU goAML credentials; refusing to "
                "construct a connector that would silently drop filings. Wire the "
                "portal or run in SIMULATOR/DISABLED mode.",
                code="goaml_portal_required",
            )
        live_filing_mode = self._mode not in (ConnectorMode.SIMULATOR, ConnectorMode.DISABLED)
        allow_ephemeral_filing_store = bool(config.get("allow_ephemeral_filing_store"))
        if live_filing_mode and (
            filing_store is None
            or (
                not bool(getattr(filing_store, "durable", False))
                and not allow_ephemeral_filing_store
            )
        ):
            raise InvalidRequestError(
                f"goaml_reporter_v1 in {self._mode.value} mode requires a durable "
                "filing_store; refusing to construct a live regulatory filing "
                "connector backed only by process memory.",
                code="goaml_filing_store_required",
            )
        self._validator = validator if validator is not None else StructuralGoamlValidator()
        self._objects = object_store if object_store is not None else InMemoryObjectStore()
        self._filings = (
            filing_store if filing_store is not None else InMemoryGoamlFilingStore()
        )
        self._audit = audit if audit is not None else InMemoryAuditSink()
        self._events = events if events is not None else InMemoryEventSink()
        self._config = config
        self._max_str_retry = max_str_retry_for_escalation

    # -- queue access ------------------------------------------------------------------

    @property
    def portal(self):
        return self._portal

    @property
    def object_store(self) -> ObjectStore:
        return self._objects

    def rows(self) -> list[GoamlFilingRow]:
        return self._filings.rows()

    def latest_for(self, report_ref: str) -> GoamlFilingRow | None:
        return self._filings.latest_for(report_ref)

    # -- FSM advance (the only state writer) ----------------------------------------------

    def _advance(self, row: GoamlFilingRow, to_state: str) -> None:
        if to_state not in _STATES:
            raise ValueError(f"unknown filing state {to_state!r}")
        if (row.state, to_state) not in _ALLOWED:
            raise FilingTransitionDeniedError(
                f"filing FSM transition {row.state} -> {to_state} is DENIED (refusal-first)"
        )
        row.state = to_state
        row.updated_at = self._clock.now()
        self._filings.save(row)

    def _emit(self, event_type: str, row: GoamlFilingRow, **extra) -> None:
        payload = {
            "filing_id": row.filing_id,
            "report_type": row.report_type,
            "report_ref": row.report_ref,
        }
        payload.update(extra)
        self._events.emit(event_type, payload)

    # -- enqueue ---------------------------------------------------------------------------

    def _enqueue(self, report_type: str, payload: dict, attempt_no: int) -> GoamlFilingRow:
        rentity_id = str(self._config.get("rentity_id", "BFIU-RE-0000"))
        xml_bytes = render_goaml_xml(
            report_type,
            payload,
            clock=self._clock,
            rentity_id=rentity_id,
            reporting_person=payload.get("reporting_person"),
        )
        self._validator.validate(xml_bytes)  # local XSD-slot pre-validation
        xml_hash = sha256_canonical({"xml": xml_bytes.decode("utf-8")})
        pointer = f"goaml/{CONNECTOR_ID}/{xml_hash}"
        self._objects.put(pointer, xml_bytes)
        row = GoamlFilingRow(
            filing_id=make_spec12_id(
                "gfil", {"report_ref": str(payload["report_ref"]), "attempt_no": attempt_no}
            ),
            report_type=report_type,
            report_ref=str(payload["report_ref"]),
            attempt_no=attempt_no,
            xml_payload_hash=xml_hash,
            xml_payload_pointer=pointer,
            next_retry_at=self._clock.now(),
            created_at=self._clock.now(),
            updated_at=self._clock.now(),
        )
        self._filings.enqueue(row)
        self._audit.record(
            "GOAML_FILING_QUEUED",
            actor_id=_SYSTEM_ACTOR,
            occurred_at=row.created_at,
            detail={"filing_id": row.filing_id, "report_ref": row.report_ref},
        )
        return row

    # -- AmlFilingConnector ------------------------------------------------------------------

    async def file_str(self, payload: dict) -> str:
        return await self._file("STR", payload)

    async def file_ctr(self, payload: dict) -> str:
        return await self._file("CTR", payload)

    async def _file(self, report_type: str, payload: dict) -> str:
        if self._mode is ConnectorMode.DISABLED:
            raise FilingTransitionDeniedError(
                "goaml_reporter_v1 is DISABLED; filings must not be silently accepted"
            )
        existing = self.latest_for(str(payload.get("report_ref", "")))
        if existing is not None and existing.state not in {"REJECTED_BY_PORTAL"}:
            # Idempotent per report_ref: the existing durable chain wins.
            return existing.goaml_submission_ref or existing.filing_id
        attempt_no = (existing.attempt_no + 1) if existing is not None else 1
        row = self._enqueue(report_type, payload, attempt_no)
        await self.process_due()
        # Durable reference now; the portal ref propagates once receipted (LB8).
        latest = self.latest_for(row.report_ref) or row
        return latest.goaml_submission_ref or latest.filing_id

    # -- the never-drop submit sweep -----------------------------------------------------------

    def _backoff_s(self, retry_count: int) -> int:
        return min(BACKOFF_INITIAL_S * (2 ** max(retry_count - 1, 0)), BACKOFF_CAP_S)

    async def process_due(self) -> dict[str, str]:
        """Claim and drive every due filing through one submit attempt.

        Failure NEVER drops the row: retry_count++, exponential backoff capped
        at 1h, row stays QUEUED. The breaker is informational for this
        connector (spec/10 budget): no circuit ever blocks a filing.
        """
        outcome: dict[str, str] = {}
        now = self._clock.now()
        lease_until = now + timedelta(seconds=CLAIM_LEASE_S)
        for row in self._filings.claim_due(now=now, lease_until=lease_until):
            filing_id = row.filing_id
            xml_bytes = self._objects.get(row.xml_payload_pointer)
            try:
                reply = await self._portal.submit_report(xml_bytes)
            except Exception:
                reply = PortalReply(status_code=599, body={"error": "transport_error"})
            if reply.status_code == 200 and reply.body.get("submission_ref"):
                row.goaml_submission_ref = str(reply.body["submission_ref"])
                row.submitted_at = self._clock.now()
                row.next_retry_at = None
                self._advance(row, "SUBMITTED")
                self._emit("goaml_filing.submitted", row)
                # Portal receipt id in hand -> ack tracking begins.
                self._advance(row, "ACK_PENDING")
                outcome[filing_id] = "ACK_PENDING"
                continue
            row.retry_count += 1
            row.state = "QUEUED"
            row.next_retry_at = now + timedelta(seconds=self._backoff_s(row.retry_count))
            row.updated_at = self._clock.now()
            self._filings.save(row)
            if row.retry_count % 10 == 0:
                self._emit("goaml_filing.retrying", row, retry_count=row.retry_count)
            outcome[filing_id] = "QUEUED"
        return outcome

    async def poll_acks(self) -> dict[str, str]:
        """30-min cadence ack poll: ACK_PENDING -> ACKED | REJECTED_BY_PORTAL."""
        outcome: dict[str, str] = {}
        for row in self._filings.rows():
            filing_id = row.filing_id
            if row.state != "ACK_PENDING" or not row.goaml_submission_ref:
                continue
            try:
                reply = await self._portal.poll_ack(row.goaml_submission_ref)
            except Exception:
                continue  # ack stays pending; next sweep retries
            status = str(reply.body.get("status", ""))
            if status == "accepted":
                row.acked_at = self._clock.now()
                self._advance(row, "ACKED")
                self._emit("goaml_filing.acked", row)
                outcome[filing_id] = "ACKED"
            elif status == "rejected":
                row.portal_reject_reason_hash = sha256_canonical(
                    {"reason": str(reply.body.get("reason", ""))}
                )
                self._advance(row, "REJECTED_BY_PORTAL")
                self._emit("goaml_filing.rejected", row)
                outcome[filing_id] = "REJECTED_BY_PORTAL"
        return outcome

    async def requeue(self, report_ref: str, regenerated_payload: dict) -> GoamlFilingRow:
        """spec/06 regenerates the payload after a portal reject: new attempt
        row, chain preserved."""
        latest = self.latest_for(report_ref)
        if latest is None or latest.state != "REJECTED_BY_PORTAL":
            raise FilingTransitionDeniedError(
                "requeue requires the latest attempt to be REJECTED_BY_PORTAL"
            )
        row = self._enqueue(latest.report_type, regenerated_payload, latest.attempt_no + 1)
        await self.process_due()
        return row

    def manual_escalate(self, report_ref: str, *, actor_id: str) -> GoamlFilingRow:
        """Operator escalation after MAX_STR_RETRY (spec/06): payload preserved."""
        row = self.latest_for(report_ref)
        if row is None or row.state != "QUEUED":
            raise FilingTransitionDeniedError("manual escalation requires a QUEUED filing")
        self._advance(row, "MANUAL_ESCALATED")
        self._audit.record(
            "GOAML_FILING_MANUAL_ESCALATED",
            actor_id=actor_id,
            occurred_at=self._clock.now(),
            detail={
                "filing_id": row.filing_id,
                "report_ref": row.report_ref,
                "xml_payload_pointer": row.xml_payload_pointer,
            },
        )
        return row

    def connector_result_for(self, report_ref: str):
        """``ConnectorResult`` view of the latest attempt (FSM side-effect rows)."""
        from bdpay.connectors.sdk import ConnectorResult, ConnectorStatus

        row = self.latest_for(report_ref)
        if row is None:
            return None
        status_by_state = {
            "QUEUED": ConnectorStatus.PENDING,
            "SUBMITTED": ConnectorStatus.PENDING,
            "ACK_PENDING": ConnectorStatus.PENDING,
            "ACKED": ConnectorStatus.SUCCESS,
            "REJECTED_BY_PORTAL": ConnectorStatus.REJECTED,
            "MANUAL_ESCALATED": ConnectorStatus.FAILED,
        }
        error_code = "goaml_portal_reject" if row.state == "REJECTED_BY_PORTAL" else None
        return ConnectorResult(
            instruction_id=make_id("ins", {"report_ref": report_ref, "kind": "goaml"}),
            connector_ref=row.filing_id,
            status=status_by_state[row.state],
            rail_transaction_id=row.goaml_submission_ref,
            responded_at=rfc3339(self._clock.now()),
            error_code=error_code,
            raw_response_hash=row.xml_payload_hash,
        )
