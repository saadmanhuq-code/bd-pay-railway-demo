"""``hsm_yubi_v1`` — YubiHSM 2 key-custody provider (non-card key custody).

Operator decision (2026-06-12): certified secure elements (2x YubiHSM 2 FIPS,
prod + DR) custody the platform's NON-CARD keys — ledger checkpoint signing,
KEK wrap/unwrap, OpenBao unseal posture (docs/HSM-CUSTODY-POSTURE.md). The
certified payment HSM (payShield, ``hsm_thales_v1``) is deferred to card
activation; card/PIN material NEVER touches this provider.

Verbs: ``sign_ed25519`` / ``public_key_ed25519`` / ``wrap_kek`` /
``unwrap_kek`` / ``attest`` / ``health_check``. Connector law: no business-DB
writes, breaker-wrapped calls (fail-closed when OPEN), credential VALUES never
appear in code or config — hardware settings name environment variables
(``YUBIHSM_CONNECTOR_URL`` / ``YUBIHSM_AUTH_KEY_ID`` / ``YUBIHSM_PASSWORD``)
and the audit trail carries labels and digest hashes only, never key material.

Modes:
  * SOFT (default until the units land): deterministic soft custody reusing
    the existing soft-HSM primitives (``SoftHsmSession`` key derivation + AES
    key-wrap-with-padding); Ed25519 seeds derive from the same seed:label
    scheme. Designed posture for build/sandbox.
  * HARDWARE: real ``yubihsm`` python SDK wire path (``YubiHsmSdkSession``),
    code-complete and activation-gated — the authenticated session opens
    lazily on first verb, so constructing the provider performs no hardware
    I/O and the build never talks to a device.
  * DISABLED: refuses every verb.

The sync surface (``sign_ed25519_sync`` / ``public_key_ed25519_sync``) exists
for the ledger checkpoint adapter (``bdpay.ledger.checkpoint_custody``), which
signs inside the ledger transaction; it is mode-gated and audited like the
async verbs. Checkpoint VERIFICATION never moves here — it stays on the
externally-supplied trusted-key set (SPEC_ERRATA E6).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from bdpay.connectors.breaker import CircuitBreaker
from bdpay.connectors.identity.hsm import (
    HsmCommandRejectedError,
    HsmKeyNotFoundError,
    SoftHsmSession,
)
from bdpay.connectors.ports import AuditSink, CredentialResolver, InMemoryAuditSink
from bdpay.connectors.registry import ConnectorRegistry
from bdpay.platform.clock import Clock
from bdpay.platform.deployment_env import (
    DeploymentEnvironmentError,
    deployment_is_production,
)
from bdpay.platform.errors import ConnectorError

__all__ = [
    "CONNECTOR_ID",
    "CUSTODY_TIMEOUT_S",
    "CustodyCommandRejectedError",
    "CustodyKeyNotFoundError",
    "CustodyMode",
    "CustodyUnavailableError",
    "ENV_AUTH_KEY_ID",
    "ENV_CONNECTOR_URL",
    "ENV_PASSWORD",
    "HardwareCustodySession",
    "KeyCustodyProvider",
    "SoftCustodySession",
    "YubiHsmSdkSession",
    "YubiHsmSettings",
    "YubiKeyCustodyProvider",
    "custody_mode_from_config",
    "record_custody_health",
]

CONNECTOR_ID = "hsm_yubi_v1"

#: Call budget mirroring the payment-HSM posture: 5s, no retries, fail-closed.
CUSTODY_TIMEOUT_S = 5.0

#: Hardware config comes from these environment variable NAMES — the values
#: (especially the auth password) are deployment secrets and never appear in
#: any file (connector law).
ENV_CONNECTOR_URL = "YUBIHSM_CONNECTOR_URL"
ENV_AUTH_KEY_ID = "YUBIHSM_AUTH_KEY_ID"
ENV_PASSWORD = "YUBIHSM_PASSWORD"

_SYSTEM_ACTOR = "system:key-custody"


class CustodyUnavailableError(ConnectorError):
    default_code = "custody_unavailable"


class CustodyKeyNotFoundError(ConnectorError):
    """Config error — page engineering; never trips the breaker."""

    default_code = "custody_key_not_found"


class CustodyCommandRejectedError(ConnectorError):
    """The secure element rejected the command (e.g. unwrap integrity)."""

    def __init__(self, reason: str) -> None:
        super().__init__(
            f"custody command rejected: {reason}",
            code=f"custody_command_rejected_{reason}",
        )


class CustodyMode(StrEnum):
    SOFT = "SOFT"
    HARDWARE = "HARDWARE"
    DISABLED = "DISABLED"


_MODE_BY_CONFIG_STRING: dict[str, CustodyMode] = {
    "soft": CustodyMode.SOFT,
    "hardware": CustodyMode.HARDWARE,
    "disabled": CustodyMode.DISABLED,
}
def _is_production_environment(environ: Mapping[str, str] | None) -> bool:
    env = os.environ if environ is None else environ
    try:
        return deployment_is_production(env)
    except DeploymentEnvironmentError as exc:
        raise CustodyUnavailableError(
            str(exc), code="deployment_env_conflict"
        ) from exc


def custody_mode_from_config(value: str | None) -> CustodyMode:
    """Map a config mode string to the custody FSM; absent => SOFT (default)."""
    if value is None:
        return CustodyMode.SOFT
    mode = _MODE_BY_CONFIG_STRING.get(str(value).strip().lower())
    if mode is None:
        raise CustodyUnavailableError(
            f"unknown custody mode {value!r}; allowed: " + ", ".join(sorted(_MODE_BY_CONFIG_STRING))
        )
    return mode


@runtime_checkable
class KeyCustodyProvider(Protocol):
    """The key-custody surface: sign, wrap/unwrap, attest, health."""

    connector_id: str

    async def sign_ed25519(self, key_label: str, message: bytes) -> bytes: ...

    async def public_key_ed25519(self, key_label: str) -> bytes: ...

    async def wrap_kek(self, wrap_key_label: str, key_material: bytes) -> bytes: ...

    async def unwrap_kek(self, wrap_key_label: str, wrapped: bytes) -> bytes: ...

    async def attest(self, key_label: str) -> dict: ...

    async def health_check(self) -> bool: ...


# -- Soft custody session (deterministic; reuses the soft-HSM primitives) ---------------


class SoftCustodySession:
    """Label-addressed deterministic custody keys derived from a seed.

    Ed25519 seeds use the same ``sha256(seed:label)`` derivation the soft-HSM
    uses for its AES keys; KEK wrap/unwrap delegates to ``SoftHsmSession``
    (AES key-wrap-with-padding) so soft custody and the soft payment HSM share
    one set of audited primitives.
    """

    def __init__(self, *, seed: str, key_labels: tuple[str, ...] = ()) -> None:
        self._seed = seed
        self._labels = set(key_labels)
        self._wrap = SoftHsmSession(seed=seed, key_labels=key_labels)

    def register_label(self, label: str) -> None:
        self._labels.add(label)
        self._wrap.register_label(label)

    def _ed25519_key(self, label: str) -> Ed25519PrivateKey:
        if self._labels and label not in self._labels:
            raise CustodyKeyNotFoundError(f"no custody key under label {label}")
        seed32 = hashlib.sha256(f"{self._seed}:ed25519:{label}".encode()).digest()
        return Ed25519PrivateKey.from_private_bytes(seed32)

    def sign_ed25519(self, key_label: str, message: bytes) -> bytes:
        return self._ed25519_key(key_label).sign(message)

    def public_key_ed25519(self, key_label: str) -> bytes:
        return self._ed25519_key(key_label).public_key().public_bytes_raw()

    def wrap_kek(self, wrap_key_label: str, key_material: bytes) -> bytes:
        try:
            return self._wrap.encrypt_under_zmk(wrap_key_label, key_material)
        except HsmKeyNotFoundError as exc:
            raise CustodyKeyNotFoundError(str(exc)) from exc

    def unwrap_kek(self, wrap_key_label: str, wrapped: bytes) -> bytes:
        try:
            return self._wrap.decrypt_under_zmk(wrap_key_label, wrapped)
        except HsmKeyNotFoundError as exc:
            raise CustodyKeyNotFoundError(str(exc)) from exc
        except HsmCommandRejectedError as exc:
            raise CustodyCommandRejectedError("unwrap_integrity") from exc

    def attest(self, key_label: str) -> dict:
        public = self.public_key_ed25519(key_label)
        return {
            "provider": CONNECTOR_ID,
            "mode": CustodyMode.SOFT.value,
            "key_label": key_label,
            "algorithm": "ed25519",
            "public_key_b64": base64.b64encode(public).decode("ascii"),
            "public_key_sha256": hashlib.sha256(public).hexdigest(),
            "device_serial": None,
        }

    def device_info(self) -> dict:
        return {"mode": CustodyMode.SOFT.value, "serial": None}


# -- Hardware session (real yubihsm SDK; activation-gated) ------------------------------


@dataclass(frozen=True)
class YubiHsmSettings:
    """Hardware connection settings sourced from the environment by NAME."""

    connector_url: str
    auth_key_id: int

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> YubiHsmSettings:
        env = os.environ if environ is None else environ
        names = (ENV_CONNECTOR_URL, ENV_AUTH_KEY_ID, ENV_PASSWORD)
        missing = [name for name in names if not env.get(name)]
        if missing:
            # Fail closed; report env NAMES only — never values.
            raise CustodyUnavailableError(
                "hardware custody config missing environment variables: " + ", ".join(missing)
            )
        try:
            auth_key_id = int(env[ENV_AUTH_KEY_ID], 0)
        except ValueError as exc:
            raise CustodyUnavailableError(
                f"{ENV_AUTH_KEY_ID} must be an integer object id"
            ) from exc
        return cls(connector_url=env[ENV_CONNECTOR_URL], auth_key_id=auth_key_id)


@runtime_checkable
class HardwareCustodySession(Protocol):
    """Verb-shaped hardware session: the SDK path and test fakes share it."""

    def sign_ed25519(self, key_label: str, message: bytes) -> bytes: ...

    def public_key_ed25519(self, key_label: str) -> bytes: ...

    def wrap_kek(self, wrap_key_label: str, key_material: bytes) -> bytes: ...

    def unwrap_kek(self, wrap_key_label: str, wrapped: bytes) -> bytes: ...

    def attest(self, key_label: str) -> dict: ...

    def device_info(self) -> dict: ...


class YubiHsmSdkSession:
    """Real YubiHSM 2 wire path over the ``yubihsm`` python SDK.

    Activation-gated: the connector URL is dialled and the authenticated
    session derived ONLY on the first verb call — construction performs no
    I/O and no SDK import. ``object_ids`` maps custody key labels to device
    object ids (deployment config, not secrets). The auth password is read
    from ``YUBIHSM_PASSWORD`` at session-open time and held only by the SDK
    session derivation — it is never logged, audited or stored.
    """

    def __init__(
        self,
        settings: YubiHsmSettings,
        *,
        object_ids: Mapping[str, int],
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self._settings = settings
        self._object_ids = {str(label): int(oid) for label, oid in object_ids.items()}
        self._environ = environ
        self._hsm = None
        self._session = None

    def _object_id(self, label: str) -> int:
        oid = self._object_ids.get(label)
        if oid is None:
            raise CustodyKeyNotFoundError(f"no device object id mapped for label {label}")
        return oid

    def _authed(self):
        if self._session is None:
            from yubihsm import YubiHsm  # SDK import deferred until activation

            env = os.environ if self._environ is None else self._environ
            password = env.get(ENV_PASSWORD)
            if not password:
                raise CustodyUnavailableError(
                    f"hardware custody config missing environment variables: {ENV_PASSWORD}"
                )
            self._hsm = YubiHsm.connect(self._settings.connector_url)
            self._session = self._hsm.create_session_derived(
                self._settings.auth_key_id, password
            )
        return self._session

    def sign_ed25519(self, key_label: str, message: bytes) -> bytes:
        from yubihsm.objects import AsymmetricKey

        return AsymmetricKey(self._authed(), self._object_id(key_label)).sign_eddsa(message)

    def public_key_ed25519(self, key_label: str) -> bytes:
        from yubihsm.objects import AsymmetricKey

        public = AsymmetricKey(self._authed(), self._object_id(key_label)).get_public_key()
        return public.public_bytes_raw()

    def wrap_kek(self, wrap_key_label: str, key_material: bytes) -> bytes:
        from yubihsm.objects import WrapKey

        return WrapKey(self._authed(), self._object_id(wrap_key_label)).wrap_data(key_material)

    def unwrap_kek(self, wrap_key_label: str, wrapped: bytes) -> bytes:
        from yubihsm.objects import WrapKey

        return WrapKey(self._authed(), self._object_id(wrap_key_label)).unwrap_data(wrapped)

    def attest(self, key_label: str) -> dict:
        from cryptography.hazmat.primitives.serialization import Encoding
        from yubihsm.objects import AsymmetricKey

        key = AsymmetricKey(self._authed(), self._object_id(key_label))
        certificate = key.attest()  # device attestation key (id 0) signs
        public = key.get_public_key().public_bytes_raw()
        return {
            "provider": CONNECTOR_ID,
            "mode": CustodyMode.HARDWARE.value,
            "key_label": key_label,
            "algorithm": "ed25519",
            "public_key_b64": base64.b64encode(public).decode("ascii"),
            "public_key_sha256": hashlib.sha256(public).hexdigest(),
            "attestation_certificate_der_hex": certificate.public_bytes(Encoding.DER).hex(),
        }

    def device_info(self) -> dict:
        self._authed()
        info = self._hsm.get_device_info()
        return {
            "mode": CustodyMode.HARDWARE.value,
            "version": ".".join(str(part) for part in info.version),
            "serial": info.serial,
        }


# -- The provider ------------------------------------------------------------------------


class YubiKeyCustodyProvider:
    """``hsm_yubi_v1``: breaker-guarded, timeout-bounded custody verbs."""

    connector_id = CONNECTOR_ID

    def __init__(
        self,
        *,
        mode: CustodyMode | str = CustodyMode.SOFT,
        clock: Clock,
        breaker: CircuitBreaker | None = None,
        credentials: CredentialResolver | None = None,
        config: dict | None = None,
        hardware_session: HardwareCustodySession | None = None,
        audit: AuditSink | None = None,
        timeout_s: float = CUSTODY_TIMEOUT_S,
        wait_for=None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self._mode = mode if isinstance(mode, CustodyMode) else custody_mode_from_config(str(mode))
        self._clock = clock
        self._breaker = breaker
        self._audit = audit if audit is not None else InMemoryAuditSink()
        self._timeout_s = timeout_s
        self._wait_for = wait_for if wait_for is not None else asyncio.wait_for
        config = dict(config or {})
        self._session: SoftCustodySession | HardwareCustodySession | None
        if self._mode is CustodyMode.SOFT:
            if _is_production_environment(environ):
                raise CustodyUnavailableError(
                    "SOFT YubiHSM custody is forbidden in production; configure "
                    "hsm_yubi_v1 for HARDWARE mode",
                    code="soft_custody_forbidden_in_production",
                )
            seed_ref = config.get("soft_seed_ref")
            if seed_ref is not None and credentials is not None:
                seed = credentials.resolve(str(seed_ref))
            else:
                seed = str(config.get("soft_seed", "yubi-soft-custody-seed"))
            self._session = SoftCustodySession(
                seed=seed, key_labels=tuple(config.get("key_labels", ()))
            )
        elif self._mode is CustodyMode.HARDWARE:
            if hardware_session is not None:
                self._session = hardware_session
            else:
                settings = YubiHsmSettings.from_env(environ)
                self._session = YubiHsmSdkSession(
                    settings,
                    object_ids=dict(config.get("object_ids", {})),
                    environ=environ,
                )
        else:
            self._session = None

    @property
    def mode(self) -> CustodyMode:
        return self._mode

    # -- guard rails --------------------------------------------------------------------

    def _require_session(self, verb: str):
        if self._mode is CustodyMode.DISABLED or self._session is None:
            raise CustodyUnavailableError(f"{CONNECTOR_ID} is DISABLED; {verb} refused")
        return self._session

    async def _admit(self, verb: str) -> None:
        self._require_session(verb)
        if self._breaker is not None and not await self._breaker.admit(
            CONNECTOR_ID, kind="SUBMIT"
        ):
            # Fail-closed: no checkpoint signatures / KEK operations while OPEN.
            raise CustodyUnavailableError(f"custody circuit open; {verb} refused")

    async def _observe(self, *, ok: bool, transport_error: bool = False) -> None:
        if self._breaker is None:
            return
        from bdpay.connectors.sdk import ConnectorStatus

        await self._breaker.observe(
            CONNECTOR_ID,
            ConnectorStatus.SUCCESS if ok else ConnectorStatus.FAILED,
            transport_error=transport_error,
        )

    def _audit_call(self, verb: str, labels: tuple[str, ...], message: bytes | None) -> None:
        detail: dict = {"verb": verb, "key_labels": list(labels), "mode": self._mode.value}
        if message is not None:  # key-material paths pass None — labels only, ever.
            detail["message_sha256"] = hashlib.sha256(message).hexdigest()
        self._audit.record(
            "CUSTODY_CALL", actor_id=_SYSTEM_ACTOR, occurred_at=self._clock.now(), detail=detail
        )

    async def _run(self, verb: str, labels: tuple[str, ...], message: bytes | None, call):
        await self._admit(verb)
        self._audit_call(verb, labels, message)
        try:
            if self._mode is CustodyMode.HARDWARE:
                result = await self._wait_for(asyncio.to_thread(call), timeout=self._timeout_s)
            else:
                result = call()
        except CustodyKeyNotFoundError:
            await self._observe(ok=True)  # config error, not element health
            raise
        except CustodyCommandRejectedError:
            await self._observe(ok=False)
            raise
        except TimeoutError as exc:
            await self._observe(ok=False, transport_error=True)
            raise CustodyUnavailableError(
                f"custody call exceeded the {self._timeout_s:g}s budget"
            ) from exc
        except CustodyUnavailableError:
            await self._observe(ok=False, transport_error=True)
            raise
        except Exception as exc:
            await self._observe(ok=False, transport_error=True)
            raise CustodyUnavailableError(f"custody failure: {type(exc).__name__}") from exc
        await self._observe(ok=True)
        return result

    # -- KeyCustodyProvider verbs ----------------------------------------------------------

    async def sign_ed25519(self, key_label: str, message: bytes) -> bytes:
        return await self._run(
            "sign_ed25519",
            (key_label,),
            message,
            lambda: self._session.sign_ed25519(key_label, message),
        )

    async def public_key_ed25519(self, key_label: str) -> bytes:
        return await self._run(
            "public_key_ed25519",
            (key_label,),
            None,
            lambda: self._session.public_key_ed25519(key_label),
        )

    async def wrap_kek(self, wrap_key_label: str, key_material: bytes) -> bytes:
        # Key-material path: audit carries the label only — never the material.
        return await self._run(
            "wrap_kek",
            (wrap_key_label,),
            None,
            lambda: self._session.wrap_kek(wrap_key_label, key_material),
        )

    async def unwrap_kek(self, wrap_key_label: str, wrapped: bytes) -> bytes:
        return await self._run(
            "unwrap_kek",
            (wrap_key_label,),
            None,
            lambda: self._session.unwrap_kek(wrap_key_label, wrapped),
        )

    async def attest(self, key_label: str) -> dict:
        return await self._run(
            "attest", (key_label,), None, lambda: self._session.attest(key_label)
        )

    async def health_check(self) -> bool:
        if self._mode is CustodyMode.DISABLED or self._session is None:
            return False
        if self._breaker is not None and not await self._breaker.admit(
            CONNECTOR_ID, kind="HEALTH"
        ):
            return False
        try:
            if self._mode is CustodyMode.HARDWARE:
                await self._wait_for(
                    asyncio.to_thread(self._session.device_info), timeout=self._timeout_s
                )
            else:
                self._session.device_info()
        except Exception:
            return False
        return True

    # -- sync surface for the ledger checkpoint adapter -------------------------------------

    def sign_ed25519_sync(self, key_label: str, message: bytes) -> bytes:
        """Mode-gated, audited sync signing (used inside the ledger transaction)."""
        session = self._require_session("sign_ed25519")
        self._audit_call("sign_ed25519", (key_label,), message)
        return session.sign_ed25519(key_label, message)

    def public_key_ed25519_sync(self, key_label: str) -> bytes:
        session = self._require_session("public_key_ed25519")
        self._audit_call("public_key_ed25519", (key_label,), None)
        return session.public_key_ed25519(key_label)


# -- chlth health wiring -------------------------------------------------------------------


async def record_custody_health(
    registry: ConnectorRegistry,
    provider: KeyCustodyProvider,
    *,
    latency_ms: int | None = None,
):
    """Run the provider health check and append a ``chlth`` sample row.

    Status deltas emit ``connector_registration.health_changed`` exactly like
    every other connector (spec/10 health states).
    """
    healthy = await provider.health_check()
    return registry.record_health(
        provider.connector_id,
        healthy=healthy,
        latency_ms=latency_ms,
        detail_code=None if healthy else "custody_unhealthy",
    )
