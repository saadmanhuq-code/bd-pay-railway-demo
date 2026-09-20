"""``porichoy_ekyc_v1`` — Porichoy NID gateway adapter (spec/12 §E).

Implements the frozen ``sdk.IdentityConnector`` protocol:
``verify_nid(nid_number, dob, selfie_hash) -> {"matched", "ref", "verified_at"}``
— the binary Matched/Not-Matched contract per the May-2025 EC directive. No
citizen data is returned or stored; the clear NID transits this adapter's
memory for the single call and is never logged or persisted here (the
ocr-client PII-redaction pattern — a log-capture test fails the suite if any
NID-shaped digit run appears).

NID normalization (binding algorithm, applied before the wire call):
Bengali digits -> ASCII; strip spaces/hyphens; all-digits or refused
(``nid_format_invalid``); 10 (smart card) and 17 pass through; 13-digit legacy
NIDs are prefixed with the 4-digit birth year from ``dob`` (the standard
13->17 expansion); any other length is refused.

Failure posture: 10s budget, 2 retries (idempotent read) with backoff through
the injected sleep, breaker 5/60s. Breaker OPEN or terminal transport failure
raises the typed :class:`PorichoyUnavailableError` — the CALLER (spec/09 T10)
maps it to ``PENDING_HUMAN_REVIEW``; onboarding is never hard-blocked.
"""

from __future__ import annotations

from bdpay.connectors.breaker import CircuitBreaker
from bdpay.connectors.identity.ids12 import sha_bucket
from bdpay.connectors.mfs.wire import RawArchive, WireTransport, parse_json_body, rfc3339
from bdpay.connectors.ports import CredentialResolver, ObjectStore
from bdpay.connectors.registry import ConnectorMode
from bdpay.platform.bangla import normalize_bengali_digits
from bdpay.platform.canonical import sha256_canonical
from bdpay.platform.clock import Clock
from bdpay.platform.errors import ConnectorError, InvalidRequestError

__all__ = [
    "NidFormatError",
    "PorichoyEkycConnector",
    "PorichoyUnavailableError",
    "normalize_nid",
]

CONNECTOR_ID = "porichoy_ekyc_v1"

#: Retry budget (spec/10 row): 2 retries, idempotent read.
RETRY_BACKOFF_S = (1, 3)

#: SIMULATOR sentinels carried in ``selfie_hash`` (deterministic scripting).
SIM_NOT_MATCHED = "sim_not_matched"
SIM_OUTAGE = "sim_outage"


class NidFormatError(InvalidRequestError):
    """The NID fails the binding normalization rules (refused pre-wire)."""

    default_code = "nid_format_invalid"


class PorichoyUnavailableError(ConnectorError):
    """Typed unavailability: the caller routes to PENDING_HUMAN_REVIEW."""

    default_code = "identity_unavailable"


def normalize_nid(nid_number: str, dob: str) -> str:
    """The binding pre-wire normalization (spec/12 §E steps 1-3)."""
    if not isinstance(nid_number, str):
        raise NidFormatError("nid must be a string")
    cleaned = normalize_bengali_digits(nid_number).replace(" ", "").replace("-", "")
    if not cleaned.isdigit():
        raise NidFormatError("nid must contain digits only after normalization")
    if len(cleaned) in (10, 17):
        return cleaned
    if len(cleaned) == 13:
        birth_year = normalize_bengali_digits(dob)[:4]
        if len(birth_year) != 4 or not birth_year.isdigit():
            raise NidFormatError("13-digit nid expansion needs a 4-digit birth year in dob")
        return birth_year + cleaned
    raise NidFormatError("nid length must be 10, 13 or 17 digits")


class PorichoyEkycConnector:
    """Frozen ``sdk.IdentityConnector`` implementation for ``porichoy_ekyc_v1``."""

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
        sleep=None,
    ) -> None:
        self._mode = mode if isinstance(mode, ConnectorMode) else ConnectorMode(str(mode))
        self._clock = clock
        self._transport = transport
        self._credentials = credentials
        self._config = dict(config or {})
        self._breaker = breaker
        self._archive = RawArchive(CONNECTOR_ID, object_store)
        self._sleep = sleep if sleep is not None else _no_sleep_default

    @property
    def object_store(self) -> ObjectStore:
        return self._archive.object_store

    async def _admit(self) -> None:
        if self._breaker is None:
            return
        if not await self._breaker.admit(CONNECTOR_ID, kind="SUBMIT"):
            raise PorichoyUnavailableError(
                "porichoy circuit is open; route to human review", code="circuit_open"
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

    def _verdict(self, matched: bool, ref: str) -> dict:
        verified_at = rfc3339(self._clock.now())
        # Binary result only — the archived raw carries no citizen data.
        self._archive.archive({"matched": matched, "ref": ref, "verified_at": verified_at})
        return {"matched": matched, "ref": ref, "verified_at": verified_at}

    # -- IdentityConnector ---------------------------------------------------------

    async def verify_nid(self, nid_number: str, dob: str, selfie_hash: str) -> dict:
        normalized = normalize_nid(nid_number, dob)
        if self._mode is ConnectorMode.DISABLED:
            raise PorichoyUnavailableError(
                "porichoy_ekyc_v1 is DISABLED", code="connector_disabled"
            )
        await self._admit()
        if self._mode in (ConnectorMode.SIMULATOR, ConnectorMode.MANUAL_LOCAL):
            return await self._simulator_verify(normalized, dob, selfie_hash)
        return await self._wire_verify(normalized, dob, selfie_hash)

    # -- SIMULATOR mode: deterministic, scriptable through selfie_hash sentinels -----

    async def _simulator_verify(self, normalized: str, dob: str, selfie_hash: str) -> dict:
        if selfie_hash == SIM_OUTAGE:
            # Model the breach-era outage: every attempt is a transport failure.
            for backoff_s in (*RETRY_BACKOFF_S, None):
                await self._observe(ok=False, transport_error=True)
                if backoff_s is not None:
                    await self._sleep(backoff_s)
            raise PorichoyUnavailableError("porichoy unreachable after retry budget")
        ref = "POR-SIM-" + sha256_canonical({"nid": normalized, "dob": dob})[:12]
        matched = selfie_hash != SIM_NOT_MATCHED and sha_bucket(normalized + dob) != 7
        await self._observe(ok=True)
        return self._verdict(matched, ref)

    # -- SANDBOX / PRODUCTION wire (httpx via injected transport) ---------------------

    def _cred(self, name: str) -> str:
        refs = self._config.get("credential_refs", {})
        ref = refs.get(
            name, f"openbao:secret/connectors/porichoy/{self._mode.value.lower()}/{name}"
        )
        return self._credentials.resolve(ref)

    async def _wire_verify(self, normalized: str, dob: str, selfie_hash: str) -> dict:
        from bdpay.platform.canonical import canonical_json

        base = str(self._config.get("base_url", "")).rstrip("/")
        product_path = str(
            self._config.get("porichoy_product", "/api/v2/verifications/autofill-with-photo")
        )
        body = canonical_json(
            {
                "national_id": normalized,
                "dob": dob,
                # Pre-signed object-store URL produced by spec/09 — Porichoy
                # fetches the selfie directly; biometric bytes never proxy here.
                "selfie_url": selfie_hash,
            }
        )
        headers = {
            "x-api-key": self._cred("api_key"),
            "content-type": "application/json",
        }
        last_error: Exception | None = None
        for backoff_s in (*RETRY_BACKOFF_S, None):
            try:
                response = await self._transport.request(
                    "POST", f"{base}{product_path}", headers=headers, content=body
                )
            except Exception as exc:  # transport failure: retry (idempotent read)
                last_error = exc
                await self._observe(ok=False, transport_error=True)
                if backoff_s is not None:
                    await self._sleep(backoff_s)
                continue
            if response.status_code >= 500:
                await self._observe(ok=False)
                if backoff_s is not None:
                    await self._sleep(backoff_s)
                continue
            parsed = parse_json_body(response.body)
            matched = bool(parsed.get("verified", parsed.get("matched", False)))
            ref = str(parsed.get("verification_id", parsed.get("ref", "")))
            await self._observe(ok=True)
            return self._verdict(matched, ref)
        raise PorichoyUnavailableError(
            "porichoy unreachable after retry budget"
        ) from last_error


async def _no_sleep_default(_seconds: float) -> None:
    return None
