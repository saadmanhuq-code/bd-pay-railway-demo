"""``veridyn_ocr_v1`` — VeridynOCR eKYC document-OCR provider adapter.

Capability sub-protocol per the spec/12 pattern (spec/00 §10 permits one
capability sub-protocol per non-payment connector; the frozen ``sdk.py`` is
never edited, so :class:`OcrConnector` is declared here, exactly like the
spec/12 §I HSM surface is declared in its own module).

The wire contract is the PROVEN consumer shape ported from
``portfolio-core/packages/ocr-client`` (ADAPT PATTERN per the spec/12 module
reuse table, row "ocr-client retry/redaction pattern"):

- ``POST {base_url}/documents/extract`` — multipart/form-data, field ``file``
- headers ``Authorization: Bearer <key>`` AND ``x-api-key: <key>`` (both)
- response text priority ``full_text_normalized`` > ``rawText`` > ``text`` >
  ``text_preview``; ``confidence`` defaults to 0.9 when absent; ``warnings``
  defaults to empty; per-page passthrough via ``pages[]``
- retry budget: 2 retries after the initial attempt, exponential backoff,
  ONLY for transport failures and 5xx; 401/403 are terminal (never retried —
  retrying a dead key only amplifies an auth incident); a 2xx body that
  cannot be parsed is terminal (contract mismatch, not a transient fault)
- filenames are sanitised to ``doc_<sha256[:12]>.<ext>`` before they reach
  the wire (the donor's telemetry-redaction rule)

Configuration (env names only — never a literal credential):

- ``VERIDYN_OCR_URL``      -> registry config ``base_url`` (composition root)
- ``VERIDYN_OCR_API_KEY``  -> credential ref
  ``openbao:secret/connectors/veridyn_ocr/<mode>/api_key``

The real service exists and is live; this adapter's SANDBOX/PRODUCTION wire
code is fully written and activates purely by credentials/config (spec/10
modes rule). Nothing in this repository calls the live endpoint: tests run
the identical wire code against the deterministic gateway in
:mod:`bdpay.connectors.identity.ocr_sim`.

Failure posture: any terminal failure raises the typed
:class:`OcrUnavailableError` family — the CALLER (the spec/09 eKYC intake)
maps every provider exception to ``PENDING_HUMAN_REVIEW``; onboarding is
never hard-blocked on OCR availability (the spec/09 T10 posture applied to
the document-OCR port).

PII posture: the OCR text is NID-bearing. It returns to the kernel intake in
memory, the raw parsed response is archived content-addressed to the object
store (E12 discipline — ``raw_response_hash`` over the parsed wire response),
and NOTHING is ever logged by this adapter. A log-capture test fails the
suite if an NID-shaped digit run appears in captured logs.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Protocol, runtime_checkable

from bdpay.connectors.breaker import CircuitBreaker
from bdpay.connectors.identity.ids12 import sha_bucket
from bdpay.connectors.mfs.wire import RawArchive, WireTransport, parse_json_body, rfc3339
from bdpay.connectors.ports import CredentialResolver, ObjectStore
from bdpay.connectors.registry import ConnectorMode
from bdpay.platform.clock import Clock
from bdpay.platform.errors import ConnectorError

__all__ = [
    "CONNECTOR_ID",
    "DEFAULT_CONFIDENCE",
    "EXTRACT_PATH",
    "OcrAuthRejectedError",
    "OcrConnector",
    "OcrExtraction",
    "OcrPage",
    "OcrResponseInvalidError",
    "OcrUnavailableError",
    "SIM_AUTH_REJECT",
    "SIM_GARBLED",
    "SIM_LOW_CONFIDENCE",
    "SIM_OUTAGE",
    "VeridynOcrConnector",
    "parse_extraction_body",
    "sanitize_document_name",
]

CONNECTOR_ID = "veridyn_ocr_v1"

#: The proven consumer's request path (portfolio-core ocr-client).
EXTRACT_PATH = "/documents/extract"

#: Env var names the composition root maps into config/credential refs.
BASE_URL_ENV = "VERIDYN_OCR_URL"
API_KEY_ENV = "VERIDYN_OCR_API_KEY"

#: Retry budget: 2 retries after the initial attempt (donor DEFAULT_MAX_RETRIES),
#: exponential shape through the injected sleep (seconds granularity here; the
#: donor's 200ms base is sub-second — the budget COUNT is the bound that
#: matters and is preserved exactly).
RETRY_BACKOFF_S = (1, 2)

#: Donor default when the response carries no ``confidence`` field.
DEFAULT_CONFIDENCE = Decimal("0.9")

#: SIMULATOR sentinels carried in the object key (deterministic scripting,
#: same pattern as the porichoy adapter's selfie_hash sentinels).
SIM_OUTAGE = "sim_outage"
SIM_AUTH_REJECT = "sim_auth_reject"
SIM_LOW_CONFIDENCE = "sim_low_confidence"
SIM_GARBLED = "sim_garbled"


class OcrUnavailableError(ConnectorError):
    """Typed unavailability: the eKYC caller routes to PENDING_HUMAN_REVIEW."""

    default_code = "ocr_unavailable"


class OcrAuthRejectedError(OcrUnavailableError):
    """401/403 from the OCR service — terminal, NEVER retried (donor rule)."""

    default_code = "ocr_auth_rejected"


class OcrResponseInvalidError(OcrUnavailableError):
    """A 2xx response whose body is not the contract shape — terminal, not
    retried (a successful status with a bad body is a contract mismatch, not
    a transient fault — donor OcrParseError semantics)."""

    default_code = "ocr_response_invalid"


@dataclass(frozen=True)
class OcrPage:
    """One extracted page (donor ``pages[]`` passthrough)."""

    index: int
    text: str


@dataclass(frozen=True)
class OcrExtraction:
    """The OCR result as the kernel intake consumes it.

    ``confidence`` is Decimal by construction — the wire parser strings every
    JSON decimal literal (``parse_float=str``) so a float can never enter the
    platform (spec/00 §6 discipline applied to a non-money decimal).
    """

    raw_text: str
    confidence: Decimal
    warnings: tuple[str, ...]
    pages: tuple[OcrPage, ...]
    raw_response_hash: str
    extracted_at: str  # RFC3339 UTC, injected clock

    def __post_init__(self) -> None:
        if isinstance(self.confidence, float):
            raise OcrResponseInvalidError("confidence must be Decimal (floats rejected)")


@runtime_checkable
class OcrConnector(Protocol):
    """The capability sub-protocol for eKYC document-OCR providers.

    Mirrors the minimalism of ``sdk.IdentityConnector``: one verb. Any raised
    exception means "provider unavailable" to the caller, which routes the
    KYC record to ``PENDING_HUMAN_REVIEW`` (spec/09 non-blocking fallback).
    """

    connector_id: str

    async def extract_document(
        self, object_key: str, *, mime_type: str = "image/jpeg"
    ) -> OcrExtraction: ...


def sanitize_document_name(object_key: str) -> str:
    """Donor ``sanitizeFilename``: ``doc_<sha256[:12]>.<ext>`` — the original
    name never reaches the wire or telemetry."""
    if not object_key:
        return "file.bin"
    basename = object_key.rsplit("/", 1)[-1]
    parts = basename.split(".")
    ext = parts[-1] if len(parts) > 1 and parts[-1] else "bin"
    digest = hashlib.sha256(object_key.encode("utf-8")).hexdigest()[:12]
    return f"doc_{digest}.{ext}"


def _multipart_body(
    *, file_bytes: bytes, filename: str, mime_type: str
) -> tuple[bytes, str]:
    """Multipart/form-data body with field ``file`` (the donor request shape).

    The boundary is content-addressed from the payload — no RNG anywhere in
    the adapter (the spec/12 §B determinism rule for adapter-generated
    values), prefixed so it cannot collide with the body bytes in practice.
    """
    digest = hashlib.sha256(file_bytes + filename.encode()).hexdigest()[:24]
    boundary = f"bdpay-ocr-{digest}"
    head = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f"Content-Type: {mime_type}\r\n\r\n"
    ).encode()
    tail = f"\r\n--{boundary}--\r\n".encode()
    return head + file_bytes + tail, f"multipart/form-data; boundary={boundary}"


_TEXT_FIELD_PRIORITY = ("full_text_normalized", "rawText", "text", "text_preview")


def parse_extraction_body(
    parsed: dict,
) -> tuple[str, Decimal, tuple[str, ...], tuple[OcrPage, ...]]:
    """Donor field-fallback mapping over the parsed wire body.

    Divergence from the donor (documented, refusal-first): a body with NO
    recognised text field is REFUSED (``ocr_response_invalid``) instead of
    silently coerced to an empty string.
    """
    raw_text: str | None = None
    for field in _TEXT_FIELD_PRIORITY:
        value = parsed.get(field)
        if isinstance(value, str):
            raw_text = value
            break
    if raw_text is None:
        raise OcrResponseInvalidError(
            "OCR response carries none of the contract text fields "
            f"{_TEXT_FIELD_PRIORITY}"
        )
    confidence_src = parsed.get("confidence")
    if confidence_src is None:
        confidence = DEFAULT_CONFIDENCE
    else:
        try:
            confidence = Decimal(str(confidence_src))
        except InvalidOperation as exc:
            raise OcrResponseInvalidError(
                "OCR response confidence is not a decimal value"
            ) from exc
        if not Decimal("0") <= confidence <= Decimal("1"):
            raise OcrResponseInvalidError("OCR response confidence outside [0, 1]")
    warnings_src = parsed.get("warnings")
    warnings = (
        tuple(str(w) for w in warnings_src) if isinstance(warnings_src, list) else ()
    )
    pages_src = parsed.get("pages")
    pages: tuple[OcrPage, ...] = ()
    if isinstance(pages_src, list):
        built: list[OcrPage] = []
        for position, page in enumerate(pages_src):
            obj = page if isinstance(page, dict) else {}
            index = obj.get("index")
            text = obj.get("text")
            built.append(
                OcrPage(
                    index=index if isinstance(index, int) else position,
                    text=text if isinstance(text, str) else "",
                )
            )
        pages = tuple(built)
    return raw_text, confidence, warnings, pages


class VeridynOcrConnector:
    """:class:`OcrConnector` implementation for ``veridyn_ocr_v1``.

    Modes (spec/10 — all four implemented, nothing pending):

    - ``DISABLED``: every call refused (``connector_disabled``).
    - ``SIMULATOR`` / ``MANUAL_LOCAL``: deterministic Bengali OCR fixtures
      (no I/O; scriptable through object-key sentinels and overrides).
    - ``SANDBOX`` / ``PRODUCTION``: the real httpx wire path through the
      injected transport — activation needs only credentials/config.
    """

    connector_id = CONNECTOR_ID

    def __init__(
        self,
        *,
        mode: ConnectorMode | str,
        clock: Clock,
        transport: WireTransport | None = None,
        credentials: CredentialResolver | None = None,
        config: dict | None = None,
        breaker: CircuitBreaker | None = None,
        object_store: ObjectStore | None = None,
        fixture_overrides: dict[str, str] | None = None,
        sleep=None,
    ) -> None:
        self._mode = mode if isinstance(mode, ConnectorMode) else ConnectorMode(str(mode))
        self._clock = clock
        self._transport = transport
        self._credentials = credentials
        self._config = dict(config or {})
        self._breaker = breaker
        self._archive = RawArchive(CONNECTOR_ID, object_store)
        self._fixture_overrides = dict(fixture_overrides or {})
        self._sleep = sleep if sleep is not None else _no_sleep_default

    @property
    def object_store(self) -> ObjectStore:
        return self._archive.object_store

    # -- breaker plumbing (identical posture to the porichoy adapter) ----------------

    async def _admit(self) -> None:
        if self._breaker is None:
            return
        if not await self._breaker.admit(CONNECTOR_ID, kind="SUBMIT"):
            raise OcrUnavailableError(
                "veridyn_ocr circuit is open; route to human review", code="circuit_open"
            )

    async def _observe(self, *, ok: bool, transport_error: bool = False) -> None:
        if self._breaker is None:
            return
        from bdpay.connectors.sdk import ConnectorStatus

        await self._breaker.observe(
            CONNECTOR_ID,
            ConnectorStatus.SUCCESS if ok else ConnectorStatus.FAILED,
            transport_error=transport_error,
        )

    # -- OcrConnector -----------------------------------------------------------------

    async def extract_document(
        self, object_key: str, *, mime_type: str = "image/jpeg"
    ) -> OcrExtraction:
        if not isinstance(object_key, str) or not object_key:
            raise OcrResponseInvalidError(
                "object_key must be a non-empty string", code="ocr_document_key_invalid"
            )
        if self._mode is ConnectorMode.DISABLED:
            raise OcrUnavailableError(
                "veridyn_ocr_v1 is DISABLED", code="connector_disabled"
            )
        await self._admit()
        if self._mode in (ConnectorMode.SIMULATOR, ConnectorMode.MANUAL_LOCAL):
            return await self._simulator_extract(object_key)
        return await self._wire_extract(object_key, mime_type=mime_type)

    # -- SIMULATOR mode: deterministic Bengali OCR text fixtures -----------------------

    async def _simulator_extract(self, object_key: str) -> OcrExtraction:
        from bdpay.connectors.identity.ocr_sim import (
            FIXTURE_GARBLED,
            fixture_text_for,
        )

        if SIM_OUTAGE in object_key:
            # Model a service outage: every attempt is a transport failure so
            # the breaker sees the identical signal as the wire path.
            for backoff_s in (*RETRY_BACKOFF_S, None):
                await self._observe(ok=False, transport_error=True)
                if backoff_s is not None:
                    await self._sleep(backoff_s)
            raise OcrUnavailableError("veridyn_ocr unreachable after retry budget")
        if SIM_AUTH_REJECT in object_key:
            await self._observe(ok=False)
            raise OcrAuthRejectedError(
                "OCR upstream rejected credentials (simulated 401)"
            )
        if object_key in self._fixture_overrides:
            text = self._fixture_overrides[object_key]
            confidence = Decimal("0.93")
        elif SIM_GARBLED in object_key:
            text = FIXTURE_GARBLED
            confidence = Decimal("0.93")
        elif SIM_LOW_CONFIDENCE in object_key:
            text = fixture_text_for(object_key)
            confidence = Decimal("0.40")
        else:
            text = fixture_text_for(object_key)
            confidence = Decimal("0.93")
        await self._observe(ok=True)
        return self._build(
            raw_text=text,
            confidence=confidence,
            warnings=(),
            pages=(OcrPage(index=0, text=text),),
            raw={
                "simulator": True,
                "object_key": object_key,
                "full_text_normalized": text,
                "confidence": str(confidence),
                "bucket": sha_bucket(object_key),
            },
        )

    # -- SANDBOX / PRODUCTION wire (httpx via injected transport) ----------------------

    def _cred(self, name: str) -> str:
        refs = self._config.get("credential_refs", {})
        ref = refs.get(
            name,
            f"openbao:secret/connectors/veridyn_ocr/{self._mode.value.lower()}/{name}",
        )
        return self._credentials.resolve(ref)

    async def _wire_extract(self, object_key: str, *, mime_type: str) -> OcrExtraction:
        base = str(self._config.get("base_url", "")).rstrip("/")
        if not base:
            # Fail closed: never guess an endpoint. VERIDYN_OCR_URL populates
            # this config key at the composition root.
            raise OcrUnavailableError(
                f"base_url is not configured (set {BASE_URL_ENV})",
                code="ocr_config_missing",
            )
        api_key = self._cred("api_key")
        file_bytes = self._archive.object_store.get(object_key)
        body, content_type = _multipart_body(
            file_bytes=file_bytes,
            filename=sanitize_document_name(object_key),
            mime_type=mime_type,
        )
        headers = {
            # Both header forms, exactly as the proven consumer sends them.
            "authorization": f"Bearer {api_key}",
            "x-api-key": api_key,
            "content-type": content_type,
        }
        last_error: Exception | None = None
        for backoff_s in (*RETRY_BACKOFF_S, None):
            try:
                response = await self._transport.request(
                    "POST", f"{base}{EXTRACT_PATH}", headers=headers, content=body
                )
            except Exception as exc:  # transport failure: transient, retry
                last_error = exc
                await self._observe(ok=False, transport_error=True)
                if backoff_s is not None:
                    await self._sleep(backoff_s)
                continue
            if response.status_code in (401, 403):
                await self._observe(ok=False)
                raise OcrAuthRejectedError(
                    f"OCR upstream rejected credentials (status {response.status_code})"
                )
            if response.status_code >= 500:
                await self._observe(ok=False)
                if backoff_s is not None:
                    await self._sleep(backoff_s)
                continue
            if response.status_code >= 400:
                # Non-auth 4xx: terminal upstream failure, not worth re-asking.
                await self._observe(ok=False)
                raise OcrUnavailableError(
                    f"OCR upstream returned status {response.status_code}",
                    code="ocr_upstream_rejected",
                )
            try:
                parsed = parse_json_body(response.body)
                raw_text, confidence, warnings, pages = parse_extraction_body(parsed)
            except OcrResponseInvalidError:
                await self._observe(ok=False)
                raise
            except Exception as exc:
                await self._observe(ok=False)
                raise OcrResponseInvalidError(
                    "OCR response was not a valid JSON object"
                ) from exc
            await self._observe(ok=True)
            return self._build(
                raw_text=raw_text,
                confidence=confidence,
                warnings=warnings,
                pages=pages,
                raw=parsed,
            )
        raise OcrUnavailableError(
            "veridyn_ocr unreachable after retry budget"
        ) from last_error

    # -- shared result assembly ---------------------------------------------------------

    def _build(
        self,
        *,
        raw_text: str,
        confidence: Decimal,
        warnings: tuple[str, ...],
        pages: tuple[OcrPage, ...],
        raw: dict,
    ) -> OcrExtraction:
        # Raw parsed response archived content-addressed (never logged); the
        # hash travels with the extraction for the kernel's audit row.
        raw_response_hash = self._archive.archive(raw)
        return OcrExtraction(
            raw_text=raw_text,
            confidence=confidence,
            warnings=warnings,
            pages=pages,
            raw_response_hash=raw_response_hash,
            extracted_at=rfc3339(self._clock.now()),
        )


async def _no_sleep_default(_seconds: float) -> None:
    return None
