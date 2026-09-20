"""Acquirer egress execution + template substitution + PAN-strip (spec/14 Plane B).

The vault performs the outbound acquirer HTTPS call itself; the card
connector only ever sees the PAN-stripped response plus
``raw_response_hash`` (computed over the RAW response before stripping).

Template grammar (binding): ``{{VAULT:PAN}}``, ``{{VAULT:CVV}}``,
``{{VAULT:EXPIRY_MMYY}}``, ``{{VAULT:EXPIRY_MMYYYY}}``,
``{{VAULT:HOLDER_NAME}}`` — whole-value substitution only: a slot must be
the entire JSON string value (or the entire field content); partial-string
splicing is rejected with ``400 / placeholder_context_invalid``.

Two executors: ``HttpxAcquirerExecutor`` is the real wire path (TLS-pinned
config hook; activation needs only endpoint credentials/config), and
``SimulatedAcquirer`` is the deterministic test/simulator-mode acquirer.

TLS SPKI pinning (PCI DSS Req 4 — strong cryptography in transit): the acquirer
egress channel carries cleartext PAN. Standard CA chain validation
(``verify=True``) is necessary but not sufficient — any CA-trusted certificate
would pass. ``HttpxAcquirerExecutor`` additionally pins the acquirer endpoint's
**Subject Public Key Info** (SPKI) against a config-driven pin set (RFC 7469
``base64(sha256(SPKI-DER))`` form). Pinning is layered ON TOP of ``verify=True``
and is enforced **during the TLS handshake, before any request body is written**:
the pin check runs in a :class:`ssl.SSLObject.do_handshake` override, so a leaf
whose key is not pinned aborts the connection *before* the cleartext PAN reaches
the wire. A non-matching pin — or no peer certificate at all when pins are
configured — fails closed; cleartext PAN never leaves without a verified pin.

Two further fail-closed guards layer onto this (PCI Req 4): the PAN-carrying
egress URL MUST use the ``https`` scheme — any non-``https`` URL is rejected with
:class:`InsecureEgressSchemeError` as the first act of ``execute`` (before a
client is built), so cleartext PAN never goes over a plaintext channel; and a
live executor (``require_pins=True``, set by the wiring's live egress branch)
refuses to construct with zero SPKI pins, because an unpinned acquirer channel
relies on CA validation alone.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import ssl
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

import httpx
from cryptography import x509
from cryptography.hazmat.primitives import serialization

from bdpay.platform.canonical import sha256_canonical
from bdpay.platform.errors import InvalidRequestError
from bdpay.vault.cards import luhn_valid

__all__ = [
    "ALLOWED_SLOTS",
    "AcquirerEgressExecutor",
    "EgressResponse",
    "EgressTimeoutError",
    "EgressUnreachableError",
    "HttpxAcquirerExecutor",
    "InsecureEgressSchemeError",
    "PinValidationError",
    "SimulatedAcquirer",
    "host_of",
    "pinned_ssl_context",
    "raw_response_hash",
    "spki_pin_sha256",
    "strip_response",
    "substitute_template",
    "template_slots",
    "verify_spki_pin",
]

ALLOWED_SLOTS = frozenset(
    {
        "{{VAULT:PAN}}",
        "{{VAULT:CVV}}",
        "{{VAULT:EXPIRY_MMYY}}",
        "{{VAULT:EXPIRY_MMYYYY}}",
        "{{VAULT:HOLDER_NAME}}",
    }
)

_SLOT_RE = re.compile(r"\{\{VAULT:[A-Z_]+\}\}")
_PAN_RUN_RE = re.compile(r"(?<!\d)\d{13,19}(?!\d)")
_SENSITIVE_FIELD_RE = re.compile(r"^(pan|card.?number|track\d?|cvv2?|cvc\d?)$", re.IGNORECASE)
_SENSITIVE_TEXT_RE = re.compile(
    r"(\"(?:pan|card.?number|track\d?|cvv2?|cvc\d?)\"\s*:\s*)\"(?:[^\"\\]|\\.)*\"",
    re.IGNORECASE,
)


class EgressTimeoutError(Exception):
    """The acquirer call exceeded ``timeout_ms`` (partial contact possible)."""


class EgressUnreachableError(Exception):
    """No contact could be made with the acquirer endpoint."""


class InsecureEgressSchemeError(EgressUnreachableError):
    """The acquirer egress URL was not ``https://`` (PCI Req 4).

    The acquirer egress channel carries the cleartext PAN, so it MUST be TLS:
    a plaintext ``http://`` (or any non-``https``) scheme would put the PAN on
    the wire with no encryption and no SPKI pin check at all. A subclass of
    :class:`EgressUnreachableError` so the existing fail-closed egress path
    denies the detokenization with ``acquirer_unreachable`` (no cleartext PAN is
    transmitted) without widening the caller contract; the distinct type names
    the insecure-scheme cause for ops/audit.
    """


class PinValidationError(EgressUnreachableError):
    """The acquirer TLS leaf SPKI did not match the configured pin set, or no
    peer certificate could be obtained while pins were configured (PCI Req 4).

    A subclass of :class:`EgressUnreachableError` so the existing fail-closed
    egress path already denies the detokenization with ``acquirer_unreachable``
    (no cleartext PAN is transmitted); the distinct type names the pin-failure
    cause for ops/audit without widening the caller contract.
    """


def spki_pin_sha256(cert_der: bytes) -> str:
    """RFC 7469 SPKI pin: ``base64(sha256(SubjectPublicKeyInfo DER))`` of a leaf
    certificate (passed in DER form). This is the public-key pin, NOT a pin over
    the whole certificate, so it survives certificate renewal that keeps the key."""
    cert = x509.load_der_x509_certificate(cert_der)
    spki_der = cert.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return base64.b64encode(hashlib.sha256(spki_der).digest()).decode("ascii")


def verify_spki_pin(cert_der: bytes | None, pins: frozenset[str]) -> None:
    """Fail closed unless the leaf SPKI pin is in ``pins`` (PCI Req 4).

    Raises :class:`PinValidationError` when ``pins`` is non-empty and either no
    peer certificate was presented (``cert_der is None``) or the computed pin is
    not in the set. With an empty ``pins`` set this is a no-op — pinning is opt-in
    via configuration, and an unpinned channel relies on ``verify=True`` alone."""
    if not pins:
        return
    if cert_der is None:
        raise PinValidationError(
            "acquirer SPKI pinning is configured but no peer certificate was "
            "presented (fail closed)"
        )
    presented = spki_pin_sha256(cert_der)
    if presented not in pins:
        raise PinValidationError(
            "acquirer TLS leaf SPKI did not match any configured pin (fail closed)"
        )


def pinned_ssl_context(verify: bool | str, pins: frozenset[str]) -> ssl.SSLContext:
    """An SSL context that does standard CA validation (``verify``) AND enforces
    the SPKI pin set *at handshake time* (PCI Req 4).

    The pin check runs inside an :class:`ssl.SSLObject.do_handshake` override, so
    a non-pinned leaf aborts the connection BEFORE the request body (cleartext
    PAN) is ever written to the socket — detection-after-send would already have
    leaked the PAN to an impostor that merely chains to a trusted CA. httpx's
    async transport uses memory-BIO (``wrap_bio``) sockets, which instantiate
    ``sslobject_class``; ``sslsocket_class`` is overridden too for the sync path.

    ``verify`` mirrors httpx's parameter: ``True`` -> the system trust store,
    ``str`` -> a CA bundle path. ``verify=False`` is rejected when pins are set —
    pinning on an unauthenticated channel is meaningless and unsafe."""
    if verify is False:
        raise ValueError(
            "SPKI pinning requires certificate verification (verify must not be False)"
        )
    if isinstance(verify, str):
        context = ssl.create_default_context(cafile=verify)
    else:
        context = ssl.create_default_context()

    pin_set = frozenset(pins)

    def _check(ssl_obj: ssl.SSLObject | ssl.SSLSocket) -> None:
        try:
            cert_der = ssl_obj.getpeercert(binary_form=True)
        except ValueError:
            cert_der = None
        verify_spki_pin(cert_der, pin_set)

    class _PinnedSSLObject(ssl.SSLObject):
        def do_handshake(self) -> None:  # memory-BIO variant takes no args
            super().do_handshake()
            _check(self)

    class _PinnedSSLSocket(ssl.SSLSocket):
        def do_handshake(self, block: bool = False) -> None:
            super().do_handshake(block)
            _check(self)

    context.sslobject_class = _PinnedSSLObject
    context.sslsocket_class = _PinnedSSLSocket
    return context


@dataclass(frozen=True)
class EgressResponse:
    status_code: int
    headers: dict[str, str]
    body: bytes


@runtime_checkable
class AcquirerEgressExecutor(Protocol):
    async def execute(
        self, *, method: str, url: str, headers: dict[str, str], body: str, timeout_ms: int
    ) -> EgressResponse: ...


def host_of(url: str) -> str:
    return httpx.URL(url).host


def template_slots(template: str) -> frozenset[str]:
    """All ``{{VAULT:*}}`` slots present; raises on any slot outside the
    binding grammar (guard 6 first half)."""
    found = frozenset(_SLOT_RE.findall(template))
    unknown = found - ALLOWED_SLOTS
    if unknown:
        raise InvalidRequestError(
            "template contains a slot outside the binding grammar",
            code="placeholder_context_invalid",
        )
    return found


def substitute_template(template: str, values: dict[str, str]) -> str:
    """Whole-value substitution: every slot occurrence must be the entire
    quoted JSON string value (``"{{VAULT:PAN}}"``); splicing a slot into a
    larger string is rejected."""
    for slot in template_slots(template):
        quoted = '"' + slot + '"'
        if template.count(slot) != template.count(quoted):
            raise InvalidRequestError(
                "slot must be the entire string value (no partial splicing)",
                code="placeholder_context_invalid",
            )
    out = template
    for slot, value in values.items():
        out = out.replace('"' + slot + '"', json.dumps(value, ensure_ascii=False))
    return out


def raw_response_hash(status_code: int, headers: dict[str, str], body: bytes) -> str:
    """sha256(canonical_json({status, headers, body_b64})) over the RAW
    response — computed BEFORE any strip; surfaced unchanged as
    ``ConnectorResult.raw_response_hash`` (SDK invariant)."""
    return sha256_canonical(
        {
            "status": status_code,
            "headers": {key.lower(): value for key, value in sorted(headers.items())},
            "body_b64": base64.b64encode(body).decode("ascii"),
        }
    )


def _strip_text(text: str, pan: str, token: str) -> str:
    marker = f"tok:{token}:REDACTED"
    if pan:
        text = text.replace(pan, marker)
    text = _PAN_RUN_RE.sub(
        lambda match: marker if luhn_valid(match.group(0)) else match.group(0), text
    )
    return _SENSITIVE_TEXT_RE.sub(lambda match: match.group(1) + json.dumps(marker), text)


def _strip_json(value: object, pan: str, token: str) -> object:
    marker = f"tok:{token}:REDACTED"
    if isinstance(value, dict):
        out: dict = {}
        for key, item in value.items():
            if isinstance(key, str) and _SENSITIVE_FIELD_RE.match(key):
                out[key] = marker
            else:
                out[key] = _strip_json(item, pan, token)
        return out
    if isinstance(value, list):
        return [_strip_json(item, pan, token) for item in value]
    if isinstance(value, str):
        return _strip_text(value, pan, token)
    if isinstance(value, int) and not isinstance(value, bool):
        digits = str(value)
        if 13 <= len(digits) <= 19 and luhn_valid(digits):
            return marker
    return value


def strip_response(
    status_code: int, headers: dict[str, str], body: bytes, *, pan: str, token: str
) -> tuple[dict[str, str], str]:
    """Binding strip order (spec/14): (a) every occurrence of the injected
    PAN; (b) every 13-19-digit Luhn-valid run; (c) every value of
    header/body fields named like pan|card.?number|track|cvv|cvc — each
    replaced with ``tok:<token>:REDACTED``. Returns (headers, body) stripped.
    The raw response is not stored anywhere — hash only."""
    marker = f"tok:{token}:REDACTED"
    stripped_headers: dict[str, str] = {}
    for key, value in headers.items():
        if _SENSITIVE_FIELD_RE.match(key):
            stripped_headers[key.lower()] = marker
        else:
            stripped_headers[key.lower()] = _strip_text(value, pan, token)
    text = body.decode("utf-8", errors="replace")
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        return stripped_headers, _strip_text(text, pan, token)
    cleaned = _strip_json(parsed, pan, token)
    if isinstance(cleaned, str):
        cleaned = _strip_text(cleaned, pan, token)
        return stripped_headers, json.dumps(cleaned, ensure_ascii=False)
    return stripped_headers, json.dumps(
        _strip_json_text_pass(cleaned, pan, token), ensure_ascii=False, separators=(",", ":")
    )


def _strip_json_text_pass(value: object, pan: str, token: str) -> object:
    """Second pass over an already field-stripped structure: digit runs that
    survive inside non-sensitive string values still get redacted."""
    if isinstance(value, dict):
        return {key: _strip_json_text_pass(item, pan, token) for key, item in value.items()}
    if isinstance(value, list):
        return [_strip_json_text_pass(item, pan, token) for item in value]
    if isinstance(value, str):
        return _strip_text(value, pan, token)
    if isinstance(value, int) and not isinstance(value, bool):
        digits = str(value)
        if 13 <= len(digits) <= 19 and luhn_valid(digits):
            return f"tok:{token}:REDACTED"
    return value


class HttpxAcquirerExecutor:
    """Real outbound acquirer call (sandbox/live wire path — fully written;
    activation needs only endpoint configuration). ``transport`` injection
    keeps the wire code testable without a network.

    ``spki_pins`` (RFC 7469 ``base64(sha256(SPKI-DER))`` strings) enables TLS
    public-key pinning on top of ``verify=True``, enforced DURING the handshake
    (see :func:`pinned_ssl_context`): a non-pinned leaf aborts the connection
    before the request body (cleartext PAN) is written. When pins are configured
    the ``verify`` argument must authenticate the channel (not ``False``), and a
    non-TLS transport (e.g. an injected mock) is refused — the PAN must never go
    over a channel where the pin cannot be checked (PCI Req 4)."""

    def __init__(
        self,
        *,
        verify: bool | str = True,
        transport: httpx.AsyncBaseTransport | None = None,
        spki_pins: frozenset[str] | None = None,
        require_pins: bool = False,
    ) -> None:
        self._spki_pins = frozenset(spki_pins) if spki_pins else frozenset()
        self._transport = transport
        # Live/production egress (PCI Req 4): SPKI pinning is mandatory. An
        # unpinned acquirer channel that carries cleartext PAN relies on CA
        # validation alone — any CA-trusted impostor would pass. Refuse to
        # construct a live executor with zero pins (fail closed at wiring).
        if require_pins and not self._spki_pins:
            raise ValueError(
                "live acquirer egress requires at least one SPKI pin "
                "(no pins configured — fail closed)"
            )
        if self._spki_pins:
            if verify is False:
                raise ValueError(
                    "SPKI pinning requires certificate verification (verify must not be False)"
                )
            # Handshake-time pin enforcement: the pinned context aborts the TLS
            # handshake before any application data (the PAN) is sent.
            self._verify: bool | str | ssl.SSLContext = pinned_ssl_context(
                verify, self._spki_pins
            )
        else:
            self._verify = verify

    @staticmethod
    def _find_pin_error(exc: BaseException) -> PinValidationError | None:
        """Recover a :class:`PinValidationError` raised inside the TLS handshake
        from the wrapping httpx/ssl exception chain (cause/context)."""
        seen: set[int] = set()
        current: BaseException | None = exc
        while current is not None and id(current) not in seen:
            if isinstance(current, PinValidationError):
                return current
            seen.add(id(current))
            current = current.__cause__ or current.__context__
        return None

    async def execute(
        self, *, method: str, url: str, headers: dict[str, str], body: str, timeout_ms: int
    ) -> EgressResponse:
        # Scheme guard (PCI Req 4): the body carries the cleartext PAN, so the
        # channel MUST be TLS. Reject any non-https scheme BEFORE a client is
        # built or a byte is sent — an http:// URL would ship the PAN with no
        # encryption and never consult the SPKI pin. Fail closed.
        if httpx.URL(url).scheme != "https":
            raise InsecureEgressSchemeError(
                "acquirer egress URL must use https (cleartext PAN must never "
                "leave over a non-TLS scheme — fail closed)"
            )
        if self._spki_pins and self._transport is not None:
            # A non-TLS transport cannot present a peer certificate to pin: fail
            # closed rather than ship the PAN over an unpinned channel.
            raise PinValidationError(
                "SPKI pinning is configured but the transport is not a real TLS "
                "connection (fail closed)"
            )
        timeout = httpx.Timeout(timeout_ms / 1000.0)
        try:
            async with httpx.AsyncClient(
                verify=self._verify, transport=self._transport, timeout=timeout
            ) as client:
                response = await client.request(
                    method, url, headers=headers, content=body.encode("utf-8")
                )
        except httpx.TimeoutException as exc:
            raise EgressTimeoutError(str(type(exc).__name__)) from exc
        except httpx.TransportError as exc:
            pin_error = self._find_pin_error(exc)
            if pin_error is not None:
                raise pin_error from exc
            raise EgressUnreachableError(str(type(exc).__name__)) from exc
        return EgressResponse(
            status_code=response.status_code,
            headers={key.lower(): value for key, value in response.headers.items()},
            body=response.content,
        )


class SimulatedAcquirer:
    """Deterministic acquirer for simulator-mode certification and tests.

    Behavior is selected by the ``X-Sim-Behavior`` request header:

    - ``approved`` (default): 200, body echoes the submitted pan/cvv fields
      back (exercises the strip path exactly like a chatty real acquirer);
    - ``declined``: 200 with ``result: declined`` and an ``error_code``;
    - ``server_error``: 503 body;
    - ``timeout``: raises :class:`EgressTimeoutError`;
    - ``unreachable``: raises :class:`EgressUnreachableError`.

    No wall-clock reads; identical inputs yield identical responses.
    """

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def execute(
        self, *, method: str, url: str, headers: dict[str, str], body: str, timeout_ms: int
    ) -> EgressResponse:
        behavior = headers.get("X-Sim-Behavior", headers.get("x-sim-behavior", "approved"))
        self.calls.append({"method": method, "url": url, "behavior": behavior})
        if behavior == "timeout":
            raise EgressTimeoutError("simulated acquirer timeout")
        if behavior == "unreachable":
            raise EgressUnreachableError("simulated acquirer unreachable")
        try:
            submitted = json.loads(body)
        except ValueError:
            submitted = {}
        if behavior == "declined":
            payload = {"result": "declined", "error_code": "do_not_honour"}
            return EgressResponse(
                status_code=200,
                headers={"content-type": "application/json"},
                body=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
            )
        if behavior == "server_error":
            return EgressResponse(
                status_code=503,
                headers={"content-type": "application/json"},
                body=b'{"result":"unavailable"}',
            )
        payload = {
            "result": "approved",
            "approval_code": sha256_canonical({"body": body})[:6],
            "pan": submitted.get("pan", ""),
            "cvv2": submitted.get("cvv2", ""),
            "echo": submitted,
        }
        return EgressResponse(
            status_code=200,
            headers={"content-type": "application/json"},
            body=json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8"),
        )
