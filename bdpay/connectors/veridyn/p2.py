"""``veridyn_p2_compliance_v1`` — Veridyn P2 compliance sidecar adapter (spec/12 §H).

THE TIERING RULE IS BINDING: P2 verdicts are ADVISORY (anything that could
inform a payment decision) or EVIDENCE (case-management / STR-narrative
support) — ``veridyn_check_requests.tier`` admits ONLY those two values and
``AUTHORITATIVE_PAYMENT`` is structurally impossible. The synchronous money
path never calls this adapter: it implements no ``submit``/``query_status``/
``reverse`` (it is not a ``PaymentConnector``), its registration carries no
payment capability and no supported method (see ``registration.py``), and the
caller reference is gated to the compliance-side identities (``aml_``,
``case_``, ``str_``) — a PaymentIntent/Attempt/Instruction id is refused
pre-wire. P2 outage therefore cannot delay or alter a payment.

Wire (governance-client pattern, portfolio-core — ADAPT): ``POST
{base_url}/governance/check`` with ``x-tenant-id`` + bearer client token; the
request body is ``{"facts", "domain", "pack_hint"}`` over the one E12
canonical serializer. The response carries the signed
``authoritative_output_envelope`` — the client is OPEN-SCHEMA PASS-THROUGH:
no field whitelisting, unknown fields preserved verbatim into
``envelope_pointer``. The adapter verifies the envelope's Ed25519 signature
against the PINNED P2 public key; an invalid signature stores the envelope,
marks it unusable as evidence and alarms (never silently trusted, never
silently dropped). Deployment config names only env-var/ref names for the
live endpoint (``VERIDYN_P2_URL`` / ``VERIDYN_P2_CLIENT_TOKEN``) — no
credential value ever appears in code or config files.

Budget (spec/10 row via the registry): 20s hard timeout, 0 retries — the
caller re-asks; the breaker is 5/60s INFORMATIONAL (nothing waits on this
connector synchronously). Idempotent on the content-addressed ``vchk`` id:
an ANSWERED check replays without touching the sidecar again; a FAILED or
TIMED_OUT check may be re-asked (one new sidecar attempt, same row identity).
"""

from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
from datetime import datetime

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from bdpay.connectors.breaker import CircuitBreaker
from bdpay.connectors.mfs.wire import RawArchive, WireTransport, parse_json_body, rfc3339
from bdpay.connectors.ports import (
    AuditSink,
    CredentialResolver,
    EventSink,
    InMemoryAuditSink,
    InMemoryEventSink,
    ObjectStore,
)
from bdpay.connectors.registry import ConnectorMode
from bdpay.connectors.veridyn.ids import make_veridyn_id
from bdpay.platform.canonical import CanonicalError, canonical_json, sha256_canonical
from bdpay.platform.clock import Clock
from bdpay.platform.errors import ConflictError, ConnectorError, InvalidRequestError

__all__ = [
    "ALLOWED_CALLER_PREFIXES",
    "CONNECTOR_ID",
    "CheckTransitionDeniedError",
    "DEFAULT_TIMEOUT_MS",
    "TIERS",
    "VeridynCallerError",
    "VeridynCheckRow",
    "VeridynFactBundleError",
    "VeridynP2ComplianceConnector",
    "VeridynTierError",
    "VeridynUnavailableError",
    "check_row_summary",
    "usable_as_evidence",
]

CONNECTOR_ID = "veridyn_p2_compliance_v1"

#: spec/12 §H budget row: 20s hard timeout, 0 retries (caller re-asks).
DEFAULT_TIMEOUT_MS = 20_000

#: The binding tier set — NEVER 'AUTHORITATIVE_PAYMENT' (spec/12 §H DDL CHECK).
TIERS = ("ADVISORY", "EVIDENCE")

#: ``caller_ref`` admits only compliance-side identities (spec/12 §H DDL:
#: ``aml_<…> | case_<…> | str_<…>``). Payment-path ids are refused pre-wire.
ALLOWED_CALLER_PREFIXES = frozenset({"aml", "case", "str"})

_STATUSES = frozenset({"PENDING", "ANSWERED", "FAILED", "TIMED_OUT"})
#: Refusal-first status FSM. FAILED/TIMED_OUT -> PENDING is the caller
#: re-ask (spec/12 §H: 0 retries, caller re-asks); PENDING -> PENDING covers
#: a re-ask after an interrupted (cancelled mid-flight) call; ANSWERED is
#: terminal and replays without a second sidecar touch.
_ALLOWED_TRANSITIONS = frozenset(
    {
        ("PENDING", "ANSWERED"),
        ("PENDING", "FAILED"),
        ("PENDING", "TIMED_OUT"),
        ("PENDING", "PENDING"),
        ("FAILED", "PENDING"),
        ("TIMED_OUT", "PENDING"),
    }
)

_SYSTEM_ACTOR = "system:veridyn-p2"

#: Default live-endpoint ref names — the deployment resolver maps these to
#: the operator VM's URL and client token. Names only; never values.
_BASE_URL_REF_DEFAULT = "env:VERIDYN_P2_URL"
_CLIENT_TOKEN_REF_DEFAULT = "env:VERIDYN_P2_CLIENT_TOKEN"


class VeridynTierError(InvalidRequestError):
    """The tier is outside ADVISORY|EVIDENCE (the binding firewall tiering)."""

    default_code = "veridyn_tier_forbidden"


class VeridynCallerError(InvalidRequestError):
    """The caller_ref is not a compliance-side identity (refused pre-wire)."""

    default_code = "veridyn_caller_forbidden"


class VeridynFactBundleError(InvalidRequestError):
    """The fact bundle cannot be canonically content-addressed (refused)."""

    default_code = "veridyn_fact_bundle_invalid"


class VeridynUnavailableError(ConnectorError):
    """Typed unavailability — the case workbench shows 'advisory unavailable';
    zero payment impact by construction (the money path never calls here)."""

    default_code = "veridyn_unavailable"


class CheckTransitionDeniedError(ConflictError):
    default_code = "veridyn_check_transition_denied"


@dataclass
class VeridynCheckRow:
    """One ``veridyn_check_requests`` row (spec/12 Data Model, append-only)."""

    check_id: str
    caller_ref: str
    tier: str
    fact_bundle_hash: str
    fact_bundle_pointer: str
    envelope_hash: str | None = None
    envelope_pointer: str | None = None
    envelope_signature_valid: bool | None = None
    pack_hash: str | None = None
    status: str = "PENDING"
    latency_ms: int | None = None
    requested_at: datetime | None = None
    responded_at: datetime | None = None
    schema_version: int = 1


def usable_as_evidence(row: VeridynCheckRow) -> bool:
    """A check is evidence-grade ONLY when answered AND signature-valid."""
    return row.status == "ANSWERED" and row.envelope_signature_valid is True


class VeridynP2ComplianceConnector:
    """The spec/12 §H bespoke sidecar surface for ``veridyn_p2_compliance_v1``.

    Deliberately NOT a ``PaymentConnector``: there is no ``submit``, no
    ``query_status``, no ``reverse`` — the kernel's only dispatch path cannot
    type-check this adapter onto a payment, and the registration layer pins
    the same invariant at capability level.
    """

    connector_id = CONNECTOR_ID

    def __init__(
        self,
        *,
        mode: ConnectorMode | str,
        clock: Clock,
        sidecar=None,
        transport: WireTransport | None = None,
        credentials: CredentialResolver | None = None,
        config: dict | None = None,
        breaker: CircuitBreaker | None = None,
        object_store: ObjectStore | None = None,
        audit: AuditSink | None = None,
        events: EventSink | None = None,
        timeout_ms: int | None = None,
        wait_for=None,
    ) -> None:
        self._mode = mode if isinstance(mode, ConnectorMode) else ConnectorMode(str(mode))
        self._clock = clock
        self._config = dict(config or {})
        if sidecar is not None:
            self._sidecar = sidecar
        elif self._mode is ConnectorMode.SIMULATOR:
            from bdpay.connectors.simulators.veridyn_p2_sim import SimulatedP2Sidecar

            self._sidecar = SimulatedP2Sidecar(clock=clock)
        else:
            self._sidecar = None
        self._transport = transport
        self._credentials = credentials
        self._breaker = breaker
        self._archive = RawArchive(CONNECTOR_ID, object_store)
        self._audit = audit if audit is not None else InMemoryAuditSink()
        self._events = events if events is not None else InMemoryEventSink()
        self._timeout_ms = (
            int(timeout_ms)
            if timeout_ms is not None
            else int(self._config.get("timeout_ms", DEFAULT_TIMEOUT_MS))
        )
        self._wait_for = wait_for if wait_for is not None else asyncio.wait_for
        self._rows: dict[str, VeridynCheckRow] = {}
        self._order: list[str] = []

    # -- row access ---------------------------------------------------------------

    @property
    def object_store(self) -> ObjectStore:
        return self._archive.object_store

    @property
    def sidecar(self):
        return self._sidecar

    def rows(self) -> list[VeridynCheckRow]:
        return [self._rows[k] for k in self._order]

    def row_for(self, check_id: str) -> VeridynCheckRow | None:
        return self._rows.get(check_id)

    # -- FSM advance (the only status writer) ---------------------------------------

    def _advance(self, row: VeridynCheckRow, to_status: str) -> None:
        if to_status not in _STATUSES:
            raise ValueError(f"unknown check status {to_status!r}")
        if (row.status, to_status) not in _ALLOWED_TRANSITIONS:
            raise CheckTransitionDeniedError(
                f"check FSM transition {row.status} -> {to_status} is DENIED (refusal-first)"
            )
        row.status = to_status

    # -- validation gates (all refusals happen BEFORE any wire/sidecar touch) --------

    @staticmethod
    def _validate_tier(tier: str) -> str:
        if tier not in TIERS:
            raise VeridynTierError(
                "tier must be ADVISORY or EVIDENCE; the authoritative payment "
                "verdict path never reaches this connector"
            )
        return tier

    @staticmethod
    def _validate_caller(caller_ref: str) -> str:
        prefix, _, rest = str(caller_ref).partition("_")
        if prefix not in ALLOWED_CALLER_PREFIXES or not rest:
            raise VeridynCallerError(
                "caller_ref must be a compliance-side identity "
                "(aml_/case_/str_); payment-path identities are refused"
            )
        return caller_ref

    def _content_address(self, facts: dict) -> str:
        if not isinstance(facts, dict) or not facts:
            raise VeridynFactBundleError("the fact bundle must be a non-empty dict")
        try:
            return sha256_canonical(facts)
        except CanonicalError as exc:
            raise VeridynFactBundleError(
                f"fact bundle is not canonically serializable: {exc}"
            ) from exc

    # -- breaker (informational per spec/12 §H — observed, never load-bearing) --------

    async def _admit(self) -> None:
        if self._breaker is None:
            return
        if not await self._breaker.admit(CONNECTOR_ID, kind="SUBMIT"):
            raise VeridynUnavailableError(
                "veridyn p2 circuit denies; advisory unavailable", code="circuit_open"
            )

    async def _observe(self, status: str, *, transport_error: bool = False) -> None:
        if self._breaker is None:
            return
        from bdpay.connectors.sdk import ConnectorStatus

        outcome = (
            ConnectorStatus.SUCCESS
            if status == "ANSWERED"
            else (
                ConnectorStatus.TIMED_OUT
                if status == "TIMED_OUT"
                else ConnectorStatus.FAILED
            )
        )
        await self._breaker.observe(CONNECTOR_ID, outcome, transport_error=transport_error)

    # -- pinned key -------------------------------------------------------------------

    def _pinned_public_key(self) -> Ed25519PublicKey:
        pinned_b64 = self._config.get("p2_public_key_b64")
        if not pinned_b64 and self._sidecar is not None:
            pinned_b64 = getattr(self._sidecar, "public_key_b64", None)
        if not pinned_b64:
            raise VeridynUnavailableError(
                "no pinned P2 public key configured; envelopes cannot be "
                "verified — checks are refused (fail-closed)",
                code="p2_public_key_unpinned",
            )
        try:
            decoded = base64.b64decode(str(pinned_b64), validate=True)
            return Ed25519PublicKey.from_public_bytes(decoded)
        except (ValueError, TypeError) as exc:
            raise VeridynUnavailableError(
                "the pinned P2 public key is not a valid Ed25519 key",
                code="p2_public_key_invalid",
            ) from exc

    def _verify_envelope(self, envelope: dict, signature_hex: object) -> bool:
        if not isinstance(signature_hex, str) or not signature_hex:
            return False
        try:
            signature = bytes.fromhex(signature_hex)
        except ValueError:
            return False
        try:
            self._pinned_public_key().verify(
                signature, sha256_canonical(envelope).encode("utf-8")
            )
        except InvalidSignature:
            return False
        return True

    # -- telemetry ---------------------------------------------------------------------

    def _emit(self, event_type: str, row: VeridynCheckRow) -> None:
        self._events.emit(
            event_type,
            {
                "check_id": row.check_id,
                "tier": row.tier,
                "envelope_signature_valid": row.envelope_signature_valid,
                "latency_ms": row.latency_ms,
            },
        )

    def _audit_outcome(self, action: str, row: VeridynCheckRow, detail_code: str) -> None:
        self._audit.record(
            action,
            actor_id=_SYSTEM_ACTOR,
            occurred_at=self._clock.now(),
            detail={
                "check_id": row.check_id,
                "caller_ref": row.caller_ref,
                "tier": row.tier,
                "status": row.status,
                "detail_code": detail_code,
            },
        )

    def _resolve_failure(
        self, row: VeridynCheckRow, status: str, detail_code: str
    ) -> VeridynCheckRow:
        row.responded_at = self._clock.now()
        row.latency_ms = _latency_ms(row.requested_at, row.responded_at)
        self._advance(row, status)
        self._audit_outcome("VERIDYN_CHECK_FAILED", row, detail_code)
        self._emit("veridyn_check.failed", row)
        return row

    # -- the check surface ----------------------------------------------------------------

    async def check(self, *, caller_ref: str, tier: str, facts: dict) -> VeridynCheckRow:
        """One P2 sidecar check; idempotent on the content-addressed vchk id."""
        self._validate_tier(tier)
        self._validate_caller(caller_ref)
        fact_bundle_hash = self._content_address(facts)
        if self._mode is ConnectorMode.DISABLED:
            raise VeridynUnavailableError(
                "veridyn_p2_compliance_v1 is DISABLED", code="connector_disabled"
            )
        if self._mode not in (
            ConnectorMode.SIMULATOR,
            ConnectorMode.SANDBOX,
            ConnectorMode.PRODUCTION,
        ):
            raise VeridynUnavailableError(
                "an advisory sidecar has no manual contingency; mode refused",
                code="mode_unsupported",
            )
        check_id = make_veridyn_id(
            "vchk", {"caller_ref": caller_ref, "fact_bundle_hash": fact_bundle_hash}
        )
        existing = self._rows.get(check_id)
        if existing is not None and existing.status == "ANSWERED":
            return existing  # idempotent replay: no second sidecar touch
        # All refusal gates fire BEFORE the row mutates: a denied check must
        # never strand a PENDING row (circuit denial / missing pinned key).
        await self._admit()
        self._pinned_public_key()  # fail-closed pre-wire: unverifiable => refused
        if existing is not None:
            self._advance(existing, "PENDING")  # caller re-ask
            row = existing
            row.requested_at = self._clock.now()
            row.responded_at = None
            row.latency_ms = None
        else:
            pointer = f"veridyn/{CONNECTOR_ID}/facts/{fact_bundle_hash}"
            self._archive.object_store.put(pointer, canonical_json(facts))
            row = VeridynCheckRow(
                check_id=check_id,
                caller_ref=caller_ref,
                tier=tier,
                fact_bundle_hash=fact_bundle_hash,
                fact_bundle_pointer=pointer,
                requested_at=self._clock.now(),
            )
            self._rows[check_id] = row
            self._order.append(check_id)
        request_body = {
            "facts": facts,
            "domain": str(self._config.get("domain", "payment_systems_bd")),
            "pack_hint": str(
                self._config.get("pack_hint", "pack_veridyn_bd_full_compiled")
            ),
        }
        transport_error = False
        try:
            if self._mode is ConnectorMode.SIMULATOR:
                response = await self._wait_for(
                    self._sidecar.governance_check(request_body),
                    timeout=self._timeout_ms / 1000,
                )
                self._ingest_response(row, response)
            else:
                await self._wire_check(row, request_body)
        except TimeoutError:
            self._resolve_failure(row, "TIMED_OUT", "hard_timeout")
        except asyncio.CancelledError:
            raise
        except VeridynUnavailableError:
            raise
        except Exception:
            transport_error = True
            self._resolve_failure(row, "FAILED", "transport_error")
        await self._observe(row.status, transport_error=transport_error)
        return row

    # -- SANDBOX / PRODUCTION wire (real code; activated purely by config/credentials) --

    def _base_url(self) -> str:
        configured = self._config.get("base_url")
        if configured:
            return str(configured).rstrip("/")
        ref = str(self._config.get("base_url_ref", _BASE_URL_REF_DEFAULT))
        return str(self._credentials.resolve(ref)).rstrip("/")

    def _client_token(self) -> str:
        refs = self._config.get("credential_refs", {})
        return self._credentials.resolve(
            str(refs.get("client_token", _CLIENT_TOKEN_REF_DEFAULT))
        )

    def _headers(self) -> dict:
        return {
            "x-tenant-id": str(self._config.get("tenant_id", "bdpay")),
            "authorization": f"Bearer {self._client_token()}",
            "content-type": "application/json",
        }

    async def _wire_check(self, row: VeridynCheckRow, request_body: dict) -> None:
        check_path = str(self._config.get("check_path", "/governance/check"))
        response = await self._wait_for(
            self._transport.request(
                "POST",
                f"{self._base_url()}{check_path}",
                headers=self._headers(),
                content=canonical_json(request_body),
            ),
            timeout=self._timeout_ms / 1000,
        )
        if response.status_code < 200 or response.status_code >= 300:
            try:
                self._archive.archive(parse_json_body(response.body))
            except Exception:
                self._archive.archive(
                    {"http_status": response.status_code, "body_undecodable": True}
                )
            self._resolve_failure(row, "FAILED", f"p2_http_{response.status_code}")
            return
        self._ingest_response(row, parse_json_body(response.body))

    # -- shared ingest: archive raw, pass envelope through verbatim, verify pin ---------

    def _ingest_response(self, row: VeridynCheckRow, response: dict) -> None:
        self._archive.archive(response)  # raw_response_hash discipline (E12)
        envelope = response.get("authoritative_output_envelope")
        if not isinstance(envelope, dict) or not envelope:
            self._resolve_failure(row, "FAILED", "envelope_missing")
            return
        envelope_hash = sha256_canonical(envelope)
        pointer = f"veridyn/{CONNECTOR_ID}/envelopes/{envelope_hash}"
        # Open-schema pass-through: every field of the signed envelope is
        # stored verbatim (canonical bytes of the FULL dict — no whitelist).
        self._archive.object_store.put(pointer, canonical_json(envelope))
        row.envelope_hash = envelope_hash
        row.envelope_pointer = pointer
        row.envelope_signature_valid = self._verify_envelope(
            envelope, response.get("envelope_signature")
        )
        pack_hash = envelope.get("pack_hash")
        row.pack_hash = str(pack_hash) if pack_hash is not None else None
        row.responded_at = self._clock.now()
        row.latency_ms = _latency_ms(row.requested_at, row.responded_at)
        self._advance(row, "ANSWERED")
        if not row.envelope_signature_valid:
            # Stored but unusable as evidence + alarm (spec/12 §K veridyn row).
            self._audit_outcome(
                "VERIDYN_ENVELOPE_SIGNATURE_INVALID", row, "envelope_signature_invalid"
            )
        self._audit_outcome(
            "VERIDYN_CHECK_ANSWERED",
            row,
            "answered" if row.envelope_signature_valid else "answered_signature_invalid",
        )
        self._emit("veridyn_check.answered", row)

    # -- health ---------------------------------------------------------------------------

    async def health_check(self) -> bool:
        if self._mode is ConnectorMode.DISABLED:
            return False
        if self._mode is ConnectorMode.SIMULATOR:
            return self._sidecar is not None
        if self._mode not in (ConnectorMode.SANDBOX, ConnectorMode.PRODUCTION):
            return False
        health_path = str(self._config.get("health_path", "/health"))
        try:
            response = await self._wait_for(
                self._transport.request(
                    "GET",
                    f"{self._base_url()}{health_path}",
                    headers={"x-tenant-id": str(self._config.get("tenant_id", "bdpay"))},
                    content=b"",
                ),
                timeout=self._timeout_ms / 1000,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            return False
        return 200 <= response.status_code < 300


def _latency_ms(requested_at: datetime | None, responded_at: datetime) -> int:
    if requested_at is None:
        return 0
    return int((responded_at - requested_at).total_seconds() * 1000)


def check_row_summary(row: VeridynCheckRow) -> dict:
    """PII-free operator view of one check (the ``GET /v1/veridyn/checks/{id}``
    shape: metadata + envelope pointer; raw envelope stays investigator-gated)."""
    return {
        "check_id": row.check_id,
        "caller_ref": row.caller_ref,
        "tier": row.tier,
        "status": row.status,
        "fact_bundle_hash": row.fact_bundle_hash,
        "envelope_hash": row.envelope_hash,
        "envelope_pointer": row.envelope_pointer,
        "envelope_signature_valid": row.envelope_signature_valid,
        "pack_hash": row.pack_hash,
        "latency_ms": row.latency_ms,
        "usable_as_evidence": usable_as_evidence(row),
        "requested_at": rfc3339(row.requested_at) if row.requested_at else None,
        "responded_at": rfc3339(row.responded_at) if row.responded_at else None,
    }
