"""``hsm_thales_v1`` — HSM call boundary (spec/12 §I; key custody is spec/14).

Thin surface: exactly five verbs over a PKCS#11-shaped session. Binding rules:
key **labels** only — no key material ever crosses this boundary or enters
caller memory; 5s timeout; breaker 2/30s; breaker OPEN => **fail-closed** (the
NPSB MAC-bearing and card paths become unavailable — an unauthenticated
financial message is never sent). ``verify_mac`` returning False is NOT an
error (callers fail closed on it). Error taxonomy: ``hsm_unavailable``,
``hsm_key_not_found``, ``hsm_command_rejected_<code>``.

Modes: SIMULATOR/SANDBOX run the deterministic **soft-HSM** (cryptography
lib: AES-CMAC MACs, AES key-wrap-with-padding for ZMK transport, AES-ECB
single-block PIN-block translation) with keys derived from a seed credential —
the designed posture until the payShield hardware lands (spec/14). PRODUCTION
runs the payShield host-command TCP client (framing fully written; command
codes are registry config). DISABLED refuses every verb.

Audit discipline: every call records ``verb + key_label + preimage_hash`` —
never preimage bytes; PIN-path calls record labels only (no PIN material, not
even hashed).
"""

from __future__ import annotations

import asyncio
import hashlib
from typing import Protocol, runtime_checkable

from bdpay.connectors.breaker import CircuitBreaker
from bdpay.connectors.ports import AuditSink, CredentialResolver, InMemoryAuditSink
from bdpay.connectors.registry import ConnectorMode
from bdpay.platform.clock import Clock
from bdpay.platform.errors import ConnectorError

__all__ = [
    "AsyncioTcpHostTransport",
    "HostCommandTransport",
    "HsmCommandRejectedError",
    "HsmConnector",
    "HsmKeyNotFoundError",
    "HsmThalesConnector",
    "HsmUnavailableError",
    "PayshieldHostCommandClient",
    "SoftHsmSession",
]

CONNECTOR_ID = "hsm_thales_v1"

#: Hard call budget (spec/10 row): 5s, no retries, breaker 2/30s.
HSM_TIMEOUT_S = 5.0

_SYSTEM_ACTOR = "system:hsm-boundary"


class HsmUnavailableError(ConnectorError):
    default_code = "hsm_unavailable"


class HsmKeyNotFoundError(ConnectorError):
    """Config error — page engineering (spec/12 §I taxonomy)."""

    default_code = "hsm_key_not_found"


class HsmCommandRejectedError(ConnectorError):
    """payShield rejected the command; code carries the rail's reject code."""

    def __init__(self, reject_code: str) -> None:
        super().__init__(
            f"hsm command rejected with code {reject_code}",
            code=f"hsm_command_rejected_{reject_code}",
        )


@runtime_checkable
class HsmConnector(Protocol):
    """The bespoke HSM surface (spec/12 §I — exactly five verbs)."""

    connector_id: str

    async def generate_mac(self, key_label: str, preimage: bytes) -> bytes: ...

    async def verify_mac(self, key_label: str, preimage: bytes, mac: bytes) -> bool: ...

    async def translate_pin_block(
        self, src_key_label: str, dst_key_label: str, pin_block: bytes, pan_token: str
    ) -> bytes: ...

    async def encrypt_under_zmk(self, zmk_label: str, clear: bytes) -> bytes: ...

    async def decrypt_under_zmk(self, zmk_label: str, cipher: bytes) -> bytes: ...


# -- Soft-HSM (PKCS#11-shaped session over the cryptography lib) -----------------------


class SoftHsmSession:
    """Deterministic soft-HSM: label-addressed AES keys derived from a seed.

    The session object mirrors the PKCS#11 mental model (open session, find
    key by label, run mechanism) without key material ever leaving the
    session: callers pass labels, never keys.
    """

    def __init__(self, *, seed: str, key_labels: tuple[str, ...] = ()) -> None:
        self._seed = seed
        self._labels = set(key_labels)

    def register_label(self, label: str) -> None:
        self._labels.add(label)

    def _key(self, label: str) -> bytes:
        if self._labels and label not in self._labels:
            raise HsmKeyNotFoundError(f"no key under label {label}")
        return hashlib.sha256(f"{self._seed}:{label}".encode()).digest()

    def generate_mac(self, key_label: str, preimage: bytes) -> bytes:
        from cryptography.hazmat.primitives import cmac
        from cryptography.hazmat.primitives.ciphers import algorithms

        mac = cmac.CMAC(algorithms.AES(self._key(key_label)))
        mac.update(preimage)
        return mac.finalize()

    def verify_mac(self, key_label: str, preimage: bytes, mac: bytes) -> bool:
        import hmac as hmac_mod

        expected = self.generate_mac(key_label, preimage)
        return hmac_mod.compare_digest(expected, mac)

    def translate_pin_block(
        self, src_key_label: str, dst_key_label: str, pin_block: bytes
    ) -> bytes:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

        if len(pin_block) != 16:
            raise HsmCommandRejectedError("15")  # invalid input data block
        decryptor = Cipher(algorithms.AES(self._key(src_key_label)), modes.ECB()).decryptor()
        clear_block = decryptor.update(pin_block) + decryptor.finalize()
        encryptor = Cipher(algorithms.AES(self._key(dst_key_label)), modes.ECB()).encryptor()
        return encryptor.update(clear_block) + encryptor.finalize()

    def encrypt_under_zmk(self, zmk_label: str, clear: bytes) -> bytes:
        from cryptography.hazmat.primitives.keywrap import aes_key_wrap_with_padding

        return aes_key_wrap_with_padding(self._key(zmk_label), clear)

    def decrypt_under_zmk(self, zmk_label: str, cipher: bytes) -> bytes:
        from cryptography.hazmat.primitives.keywrap import (
            InvalidUnwrap,
            aes_key_unwrap_with_padding,
        )

        try:
            return aes_key_unwrap_with_padding(self._key(zmk_label), cipher)
        except InvalidUnwrap as exc:
            raise HsmCommandRejectedError("10") from exc  # unwrap integrity failure


# -- payShield host-command client (PRODUCTION wire path) -------------------------------


@runtime_checkable
class HostCommandTransport(Protocol):
    """One request/response exchange with the payShield host port."""

    async def exchange(self, frame: bytes) -> bytes: ...


class AsyncioTcpHostTransport:
    """Real TCP transport (asyncio streams) for the payShield host port."""

    def __init__(self, host: str, port: int) -> None:
        self._host = host
        self._port = port

    async def exchange(self, frame: bytes) -> bytes:
        reader, writer = await asyncio.open_connection(self._host, self._port)
        try:
            writer.write(frame)
            await writer.drain()
            length_bytes = await reader.readexactly(2)
            length = int.from_bytes(length_bytes, "big")
            return await reader.readexactly(length)
        finally:
            writer.close()
            await writer.wait_closed()


class PayshieldHostCommandClient:
    """payShield host-command framing: 2-byte length + 4-char header + code.

    Command codes are registry config (payShield variants differ by firmware
    and LMK scheme); responses echo the header, carry ``code+1`` as the
    response code and a two-digit error code (``"00"`` = success). Fields are
    hex-encoded.
    """

    DEFAULT_COMMANDS = {
        "generate_mac": "M6",
        "verify_mac": "M8",
        "translate_pin": "CA",
        "encrypt_zmk": "A0",
        "decrypt_zmk": "A2",
    }

    def __init__(
        self,
        transport: HostCommandTransport,
        *,
        header: str = "0000",
        commands: dict | None = None,
    ) -> None:
        self._transport = transport
        self._header = header
        self._commands = {**self.DEFAULT_COMMANDS, **dict(commands or {})}

    def _frame(self, command: str, fields: list[str]) -> bytes:
        message = (self._header + command + ";".join(fields)).encode("ascii")
        return len(message).to_bytes(2, "big") + message

    async def _exchange(self, verb: str, fields: list[str]) -> str:
        command = self._commands[verb]
        try:
            response = await self._transport.exchange(self._frame(command, fields))
        except Exception as exc:
            raise HsmUnavailableError(f"hsm host unreachable: {type(exc).__name__}") from exc
        text = response.decode("ascii", errors="replace")
        if len(text) < 8:
            raise HsmUnavailableError("hsm response frame too short")
        error_code = text[6:8]
        if error_code != "00":
            raise HsmCommandRejectedError(error_code)
        return text[8:]

    async def generate_mac(self, key_label: str, preimage: bytes) -> bytes:
        payload = await self._exchange("generate_mac", [key_label, preimage.hex()])
        return bytes.fromhex(payload)

    async def verify_mac(self, key_label: str, preimage: bytes, mac: bytes) -> bool:
        try:
            await self._exchange("verify_mac", [key_label, preimage.hex(), mac.hex()])
            return True
        except HsmCommandRejectedError as exc:
            if exc.code == "hsm_command_rejected_01":  # MAC verification failure
                return False
            raise

    async def translate_pin_block(
        self, src_key_label: str, dst_key_label: str, pin_block: bytes
    ) -> bytes:
        payload = await self._exchange(
            "translate_pin", [src_key_label, dst_key_label, pin_block.hex()]
        )
        return bytes.fromhex(payload)

    async def encrypt_under_zmk(self, zmk_label: str, clear: bytes) -> bytes:
        payload = await self._exchange("encrypt_zmk", [zmk_label, clear.hex()])
        return bytes.fromhex(payload)

    async def decrypt_under_zmk(self, zmk_label: str, cipher: bytes) -> bytes:
        payload = await self._exchange("decrypt_zmk", [zmk_label, cipher.hex()])
        return bytes.fromhex(payload)


# -- The connector ---------------------------------------------------------------------------


class HsmThalesConnector:
    """The ``hsm_thales_v1`` boundary: breaker-guarded, timeout-bounded verbs."""

    connector_id = CONNECTOR_ID

    def __init__(
        self,
        *,
        mode: ConnectorMode | str,
        clock: Clock,
        breaker: CircuitBreaker | None = None,
        credentials: CredentialResolver | None = None,
        config: dict | None = None,
        host_transport: HostCommandTransport | None = None,
        audit: AuditSink | None = None,
        timeout_s: float = HSM_TIMEOUT_S,
        wait_for=None,
    ) -> None:
        self._mode = mode if isinstance(mode, ConnectorMode) else ConnectorMode(str(mode))
        self._clock = clock
        self._breaker = breaker
        self._audit = audit if audit is not None else InMemoryAuditSink()
        self._timeout_s = timeout_s
        self._wait_for = wait_for if wait_for is not None else asyncio.wait_for
        config = dict(config or {})
        if self._mode in (ConnectorMode.SIMULATOR, ConnectorMode.MANUAL_LOCAL):
            self._session = SoftHsmSession(
                seed=str(config.get("soft_seed", "soft-hsm-sim-seed")),
                key_labels=tuple(config.get("key_labels", ())),
            )
        elif self._mode is ConnectorMode.SANDBOX:
            seed_ref = config.get("soft_seed_ref", "openbao:secret/connectors/hsm/soft_seed")
            self._session = SoftHsmSession(
                seed=credentials.resolve(seed_ref),
                key_labels=tuple(config.get("key_labels", ())),
            )
        elif self._mode is ConnectorMode.PRODUCTION:
            self._session = PayshieldHostCommandClient(
                host_transport, commands=config.get("host_commands")
            )
        else:
            self._session = None

    # -- guard rails --------------------------------------------------------------------

    async def _admit(self, verb: str) -> None:
        if self._mode is ConnectorMode.DISABLED or self._session is None:
            raise HsmUnavailableError("hsm_thales_v1 is DISABLED")
        if self._breaker is not None and not await self._breaker.admit(
            CONNECTOR_ID, kind="SUBMIT"
        ):
            # Fail-closed: NPSB MAC-bearing + card paths are unavailable.
            raise HsmUnavailableError(f"hsm circuit open; {verb} refused", code="hsm_unavailable")

    async def _observe(self, *, ok: bool, transport_error: bool = False) -> None:
        if self._breaker is None:
            return
        from bdpay.connectors.sdk import ConnectorStatus

        await self._breaker.observe(
            CONNECTOR_ID,
            ConnectorStatus.SUCCESS if ok else ConnectorStatus.FAILED,
            transport_error=transport_error,
        )

    def _audit_call(self, verb: str, labels: tuple[str, ...], preimage: bytes | None) -> None:
        detail: dict = {"verb": verb, "key_labels": list(labels)}
        if preimage is not None:  # PIN paths pass None — labels only, ever.
            detail["preimage_hash"] = hashlib.sha256(preimage).hexdigest()
        self._audit.record(
            "HSM_CALL", actor_id=_SYSTEM_ACTOR, occurred_at=self._clock.now(), detail=detail
        )

    async def _run(self, verb: str, labels: tuple[str, ...], preimage: bytes | None, call):
        await self._admit(verb)
        self._audit_call(verb, labels, preimage)
        try:
            if self._mode is ConnectorMode.PRODUCTION:
                result = await self._wait_for(call(), timeout=self._timeout_s)
            else:
                result = call()
        except HsmKeyNotFoundError:
            await self._observe(ok=True)  # config error, not rail health
            raise
        except HsmCommandRejectedError:
            await self._observe(ok=False)
            raise
        except TimeoutError as exc:
            await self._observe(ok=False, transport_error=True)
            raise HsmUnavailableError("hsm call exceeded the 5s budget") from exc
        except HsmUnavailableError:
            await self._observe(ok=False, transport_error=True)
            raise
        except Exception as exc:
            await self._observe(ok=False, transport_error=True)
            raise HsmUnavailableError(f"hsm failure: {type(exc).__name__}") from exc
        await self._observe(ok=True)
        return result

    # -- HsmConnector (the five verbs) ----------------------------------------------------

    async def generate_mac(self, key_label: str, preimage: bytes) -> bytes:
        return await self._run(
            "generate_mac",
            (key_label,),
            preimage,
            lambda: self._session.generate_mac(key_label, preimage),
        )

    async def verify_mac(self, key_label: str, preimage: bytes, mac: bytes) -> bool:
        return await self._run(
            "verify_mac",
            (key_label,),
            preimage,
            lambda: self._session.verify_mac(key_label, preimage, mac),
        )

    async def translate_pin_block(
        self, src_key_label: str, dst_key_label: str, pin_block: bytes, pan_token: str
    ) -> bytes:
        # PIN path: audit carries labels only — never PIN material in any form.
        return await self._run(
            "translate_pin_block",
            (src_key_label, dst_key_label),
            None,
            lambda: self._session.translate_pin_block(src_key_label, dst_key_label, pin_block),
        )

    async def encrypt_under_zmk(self, zmk_label: str, clear: bytes) -> bytes:
        return await self._run(
            "encrypt_under_zmk",
            (zmk_label,),
            None,
            lambda: self._session.encrypt_under_zmk(zmk_label, clear),
        )

    async def decrypt_under_zmk(self, zmk_label: str, cipher: bytes) -> bytes:
        return await self._run(
            "decrypt_under_zmk",
            (zmk_label,),
            None,
            lambda: self._session.decrypt_under_zmk(zmk_label, cipher),
        )

    async def health_check(self) -> bool:
        if self._mode is ConnectorMode.DISABLED or self._session is None:
            return False
        if self._breaker is not None and not await self._breaker.admit(
            CONNECTOR_ID, kind="HEALTH"
        ):
            return False
        return True
